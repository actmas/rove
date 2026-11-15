import React, {useState, useEffect, useRef} from 'react';
import {X, RefreshCw, Zap, Clock, ShieldCheck, Sparkles, Cpu, Layers} from 'lucide-react';
import {api, post} from './api';

function collectEarliestReset(data) {
  if (!data) return 0;
  const list = [];
  const codex = data.codex || {};
  if (codex.primary?.resets_at > 0) list.push(codex.primary.resets_at);
  if (codex.secondary?.resets_at > 0) list.push(codex.secondary.resets_at);
  const agy = data.antigravity || {};
  for (const m of agy.models || []) {
    if (m?.resets_at > 0) list.push(m.resets_at);
  }
  if (agy.gemini_4_argon?.resets_at > 0) list.push(agy.gemini_4_argon.resets_at);
  if (agy.gemini_4_argon?.weekly?.resets_at > 0) list.push(agy.gemini_4_argon.weekly.resets_at);
  if (agy.gemini_4_argon?.rolling_5h?.resets_at > 0) list.push(agy.gemini_4_argon.rolling_5h.resets_at);
  if (agy.general_flash?.resets_at > 0) list.push(agy.general_flash.resets_at);
  if (agy.general_flash?.weekly?.resets_at > 0) list.push(agy.general_flash.weekly.resets_at);
  if (agy.general_flash?.rolling_5h?.resets_at > 0) list.push(agy.general_flash.rolling_5h.resets_at);
  if (agy.claude_gpt?.resets_at > 0) list.push(agy.claude_gpt.resets_at);
  if (agy.claude_gpt?.weekly?.resets_at > 0) list.push(agy.claude_gpt.weekly.resets_at);
  if (agy.claude_gpt?.rolling_5h?.resets_at > 0) list.push(agy.claude_gpt.rolling_5h.resets_at);
  return list.length ? Math.min(...list) : 0;
}

export default function QuotaModal({isOpen, onClose}) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const lastForceAtRef = useRef(0);

  async function loadQuotas(force = false) {
    setLoading(true);
    setError('');
    try {
      const res = force ? await post('quotas/refresh', {}) : await api('quotas');
      setData(res);
    } catch (err) {
      setError(err.message || '获取额度信息失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (isOpen) {
      loadQuotas(false);
      const timer = setInterval(() => {
        if (!document.hidden) loadQuotas(false);
      }, 20000);
      return () => clearInterval(timer);
    }
  }, [isOpen]);

  // Automatically force-refresh as soon as the earliest quota window reaches its reset timestamp
  useEffect(() => {
    if (!isOpen || !data) return;
    const earliest = collectEarliestReset(data);
    if (!earliest) return;
    const nowMs = Date.now();
    const targetMs = earliest * 1000;
    const diffMs = targetMs - nowMs;
    if (diffMs <= 0) {
      if (nowMs - lastForceAtRef.current > 5000) {
        lastForceAtRef.current = nowMs;
        loadQuotas(true);
      }
      return;
    }
    const waitMs = Math.min(60000, Math.max(600, diffMs + 800));
    const timer = setTimeout(() => {
      lastForceAtRef.current = Date.now();
      loadQuotas(true);
    }, waitMs);
    return () => clearTimeout(timer);
  }, [isOpen, data]);

  if (!isOpen) return null;

  const codex = data?.codex || {};
  const agy = data?.antigravity || {};
  const argon = agy?.gemini_4_argon;
  const flash = agy?.general_flash;
  const claudeGpt = agy?.claude_gpt;
  const flagshipModels = agy?.flagship_models?.length
    ? agy.flagship_models
    : (agy?.models || []).filter(m => (m.model_id || '').toLowerCase().includes('argon'));
  const generalModels = agy?.general_models?.length
    ? agy.general_models
    : (agy?.models || []).filter(m => !(m.model_id || '').toLowerCase().includes('argon'));

  function renderProgressBar(remainingPercent, isArgon = false) {
    const clamped = Math.max(0, Math.min(100, remainingPercent ?? 0));
    let barColor = 'var(--emerald)';
    if (clamped <= 10) {
      barColor = 'var(--rose)';
    } else if (clamped <= 25) {
      barColor = 'var(--amber)';
    } else if (isArgon) {
      barColor = 'linear-gradient(90deg, #4285F4 0%, #9B51E0 50%, #FF6D00 100%)';
    }
    return (
      <div className="quota-bar-track" title={`剩余可用额度 ${clamped}%`}>
        <div
          className="quota-bar-fill"
          style={{
            width: `${clamped}%`,
            background: barColor,
          }}
        />
      </div>
    );
  }

  return (
    <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <section role="dialog" aria-modal="true" aria-labelledby="quota-modal-title" className="settings-modal quota-modal">
        <div className="sheet-handle mobile-only" aria-hidden="true" />
        <div className="pane-toolbar">
          <div className="quota-header-title">
            <Zap size={18} className="quota-title-icon" />
            <h2 id="quota-modal-title">模型剩余额度与刷新倒计时</h2>
          </div>
          <div className="quota-header-actions">
            <button
              type="button"
              className={`icon-button ${loading ? 'spinning' : ''}`}
              title="立即刷新额度"
              aria-label="立即刷新额度"
              onClick={() => loadQuotas(true)}
              disabled={loading}
            >
              <RefreshCw size={16} />
            </button>
            <button type="button" className="icon-button" aria-label="关闭额度面板" onClick={onClose}>
              <X size={19} />
            </button>
          </div>
        </div>

        <div className="settings-body quota-body">
          {error && <div className="banner error">{error}</div>}

          {/* Section 1: OpenAI Codex */}
          <section className="quota-section">
            <div className="quota-section-header">
              <div className="quota-brand-badge">
                <Cpu size={15} />
                <strong>OpenAI Codex</strong>
                {codex.plan_type && <span className="quota-plan-pill">{codex.plan_type}</span>}
              </div>
              {codex.status === 'ready' && (
                <span className="engine-label ready">正常服务</span>
              )}
            </div>

            {codex.status === 'ready' ? (
              <div className="quota-cards-grid">
                {/* 5-Hour Limit */}
                <div className="quota-card">
                  <div className="quota-card-top">
                    <span className="quota-card-name">
                      <Clock size={13} /> {codex.primary?.name || '5小时额度'}
                    </span>
                    <span className="quota-stat-nums">
                      剩余 <strong>{codex.primary?.remaining_percent}%</strong> (已用 {codex.primary?.used_percent}%)
                    </span>
                  </div>
                  {renderProgressBar(codex.primary?.remaining_percent)}
                  <div className="quota-card-footer">
                    <span className="quota-reset-text">
                      ⏱️ 距刷新 <strong>{codex.primary?.countdown_text || '即将刷新'}</strong>
                    </span>
                    <span className="quota-reset-sub">{codex.primary?.reset_time_formatted ? `${codex.primary.reset_time_formatted} 刷新` : ''}</span>
                  </div>
                </div>

                {/* Weekly Limit */}
                <div className="quota-card">
                  <div className="quota-card-top">
                    <span className="quota-card-name">
                      <Layers size={13} /> {codex.secondary?.name || '周额度'}
                    </span>
                    <span className="quota-stat-nums">
                      剩余 <strong>{codex.secondary?.remaining_percent}%</strong> (已用 {codex.secondary?.used_percent}%)
                    </span>
                  </div>
                  {renderProgressBar(codex.secondary?.remaining_percent)}
                  <div className="quota-card-footer">
                    <span className="quota-reset-text">
                      ⏱️ 距刷新 <strong>{codex.secondary?.countdown_text || '即将刷新'}</strong>
                    </span>
                    <span className="quota-reset-sub">{codex.secondary?.reset_time_formatted ? `${codex.secondary.reset_time_formatted} 刷新` : ''}</span>
                  </div>
                </div>

                {/* Reset Credits */}
                {codex.reset_credits?.available_count > 0 && (
                  <div className="quota-credit-badge">
                    <Sparkles size={14} className="sparkle-icon" />
                    <div className="quota-credit-info">
                      <strong>拥有 {codex.reset_credits.available_count} 次免费完全额度重置 (Full Reset)</strong>
                      <span>当 5 小时或周额度用尽时可立即重置恢复</span>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="quota-empty-note">{codex.detail || '未检测到 Codex 额度信息'}</div>
            )}
          </section>

          {/* Section 2: Google Antigravity */}
          <section className="quota-section">
            <div className="quota-section-header">
              <div className="quota-brand-badge">
                <ShieldCheck size={15} />
                <strong>Google Antigravity</strong>
                {agy.plan_name && <span className="quota-plan-pill agy-pill">{agy.plan_name}</span>}
              </div>
              {agy.account_email && (
                <span className="quota-account-email" title={agy.tier_name}>{agy.account_email}</span>
              )}
            </div>

            {agy.status === 'ready' ? (
              <div className="quota-cards-grid">
                {/* Dual Primary Cards: Weekly Flagship + Cycle General */}
                {argon && (
                  <div className="quota-card quota-card-argon">
                    <div className="quota-card-top">
                      <span className="quota-card-name argon-title">
                        <Sparkles size={14} /> {argon.label}
                      </span>
                      <span className="quota-stat-nums">
                        周配额剩余 <strong>{argon.remaining_percent}%</strong> (已用 {argon.used_percent}%)
                      </span>
                    </div>
                    {renderProgressBar(argon.remaining_percent, true)}
                    <div className="quota-card-footer">
                      <span className="quota-reset-text">
                        ⏱️ 每周额度距重置 <strong>{argon.countdown_text || '正常'}</strong>
                      </span>
                      <span className="quota-reset-sub">{argon.reset_time_formatted ? `${argon.reset_time_formatted} 刷新` : ''}</span>
                    </div>
                    {argon.rolling_5h && (
                      <div className="quota-card-extra-row" style={{flexDirection: 'column', alignItems: 'stretch', gap: '6px'}}>
                        <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '6px'}}>
                          <span className="extra-badge-pill tone-blue">
                            ⚡ 5小时滚动池：剩余 {argon.rolling_5h.remaining_percent}% (已用 {argon.rolling_5h.used_percent}%)
                          </span>
                          <span className="extra-hint">
                            {argon.rolling_5h.reset_time_formatted ? `${argon.rolling_5h.reset_time_formatted} 刷新 · ` : ''}距刷新 {argon.rolling_5h.countdown_text || '充沛'}
                          </span>
                        </div>
                        {renderProgressBar(argon.rolling_5h.remaining_percent, false)}
                      </div>
                    )}
                  </div>
                )}

                {flash && (
                  <div className="quota-card quota-card-flash">
                    <div className="quota-card-top">
                      <span className="quota-card-name flash-title">
                        <Zap size={14} className="flash-icon" /> {flash.label}
                      </span>
                      <span className="quota-stat-nums">
                        周配额剩余 <strong>{flash.weekly ? flash.weekly.remaining_percent : flash.remaining_percent}%</strong> (已用 {flash.weekly ? flash.weekly.used_percent : flash.used_percent}%)
                      </span>
                    </div>
                    {renderProgressBar(flash.weekly ? flash.weekly.remaining_percent : flash.remaining_percent, true)}
                    <div className="quota-card-footer">
                      <span className="quota-reset-text">
                        ⏱️ 每周额度距重置 <strong>{flash.weekly?.countdown_text || flash.countdown_text || '即将刷新'}</strong>
                      </span>
                      <span className="quota-reset-sub">{(flash.weekly?.reset_time_formatted || flash.reset_time_formatted) ? `${flash.weekly?.reset_time_formatted || flash.reset_time_formatted} 刷新` : ''}</span>
                    </div>
                    {flash.rolling_5h && (
                      <div className="quota-card-extra-row" style={{flexDirection: 'column', alignItems: 'stretch', gap: '6px'}}>
                        <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '6px'}}>
                          <span className="extra-badge-pill tone-blue">
                            ⚡ 5小时滚动池：剩余 {flash.rolling_5h.remaining_percent}% (已用 {flash.rolling_5h.used_percent}%)
                          </span>
                          <span className="extra-hint">
                            {flash.rolling_5h.reset_time_formatted ? `${flash.rolling_5h.reset_time_formatted} 刷新 · ` : ''}距刷新 {flash.rolling_5h.countdown_text || '充沛'}
                          </span>
                        </div>
                        {renderProgressBar(flash.rolling_5h.remaining_percent, false)}
                      </div>
                    )}
                  </div>
                )}

                {claudeGpt && (
                  <div className="quota-card quota-card-claude">
                    <div className="quota-card-top">
                      <span className="quota-card-name claude-title">
                        <Cpu size={14} /> {claudeGpt.label}
                      </span>
                      <span className="quota-stat-nums">
                        周配额剩余 <strong>{claudeGpt.weekly ? claudeGpt.weekly.remaining_percent : claudeGpt.remaining_percent}%</strong> (已用 {claudeGpt.weekly ? claudeGpt.weekly.used_percent : claudeGpt.used_percent}%)
                      </span>
                    </div>
                    {renderProgressBar(claudeGpt.weekly ? claudeGpt.weekly.remaining_percent : claudeGpt.remaining_percent, true)}
                    <div className="quota-card-footer">
                      <span className="quota-reset-text">
                        ⏱️ 每周额度距重置 <strong>{claudeGpt.weekly?.countdown_text || claudeGpt.countdown_text || '即将刷新'}</strong>
                      </span>
                      <span className="quota-reset-sub">{(claudeGpt.weekly?.reset_time_formatted || claudeGpt.reset_time_formatted) ? `${claudeGpt.weekly?.reset_time_formatted || claudeGpt.reset_time_formatted} 刷新` : ''}</span>
                    </div>
                    {claudeGpt.rolling_5h && (
                      <div className="quota-card-extra-row" style={{flexDirection: 'column', alignItems: 'stretch', gap: '6px'}}>
                        <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '6px'}}>
                          <span className="extra-badge-pill tone-blue">
                            ⚡ 5小时滚动池：剩余 {claudeGpt.rolling_5h.remaining_percent}% (已用 {claudeGpt.rolling_5h.used_percent}%)
                          </span>
                          <span className="extra-hint">
                            {claudeGpt.rolling_5h.reset_time_formatted ? `${claudeGpt.rolling_5h.reset_time_formatted} 刷新 · ` : ''}距刷新 {claudeGpt.rolling_5h.countdown_text || '充沛'}
                          </span>
                        </div>
                        {renderProgressBar(claudeGpt.rolling_5h.remaining_percent, false)}
                      </div>
                    )}
                  </div>
                )}

                {/* Credits Row */}
                {agy.credits && (
                  <div className="quota-micro-stats">
                    <div className="micro-stat-item">
                      <span className="micro-label">可用 Prompt 配额</span>
                      <strong className="micro-val">{agy.credits.available_prompt_credits?.toLocaleString()}</strong>
                      <span className="micro-sub">/ {agy.credits.monthly_prompt_credits?.toLocaleString()} 月度</span>
                    </div>
                    <div className="micro-stat-item">
                      <span className="micro-label">可用 Flow 配额</span>
                      <strong className="micro-val">{agy.credits.available_flow_credits?.toLocaleString()}</strong>
                      <span className="micro-sub">/ {agy.credits.monthly_flow_credits?.toLocaleString()} 月度</span>
                    </div>
                  </div>
                )}

                {/* All Models Full Breakdown */}
                {flagshipModels.length > 0 && (
                  <div className="quota-other-models">
                    <h4 className="other-models-title">
                      <Sparkles size={12} /> 旗舰高阶模型明细 (剩余额度进度)
                    </h4>
                    <div className="other-models-grid">
                      {flagshipModels.map(m => (
                        <div key={m.model_id} className="other-model-chip is-flagship">
                          <div className="chip-head">
                            <span className="other-model-name" title={m.label}>{m.label}</span>
                            <span className="other-model-percent">剩余 <strong>{m.remaining_percent}%</strong></span>
                          </div>
                          {renderProgressBar(m.remaining_percent, true)}
                          <span className="chip-countdown">
                            ⏱️ 距重置 {m.countdown_text || '正常'}{m.reset_time_formatted ? ` (${m.reset_time_formatted})` : ''}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {generalModels.length > 0 && (
                  <div className="quota-other-models">
                    <h4 className="other-models-title">
                      <Zap size={12} /> 通用日常与快速模型明细 (剩余额度进度)
                    </h4>
                    <div className="other-models-grid">
                      {generalModels.map(m => (
                        <div key={m.model_id} className="other-model-chip">
                          <div className="chip-head">
                            <span className="other-model-name" title={m.label}>{m.label}</span>
                            <span className="other-model-percent">剩余 <strong>{m.remaining_percent}%</strong></span>
                          </div>
                          {renderProgressBar(m.remaining_percent, false)}
                          <span className="chip-countdown">
                            ⏱️ 距刷新 {m.countdown_text || '即将刷新'}{m.reset_time_formatted ? ` (${m.reset_time_formatted})` : ''}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="quota-empty-note">{agy.detail || '未检测到 Antigravity 额度信息'}</div>
            )}
          </section>
        </div>
      </section>
    </div>
  );
}
