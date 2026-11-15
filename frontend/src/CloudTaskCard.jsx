import {useState, useCallback} from 'react';
import {
  Cloud, ExternalLink, RefreshCw, Check, Copy, FileDiff, Info, AlertTriangle, ChevronDown, Terminal
} from 'lucide-react';
import {api} from './api';

export default function CloudTaskCard({item}) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);

  const taskId = item.task_id || '';
  const environment = item.environment || '';
  const taskUrl = item.url || (taskId ? `https://chatgpt.com/codex/tasks/${encodeURIComponent(taskId)}` : 'https://chatgpt.com/codex');

  const copyTaskId = useCallback(e => {
    e.stopPropagation();
    if (!taskId) return;
    navigator.clipboard?.writeText(taskId).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    }).catch(() => {});
  }, [taskId]);

  async function refresh() {
    if (!item.job_id || busy) return;
    setBusy(true);
    setError('');
    try {
      const res = await api(`jobs/${item.job_id}/cloud`, {timeoutMs: 30000});
      setData(res);
    } catch (e) {
      setError(e.message || '查询云端任务状态失败');
    } finally {
      setBusy(false);
    }
  }

  // Determine current badge status
  let statusBadge = {label: '已提交云端', tone: 'tone-blue'};
  if (data?.status) {
    const s = data.status.toLowerCase();
    if (s.includes('completed') || s.includes('ready') || s.includes('success')) {
      statusBadge = {label: '云端已就绪', tone: 'tone-green'};
    } else if (s.includes('running') || s.includes('executing')) {
      statusBadge = {label: '云端运行中', tone: 'tone-amber'};
    } else if (s.includes('failed') || s.includes('error')) {
      statusBadge = {label: '云端执行失败', tone: 'tone-red'};
    }
  }

  const renderDiffLines = (rawDiff) => {
    if (!rawDiff || typeof rawDiff !== 'string') return null;
    const lines = rawDiff.split('\n');
    return (
      <div className="cloud-diff-content">
        {lines.map((line, idx) => {
          let lineType = 'diff-ctx';
          if (line.startsWith('+') && !line.startsWith('+++')) lineType = 'diff-add';
          else if (line.startsWith('-') && !line.startsWith('---')) lineType = 'diff-del';
          else if (line.startsWith('@@')) lineType = 'diff-hunk';
          else if (line.startsWith('diff ') || line.startsWith('index ')) lineType = 'diff-header';
          return (
            <div key={idx} className={`cloud-diff-line ${lineType}`}>
              <code>{line || ' '}</code>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <article className="message tool-card cloud-task-card" role="region" aria-label="云端任务状态卡片">
      <div className="cloud-card-header">
        <div className="cloud-card-title-group">
          <div className="cloud-card-badge-icon">
            <Cloud size={15}/>
          </div>
          <div className="cloud-card-info">
            <div className="cloud-card-title-row">
              <strong className="cloud-card-title">Codex 云端任务</strong>
              <span className={`cloud-card-status-pill ${statusBadge.tone}`}>{statusBadge.label}</span>
            </div>
            <div className="cloud-card-sub-row">
              {environment && (
                <span className="cloud-env-tag" title="云端执行环境 ID">
                  环境：{environment}
                </span>
              )}
              {taskId && (
                <button
                  type="button"
                  className="cloud-id-chip"
                  onClick={copyTaskId}
                  title="点击复制云端任务 ID"
                  aria-label="复制任务 ID"
                >
                  <code>{taskId}</code>
                  {copied ? <Check size={11} className="copied-icon"/> : <Copy size={11}/>}
                </button>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="cloud-card-actions">
        {taskUrl && (
          <a
            href={taskUrl}
            target="_blank"
            rel="noreferrer"
            className="cloud-card-btn cloud-primary-link"
            title="在新标签页中打开官方 Codex 云端任务"
          >
            <ExternalLink size={13}/>
            <span>打开云端任务详情</span>
          </a>
        )}
        <button
          type="button"
          className="cloud-card-btn cloud-refresh-btn"
          onClick={refresh}
          disabled={busy}
          title="拉取云端容器的实时执行状态与 Git Diff"
        >
          <RefreshCw size={13} className={busy ? 'spinning' : ''}/>
          <span>{busy ? '正在拉取…' : '查询状态与 Diff'}</span>
        </button>
      </div>

      {error && (
        <div className="cloud-card-error" role="alert">
          <AlertTriangle size={13}/>
          <span>{error}</span>
        </div>
      )}

      {data && (
        <div className="cloud-card-details">
          {data.status && (
            <div className="cloud-status-section">
              <div className="cloud-section-label">
                <Terminal size={12}/>
                <span>云端执行状态</span>
              </div>
              <pre className="cloud-status-pre">{data.status}</pre>
            </div>
          )}

          {data.diff && (
            <details className="cloud-diff-section" open={data.diff.trim().length > 0}>
              <summary className="cloud-diff-summary">
                <div className="cloud-diff-summary-left">
                  <FileDiff size={13}/>
                  <span>云端代码变更 (Diff)</span>
                </div>
                <span className="cloud-diff-status-pill">尚未同步到本地</span>
                <ChevronDown size={13} className="cloud-diff-arrow"/>
              </summary>
              <div className="cloud-diff-body">
                {renderDiffLines(data.diff)}
              </div>
            </details>
          )}

          <div className="cloud-card-footer-tip">
            <Info size={12}/>
            <span>说明：云端任务在远端沙箱容器中独立运行。满意修改后可在 Codex 云端合并，或在本地执行 <code>git pull</code> 拉取更新。</span>
          </div>
        </div>
      )}
    </article>
  );
}
