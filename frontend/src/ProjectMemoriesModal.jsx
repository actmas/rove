import React, {useState, useEffect} from 'react';
import {X, Brain, Plus, Pin, Trash2, CheckCircle2, Sparkles, Filter, AlertCircle, Palette} from 'lucide-react';
import {api, post, patch, del} from './api';

const CATEGORY_MAP = {
  convention: {label: '开发规范', color: 'var(--accent)', bg: 'rgba(59, 130, 246, 0.1)'},
  architecture: {label: '架构决策', color: '#9333ea', bg: 'rgba(147, 51, 234, 0.1)'},
  constraint: {label: '硬性红线', color: 'var(--rose)', bg: 'rgba(239, 68, 68, 0.1)'},
  tooling: {label: '构建测试', color: 'var(--amber)', bg: 'rgba(245, 158, 11, 0.1)'},
  general: {label: '项目备忘', color: 'var(--muted)', bg: 'rgba(107, 114, 128, 0.1)'},
};

export default function ProjectMemoriesModal({isOpen, onClose, project, onMemoriesChange}) {
  const [memories, setMemories] = useState([]);
  const [categories, setCategories] = useState({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [filterCat, setFilterCat] = useState('all');

  // Form state for creating a new memory
  const [isAdding, setIsAdding] = useState(false);
  const [newContent, setNewContent] = useState('');
  const [newCategory, setNewCategory] = useState('convention');
  const [newPinned, setNewPinned] = useState(false);
  const [saving, setSaving] = useState(false);

  async function loadMemories() {
    if (!project?.id) return;
    setLoading(true);
    setError('');
    try {
      const res = await api(`projects/${project.id}/memories`);
      setMemories(res.memories || []);
      setCategories(res.categories || {});
    } catch (err) {
      setError(err.message || '获取项目记忆失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (isOpen && project?.id) {
      loadMemories();
      setIsAdding(false);
      setNewContent('');
    }
  }, [isOpen, project?.id]);

  if (!isOpen || !project) return null;

  async function handleSeedDesign() {
    setSaving(true);
    setError('');
    try {
      await post(`projects/${project.id}/design/seed`);
      await loadMemories();
      onMemoriesChange?.();
    } catch (err) {
      setError(err.message || '导入设计规范失败');
    } finally {
      setSaving(false);
    }
  }

  async function handleCreate(e) {
    e.preventDefault();
    if (!newContent.trim()) return;
    setSaving(true);
    setError('');
    try {
      const created = await post(`projects/${project.id}/memories`, {
        content: newContent.trim(),
        category: newCategory,
        pinned: newPinned,
      });
      setMemories(prev => [created, ...prev.filter(m => m.id !== created.id)]);
      setNewContent('');
      setIsAdding(false);
      onMemoriesChange?.();
    } catch (err) {
      setError(err.message || '保存记忆失败');
    } finally {
      setSaving(false);
    }
  }

  async function handleTogglePin(mem) {
    const nextPin = !mem.pinned;
    try {
      const updated = await patch(`memories/${mem.id}`, {pinned: nextPin});
      setMemories(prev => prev.map(m => (m.id === mem.id ? updated : m)).sort((a, b) => b.pinned - a.pinned));
      onMemoriesChange?.();
    } catch (err) {
      setError(err.message || '更新置顶状态失败');
    }
  }

  async function handleDelete(id) {
    try {
      await del(`memories/${id}`);
      setMemories(prev => prev.filter(m => m.id !== id));
      onMemoriesChange?.();
    } catch (err) {
      setError(err.message || '删除记忆失败');
    }
  }

  const displayedMemories = filterCat === 'all'
    ? memories
    : memories.filter(m => m.category === filterCat);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal quota-modal memory-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title-group">
            <Brain size={20} className="modal-title-icon" style={{color: 'var(--accent)'}} />
            <h2>项目持久记忆 · {project.name}</h2>
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

          <div className="memory-info-tip">
            <Sparkles size={14} style={{flexShrink: 0, color: 'var(--accent)'}} />
            <span>
              已沉淀 <strong>{memories.length}</strong> 条跨会话记忆。在手机端新建会话时，系统将自动把它们注入到 AI 提示词首部，无需重复交待背景与规范。
            </span>
          </div>

          <div className="memory-toolbar">
            <div className="memory-filter-pills">
              <button
                type="button"
                className={`memory-pill ${filterCat === 'all' ? 'active' : ''}`}
                onClick={() => setFilterCat('all')}
              >
                全部 ({memories.length})
              </button>
              {Object.entries(CATEGORY_MAP).map(([catKey, catMeta]) => {
                const count = memories.filter(m => m.category === catKey).length;
                if (count === 0 && filterCat !== catKey) return null;
                return (
                  <button
                    key={catKey}
                    type="button"
                    className={`memory-pill ${filterCat === catKey ? 'active' : ''}`}
                    onClick={() => setFilterCat(catKey)}
                    style={filterCat === catKey ? {borderColor: catMeta.color, color: catMeta.color} : {}}
                  >
                    {catMeta.label} ({count})
                  </button>
                );
              })}
            </div>

            {!isAdding && (
              <div style={{display: 'flex', gap: 6}}>
                <button
                  type="button"
                  className="btn btn-secondary memory-add-btn"
                  onClick={handleSeedDesign}
                  disabled={saving}
                  title="一键为项目注入基于 pbakaus/impeccable 的 Anti-AI-Slop 前端防平庸审美规范"
                >
                  <Palette size={13} style={{color: '#9333ea'}} />
                  <span>注入审美规范</span>
                </button>
                <button
                  type="button"
                  className="btn btn-secondary memory-add-btn"
                  onClick={() => setIsAdding(true)}
                >
                  <Plus size={14} />
                  <span>记一条</span>
                </button>
              </div>
            )}
          </div>

          {isAdding && (
            <form className="memory-create-form" onSubmit={handleCreate}>
              <div className="memory-form-row">
                <select
                  value={newCategory}
                  onChange={e => setNewCategory(e.target.value)}
                  className="memory-select"
                >
                  <option value="convention">开发规范（如代码风格、工具）</option>
                  <option value="architecture">架构决策（如数据结构、框架）</option>
                  <option value="constraint">硬性红线（如禁止修改的文件）</option>
                  <option value="tooling">构建测试（如特定测试命令）</option>
                  <option value="general">项目备忘</option>
                </select>

                <label className="memory-pin-toggle">
                  <input
                    type="checkbox"
                    checked={newPinned}
                    onChange={e => setNewPinned(e.target.checked)}
                  />
                  <span>置顶优先注入</span>
                </label>
              </div>

              <textarea
                className="memory-textarea"
                rows={3}
                placeholder="例如：本项目使用 pnpm 管理依赖，测试统一执行 python -m unittest discover -s tests -v"
                value={newContent}
                onChange={e => setNewContent(e.target.value)}
                autoFocus
                required
              />

              <div className="memory-form-actions">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setIsAdding(false)}
                  disabled={saving}
                >
                  取消
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={saving || !newContent.trim()}
                >
                  {saving ? '保存中…' : '记住这条约定'}
                </button>
              </div>
            </form>
          )}

          <div className="memory-list">
            {loading && memories.length === 0 ? (
              <div className="memory-empty-state">正在读取持久记忆…</div>
            ) : displayedMemories.length === 0 ? (
              <div className="memory-empty-state">
                <Brain size={32} style={{opacity: 0.3, marginBottom: 8}} />
                <p>当前项目暂无持久记忆</p>
                <p className="subtext">
                  点击上方“记一条”添加团队约定，或在平时对话中说“请记住：...”，工作台会自动提炼保存。
                </p>
              </div>
            ) : (
              displayedMemories.map(mem => {
                const meta = CATEGORY_MAP[mem.category] || CATEGORY_MAP.convention;
                return (
                  <div
                    key={mem.id}
                    className={`memory-card ${mem.pinned ? 'is-pinned' : ''}`}
                  >
                    <div className="memory-card-header">
                      <span
                        className="memory-badge"
                        style={{color: meta.color, backgroundColor: meta.bg}}
                      >
                        {meta.label}
                      </span>

                      <div className="memory-card-actions">
                        <button
                          type="button"
                          className={`icon-button memory-pin-btn ${mem.pinned ? 'active' : ''}`}
                          onClick={() => handleTogglePin(mem)}
                          title={mem.pinned ? '取消置顶' : '置顶'}
                        >
                          <Pin size={13} style={mem.pinned ? {fill: 'currentColor'} : {}} />
                        </button>
                        <button
                          type="button"
                          className="icon-button memory-delete-btn"
                          onClick={() => handleDelete(mem.id)}
                          title="删除此记忆"
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </div>

                    <div className="memory-card-content">{mem.content}</div>

                    <div className="memory-card-footer">
                      <span>{new Date(mem.updated * 1000).toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'})}</span>
                      {mem.source_session_id && <span>来自对话提炼</span>}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        <div className="modal-footer" style={{justifyContent: 'space-between'}}>
          <span style={{fontSize: 12, color: 'var(--muted)'}}>
            💡 提示：在与 AI 对话时输入“请记住：...”，本系统会自动识别并沉淀到记忆库。
          </span>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            完成
          </button>
        </div>
      </div>
    </div>
  );
}
