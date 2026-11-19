import {useCallback, useEffect, useRef, useState} from 'react';
import {
  ArrowUp, Folder, Square, ChevronDown, Brain, SlidersHorizontal,
  ShieldCheck, Zap, ImagePlus, AtSign, X, FileCode, Sparkles,
  Cloud, Laptop, ExternalLink, Info, Pause, Play, Pencil, Trash2, ListOrdered, Mic, MicOff, Minimize2,
} from 'lucide-react';
import {api, post, compressImageFile, previewAutoSelect, getProjectCommands} from './api';

export default function Composer({
  sessionId = 'new', initialExecutionTarget = 'local', initialCloudEnvironment = '',
  initialPrompt = '', initialImages = [], onSend,
  prompt: propPrompt, setPrompt: propSetPrompt, images: propImages, setImages: propSetImages, submit,
  active, externalActive, writerLocked, sending, onStop,
  error, onError, projects, projectId, onProjectChange, sessionLocked, sessionCwd,
  engine, onEngineChange, engineState, catalog,
  model, onModelChange, effort, onEffortChange, mode, onModeChange, fast, onFastChange,
  orchestration = 'off', onOrchestrationChange,
  tokensUsed = 0, onCompact,
  onOpenSettings, externalRef, quotedRef, onClearQuote, lastPrompt,
  queue = [], queuePaused = false, supportsLiveSteer = false,
  onEnqueue, onUpdateQueue, onDeleteQueue, onToggleQueuePause,
  sendStatus = null, onRetryOutbox,
}) {
  const [textAttachments, setTextAttachments] = useState([]);
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
  const draftAttachKey = `walking-workstation:draft-att:${sessionId || 'new'}`;
  const [editingQueueId, setEditingQueueId] = useState(null);
  const [editingQueueText, setEditingQueueText] = useState('');
  const [listening, setListening] = useState(false);
  const speechRef = useRef(null);

  function toggleDictation() {
    if (listening && speechRef.current) {
      speechRef.current.stop();
      setListening(false);
      return;
    }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      onError?.('当前浏览器不支持网页语音识别，请使用手机键盘语音输入');
      return;
    }
    try {
      const rec = new SR();
      rec.lang = 'zh-CN';
      rec.interimResults = false;
      rec.continuous = false;
      rec.onresult = ev => {
        const transcript = Array.from(ev.results || []).map(r => r[0]?.transcript || '').join('').trim();
        if (transcript) {
          setPrompt(prev => (prev ? `${prev.trimEnd()} ${transcript}` : transcript));
        }
      };
      rec.onerror = () => setListening(false);
      rec.onend = () => setListening(false);
      speechRef.current = rec;
      setListening(true);
      rec.start();
    } catch {
      setListening(false);
    }
  }

  // Isolate draft text and image state locally if not controlled by parent
  const [internalPrompt, setInternalPrompt] = useState(() => {
    try {
      const raw = localStorage.getItem(draftStorageKey) || sessionStorage.getItem(draftStorageKey);
      if (raw) {
        try {
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === 'object' && parsed.text !== undefined) {
            // Expire after 7 days (7 * 86400 * 1000 ms)
            if (Date.now() - (parsed.time || 0) < 7 * 86400 * 1000) {
              return parsed.text;
            } else {
              localStorage.removeItem(draftStorageKey);
              sessionStorage.removeItem(draftStorageKey);
              return initialPrompt;
            }
          }
        } catch {
          return raw;
        }
      }
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
      const raw = localStorage.getItem(draftStorageKey) || '';
      let text = raw;
      if (raw) {
        try {
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === 'object' && parsed.text !== undefined) {
            if (Date.now() - (parsed.time || 0) < 7 * 86400 * 1000) {
              text = parsed.text;
            } else {
              text = '';
              localStorage.removeItem(draftStorageKey);
            }
          }
        } catch {}
      }
      setInternalPrompt(text || initialPrompt);
      const savedAtt = localStorage.getItem(draftAttachKey);
      setTextAttachments(savedAtt ? JSON.parse(savedAtt) : []);
    } catch {}
  }, [draftStorageKey, draftAttachKey]);

  useEffect(() => {
    if (propPrompt !== undefined) return;
    try {
      if (internalPrompt) {
        localStorage.setItem(draftStorageKey, JSON.stringify({text: internalPrompt, time: Date.now()}));
      } else {
        localStorage.removeItem(draftStorageKey);
      }
    } catch {}
  }, [internalPrompt, draftStorageKey, propPrompt]);

  useEffect(() => {
    try {
      if (textAttachments.length > 0) {
        localStorage.setItem(draftAttachKey, JSON.stringify(textAttachments));
      } else {
        localStorage.removeItem(draftAttachKey);
      }
    } catch {}
  }, [textAttachments, draftAttachKey]);

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
  const isAutoEngine = engine === 'auto';
  const isAutoMode = !cloud && (isAutoEngine || model === 'auto' || effort === 'auto');
  const models = isAutoEngine ? [] : (catalog?.[engine]?.models || []);
  const currentModel = model === 'auto'
    ? null
    : (models.find(m => m.id === model) || models.find(m => m.isDefault) || models[0]);
  const efforts = (isAutoEngine || model === 'auto')
    ? ['low', 'medium', 'high', 'xhigh']
    : (currentModel?.efforts || ['low', 'medium', 'high']);
  const effortMap = catalog?.efforts || {};
  const modes = (!isAutoEngine && catalog?.modes?.[engine]) || [
    {id: 'standard', label: '标准审批'},
    {id: 'full', label: '完全访问'},
    {id: 'auto', label: '自动执行'},
    {id: 'plan', label: '只读规划'},
  ];

  const [autoPreview, setAutoPreview] = useState(null);
  const quotasPrimedRef = useRef(false);

  useEffect(() => {
    if (!isAutoMode) {
      setAutoPreview(null);
      return;
    }
    let cancelled = false;
    const runPreview = async () => {
      if (!quotasPrimedRef.current) {
        quotasPrimedRef.current = true;
        try { await api('quotas'); } catch {}
      }
      try {
        const res = await previewAutoSelect({
          prompt: prompt || '',
          mode,
          has_images: images.length > 0,
          engine: engine || 'auto',
          model: model || 'auto',
          effort: effort || 'auto',
          fast: Boolean(fast),
        });
        if (!cancelled && res && res.model) {
          setAutoPreview(res);
        }
      } catch {
        if (!cancelled) setAutoPreview(null);
      }
    };
    const timer = setTimeout(runPreview, prompt ? 260 : 40);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [isAutoMode, prompt, mode, images.length, engine, model, effort, fast]);

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

  const [slashQuery, setSlashQuery] = useState(null);
  const [slashCommands, setSlashCommands] = useState([]);
  const [slashIndex, setSlashIndex] = useState(0);
  const slashLoadedProjRef = useRef('');

  useEffect(() => {
    if (slashQuery === null || !projectId || slashLoadedProjRef.current === projectId) return;
    let cancelled = false;
    getProjectCommands(projectId)
      .then(res => {
        if (!cancelled && Array.isArray(res?.commands)) {
          slashLoadedProjRef.current = projectId;
          setSlashCommands(res.commands);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [slashQuery, projectId]);

  const filteredSlash = slashQuery === null
    ? []
    : slashCommands.filter(c => c.name.toLowerCase().includes(slashQuery.toLowerCase())).slice(0, 8);

  function detectSlash(value, cursor) {
    if (cursor == null || !value || !value.startsWith('/')) {
      setSlashQuery(prev => (prev !== null ? null : prev));
      return;
    }
    const before = value.slice(0, cursor);
    const match = before.match(/^\/([A-Za-z0-9_-]{0,40})$/);
    if (!match) {
      setSlashQuery(prev => (prev !== null ? null : prev));
      return;
    }
    setSlashQuery(match[1]);
    setSlashIndex(0);
  }

  function applySlashCommand(cmd) {
    if (!cmd) return;
    setSlashQuery(null);
    if (cmd.action === 'mode') {
      onModeChange?.(cmd.value);
      setPrompt('');
    } else if (cmd.action === 'compact') {
      setPrompt('');
      onCompact?.();
    } else {
      setPrompt(cmd.value || `/${cmd.name} `);
    }
    requestAnimationFrame(() => {
      const el = localRef.current;
      if (el) {
        el.focus();
        adjustHeight(el);
      }
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
    } else {
      const pastedText = e.clipboardData?.getData('text/plain') || '';
      if (pastedText.length >= 32768 && !e.shiftKey) {
        e.preventDefault();
        const kb = (pastedText.length / 1024).toFixed(1);
        setTextAttachments(prev => [...prev, {name: `paste-${prev.length + 1}.txt`, sizeLabel: `${kb} KB`, text: pastedText}].slice(0, 5));
        return;
      }
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
    detectSlash(val, e.target.selectionStart);
    adjustHeight(e.target);
  }

  function handleChange(e) {
    const val = e.target.value;
    setPrompt(val);
    if (!isComposingRef.current) {
      detectMention(val, e.target.selectionStart);
      detectSlash(val, e.target.selectionStart);
    }
    adjustHeight(e.target);
  }

  function handleKeyDown(e) {
    if (e.nativeEvent.isComposing || isComposingRef.current || e.keyCode === 229) {
      return;
    }
    if (slashQuery !== null && filteredSlash.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSlashIndex(i => (i + 1) % filteredSlash.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSlashIndex(i => (i - 1 + filteredSlash.length) % filteredSlash.length);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        applySlashCommand(filteredSlash[slashIndex] || filteredSlash[0]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setSlashQuery(null);
        return;
      }
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
    if (e.key === 'ArrowUp' && !prompt && lastPrompt && !mention) {
      e.preventDefault();
      setPrompt(lastPrompt);
      requestAnimationFrame(() => adjustHeight(localRef.current));
      return;
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
    if ((!prompt.trim() && !images.length && !textAttachments.length) || busy) return;
    const parts = [];
    if (quotedRef) {
      parts.push(`> 引用前序回复：\n> ${quotedRef.replace(/\n/g, '\n> ')}`);
    }
    if (prompt.trim()) {
      parts.push(prompt.trim());
    }
    for (const att of textAttachments) {
      parts.push(`[附加长文本片段 ${att.name} (${att.sizeLabel})]\n\`\`\`text\n${att.text}\n\`\`\``);
    }
    const finalPrompt = parts.join('\n\n');
    const trimmed = finalPrompt.trim();
    if (sessionId && sessionId !== 'new' && /^\/review(?:\s+([\s\S]*))?$/i.test(trimmed)) {
      if (images.length) {
        onError?.('/review 命令暂不支持同时附加图片');
        return;
      }
      const focus = trimmed.replace(/^\/review\s*/i, '').trim();
      try {
        await post(`sessions/${sessionId}/review`, {engine, model, focus});
        try { sessionStorage.removeItem(draftStorageKey); } catch {}
        setPrompt('');
        setMention(null);
        if (localRef.current) {
          localRef.current.style.height = '56px';
        }
      } catch (err) {
        onError?.(err.message || '启动独立 /review 失败');
      }
      return;
    }
    if (onSend) {
      if (cloud && engine !== 'codex' && !agentSpec?.builtin && agentSpec?.cloud_url) {
        window.open(agentSpec.cloud_url, '_blank', 'noopener,noreferrer');
        return;
      }
      if (cloud && !cloudEnvironment.trim()) { onError?.('请填写 Codex 云端环境 ID'); return; }
      const ok = await onSend(finalPrompt, images, {
        execution_target: executionTarget,
        cloud_environment: cloudEnvironment.trim(),
        orchestration,
      });
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
        setTextAttachments([]);
        onClearQuote?.();
        setMention(null);
        if (localRef.current) {
          localRef.current.style.height = '56px';
        }
      }
    }
  }

  const projectObj = projects.find(p => p.id === projectId);
  const busy = active || Boolean(externalActive) || sending;
  const canSend = (prompt.trim().length > 0 || images.length > 0 || textAttachments.length > 0) && !busy;
  const workspaceProjects = projects.filter(p => p.is_workspace);
  const repoProjects = projects.filter(p => !p.is_workspace);

  return (
    <div className="composer-wrap">
      {slashQuery !== null && filteredSlash.length > 0 && (
        <div className="mention-popover slash-popover" role="listbox" aria-label="快捷斜杠命令">
          <div className="mention-header">快捷命令与项目工作流 (/)</div>
          {filteredSlash.map((cmd, idx) => (
            <button
              type="button"
              key={`${cmd.source}-${cmd.name}`}
              className={`mention-item ${idx === slashIndex ? 'active' : ''}`}
              onMouseDown={e => {e.preventDefault(); applySlashCommand(cmd);}}
            >
              <span className="slash-cmd-name">/{cmd.name}</span>
              <span className="slash-cmd-desc">{cmd.description}</span>
              {cmd.source !== 'builtin' && <span className="slash-cmd-tag">{cmd.source}</span>}
            </button>
          ))}
        </div>
      )}

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

      {(queue.length > 0 || queuePaused) && (
        <div className="queue-panel">
          <div className="queue-panel-head">
            <span className="queue-panel-title">
              <ListOrdered size={13}/>
              <span>待执行队列 ({queue.length}){queuePaused ? ' · 已暂停' : ''}</span>
            </span>
            {onToggleQueuePause && (
              <button type="button" className="queue-action-btn" onClick={onToggleQueuePause}>
                {queuePaused ? <><Play size={11}/> 恢复队列</> : <><Pause size={11}/> 暂停队列</>}
              </button>
            )}
          </div>
          {queue.map((item, idx) => (
            <div key={item.id} className="queue-item">
              {editingQueueId === item.id ? (
                <div className="queue-edit-row">
                  <input
                    className="queue-edit-input"
                    value={editingQueueText}
                    onChange={e => setEditingQueueText(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && editingQueueText.trim()) {
                        onUpdateQueue?.(item.id, editingQueueText.trim());
                        setEditingQueueId(null);
                      } else if (e.key === 'Escape') {
                        setEditingQueueId(null);
                      }
                    }}
                  />
                  <button
                    type="button"
                    className="queue-action-btn"
                    onClick={() => {
                      if (editingQueueText.trim()) onUpdateQueue?.(item.id, editingQueueText.trim());
                      setEditingQueueId(null);
                    }}
                  >保存</button>
                  <button type="button" className="queue-action-btn" onClick={() => setEditingQueueId(null)}>取消</button>
                </div>
              ) : (
                <>
                  <span className="queue-item-idx">#{idx + 1}{item.steer ? ' 优先' : ''}</span>
                  <span className="queue-item-text" title={item.prompt}>{item.prompt}</span>
                  <div className="queue-item-ops">
                    {onUpdateQueue && (
                      <button
                        type="button"
                        className="queue-icon-btn"
                        aria-label="编辑排队消息"
                        onClick={() => {
                          setEditingQueueId(item.id);
                          setEditingQueueText(item.prompt);
                        }}
                      ><Pencil size={12}/></button>
                    )}
                    {onDeleteQueue && (
                      <button
                        type="button"
                        className="queue-icon-btn"
                        aria-label="删除排队消息"
                        onClick={() => onDeleteQueue(item.id)}
                      ><Trash2 size={12}/></button>
                    )}
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      )}

      <form className={`composer ${mode === 'plan' ? 'mode-plan' : mode === 'auto' ? 'mode-auto' : ''}`} onSubmit={handleSubmit}>
        {error && <div className="error composer-error" role="alert">{error}</div>}
        {sendStatus && (
          <div className={`send-status-pill status-${sendStatus.state}`}>
            <span>
              {sendStatus.state === 'sending' && '正在发送至工作站…'}
              {sendStatus.state === 'acked' && '服务器已接收'}
              {sendStatus.state === 'failed' && `发送失败：${sendStatus.error || '网络异常'}`}
            </span>
            {sendStatus.state === 'failed' && onRetryOutbox && (
              <button type="button" className="queue-action-btn" onClick={onRetryOutbox}>点击重试</button>
            )}
          </div>
        )}

        {(quotedRef || textAttachments.length > 0 || images.length > 0) && (
          <div className="composer-attachments">
            {quotedRef && (
              <div className="attachment-chip quote-chip" title={quotedRef}>
                <span>引用：{quotedRef.slice(0, 36)}{quotedRef.length > 36 ? '…' : ''}</span>
                <button type="button" aria-label="移除引用" onClick={onClearQuote}>
                  <X size={12}/>
                </button>
              </div>
            )}
            {textAttachments.map((att, idx) => (
              <div key={`txt-${idx}`} className="attachment-chip">
                <FileCode size={13}/>
                <span>{att.name} ({att.sizeLabel})</span>
                <button
                  type="button"
                  aria-label="移除长文本附件"
                  onClick={() => setTextAttachments(prev => prev.filter((_, i) => i !== idx))}
                >
                  <X size={12}/>
                </button>
              </div>
            ))}
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

        {isAutoMode && autoPreview && (
          <div className={`auto-route-preview budget-${autoPreview.budget_state || 'normal'}`} title={autoPreview.reason}>
            <Sparkles size={12} className="auto-route-icon"/>
            <span className="auto-route-main">
              拟选：{autoPreview.engine === 'antigravity' ? 'Antigravity' : autoPreview.engine === 'codex' ? 'Codex' : autoPreview.engine} · {autoPreview.model_name} · {autoPreview.effort_label}
            </span>
            {autoPreview.quota_summary && (
              <span className="auto-route-quota">
                {autoPreview.budget_state === 'expiring_abundant'
                  ? `⚡ ${autoPreview.quota_summary}·即将刷新优先用`
                  : autoPreview.budget_state === 'scarce'
                    ? `🛡 ${autoPreview.quota_summary}·动态节流`
                    : autoPreview.quota_summary}
              </span>
            )}
          </div>
        )}

        {sessionId && sessionId !== 'new' && tokensUsed >= 60000 && (
          <div className={`context-meter-pill ${tokensUsed >= 140000 ? 'is-high' : ''}`}>
            <span>上下文约 {Math.round(tokensUsed / 1000)}k tokens{tokensUsed >= 140000 ? '（建议压缩换窗）' : ''}</span>
            {onCompact && !active && (
              <button type="button" className="queue-action-btn" onClick={onCompact} title="主动归档旧窗口并压缩上下文 (/compact)">
                <Minimize2 size={11}/>
                <span>压缩</span>
              </button>
            )}
          </div>
        )}

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
              title="切换 AI 编程引擎（支持「智能调度」按任务难度与7天刷新额度跨引擎自动选优）"
            >
              <Sparkles size={14} className={engine === 'auto' ? 'tone-amber' : engine === 'antigravity' ? 'tone-purple' : engine === 'codex' ? 'tone-green' : 'tone-blue'}/>
              <select
                aria-label="AI 引擎"
                value={engine}
                onChange={e => onEngineChange(e.target.value)}
                disabled={active || sending}
              >
                <option value="auto">✨ 智能调度 (Auto)</option>
                {engineOptions.map(ag => (
                  <option key={ag.id} value={ag.id}>
                    {ag.id === 'codex' ? 'Codex' : ag.id === 'antigravity' ? 'Antigravity' : (ag.name || ag.id)}
                  </option>
                ))}
              </select>
              <ChevronDown size={12}/>
            </label>

            {!cloud && onOrchestrationChange && (
              <label
                className={`pill-select orch-picker ${orchestration !== 'off' ? 'orch-active' : ''}`}
                title="多模型协同编排：自动拆解任务并调度规划/编码/异构交叉审查智能体"
              >
                <span className="orch-dot" aria-hidden="true"/>
                <select
                  aria-label="多模型协同"
                  value={orchestration}
                  onChange={e => onOrchestrationChange(e.target.value)}
                  disabled={active || sending}
                >
                  <option value="off">单 Agent</option>
                  <option value="relay">串行协同</option>
                  <option value="auto">自动协同</option>
                </select>
                <ChevronDown size={12}/>
              </label>
            )}

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
            <label className="pill-select model-picker" title={model === 'auto' ? '根据任务复杂度与7天刷新额度自动选模型' : (currentModel?.description || '选择模型')}>
              <Brain size={14}/>
              <select
                aria-label="选择模型"
                value={isAutoEngine || model === 'auto' ? 'auto' : (currentModel?.id || '')}
                onChange={e => onModelChange(e.target.value)}
                disabled={active || sending}
              >
                <option value="auto">✨ 自动选模型 (Auto)</option>
                {models.map(m => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </select>
              <ChevronDown size={12}/>
            </label>

            <label className="pill-select effort-picker" title={effort === 'auto' ? '根据任务难度与剩余额度动态调节思考深度' : (effortMap[effort]?.description || '调整思考与推理强度')}>
              <SlidersHorizontal size={14}/>
              <select
                aria-label="思考强度"
                value={effort === 'auto' ? 'auto' : (efforts.includes(effort) ? effort : (currentModel?.defaultEffort || efforts[0]))}
                onChange={e => onEffortChange(e.target.value)}
                disabled={active || sending}
              >
                <option value="auto">✨ 自动调强度 (Auto)</option>
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
            <button
              type="button"
              className={`tool-icon-btn ${listening ? 'is-listening' : ''}`}
              title={listening ? '停止语音口述' : '语音口述输入'}
              aria-label="语音输入"
              onClick={toggleDictation}
              disabled={sending}
            >
              {listening ? <MicOff size={16}/> : <Mic size={16}/>}
            </button>

            {(active || externalActive) ? (
              <div className="active-send-controls">
                {active && onEnqueue && prompt.trim() && (
                  <>
                    <button
                      type="button"
                      className="queue-send-btn"
                      title="加入排队，当前轮次完成后自动执行"
                      onClick={() => {
                        onEnqueue(prompt.trim(), false);
                        setPrompt('');
                      }}
                    >
                      下一轮执行
                    </button>
                    <button
                      type="button"
                      className="queue-send-btn steer-btn"
                      disabled={!supportsLiveSteer}
                      title={supportsLiveSteer ? '立即插话注入当前任务' : '当前引擎不支持运行中实时注入（仅支持「下一轮执行」排队）'}
                      onClick={() => {
                        if (!supportsLiveSteer) return;
                        onEnqueue(prompt.trim(), true);
                        setPrompt('');
                      }}
                    >
                      立即插话
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="send-button stop-button"
                  aria-label={externalActive && !active ? '停止外部任务并解锁' : '停止任务'}
                  title={externalActive && !active ? '停止本机运行中任务并解锁输入' : '停止当前任务'}
                  onClick={onStop}
                >
                  <Square size={13} fill="currentColor"/>
                </button>
              </div>
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
