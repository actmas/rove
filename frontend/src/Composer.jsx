import {useCallback, useEffect, useRef, useState} from 'react';
import {
  ArrowUp, Folder, Square, ChevronDown, Brain, SlidersHorizontal,
  ShieldCheck, Zap, ImagePlus, AtSign, X, FileCode, Sparkles,
  Cloud, Laptop, ExternalLink, Info,
} from 'lucide-react';
import {api, compressImageFile} from './api';

export default function Composer({
  sessionId = 'new', initialExecutionTarget = 'local', initialCloudEnvironment = '',
  initialPrompt = '', initialImages = [], onSend,
  prompt: propPrompt, setPrompt: propSetPrompt, images: propImages, setImages: propSetImages, submit,
  active, externalActive, writerLocked, sending, onStop,
  error, onError, projects, projectId, onProjectChange, sessionLocked, sessionCwd,
  engine, onEngineChange, engineState, catalog,
  model, onModelChange, effort, onEffortChange, mode, onModeChange, fast, onFastChange,
  onOpenSettings, externalRef,
}) {
  const [targetChoice, setTargetChoice] = useState(() => {
    if (initialExecutionTarget && initialExecutionTarget !== 'local') return initialExecutionTarget;
    try {
      return localStorage.getItem(`walking-workstation:target:${engine}`) || 'local';
    } catch {
      return 'local';
    }
  });

  const [cloudEnvironment, setCloudEnvironment] = useState(() => {
    if (initialCloudEnvironment) return initialCloudEnvironment;
    try {
      return localStorage.getItem('walking-workstation:last-cloud-env') || '';
    } catch {
      return '';
    }
  });

  const [recentEnvs, setRecentEnvs] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('walking-workstation:recent-cloud-envs') || '[]');
      return Array.isArray(saved) ? saved : [];
    } catch {
      return [];
    }
  });

  useEffect(() => {
    if (initialExecutionTarget) setTargetChoice(initialExecutionTarget);
    if (initialCloudEnvironment) setCloudEnvironment(initialCloudEnvironment);
  }, [initialExecutionTarget, initialCloudEnvironment]);
  const localRef = useRef(null);
  const fileInputRef = useRef(null);
  const isComposingRef = useRef(false);
  const draftStorageKey = `walking-workstation:draft:${sessionId || 'new'}`;

  // Isolate draft text and image state locally if not controlled by parent
  const [internalPrompt, setInternalPrompt] = useState(() => {
    try {
      const saved = sessionStorage.getItem(draftStorageKey);
      if (saved) return saved;
    } catch {}
    return initialPrompt;
  });
  const [internalImages, setInternalImages] = useState(initialImages);

  const prompt = propPrompt !== undefined ? propPrompt : internalPrompt;
  const setPrompt = propSetPrompt || setInternalPrompt;

  const images = propImages !== undefined ? propImages : internalImages;
  const setImages = propSetImages || setInternalImages;

  useEffect(() => {
    if (propPrompt !== undefined) return;
    try {
      if (internalPrompt) {
        sessionStorage.setItem(draftStorageKey, internalPrompt);
      } else {
        sessionStorage.removeItem(draftStorageKey);
      }
    } catch {}
  }, [internalPrompt, draftStorageKey, propPrompt]);

  const [mention, setMention] = useState(null);
  const [fileResults, setFileResults] = useState([]);
  const [activeIndex, setActiveIndex] = useState(0);

  const availableAgents = (catalog?.agents || []).filter(a => a.enabled !== false);
  const engineOptions = availableAgents.length >= 2 ? availableAgents : [
    {id: 'codex', name: 'Codex', short_name: 'Codex'},
    {id: 'antigravity', name: 'Antigravity', short_name: 'Antigravity'},
  ];
  const agentSpec = availableAgents.find(a => a.id === engine);
  const defaultTargets = engine === 'codex' ? ['local', 'cloud'] : ['local'];
  const supportedTargets = agentSpec?.execution_targets || defaultTargets;
  const dualTarget = supportedTargets.includes('local') && supportedTargets.includes('cloud');
  const executionTarget = dualTarget ? targetChoice : 'local';
  const cloud = executionTarget === 'cloud';

  const handleTargetChange = (nextTarget) => {
    setTargetChoice(nextTarget);
    try {
      localStorage.setItem(`walking-workstation:target:${engine}`, nextTarget);
    } catch {}
  };
  const models = catalog?.[engine]?.models || [];
  const currentModel = models.find(m => m.id === model) || models.find(m => m.isDefault) || models[0];
  const efforts = currentModel?.efforts || ['low', 'medium', 'high'];
  const effortMap = catalog?.efforts || {};
  const modes = catalog?.modes?.[engine] || [
    {id: 'standard', label: '标准审批'},
    {id: 'full', label: '完全访问'},
    {id: 'auto', label: '自动执行'},
    {id: 'plan', label: '只读规划'},
  ];

  const adjustHeight = useCallback(el => {
    if (!el) return;
    const val = el.value || '';
    if (!val) {
      if (el.style.height) el.style.height = '';
      return;
    }
    const isMobile = window.innerWidth <= 768;
    const minH = isMobile ? 48 : 56;
    const maxH = isMobile ? 160 : 220;
    if (el.clientHeight >= minH && el.scrollHeight <= el.clientHeight && !val.includes('\n')) {
      return;
    }
    requestAnimationFrame(() => {
      if (!el) return;
      el.style.height = 'auto';
      const next = Math.min(Math.max(el.scrollHeight, minH), maxH);
      el.style.height = `${next}px`;
    });
  }, []);

  // Expose imperative methods to externalRef (DOM element with helpers attached)
  useEffect(() => {
    const el = localRef.current;
    if (!el) return;
    if (externalRef) {
      if (typeof externalRef === 'function') {
        externalRef(el);
      } else {
        externalRef.current = el;
      }
    }
    el.setPrompt = text => {
      setPrompt(text);
      requestAnimationFrame(() => adjustHeight(el));
    };
    el.appendPrompt = text => {
      setPrompt(prev => {
        const sep = prev && !prev.endsWith(' ') && !prev.endsWith('\n') ? ' ' : '';
        return `${prev}${sep}${text}`;
      });
      requestAnimationFrame(() => adjustHeight(el));
    };
    el.clearDraft = () => {
      try { sessionStorage.removeItem(draftStorageKey); } catch {}
      setPrompt('');
      setImages([]);
      setMention(null);
      el.style.height = '56px';
    };
  }, [adjustHeight, draftStorageKey, externalRef, setImages, setPrompt]);

  // Adjust height on initial render
  useEffect(() => {
    adjustHeight(localRef.current);
  }, [adjustHeight]);

  useEffect(() => {
    if (!mention || !projectId) {
      setFileResults([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const list = await api(`projects/${projectId}/search_files?q=${encodeURIComponent(mention.query)}`);
        if (!cancelled) {
          setFileResults(list.slice(0, 8));
          setActiveIndex(0);
        }
      } catch {
        if (!cancelled) setFileResults([]);
      }
    }, 120);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [mention, projectId]);

  function detectMention(value, cursor) {
    if (cursor == null || !value || !value.includes('@')) {
      setMention(prev => (prev !== null ? null : prev));
      return;
    }
    const before = value.slice(0, cursor);
    const match = before.match(/(?:^|\s)@([A-Za-z0-9_./-]{0,80})$/);
    if (!match) {
      setMention(prev => (prev !== null ? null : prev));
      return;
    }
    const query = match[1];
    const start = cursor - query.length - 1;
    setMention(prev => {
      if (prev && prev.start === start && prev.end === cursor && prev.query === query) return prev;
      return {start, end: cursor, query};
    });
  }

  function insertMention(item) {
    if (!mention) return;
    const next = `${prompt.slice(0, mention.start)}\`${item.path}\` ${prompt.slice(mention.end)}`;
    setPrompt(next);
    setMention(null);
    requestAnimationFrame(() => {
      const el = localRef.current;
      if (el) {
        el.focus();
        adjustHeight(el);
      }
    });
  }

  function triggerAtButton() {
    const el = localRef.current;
    if (!el) return;
    const pos = el.selectionStart ?? prompt.length;
    const prefix = pos > 0 && !/\s/.test(prompt[pos - 1]) ? ' @' : '@';
    const next = prompt.slice(0, pos) + prefix + prompt.slice(pos);
    const cursor = pos + prefix.length;
    setPrompt(next);
    setMention({start: cursor - 1, end: cursor, query: ''});
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(cursor, cursor);
      adjustHeight(el);
    });
  }

  async function addFiles(fileList) {
    const incoming = Array.from(fileList || []).filter(f => f.type?.startsWith('image/'));
    if (!incoming.length) return;
    const slots = Math.max(0, 4 - images.length);
    if (!slots) {
      onError?.('单次最多附加 4 张图片');
      return;
    }
    try {
      const processed = [];
      for (const file of incoming.slice(0, slots)) {
        processed.push(await compressImageFile(file));
      }
      setImages(prev => [...prev, ...processed].slice(0, 4));
    } catch (err) {
      onError?.(err.message || '图片处理失败');
    }
  }

  function handlePaste(e) {
    const items = Array.from(e.clipboardData?.items || []);
    const imageFiles = items.filter(it => it.type.startsWith('image/')).map(it => it.getAsFile()).filter(Boolean);
    if (imageFiles.length) {
      e.preventDefault();
      addFiles(imageFiles);
    }
    requestAnimationFrame(() => adjustHeight(localRef.current));
  }

  function handleCompositionStart() {
    isComposingRef.current = true;
  }

  function handleCompositionEnd(e) {
    isComposingRef.current = false;
    const val = e.target.value;
    setPrompt(val);
    detectMention(val, e.target.selectionStart);
    adjustHeight(e.target);
  }

  function handleChange(e) {
    const val = e.target.value;
    setPrompt(val);
    if (!isComposingRef.current) {
      detectMention(val, e.target.selectionStart);
    }
    adjustHeight(e.target);
  }

  function handleKeyDown(e) {
    if (e.nativeEvent.isComposing || isComposingRef.current || e.keyCode === 229) {
      return;
    }
    if (mention && fileResults.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIndex(i => (i + 1) % fileResults.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIndex(i => (i - 1 + fileResults.length) % fileResults.length);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        insertMention(fileResults[activeIndex] || fileResults[0]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setMention(null);
        return;
      }
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      handleSubmit(e);
    }
  }

  async function handleSubmit(e) {
    if (e && e.preventDefault) e.preventDefault();
    if (submit) {
      return submit(e);
    }
    if ((!prompt.trim() && !images.length) || busy || writerLocked) return;
    if (onSend) {
      if (cloud && engine !== 'codex' && !agentSpec?.builtin && agentSpec?.cloud_url) {
        window.open(agentSpec.cloud_url, '_blank', 'noopener,noreferrer');
        return;
      }
      if (cloud && !cloudEnvironment.trim()) { onError?.('请填写 Codex 云端环境 ID'); return; }
      const ok = await onSend(prompt, images, {execution_target: executionTarget, cloud_environment: cloudEnvironment.trim()});
      if (ok) {
        if (cloud && cloudEnvironment.trim()) {
          try {
            const env = cloudEnvironment.trim();
            localStorage.setItem('walking-workstation:last-cloud-env', env);
            const nextList = [env, ...recentEnvs.filter(x => x !== env)].slice(0, 6);
            setRecentEnvs(nextList);
            localStorage.setItem('walking-workstation:recent-cloud-envs', JSON.stringify(nextList));
          } catch {}
        }
        try { sessionStorage.removeItem(draftStorageKey); } catch {}
        setPrompt('');
        setImages([]);
        setMention(null);
        if (localRef.current) {
          localRef.current.style.height = '56px';
        }
      }
    }
  }

  const projectObj = projects.find(p => p.id === projectId);
  const busy = active || Boolean(externalActive) || sending;
  const canSend = (prompt.trim().length > 0 || images.length > 0) && !busy && !writerLocked;
  const workspaceProjects = projects.filter(p => p.is_workspace);
  const repoProjects = projects.filter(p => !p.is_workspace);

  return (
    <div className="composer-wrap">
      {mention && fileResults.length > 0 && (
        <div className="mention-popover" role="listbox" aria-label="引用项目文件">
          <div className="mention-header">选择项目文件插入引用</div>
          {fileResults.map((item, idx) => (
            <button
              type="button"
              key={item.path}
              className={`mention-item ${idx === activeIndex ? 'active' : ''}`}
              onMouseDown={e => {e.preventDefault(); insertMention(item);}}
            >
              <FileCode size={14}/>
              <span className="mention-path">{item.path}</span>
            </button>
          ))}
        </div>
      )}

      <form className={`composer ${mode === 'plan' ? 'mode-plan' : mode === 'auto' ? 'mode-auto' : ''}`} onSubmit={handleSubmit}>
        {error && <div className="error composer-error" role="alert">{error}</div>}

        {images.length > 0 && (
          <div className="composer-attachments">
            {images.map((img, idx) => (
              <div key={idx} className="attachment-chip">
                <img src={img.dataUrl} alt={img.name}/>
                <span>{img.name}</span>
                <button
                  type="button"
                  aria-label="移除图片"
                  onClick={() => setImages(prev => prev.filter((_, i) => i !== idx))}
                >
                  <X size={12}/>
                </button>
              </div>
            ))}
          </div>
        )}

        <textarea
          ref={localRef}
          aria-label="任务内容"
          placeholder={
            externalActive
              ? 'Mac 本机 Codex 正在执行当前回合，完成后可在此无缝接力…'
              : writerLocked
                ? '此原生会话当前被其他客户端持有写入锁，释放后可在此继续发送…'
                : mode === 'plan'
                  ? '输入想分析的问题或架构方案（只读规划，不修改代码）…'
                  : '描述编程任务、粘贴截图，或输入 @ 引用项目文件…'
          }
          value={prompt}
          onChange={handleChange}
          onCompositionStart={handleCompositionStart}
          onCompositionEnd={handleCompositionEnd}
          onClick={e => !isComposingRef.current && detectMention(e.currentTarget.value, e.currentTarget.selectionStart)}
          onPaste={handlePaste}
          onKeyDown={handleKeyDown}
        />

        <div className="composer-toolbar">
          <div className="composer-selectors">
            {!sessionLocked ? (
              <label className="pill-select project-picker" title="选择目标项目">
                <Folder size={14}/>
                <select aria-label="选择项目" value={projectId} onChange={e => onProjectChange(e.target.value)} disabled={busy}>
                  {workspaceProjects.length > 0 && (
                    <optgroup label="Codex 工作区">
                      {workspaceProjects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                    </optgroup>
                  )}
                  {repoProjects.length > 0 ? (
                    <optgroup label={workspaceProjects.length > 0 ? '子项目仓库' : '项目列表'}>
                      {repoProjects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                    </optgroup>
                  ) : (
                    !workspaceProjects.length && projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)
                  )}
                </select>
                <ChevronDown size={12}/>
              </label>
            ) : (
              <span className="pill-static" title={`工作目录：${sessionCwd || projectObj?.path || projectObj?.name || ''}`}>
                <Folder size={13}/>
                <span>{projectObj?.name || '项目'}</span>
              </span>
            )}

            <label
              className={`pill-select engine-picker engine-pill-${engine}`}
              title="切换 AI 编程引擎（自动跨引擎同步上下文记忆）"
            >
              <Sparkles size={14} className={engine === 'antigravity' ? 'tone-purple' : engine === 'codex' ? 'tone-green' : 'tone-blue'}/>
              <select
                aria-label="AI 引擎"
                value={engine}
                onChange={e => onEngineChange(e.target.value)}
                disabled={active || sending}
              >
                {engineOptions.map(ag => (
                  <option key={ag.id} value={ag.id}>
                    {ag.id === 'codex' ? 'Codex' : ag.id === 'antigravity' ? 'Antigravity' : (ag.name || ag.id)}
                  </option>
                ))}
              </select>
              <ChevronDown size={12}/>
            </label>

            {dualTarget && (
              <label
                className={`pill-select target-picker ${cloud ? 'target-pill-cloud' : 'target-pill-local'}`}
                title={`切换代码运行位置（当前：${cloud ? '云端环境 (远程沙箱)' : '本地工作区 (本机)'}）`}
              >
                {cloud ? <Cloud size={13} className="tone-blue"/> : <Laptop size={13}/>}
                <select
                  aria-label="运行位置"
                  value={executionTarget}
                  disabled={active || sending}
                  onChange={e => handleTargetChange(e.target.value)}
                >
                  <option value="local">本地运行</option>
                  <option value="cloud">云端运行</option>
                </select>
                <ChevronDown size={12}/>
              </label>
            )}

            {cloud && (
              <div className="cloud-mode-toolbar-group">
                {engine === 'codex' && (
                  <div className="cloud-env-pill" title="Codex 云端任务环境 ID（绑定代码仓库的远端沙箱）">
                    <Cloud size={13} className="cloud-pill-icon tone-blue"/>
                    <input
                      className="cloud-env-input"
                      aria-label="Codex 云端环境 ID"
                      placeholder="云端环境 ID (如 env_...)"
                      value={cloudEnvironment}
                      disabled={active || sending}
                      onChange={e => setCloudEnvironment(e.target.value)}
                      list="recent-cloud-envs-list"
                    />
                    {recentEnvs.length > 0 && (
                      <datalist id="recent-cloud-envs-list">
                        {recentEnvs.map(env => (
                          <option key={env} value={env}/>
                        ))}
                      </datalist>
                    )}
                  </div>
                )}

                {agentSpec?.cloud_url ? (
                  <a
                    href={agentSpec.cloud_url}
                    target="_blank"
                    rel="noreferrer"
                    className="cloud-toolbar-link-btn"
                    title="在浏览器中打开云端服务商页面"
                  >
                    <ExternalLink size={12}/>
                    <span>打开云端</span>
                  </a>
                ) : engine === 'codex' ? (
                  <a
                    href="https://chatgpt.com/codex"
                    target="_blank"
                    rel="noreferrer"
                    className="cloud-toolbar-link-btn"
                    title="打开官方 Codex 云端工作台"
                  >
                    <ExternalLink size={12}/>
                    <span>Codex 控制台</span>
                  </a>
                ) : null}

                <span className="cloud-mode-badge" title="云端模式下任务在远端沙箱独立运行，模型与权限由云端环境决定，本地未提交内容不会自动上传">
                  <Info size={11}/>
                  <span>云端沙箱隔离</span>
                </span>
              </div>
            )}
            {!cloud && <>
            <label className="pill-select model-picker" title={currentModel?.description || '选择模型'}>
              <Brain size={14}/>
              <select
                aria-label="选择模型"
                value={currentModel?.id || ''}
                onChange={e => onModelChange(e.target.value)}
                disabled={active || sending}
              >
                {models.map(m => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </select>
              <ChevronDown size={12}/>
            </label>

            <label className="pill-select effort-picker" title={effortMap[effort]?.description || '调整思考与推理强度'}>
              <SlidersHorizontal size={14}/>
              <select
                aria-label="思考强度"
                value={efforts.includes(effort) ? effort : (currentModel?.defaultEffort || efforts[0])}
                onChange={e => onEffortChange(e.target.value)}
                disabled={active || sending}
              >
                {efforts.map(ef => (
                  <option key={ef} value={ef}>{effortMap[ef]?.label || ef}</option>
                ))}
              </select>
              <ChevronDown size={12}/>
            </label>

            <label className={`pill-select mode-picker mode-badge-${mode}`} title={modes.find(m => m.id === mode)?.description || '执行与审批策略'}>
              <ShieldCheck size={14}/>
              <select
                aria-label="执行模式"
                value={mode}
                onChange={e => onModeChange(e.target.value)}
                disabled={active || sending}
              >
                {modes.map(m => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </select>
              <ChevronDown size={12}/>
            </label>

            </>}
            {!cloud && engine === 'codex' && currentModel?.supportsFast && (
              <button
                type="button"
                className={`pill-toggle ${fast ? 'active' : ''}`}
                onClick={() => onFastChange(!fast)}
                disabled={active || sending}
                title="Priority 极速通道（2x 输出速度，消耗更高额度）"
                aria-pressed={fast}
              >
                <Zap size={13}/>
                <span>极速</span>
              </button>
            )}
          </div>

          <div className="composer-actions">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              multiple
              hidden
              onChange={e => {
                addFiles(e.target.files);
                e.target.value = '';
              }}
            />
            <button
              type="button"
              className="tool-icon-btn"
              title="引用项目文件 (@)"
              aria-label="引用项目文件"
              onClick={triggerAtButton}
              disabled={active || sending}
            >
              <AtSign size={16}/>
            </button>
            <button
              type="button"
              className="tool-icon-btn"
              title="上传或粘贴截图（最多 4 张）"
              aria-label="添加截图"
              onClick={() => fileInputRef.current?.click()}
              disabled={active || sending || images.length >= 4}
            >
              <ImagePlus size={16}/>
            </button>

            {active ? (
              <button
                type="button"
                className="send-button stop-button"
                aria-label="停止任务"
                title="停止当前任务"
                onClick={onStop}
              >
                <Square size={13} fill="currentColor"/>
              </button>
            ) : (
              <button
                type="submit"
                className="send-button"
                aria-label="发送任务"
                title="发送任务 · ⌘/Ctrl + Enter"
                disabled={!canSend}
              >
                <ArrowUp size={18}/>
              </button>
            )}
          </div>
        </div>

        {writerLocked && !externalActive && (
          <div className="engine-warning lock-warning">
            该会话正被本机其他客户端（如飞书桥接）持有写入锁；释放后可直接继续，或点击左上角“新建任务”。
          </div>
        )}

        {engine === 'antigravity' && engineState?.status === 'blocked' && (
          <button type="button" className="engine-warning" onClick={onOpenSettings}>
            Antigravity 当前账号地区资格受限 · 点击查看诊断详情
          </button>
        )}
      </form>

      <div className="composer-footnote">
        <span>任务在 Mac mini 独立进程运行，锁屏或关闭网页不会中断</span>
        <span className="desktop-only">
          · 支持粘贴截图、<kbd>@</kbd> 引用文件、<kbd>⌘ Enter</kbd> 发送
        </span>
      </div>
    </div>
  );
}
