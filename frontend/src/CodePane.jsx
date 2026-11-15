import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  Folder, FileCode, ChevronRight, ArrowLeft, RefreshCw, GitBranch,
  Search, Copy, Check, AtSign, FileDiff,
} from 'lucide-react';
import {api, parseUnifiedDiff} from './api';

function FileDiffCard({item, onOpenFile, onMentionFile}) {
  return (
    <details className="diff-card" open>
      <summary className="diff-card-header">
        <FileDiff size={14}/>
        <code className="diff-path">{item.path}</code>
        <span className="diff-stats">
          {item.additions > 0 && <em className="stat-add">+{item.additions}</em>}
          {item.deletions > 0 && <em className="stat-del">-{item.deletions}</em>}
        </span>
        <div className="diff-card-actions" onClick={e => e.stopPropagation()}>
          {onOpenFile && (
            <button type="button" className="link-btn" onClick={() => onOpenFile(item.path)}>
              源码
            </button>
          )}
          {onMentionFile && (
            <button type="button" className="link-btn" onClick={() => onMentionFile(item.path)}>
              @引用
            </button>
          )}
        </div>
      </summary>
      <div className="diff-table-wrap">
        <table className="diff-table">
          <tbody>
            {item.rows.map((row, idx) => (
              <tr key={idx} className={`diff-row ${row.type}`}>
                <td className="ln">{row.oldNum}</td>
                <td className="ln">{row.newNum}</td>
                <td className="code">{row.text}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

export default function CodePane({project, sessionId, onError, initialFile, onConsumeInitialFile, onMentionFile}) {
  const [diff, setDiff] = useState(null);
  const [files, setFiles] = useState([]);
  const [path, setPath] = useState('');
  const [file, setFile] = useState(null);
  const [mode, setMode] = useState('diff');
  const [filter, setFilter] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [copied, setCopied] = useState(false);
  const [maxDisplayLines, setMaxDisplayLines] = useState(600);

  const sessionQuery = sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : '';
  const diffQuery = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';

  const loadFiles = useCallback(async () => {
    if (!project) return;
    try {
      const f = await api(`projects/${project.id}/files?path=${encodeURIComponent(path)}${sessionQuery}`);
      setFiles(f);
    } catch (e) {
      onError(e.message);
    }
  }, [project?.id, path, sessionQuery, onError]);

  const loadDiff = useCallback(async () => {
    if (!project) return;
    try {
      const d = await api(`projects/${project.id}/diff${diffQuery}`);
      setDiff(d);
    } catch (e) {
      onError(e.message);
    }
  }, [project?.id, diffQuery, onError]);

  useEffect(() => {
    loadFiles();
  }, [loadFiles]);

  useEffect(() => {
    loadDiff();
  }, [loadDiff]);

  const refreshAll = useCallback(() => {
    loadFiles();
    loadDiff();
  }, [loadFiles, loadDiff]);

  useEffect(() => {
    setPath('');
    setFile(null);
    setFilter('');
  }, [project?.id, sessionId]);

  const openFileByPath = useCallback(async (filePath) => {
    if (!project || !filePath) return;
    try {
      const data = await api(`projects/${project.id}/file?path=${encodeURIComponent(filePath)}${sessionQuery}`);
      const parent = filePath.includes('/') ? filePath.split('/').slice(0, -1).join('/') : '';
      setPath(parent);
      setFile(data);
      setMaxDisplayLines(600);
      setMode('files');
    } catch (e) {
      onError(e.message);
    }
  }, [project, sessionQuery, onError]);

  useEffect(() => {
    if (initialFile && project) {
      openFileByPath(initialFile);
      onConsumeInitialFile?.();
    }
  }, [initialFile, project, openFileByPath, onConsumeInitialFile]);

  useEffect(() => {
    if (!filter.trim() || !project) {
      setSearchResults([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const res = await api(`projects/${project.id}/search_files?q=${encodeURIComponent(filter.trim())}${sessionQuery}`);
        if (!cancelled) setSearchResults(res);
      } catch {
        if (!cancelled) setSearchResults([]);
      }
    }, 120);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [filter, project, sessionQuery]);

  async function open(item) {
    if (item.directory) {
      setPath(item.path);
      return;
    }
    await openFileByPath(item.path);
  }

  async function copyContent() {
    if (!file) return;
    try {
      await navigator.clipboard.writeText(file.text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      onError('当前浏览器不允许复制');
    }
  }

  const unstagedFiles = useMemo(() => parseUnifiedDiff(diff?.diff), [diff?.diff]);
  const stagedFiles = useMemo(() => parseUnifiedDiff(diff?.staged), [diff?.staged]);
  const statusLines = useMemo(
    () => (diff?.status || '').split('\n').map(s => s.trimEnd()).filter(Boolean),
    [diff?.status],
  );
  const fileLines = useMemo(() => (file?.text ? file.text.split('\n') : []), [file?.text]);

  return (
    <section className="code-pane">
      <div className="pane-toolbar">
        <div className="button-row">
          <div className="segmented">
            <button
              type="button"
              className={mode === 'diff' ? 'active' : ''}
              onClick={() => setMode('diff')}
            >
              Git 变更{statusLines.length > 0 ? ` (${statusLines.length})` : ''}
            </button>
            <button
              type="button"
              className={mode === 'files' ? 'active' : ''}
              onClick={() => setMode('files')}
            >
              项目文件
            </button>
          </div>
          {diff?.is_git && diff.branch && (
            <span className="git-branch-pill" title={diff.head || '当前 Git 分支'}>
              <GitBranch size={13}/>
              <strong>{diff.branch}</strong>
              {diff.head && <span className="desktop-only">· {diff.head.slice(0, 44)}</span>}
            </span>
          )}
        </div>
        <button type="button" onClick={refreshAll} aria-label="刷新代码" title="刷新 Git 状态与文件列表">
          <RefreshCw size={15}/>
          <span className="desktop-only">刷新</span>
        </button>
      </div>

      {mode === 'diff' ? (
        <div className="diff-view">
          {!diff ? (
            <p className="empty-note">正在读取 Git 工作区…</p>
          ) : !diff.is_git ? (
            <p className="empty-note">当前项目未初始化 Git 仓库，可切换至“项目文件”浏览源码。</p>
          ) : (
            <div className="diff-container">
              <div className="status-card">
                <h3>工作区概览</h3>
                {statusLines.length === 0 ? (
                  <p className="clean-tree">工作区干净，没有未提交的修改。</p>
                ) : (
                  <div className="status-items">
                    {statusLines.map((line, i) => {
                      const code = line.slice(0, 2).trim() || 'M';
                      const rel = line.slice(3).trim().replace(/^"|"$/g, '');
                      const isDir = rel.endsWith('/');
                      const canOpen = !code.includes('D') && !isDir;
                      return (
                        <button
                          type="button"
                          key={i}
                          className="status-chip"
                          onClick={() => isDir ? setMode('files') : (canOpen && openFileByPath(rel))}
                          title={isDir ? '切换至项目文件浏览目录' : (canOpen ? `点击查看 ${rel}` : rel)}
                        >
                          <span className={`git-code code-${code[0]}`}>{code}</span>
                          <code>{rel === './' ? '当前项目目录尚未纳入 Git 提交（点击浏览文件）' : rel}</code>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>

              {stagedFiles.length > 0 && (
                <div className="diff-section">
                  <h3>已暂存变更 ({stagedFiles.length})</h3>
                  {stagedFiles.map(item => (
                    <FileDiffCard
                      key={`staged-${item.path}`}
                      item={item}
                      onOpenFile={openFileByPath}
                      onMentionFile={onMentionFile}
                    />
                  ))}
                </div>
              )}

              {unstagedFiles.length > 0 && (
                <div className="diff-section">
                  <h3>未暂存变更 ({unstagedFiles.length})</h3>
                  {unstagedFiles.map(item => (
                    <FileDiffCard
                      key={`unstaged-${item.path}`}
                      item={item}
                      onOpenFile={openFileByPath}
                      onMentionFile={onMentionFile}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className={`file-workspace ${file ? 'has-file-open' : ''}`}>
          <nav className="file-list" aria-label="项目文件">
            <div className="file-search-box">
              <Search size={14}/>
              <input
                placeholder="快速搜索文件…"
                value={filter}
                onChange={e => setFilter(e.target.value)}
                aria-label="搜索项目文件"
              />
            </div>

            {filter.trim() ? (
              <div className="file-items">
                {searchResults.map(f => (
                  <button
                    type="button"
                    key={f.path}
                    onClick={() => openFileByPath(f.path)}
                    className={file?.path === f.path ? 'selected' : ''}
                    title={f.path}
                  >
                    <FileCode size={15}/>
                    <span>{f.path}</span>
                  </button>
                ))}
                {!searchResults.length && <p className="sidebar-empty">无匹配文件</p>}
              </div>
            ) : (
              <div className="file-items">
                <button
                  type="button"
                  className="folder-up"
                  onClick={() => {
                    setPath(path.split('/').slice(0, -1).join('/'));
                    setFile(null);
                  }}
                  disabled={!path}
                >
                  <ArrowLeft size={14}/>
                  <span>{path || project?.name || '根目录'}</span>
                </button>
                {files.map(f => (
                  <button
                    type="button"
                    key={f.path}
                    onClick={() => open(f)}
                    className={file?.path === f.path ? 'selected' : ''}
                  >
                    {f.directory ? <Folder size={15}/> : <FileCode size={15}/>}
                    <span>{f.name}</span>
                    {f.directory && <ChevronRight size={13}/>}
                  </button>
                ))}
              </div>
            )}
          </nav>

          <div className="file-content">
            {file ? (
              <>
                <div className="file-viewer-header">
                  <button
                    type="button"
                    className="mobile-only file-back-btn"
                    onClick={() => setFile(null)}
                    aria-label="返回文件列表"
                  >
                    <ArrowLeft size={14}/>
                    <span>列表</span>
                  </button>
                  <code>{file.path}</code>
                  <div className="button-row">
                    {onMentionFile && (
                      <button type="button" onClick={() => onMentionFile(file.path)}>
                        <AtSign size={13}/>引用到对话
                      </button>
                    )}
                    <button type="button" onClick={copyContent}>
                      {copied ? <Check size={13}/> : <Copy size={13}/>}
                      <span>{copied ? '已复制' : '复制'}</span>
                    </button>
                  </div>
                </div>
                <div className="code-table-wrap">
                  <table className="code-table">
                    <tbody>
                      {fileLines.slice(0, maxDisplayLines).map((line, i) => (
                        <tr key={i}>
                          <td className="ln">{i + 1}</td>
                          <td className="code">{line || ' '}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {fileLines.length > maxDisplayLines && (
                    <div style={{padding: '12px', textAlign: 'center'}}>
                      <button
                        type="button"
                        className="btn-secondary"
                        onClick={() => setMaxDisplayLines(v => v + 1000)}
                        style={{padding: '6px 14px', fontSize: '13px', borderRadius: '6px', cursor: 'pointer'}}
                      >
                        显示更多（已展示前 {maxDisplayLines} 行 / 共 {fileLines.length} 行）
                      </button>
                    </div>
                  )}
                </div>
              </>
            ) : (
              <div className="empty-note">从左侧选择或搜索文件查看源码。</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
