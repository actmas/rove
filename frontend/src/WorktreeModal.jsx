import React, {useState, useEffect} from 'react';
import {X, GitBranch, GitMerge, Trash2, ArrowUpRight, AlertCircle, CheckCircle2, RefreshCw, FileText} from 'lucide-react';
import {api, post, del} from './api';

export default function WorktreeModal({isOpen, onClose, session, project, onWorktreeChanged}) {
  const [data, setData] = useState(null);
  const [diff, setDiff] = useState('');
  const [showDiff, setShowDiff] = useState(false);
  const [loading, setLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [mergeMessage, setMergeMessage] = useState('');

  async function loadStatus() {
    if (!session?.id) return;
    setLoading(true);
    setError('');
    try {
      const res = await api(`sessions/${session.id}/worktree`);
      setData(res);
    } catch (err) {
      setError(err.message || '获取工作树状态失败');
    } finally {
      setLoading(false);
    }
  }

  async function loadDiff() {
    if (!session?.id) return;
    setActionBusy(true);
    try {
      const res = await api(`sessions/${session.id}/worktree/diff`);
      setDiff(res.diff || '(暂无差异)');
      setShowDiff(true);
    } catch (err) {
      setError(err.message || '获取工作树差异失败');
    } finally {
      setActionBusy(false);
    }
  }

  useEffect(() => {
    if (isOpen && session?.id) {
      loadStatus();
      setShowDiff(false);
      setDiff('');
      setSuccess('');
      setError('');
    }
  }, [isOpen, session?.id]);

  if (!isOpen || !session) return null;

  async function handleEnable() {
    setActionBusy(true);
    setError('');
    try {
      await post(`sessions/${session.id}/worktree/enable`);
      setSuccess('已成功创建并绑定独立工作树分支');
      await loadStatus();
      onWorktreeChanged?.();
    } catch (err) {
      setError(err.message || '启用工作树失败');
    } finally {
      setActionBusy(false);
    }
  }

  async function handleMerge() {
    setActionBusy(true);
    setError('');
    setSuccess('');
    try {
      const res = await post(`sessions/${session.id}/worktree/merge`, {message: mergeMessage.trim() || null});
      if (res.status === 'merged') {
        setSuccess(res.detail || '已成功将代码合并至主分支！');
        setMergeMessage('');
        await loadStatus();
        onWorktreeChanged?.();
      } else if (res.status === 'conflict') {
        setError(res.detail || '合并存在代码冲突，请在终端处理');
      } else {
        setSuccess(res.detail || '工作树与主分支代码已同步');
      }
    } catch (err) {
      setError(err.message || '合并工作树失败');
    } finally {
      setActionBusy(false);
    }
  }

  async function handleDelete() {
    if (!confirm('确定要清理并删除此独立工作树吗？这不会删除主仓库代码。')) return;
    setActionBusy(true);
    setError('');
    try {
      await del(`sessions/${session.id}/worktree`);
      setSuccess('已移除独立工作树');
      await loadStatus();
      onWorktreeChanged?.();
      setTimeout(onClose, 800);
    } catch (err) {
      setError(err.message || '删除工作树失败');
    } finally {
      setActionBusy(false);
    }
  }

  const isEnabled = data?.use_worktree && data?.worktree;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal quota-modal worktree-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title-group">
            <GitBranch size={20} className="modal-title-icon" style={{color: '#10b981'}} />
            <h2>Git Worktree · 任务并行隔离池</h2>
          </div>
          <button type="button" className="icon-button modal-close" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </div>

        <div className="modal-body">
          {error && (
            <div className="notice-banner error" style={{marginBottom: 12}}>
              <AlertCircle size={15} />
              <span>{error}</span>
            </div>
          )}
          {success && (
            <div className="notice-banner success" style={{marginBottom: 12}}>
              <CheckCircle2 size={15} />
              <span>{success}</span>
            </div>
          )}

          {!data?.is_git ? (
            <div className="memory-empty-state">
              <AlertCircle size={32} style={{color: 'var(--amber)', marginBottom: 8}} />
              <p>当前项目尚未初始化为 Git 仓库</p>
              <p className="subtext">Git Worktree 隔离需要项目为 Git 仓库且至少有一个初始提交。</p>
            </div>
          ) : !data?.has_head ? (
            <div className="memory-empty-state">
              <AlertCircle size={32} style={{color: 'var(--amber)', marginBottom: 8}} />
              <p>当前 Git 仓库尚无任何提交记录 (HEAD 无效)</p>
              <p className="subtext">请先在终端进行首次 commit，以便系统派生独立任务分支。</p>
            </div>
          ) : !isEnabled ? (
            <div className="worktree-unbound-box">
              <p>
                当前会话正直接在项目 <strong>主工作区</strong> 运行。如果主目录已有其他任务在执行，新任务会被排队串行锁定。
              </p>
              <p className="subtext">
                开启 <strong>独立工作树隔离 (Worktree)</strong> 后，系统将自动为该会话派生独立分支目录（存放在 <code>.worktrees/</code>，不污染代码库），<strong>彻底解除互斥，支持与主目录及其他会话同时并行执行！</strong>
              </p>

              <button
                type="button"
                className="btn btn-primary"
                style={{marginTop: 14}}
                onClick={handleEnable}
                disabled={actionBusy}
              >
                <GitBranch size={15} />
                <span>立即为此会话开启独立工作树 (并行执行)</span>
              </button>
            </div>
          ) : (
            <div className="worktree-active-box">
              <div className="worktree-info-card">
                <div className="worktree-card-row">
                  <span className="label">当前分支：</span>
                  <code className="val-badge branch">{data.worktree.branch}</code>
                </div>
                <div className="worktree-card-row">
                  <span className="label">隔离目录：</span>
                  <code className="val-path">{data.worktree.path}</code>
                </div>
                <div className="worktree-card-row">
                  <span className="label">修改状态：</span>
                  <span>
                    未提交变更：<strong>{data.worktree.uncommitted_files}</strong> 个文件 ·
                    超前主分支：<strong>{data.worktree.commits_ahead}</strong> 次提交
                  </span>
                </div>
              </div>

              <div className="worktree-actions-group">
                <div className="merge-form">
                  <input
                    type="text"
                    className="memory-select"
                    style={{flex: 1, padding: '7px 10px'}}
                    placeholder="合并说明（可选，留空将使用默认说明）"
                    value={mergeMessage}
                    onChange={e => setMergeMessage(e.target.value)}
                  />
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={handleMerge}
                    disabled={actionBusy}
                    title="将本工作树的所有改动 Squash Merge 合并回项目主分支"
                  >
                    <GitMerge size={14} />
                    <span>合并至主分支</span>
                  </button>
                </div>

                <div className="secondary-btn-row">
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={loadDiff}
                    disabled={actionBusy}
                  >
                    <FileText size={14} />
                    <span>{showDiff ? '刷新累计 Diff' : '查看累计 Diff'}</span>
                  </button>

                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={handleDelete}
                    disabled={actionBusy}
                  >
                    <Trash2 size={14} />
                    <span>释放并清理工作树</span>
                  </button>
                </div>
              </div>

              {showDiff && (
                <div className="worktree-diff-viewer">
                  <div className="diff-header">
                    <span>工作树累积代码差异 (HEAD...{data.worktree.branch})</span>
                    <button type="button" className="icon-button" onClick={() => setShowDiff(false)}>
                      <X size={14} />
                    </button>
                  </div>
                  <pre className="diff-pre">{diff}</pre>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="modal-footer">
          <span style={{fontSize: 12, color: 'var(--muted)'}}>
            🌲 借鉴自 worktrunk 机制：各工作树独享物理目录与编译环境，互不干扰。
          </span>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
