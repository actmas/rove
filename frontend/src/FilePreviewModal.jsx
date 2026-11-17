import {useEffect, useMemo, useState} from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  X, Download, ExternalLink, Copy, Check, Code, Eye, FileText,
  Image as ImageIcon, FileCode, FileArchive, Film, Music, AlertCircle,
} from 'lucide-react';
import {api, downloadFile, fileMetaApiPath, fileUrl, formatFileSize} from './api';

function FileKindIcon({kind}) {
  if (kind === 'image') return <ImageIcon size={16}/>;
  if (kind === 'audio') return <Music size={16}/>;
  if (kind === 'video') return <Film size={16}/>;
  if (kind === 'binary') return <FileArchive size={16}/>;
  if (kind === 'html' || kind === 'text') return <FileCode size={16}/>;
  return <FileText size={16}/>;
}

export default function FilePreviewModal({
  filePath,
  sessionId,
  projectId,
  onClose,
  onOpenInCode,
  onError,
}) {
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [viewMode, setViewMode] = useState('rendered');
  const [copiedPath, setCopiedPath] = useState(false);

  useEffect(() => {
    if (!filePath) return;
    let cancelled = false;
    setLoading(true);
    setLoadError('');
    setMeta(null);
    setViewMode('rendered');

    api(fileMetaApiPath(filePath, {sessionId, projectId}))
      .then(data => {
        if (!cancelled) {
          setMeta(data);
          if (data.kind === 'html') setViewMode('source');
          else setViewMode('rendered');
        }
      })
      .catch(err => {
        if (!cancelled) setLoadError(err?.message || '无法读取文件');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [filePath, sessionId, projectId]);

  useEffect(() => {
    if (!filePath) return;
    const handleKeyDown = e => {
      if (e.key === 'Escape') onClose?.();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [filePath, onClose]);

  const rawUrl = useMemo(
    () => fileUrl(meta?.path || filePath, {sessionId, projectId, download: false}),
    [meta?.path, filePath, sessionId, projectId],
  );
  const lines = useMemo(
    () => (meta?.preview_text != null ? meta.preview_text.split('\n') : []),
    [meta?.preview_text],
  );

  if (!filePath) return null;

  async function copyPath() {
    const text = meta?.path || filePath;
    try {
      await navigator.clipboard.writeText(text);
      setCopiedPath(true);
      setTimeout(() => setCopiedPath(false), 1600);
    } catch {
      onError?.('当前浏览器不允许复制');
    }
  }

  return (
    <div className="modal-backdrop file-preview-backdrop" onClick={onClose}>
      <div
        className="modal-card file-preview-modal"
        role="dialog"
        aria-modal="true"
        aria-label={meta?.name ? `文件预览：${meta.name}` : '文件预览'}
        onClick={e => e.stopPropagation()}
      >
        <header className="file-preview-header">
          <div className="file-preview-title-wrap">
            <span className="file-preview-icon">
              <FileKindIcon kind={meta?.kind || 'text'}/>
            </span>
            <div className="file-preview-title-meta">
              <h2 className="file-preview-name">{meta?.name || filePath.split('/').pop() || '文件预览'}</h2>
              <div className="file-preview-sub">
                <code className="file-preview-path" title={meta?.path || filePath}>
                  {meta?.relative_path || meta?.path || filePath}
                </code>
                {meta?.size != null && (
                  <span className="file-preview-badge">{formatFileSize(meta.size)}</span>
                )}
                {meta?.mime && (
                  <span className="file-preview-badge">{meta.mime.split(';')[0]}</span>
                )}
              </div>
            </div>
          </div>
          <button
            type="button"
            className="icon-button file-preview-close"
            onClick={onClose}
            aria-label="关闭文件预览"
          >
            <X size={18}/>
          </button>
        </header>

        {!loading && !loadError && meta && (
          <div className="file-preview-toolbar">
            <div className="file-preview-actions">
              <button
                type="button"
                className="file-preview-btn primary"
                onClick={() => downloadFile(meta.path || filePath, {
                  sessionId,
                  projectId,
                  filename: meta.name,
                  onError,
                })}
              >
                <Download size={14}/>
                <span>下载文件</span>
              </button>
              <a
                href={rawUrl}
                target="_blank"
                rel="noreferrer"
                className="file-preview-btn"
              >
                <ExternalLink size={14}/>
                <span>新标签页打开</span>
              </a>
              {meta.relative_path && onOpenInCode && (
                <button
                  type="button"
                  className="file-preview-btn"
                  onClick={() => {
                    onOpenInCode(meta.relative_path);
                    onClose?.();
                  }}
                >
                  <Code size={14}/>
                  <span>代码面板</span>
                </button>
              )}
              <button type="button" className="file-preview-btn" onClick={copyPath}>
                {copiedPath ? <Check size={14}/> : <Copy size={14}/>}
                <span>{copiedPath ? '已复制路径' : '复制路径'}</span>
              </button>
            </div>

            {(meta.kind === 'markdown' || meta.kind === 'html') && (
              <div className="segmented file-preview-mode">
                <button
                  type="button"
                  className={viewMode === 'rendered' ? 'active' : ''}
                  onClick={() => setViewMode('rendered')}
                >
                  <Eye size={13}/>
                  <span>预览</span>
                </button>
                <button
                  type="button"
                  className={viewMode === 'source' ? 'active' : ''}
                  onClick={() => setViewMode('source')}
                >
                  <Code size={13}/>
                  <span>源码</span>
                </button>
              </div>
            )}
          </div>
        )}

        <div className="file-preview-body">
          {loading ? (
            <div className="file-preview-state">
              <p>正在加载文件信息…</p>
            </div>
          ) : loadError ? (
            <div className="file-preview-state error">
              <AlertCircle size={22}/>
              <p>{loadError}</p>
              <code className="file-preview-path">{filePath}</code>
            </div>
          ) : meta?.kind === 'image' ? (
            <div className="file-preview-media">
              <img src={rawUrl} alt={meta.name}/>
            </div>
          ) : meta?.kind === 'pdf' ? (
            <iframe
              src={rawUrl}
              title={meta.name}
              className="file-preview-iframe"
            />
          ) : meta?.kind === 'audio' ? (
            <div className="file-preview-media">
              <audio controls src={rawUrl}/>
            </div>
          ) : meta?.kind === 'video' ? (
            <div className="file-preview-media">
              <video controls src={rawUrl}/>
            </div>
          ) : meta?.kind === 'html' && viewMode === 'rendered' ? (
            <iframe
              src={rawUrl}
              title={meta.name}
              sandbox=""
              className="file-preview-iframe"
            />
          ) : meta?.kind === 'markdown' && viewMode === 'rendered' ? (
            <div className="file-preview-markdown markdown-body">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {meta.preview_text || ''}
              </ReactMarkdown>
            </div>
          ) : meta?.preview_text != null ? (
            <div className="file-preview-code-wrap">
              {meta.truncated && (
                <div className="file-preview-notice">
                  仅展示前 512 KB 内容，完整文件请点击上方“下载文件”查看。
                </div>
              )}
              <table className="code-table">
                <tbody>
                  {lines.slice(0, 1500).map((line, i) => (
                    <tr key={i}>
                      <td className="ln">{i + 1}</td>
                      <td className="code">{line || ' '}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {lines.length > 1500 && (
                <div className="file-preview-notice">
                  仅展示前 1500 行（共 {lines.length} 行），请下载文件查看全部内容。
                </div>
              )}
            </div>
          ) : (
            <div className="file-preview-state">
              <FileArchive size={32}/>
              <p>此文件为二进制格式（{formatFileSize(meta?.size)}），暂不支持内嵌预览。</p>
              <button
                type="button"
                className="file-preview-btn primary"
                onClick={() => downloadFile(meta?.path || filePath, {
                  sessionId,
                  projectId,
                  filename: meta?.name,
                  onError,
                })}
              >
                <Download size={14}/>
                <span>立即下载 {meta?.name}</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
