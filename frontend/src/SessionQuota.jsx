import {useEffect, useRef, useState} from 'react';
import {Zap} from 'lucide-react';
import {api, post} from './api';

export function selectQuota(data, engine, model) {
  const source = data?.[engine];
  if (source?.status !== 'ready') return [];
  if (engine === 'codex') {
    return [source.primary, source.secondary].filter(w => Number.isFinite(w?.remaining_percent));
  }
  let entry = source.by_model_id?.[model] || source.models?.find(m => m.model_id === model);
  if (!entry && model) {
    const ml = model.toLowerCase();
    if (ml.includes('argon')) entry = source.gemini_4_argon;
    else if (ml.includes('claude') || ml.includes('gpt')) entry = source.claude_gpt;
    else if (ml.includes('flash') || ml.includes('pro')) entry = source.general_flash;
  }
  if (!entry) {
    entry = source.general_flash || source.gemini_4_argon || source.claude_gpt;
  }
  if (!entry) return [];
  if (entry.rolling_5h && entry.weekly && Number.isFinite(entry.rolling_5h.remaining_percent) && Number.isFinite(entry.weekly.remaining_percent)) {
    return [entry.rolling_5h, entry.weekly];
  }
  return Number.isFinite(entry?.remaining_percent) ? [{...entry, name: entry.base_name || model}] : [];
}

export default function SessionQuota({engine, model, status, executionTarget}) {
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);
  const lastForceAtRef = useRef(0);
  const supported = executionTarget !== 'cloud' && (engine === 'codex' || engine === 'antigravity');

  useEffect(() => {
    if (!supported) return;
    let disposed = false;
    let pending = false;

    async function refresh(force = false) {
      if ((document.hidden && !force) || pending) return;
      pending = true;
      try {
        const next = force ? await post('quotas/refresh', {}) : await api('quotas');
        if (!disposed) {
          setData(next);
          setFailed(false);
        }
      } catch {
        if (!disposed) setFailed(true);
      } finally {
        pending = false;
      }
    }

    refresh(false);
    const timer = setInterval(() => {
      if (!document.hidden) refresh(false);
    }, 90000);
    const onVis = () => {
      if (!document.hidden) refresh(false);
    };
    document.addEventListener('visibilitychange', onVis);
    window.addEventListener('focus', onVis);
    return () => {
      disposed = true;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVis);
      window.removeEventListener('focus', onVis);
    };
  }, [supported, engine, model, status]);

  const windows = supported ? selectQuota(data, engine, model) : [];

  // Auto force-refresh at the exact moment any displayed quota window reaches its reset timestamp
  useEffect(() => {
    if (!supported || !windows.length) return;
    const resets = windows.map(w => w.resets_at).filter(ts => Number.isFinite(ts) && ts > 0);
    if (!resets.length) return;
    const earliest = Math.min(...resets);
    const nowMs = Date.now();
    const diffMs = earliest * 1000 - nowMs;
    let disposed = false;

    async function forceReload() {
      try {
        const next = await post('quotas/refresh', {});
        if (!disposed) {
          setData(next);
          setFailed(false);
        }
      } catch {}
    }

    if (diffMs <= 0) {
      if (nowMs - lastForceAtRef.current > 5000) {
        lastForceAtRef.current = nowMs;
        forceReload();
      }
      return;
    }
    const waitMs = Math.min(60000, Math.max(600, diffMs + 800));
    const t = setTimeout(() => {
      lastForceAtRef.current = Date.now();
      forceReload();
    }, waitMs);
    return () => {
      disposed = true;
      clearTimeout(t);
    };
  }, [supported, data, engine, model]);

  const stale = failed || (data?.updated_at && Date.now() / 1000 - data.updated_at > 90);
  const minRemaining = windows.length ? Math.min(...windows.map(w => w.remaining_percent)) : null;
  const levelClass = minRemaining == null
    ? ''
    : minRemaining <= 10
      ? 'level-high'
      : minRemaining <= 25
        ? 'level-mid'
        : 'level-low';

  const title = `${model || engine} · ${engine === 'codex' ? '账户共享额度' : '模型额度'}\n${
    windows.map(w => `${w.name}剩余 ${w.remaining_percent}%${w.reset_time_formatted ? `，${w.reset_time_formatted} 重置` : ''}`).join('\n')
    || data?.[engine]?.detail
    || '供应商未提供额度数据'
  }\n每 30 秒及到达重置时刻自动刷新${data?.updated_at ? `；上次更新 ${new Date(data.updated_at * 1000).toLocaleTimeString()}` : ''}`;

  return (
    <span
      className={`token-chip context-chip ${levelClass}`}
      title={title}
      aria-label={title}
      style={{cursor: supported ? 'pointer' : 'default'}}
      onClick={() => {
        if (!supported) return;
        post('quotas/refresh', {}).then(next => {
          setData(next);
          setFailed(false);
        }).catch(() => {});
      }}
    >
      <Zap size={12}/>
      {windows.length
        ? `剩余 ${windows.map(w => `${w.name} ${w.remaining_percent}%`).join(' · ')}${stale ? '（待刷新）' : ''}`
        : `额度${executionTarget === 'cloud' ? '以云端为准' : !supported ? '未提供' : failed || data ? '暂不可用' : '查询中…'}`}
      {minRemaining != null && (
        <span className="context-bar" aria-hidden="true">
          <span style={{width: `${Math.max(0, Math.min(100, minRemaining))}%`}}/>
        </span>
      )}
    </span>
  );
}
