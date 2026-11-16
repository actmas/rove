import {createDisplayBuilder} from './displayContent';
import {createContext, useContext, memo, useEffect, useLayoutEffect, useRef, useState, useMemo, useCallback} from 'react';
import {
  MessageCircle, Code2, SquareTerminal, Check, ShieldCheck, ShieldAlert, X, Copy,
  Brain, Sparkles, CheckCircle2, Circle, Loader2, ArrowDown, FileDiff,
  Globe, Wrench, Eye, Zap, GitBranch, Bug, Layers, User,
  Bot, AlertTriangle, Info, FileText, Plug, ListChecks, Gauge, Hourglass, RefreshCw,
  ChevronDown, ChevronUp, ArrowLeftRight,
} from 'lucide-react';
import {imageSource} from './images';
import {createEventParser} from './eventParser';
export {parseEvents} from './eventParser';
import Composer from './Composer';
import SessionQuota from './SessionQuota';
import CloudTaskCard from './CloudTaskCard';
import RichUiBlock from './RichUiBlock';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  api, post, activeStates, uploadUrl, effortBadge, modeBadge, base,
} from './api';

function SafeMarkdown({children, components, onSend}) {
  const mergedComponents = useMemo(() => {
    const baseCode = components?.code;
    return {
      ...components,
      code({className, children: codeChildren, ...props}) {
        const match = /language-rove-ui\b/.test(className || '');
        if (match) {
          return <RichUiBlock source={String(codeChildren || '')} onSend={onSend}/>;
        }
        if (baseCode) {
          return baseCode({className, children: codeChildren, ...props});
        }
        return <code className={className} {...props}>{codeChildren}</code>;
      },
    };
  }, [components, onSend]);

  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={mergedComponents}>
      {children}
    </ReactMarkdown>
  );
}

export function buildMarkdownTranscript(session, project, parsedItems) {
  const lines = [
    `# ${session?.title || '会话记录'}`,
    '',
    `- **项目**: ${project?.name || ''} (\`${project?.path || ''}\`)`,
    `- **引擎**: ${session?.engine === 'antigravity' ? 'Antigravity' : 'Codex'}`,
    session?.model ? `- **模型**: \`${session.model}\`` : null,
    session?.effort ? `- **思考强度**: \`${session.effort}\`` : null,
    session?.native_id ? `- **原生会话 ID**: \`${session.native_id}\`` : null,
    '',
    '---',
    '',
  ].filter(v => v !== null);

  for (const m of parsedItems) {
    if (m.type === 'user') {
      const meta = [m.model, m.effort, m.mode && m.mode !== 'standard' ? m.mode : null].filter(Boolean).join(' · ');
      lines.push(`## 👤 用户${meta ? ` (${meta})` : ''}`, '', m.text, '');
    } else if (m.type === 'reasoning' && m.text) {
      lines.push('### 🧠 思考摘要', '', `> ${m.text.replace(/\n/g, '\n> ')}`, '');
    } else if (m.type === 'plan' && m.steps?.length) {
      lines.push('### 📋 执行计划', '');
      for (const s of m.steps) {
        const mark = s.status === 'completed' ? '[x]' : s.status === 'inProgress' ? '[-]' : '[ ]';
        lines.push(`- ${mark} ${s.step}`);
      }
      lines.push('');
    } else if (m.type === 'command') {
      lines.push(`### 💻 命令执行: \`${m.title || 'shell'}\``, '');
      if (m.text) lines.push('```text', m.text.trimEnd(), '```', '');
    } else if (m.type === 'change' && m.changes?.length) {
      lines.push('### 📝 文件变更', '');
      for (const c of m.changes) {
        lines.push(`- \`${c.path}\` (${c.kind?.type || 'update'})`);
      }
      lines.push('');
    } else if (m.type === 'agent') {
      lines.push(`## 🤖 ${session?.engine === 'antigravity' ? 'Antigravity' : 'Codex'}`, '', m.text, '');
    } else if (m.type === 'error') {
      lines.push(`> ⚠️ **错误**: ${m.text}`, '');
    }
  }
  return lines.join('\n');
}

function Approval({approval, onDone, onError}) {
  const [busy, setBusy] = useState(false);
  const [answers, setAnswers] = useState({});
  if (approval.method === 'item/tool/requestUserInput') {
    const hasSecret = approval.payload.questions.some(q => q.isSecret);
    async function answer(e) {
      e.preventDefault();
      setBusy(true);
      try {
        if (hasSecret) {
          await post(`approvals/${approval.id}/secret`, {
            answers: Object.fromEntries(
              Object.entries(answers).map(([k, v]) => [k, Array.isArray(v) ? v.join(',') : String(v || '')])
            ),
          });
        } else {
          await post(`approvals/${approval.id}/answers`, {
            answers: Object.fromEntries(Object.entries(answers).map(([k, v]) => [k, Array.isArray(v) ? v : [v]])),
          });
        }
        onDone();
      } catch (err) {
        onError(err.message);
      } finally {
        setBusy(false);
      }
    }
    return (
      <form className="approval approval-question" onSubmit={answer}>
        <h3>
          <span className="card-icon tone-blue"><MessageCircle size={14}/></span>
          {hasSecret ? '零留痕保密代输入（仅送达宿主内存，不落库）' : '需要你的回答'}
        </h3>
        {approval.payload.questions.map(q => (
          <label className="question" key={q.id}>
            <span>{q.question}</span>
            {q.isSecret ? (
              <input
                type="password"
                autoComplete="off"
                required
                placeholder="输入保密凭据（提交后立即从内存擦除）…"
                value={answers[q.id] || ''}
                onChange={e => setAnswers(v => ({...v, [q.id]: e.target.value}))}
              />
            ) : q.options?.length ? (
              <select
                multiple={!!q.isMultiSelect}
                value={answers[q.id] || (q.isMultiSelect ? [] : '')}
                onChange={e => {
                  const value = q.isMultiSelect ? Array.from(e.target.selectedOptions, o => o.value) : e.target.value;
                  setAnswers(v => ({...v, [q.id]: value}));
                }}
                required
              >
                <option value="" disabled>请选择一项…</option>
                {q.options.map(o => (
                  <option key={o.label} value={o.label}>{o.label}{o.description ? ` · ${o.description}` : ''}</option>
                ))}
              </select>
            ) : (
              <input
                required
                placeholder="输入回答…"
                value={answers[q.id] || ''}
                onChange={e => setAnswers(v => ({...v, [q.id]: e.target.value}))}
              />
            )}
          </label>
        ))}
        <div className="button-row">
          <button className="primary" disabled={busy}>
            {hasSecret ? '安全提交' : '提交回答'}
          </button>
        </div>
      </form>
    );
  }

  async function decide(decision) {
    setBusy(true);
    try {
      await post(`approvals/${approval.id}`, {decision});
      onDone();
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }
  const isPermission = approval.method === 'item/permissions/requestApproval';
  let permissionDesc = '';
  if (isPermission) {
    const net = approval.payload.permissions?.network?.enabled;
    const fs = approval.payload.permissions?.fileSystem?.entries || [];
    const parts = [];
    if (net) parts.push('🌐 允许网络访问与远端连接（如 SSH/Curl/Git）');
    if (fs.length > 0) parts.push(`📁 允许访问外部目录 (${fs.length} 项)`);
    permissionDesc = parts.join('\n') || '申请扩展运行权限';
  }

  return (
    <section className="approval" aria-label="操作审批">
      <h3>
        <span className={`card-icon ${isPermission ? 'tone-purple' : 'tone-orange'}`}>
          {isPermission ? <ShieldAlert size={14}/> : <ShieldCheck size={14}/>}
        </span>
        {isPermission ? '需要你批准权限申请' : '需要你批准本次操作'}
      </h3>
      <pre>{approval.payload.command || approval.payload.reason || permissionDesc || '允许此回合执行文件修改？'}</pre>
      {isPermission && permissionDesc && approval.payload.reason && (
        <small style={{display: 'block', marginBottom: '8px', color: 'var(--text-secondary)'}}>{permissionDesc}</small>
      )}
      {approval.payload.cwd && <small>工作目录：{approval.payload.cwd}</small>}
      <div className="button-row">
        <button className="primary" disabled={busy} onClick={() => decide('accept')}>
          <Check size={15}/>{isPermission ? '批准授权' : '允许本次'}
        </button>
        <button disabled={busy} onClick={() => decide('decline')}>
          <X size={15}/>拒绝
        </button>
      </div>
    </section>
  );
}

function CodeBlock({children}) {
  const [copied, setCopied] = useState(false);
  const rawCode = String(children?.props?.children || '');
  const lineCount = useMemo(() => rawCode.split('\n').length, [rawCode]);
  const isLong = lineCount > 24;
  const [expanded, setExpanded] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(rawCode);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }
  const lang = children?.props?.className?.replace('language-', '') || 'code';
  return (
    <div className={`markdown-code ${isLong && !expanded ? 'is-collapsed' : ''}`}>
      <div className="code-caption">
        <span>{lang}{isLong ? ` · ${lineCount} 行` : ''}</span>
        <div className="code-caption-actions">
          {isLong && (
            <button type="button" onClick={() => setExpanded(v => !v)} aria-label={expanded ? '收起代码' : '展开全部代码'}>
              {expanded ? <ChevronUp size={12}/> : <ChevronDown size={12}/>}
              <span>{expanded ? '收起' : '展开'}</span>
            </button>
          )}
          <button type="button" onClick={copy} aria-label="复制代码">
            {copied ? <Check size={12}/> : <Copy size={12}/>}
            <span>{copied ? '已复制' : '复制'}</span>
          </button>
        </div>
      </div>
      <pre>{children}</pre>
      {isLong && !expanded && (
        <button type="button" className="code-expand-btn" onClick={() => setExpanded(true)}>
          <ChevronDown size={13}/>
          <span>展开全部代码（共 {lineCount} 行）</span>
        </button>
      )}
    </div>
  );
}

const ImageContext = createContext({});

function RenderedImage({src = '', alt = ''}) {
  const {sessionId, onPreview} = useContext(ImageContext);
  const resolved = imageSource(src, base, sessionId);
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [resolved]);
  if (!resolved) return <span>图片地址无效</span>;
  return (
    <span className="markdown-img-wrap">
      {failed ? <span className="local-img-chip">🖼 {alt || '图片'}加载失败 <a href={resolved} target="_blank" rel="noreferrer">打开原图</a></span> : (
        <img src={resolved} alt={alt} loading="lazy" decoding="async"
          role="button" tabIndex={0} title="点击查看原图"
          onClick={() => onPreview?.(resolved)}
          onKeyDown={e => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPreview?.(resolved); }
          }}
          onError={() => setFailed(true)}/>
      )}
    </span>
  );
}

const ImageMessageItem = memo(function ImageMessageItem({item}) {
  return (
    <article className="message image-card" id={`msg-${item.id}`}>
      <div className="speaker">{item.title}</div>
      {item.images.map(src => <RenderedImage key={src} src={src} alt={item.title}/>)}
      {!item.images.length && <span>{item.status === 'inProgress' ? '图片处理中…' : '此记录未提供可读取的图片'}</span>}
    </article>
  );
});

const PRECISE_PATH_RE = /^(?:\/[^\s/][^\s]*|[^\s]+\/$|(?:[A-Za-z0-9_.-]+\/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10}(?::\d+(?::\d+)?)?)$/;
const FALSE_PATH_WORDS = new Set(['and/or', 'text/plain', 'text/html', 'application/json', 'n/a', 'read/write', 'true/false']);

export function isPreciseFilePath(raw) {
  const text = String(raw || '').trim();
  if (!text || text.length > 260 || /\s/.test(text)) return false;
  if (FALSE_PATH_WORDS.has(text.toLowerCase())) return false;
  if (/^https?:\/\//i.test(text)) return false;
  return PRECISE_PATH_RE.test(text);
}

const markdownComponents = {
  pre: CodeBlock,
  a: ({children, ...props}) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
  code: ({inline, className, children, ...props}) => {
    const raw = String(children || '').trim();
    if ((inline || !className) && isPreciseFilePath(raw)) {
      return (
        <code className="rove-file-chip" title={`文件路径: ${raw}`} {...props}>
          {children}
        </code>
      );
    }
    return <code className={className} {...props}>{children}</code>;
  },
  img: RenderedImage,
};

function InlinePatch({diffText}) {
  if (!diffText) return null;
  const lines = diffText.split('\n').slice(0, 180);
  return (
    <pre className="inline-patch">
      {lines.map((line, idx) => {
        const cls = line.startsWith('+') && !line.startsWith('+++')
          ? 'added'
          : line.startsWith('-') && !line.startsWith('---')
            ? 'removed'
            : line.startsWith('@@')
              ? 'hunk'
              : '';
        return <span key={idx} className={cls}>{line}{'\n'}</span>;
      })}
    </pre>
  );
}

const STARTERS = [
  {
    icon: Layers,
    tone: 'indigo',
    title: '架构梳理与代码审查',
    desc: '只读分析模块职责、数据流与潜在改进点',
    mode: 'plan',
    prompt: '请全面梳理当前项目的架构层次、核心模块与关键数据流，指出潜在风险点或最值得优化的地方。',
  },
  {
    icon: Bug,
    tone: 'red',
    title: '定位问题并修复验证',
    desc: '排查报错根因，修改代码并运行自动化测试',
    prompt: '请帮我排查并修复以下问题，完成后运行相关测试确保没有引入回归：\n',
  },
  {
    icon: Sparkles,
    tone: 'purple',
    title: '实现新功能特性',
    desc: '遵循现有代码规范，完成端到端开发与自测',
    prompt: '请遵循项目现有架构与代码风格，帮我实现以下功能并完成验证：\n',
  },
  {
    icon: GitBranch,
    tone: 'green',
    title: '审查 Git 未提交改动',
    desc: '检查当前工作区 diff、边界条件并撰写提交说明',
    mode: 'plan',
    prompt: '请检查当前 Git 工作区的代码改动，审查是否存在边界遗漏或安全隐患，并给出清晰的 Commit Message 建议。',
  },
];

/** iOS-style tinted icon tile; tone maps to a semantic system color. */
function IconTile({icon: Icon, tone, spin = false, size = 14}) {
  return (
    <span className={`card-icon tone-${tone}`} aria-hidden="true">
      <Icon size={size} className={spin ? 'spinning' : undefined}/>
    </span>
  );
}

const TOOL_TONES = {
  search: {tone: 'yellow', icon: Globe, label: '联网检索'},
  read: {tone: 'indigo', icon: FileText, label: '读取文件'},
  mcp: {tone: 'pink', icon: Plug, label: '扩展工具'},
};

const UserMessageItem = memo(function UserMessageItem({
  item: m,
  session,
  catalog,
  onPreviewImg,
  onError,
}) {
  const [copied, setCopied] = useState(false);
  const effectiveEngine = m.engine || session?.engine || 'codex';
  const engineModels = catalog?.[effectiveEngine]?.models || [];
  const modelName = engineModels.find(x => x.id === m.model)?.name || m.model;

  function copyText() {
    navigator.clipboard?.writeText(m.text)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => onError?.('当前浏览器不允许复制'));
  }

  return (
    <article className="message user question-card" id={`msg-${m.id}`}>
      <div className="speaker question-header">
        <IconTile icon={User} tone="blue" size={13}/>
        {(m.execution_target === 'cloud' || modelName || m.effort || (m.mode && m.mode !== 'standard') || m.fast) && (
          <div className="user-meta-chips">
            {m.execution_target === 'cloud' && <span className="meta-chip">云端</span>}
            {modelName && <span className="meta-chip">{modelName}</span>}
            {m.effort && <span className="meta-chip">{effortBadge[m.effort] || m.effort}</span>}
            {m.fast && <span className="meta-chip fast-chip"><Zap size={10}/>极速</span>}
          </div>
        )}
        <button
          type="button"
          className="icon-button copy-question-btn"
          title="复制提问"
          onClick={copyText}
        >
          {copied ? <Check size={12} color="var(--emerald)"/> : <Copy size={12}/>}
        </button>
      </div>

      {m.images?.length > 0 && session && (
        <div className="user-images">
          {m.images.map(img => {
            const src = uploadUrl(session.id, img);
            return (
              <button type="button" key={img} className="user-image-thumb" onClick={() => onPreviewImg(src)}>
                <img src={src} alt="附加截图" loading="lazy"/>
              </button>
            );
          })}
        </div>
      )}
      <div className="question-body">
        <pre className="prose">{m.text}</pre>
      </div>
    </article>
  );
});

const ReasoningMessageItem = memo(function ReasoningMessageItem({item: m, active}) {
  if (m.completed && !m.text) return null;
  const live = !m.completed && active;
  const [isOpen, setIsOpen] = useState(live);
  useEffect(() => {
    setIsOpen(live);
  }, [live]);

  const preview = (m.text || '')
    .split('\n')
    .map(line => line.replace(/[*#_`~>]+/g, '').trim())
    .find(Boolean)
    ?.slice(0, 72) || '正在推演思路…';
  return (
    <article className={`message reasoning-card ${live ? 'is-live' : ''}`} id={`msg-${m.id}`}>
      <details open={isOpen}>
        <summary onClick={e => { e.preventDefault(); setIsOpen(v => !v); }}>
          <IconTile icon={Brain} tone="purple"/>
          <span className="summary-title">{live ? '正在深度思考…' : '思考过程'}</span>
          <span className="summary-preview">{preview}</span>
        </summary>
        <div className="reasoning-content">
          <SafeMarkdown components={markdownComponents}>
            {m.text || '正在组织推理步骤…'}
          </SafeMarkdown>
        </div>
      </details>
    </article>
  );
});

const PlanMessageItem = memo(function PlanMessageItem({item: m}) {
  const steps = m.steps || [];
  const doneCount = steps.filter(s => s.status === 'completed').length;
  const pct = steps.length ? Math.round((doneCount / steps.length) * 100) : 0;
  return (
    <article className={`message plan-card ${steps.length && doneCount === steps.length ? 'is-done' : ''}`} id={`msg-${m.id}`}>
      <div className="plan-header">
        <IconTile icon={ListChecks} tone="teal"/>
        <strong>执行计划</strong>
        <span>{doneCount}/{steps.length} 完成</span>
      </div>
      {steps.length > 0 && (
        <div className="plan-progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <span style={{width: `${pct}%`}}/>
        </div>
      )}
      {m.explanation && <p className="plan-explanation">{m.explanation}</p>}
      <ul className="plan-steps">
        {steps.map((step, idx) => (
          <li key={idx} className={`step-${step.status}`}>
            {step.status === 'completed' ? (
              <CheckCircle2 size={15}/>
            ) : step.status === 'inProgress' ? (
              <Loader2 size={15} className="spinning"/>
            ) : (
              <Circle size={15}/>
            )}
            <span>{step.step}</span>
          </li>
        ))}
      </ul>
    </article>
  );
});

const PlanTextMessageItem = memo(function PlanTextMessageItem({item: m}) {
  return (
    <article className="message plan-card plan-text-card" id={`msg-${m.id}`}>
      <div className="plan-header">
        <IconTile icon={Sparkles} tone="teal"/>
        <strong>规划方案</strong>
      </div>
      <div className="markdown-body">
        <SafeMarkdown components={markdownComponents}>
          {m.text}
        </SafeMarkdown>
      </div>
    </article>
  );
});

const CommandSubItem = memo(function CommandSubItem({item: m, state, duration, isRunning, failed}) {
  const [open, setOpen] = useState(isRunning);
  useEffect(() => {
    setOpen(isRunning);
  }, [isRunning]);

  return (
    <div className={`command-sub-item state-${state}`} id={`msg-${m.id}`}>
      <div className="command-sub-header" onClick={() => setOpen(v => !v)}>
        <span className={`command-sub-bullet ${state}`}>
          {state === 'run' ? <Loader2 size={12} className="spinning"/> : <SquareTerminal size={12}/>}
        </span>
        <code className="cmd-title" title={m.title}>{m.title || '执行命令'}</code>
        <span className={`status-pill ${state}`}>
          {duration && <em>{duration}</em>}
          {failed ? `退出码 ${m.exitCode}` : state === 'run' ? '执行中' : '完成'}
        </span>
      </div>
      {open && (
        <pre className="command-sub-output">{m.text || '（无标准输出）'}</pre>
      )}
    </div>
  );
});

const CommandGroupMessageItem = memo(function CommandGroupMessageItem({group, active}) {
  const commands = group.commands || [];
  const hasRunning = commands.some(c => c.status === 'inProgress' || (c.exitCode == null && c.status !== 'completed' && active));
  const failedCount = commands.filter(c => c.exitCode != null && c.exitCode !== 0).length;

  const totalDurationMs = commands.reduce((acc, c) => acc + (c.durationMs || 0), 0);
  const durationStr = totalDurationMs > 0 ? `${(totalDurationMs / 1000).toFixed(1)}s` : '';

  // 运行中默认展开显示执行进度，任务完成后全部折叠收起
  const [isOpen, setIsOpen] = useState(hasRunning);
  useEffect(() => {
    setIsOpen(hasRunning);
  }, [hasRunning]);

  return (
    <article
      className={`message tool-card command-group-card ${hasRunning ? 'state-run' : failedCount > 0 ? 'state-err' : 'state-ok'}`}
      id={`msg-${group.id}`}
    >
      <details open={isOpen}>
        <summary
          className="command-group-summary"
          onClick={e => {
            e.preventDefault();
            setIsOpen(v => !v);
          }}
        >
          <IconTile
            icon={hasRunning ? Loader2 : SquareTerminal}
            tone={hasRunning ? 'blue' : failedCount > 0 ? 'red' : 'green'}
            spin={hasRunning}
          />
          <div className="command-group-meta">
            <span className="command-group-title">
              {hasRunning ? `正在执行命令 (${commands.length} 条)` : `已执行 ${commands.length} 条命令`}
            </span>
            {!isOpen && (
              <span className="command-group-sub" title={commands.map(c => c.title).filter(Boolean).join(' · ')}>
                {commands.map(c => c.title || 'cmd').join(' · ')}
              </span>
            )}
          </div>
          <span className={`status-pill ${hasRunning ? 'run' : failedCount > 0 ? 'err' : 'ok'}`}>
            {durationStr && <em>{durationStr}</em>}
            {hasRunning ? '执行中' : failedCount > 0 ? `${failedCount} 处失败` : '全部完成'}
          </span>
        </summary>

        <div className="command-group-body">
          {commands.map(cmd => {
            const isCmdRunning = cmd.status === 'inProgress' || (cmd.exitCode == null && cmd.status !== 'completed' && active);
            const cmdFailed = cmd.exitCode != null && cmd.exitCode !== 0;
            const cmdState = cmdFailed ? 'err' : isCmdRunning ? 'run' : 'ok';
            const cmdDuration = cmd.durationMs != null ? `${(cmd.durationMs / 1000).toFixed(cmd.durationMs >= 1000 ? 1 : 2)}s` : '';

            return (
              <CommandSubItem
                key={cmd.id}
                item={cmd}
                state={cmdState}
                duration={cmdDuration}
                isRunning={isCmdRunning}
                failed={cmdFailed}
              />
            );
          })}
        </div>
      </details>
    </article>
  );
});

const CommandMessageItem = memo(function CommandMessageItem({item: m, active}) {
  const duration = m.durationMs != null ? `${(m.durationMs / 1000).toFixed(m.durationMs >= 1000 ? 1 : 2)}s` : '';
  const ok = m.exitCode === 0 || (m.status === 'completed' && m.exitCode == null);
  const failed = m.exitCode != null && m.exitCode !== 0;
  const isRunning = m.status === 'inProgress' || (!m.status && m.exitCode == null && active);
  const state = failed ? 'err' : isRunning ? 'run' : ok ? 'ok' : 'run';

  // 运行中默认展开显示执行结果，任务完成后全部折叠收起，仅显示命令与结果状态
  const [isOpen, setIsOpen] = useState(isRunning);
  useEffect(() => {
    setIsOpen(isRunning);
  }, [isRunning]);

  return (
    <article className={`message tool-card command-card state-${state}`} id={`msg-${m.id}`}>
      <details open={isOpen}>
        <summary
          onClick={e => {
            e.preventDefault();
            setIsOpen(v => !v);
          }}
        >
          <IconTile
            icon={state === 'run' ? Loader2 : SquareTerminal}
            tone={state === 'err' ? 'red' : state === 'ok' ? 'green' : 'blue'}
            spin={state === 'run'}
          />
          <code className="cmd-title" title={m.title}>{m.title || '执行命令'}</code>
          <span className={`status-pill ${state}`}>
            {duration && <em>{duration}</em>}
            {failed ? `退出码 ${m.exitCode}` : state === 'run' ? '执行中' : '完成'}
          </span>
        </summary>
        <pre>{m.text || '（无标准输出）'}</pre>
      </details>
    </article>
  );
});

const ChangeMessageItem = memo(function ChangeMessageItem({item: m, onInspectFile}) {
  const changes = m.changes || [];
  const [isOpen, setIsOpen] = useState(() => changes.length <= 3);
  const counts = changes.reduce((acc, c) => {
    const k = c.kind?.type || 'update';
    acc[k] = (acc[k] || 0) + 1;
    return acc;
  }, {});
  return (
    <article className="message tool-card change-card" id={`msg-${m.id}`}>
      <details open={isOpen}>
        <summary onClick={e => { e.preventDefault(); setIsOpen(v => !v); }}>
          <IconTile icon={FileDiff} tone="orange"/>
          <span className="summary-title">修改了 {changes.length} 个文件</span>
          <span className="change-counts">
            {counts.add > 0 && <span className="change-count kind-add">+{counts.add}</span>}
            {counts.update > 0 && <span className="change-count kind-update">~{counts.update}</span>}
            {counts.delete > 0 && <span className="change-count kind-delete">−{counts.delete}</span>}
          </span>
          <span className={`status-pill ${m.status === 'completed' ? 'ok' : 'run'}`}>{m.status === 'completed' ? '已写入' : '写入中'}</span>
        </summary>
        <div className="change-files">
          {changes.map((c, idx) => {
            const kindType = c.kind?.type || 'update';
            const kindLabel = kindType === 'add' ? '新增' : kindType === 'delete' ? '删除' : '修改';
            return (
              <div key={`${c.path}-${idx}`} className="change-file-item">
                <div className="change-file-header">
                  <span className={`change-badge kind-${kindType}`}>{kindLabel}</span>
                  <code>{c.path}</code>
                  {kindType !== 'delete' && onInspectFile && (
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => onInspectFile(c.path)}
                    >
                      <Eye size={12}/>查看
                    </button>
                  )}
                </div>
                {c.diff && <InlinePatch diffText={c.diff}/>}
              </div>
            );
          })}
        </div>
      </details>
    </article>
  );
});

const ToolMessageItem = memo(function ToolMessageItem({item: m}) {
  const meta = TOOL_TONES[m.kind] || {tone: 'gray', icon: Wrench, label: '工具调用'};
  const done = m.status === 'completed';
  const title = (m.title || '').replace(/[：:]\s*$/, '') || meta.label;
  return (
    <article className={`message tool-card tool-kind-${m.kind || 'other'}`} id={`msg-${m.id}`}>
      <details>
        <summary>
          <IconTile icon={meta.icon} tone={meta.tone}/>
          <span className="summary-title">{title}</span>
          <span className={`status-pill ${done ? 'ok' : 'run'}`}>{done ? '完成' : '调用中'}</span>
        </summary>
        {m.detail && <pre>{m.detail}</pre>}
      </details>
    </article>
  );
});

const AgentOrErrorMessageItem = memo(function AgentOrErrorMessageItem({
  item: m,
  sessionEngine,
  active,
  onSwitchAndRetry,
  onError,
}) {
  const effectiveEngine = m.engine || sessionEngine || 'codex';
  const isAgy = effectiveEngine === 'antigravity';
  const altEngine = isAgy ? 'codex' : 'antigravity';
  const altLabel = isAgy ? 'Codex' : 'AGY';
  const isAnswer = m.type === 'agent';
  const engineClass = isAgy ? 'engine-antigravity' : 'engine-codex';
  const tile = isAnswer
    ? <IconTile icon={isAgy ? Sparkles : Bot} tone={isAgy ? 'violet' : 'green'} size={13}/>
    : m.type === 'error'
      ? <IconTile icon={AlertTriangle} tone="red" size={13}/>
      : <IconTile icon={Info} tone="gray" size={13}/>;

  const [copied, setCopied] = useState(false);

  function copyText() {
    navigator.clipboard?.writeText(m.text)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => onError?.('当前浏览器不允许复制'));
  }

  return (
    <article
      className={`message ${m.type} ${isAnswer ? `answer-card ${engineClass}` : ''}`}
      id={`msg-${m.id}`}
    >
      <div className="speaker answer-header">
        {tile}
        <div className="answer-header-actions">
          {isAnswer && !active && m.promptText && onSwitchAndRetry && (
            <button
              type="button"
              className="switch-retry-btn"
              title={`切换至 ${isAgy ? 'Codex' : 'Antigravity'} 重新回答此问题`}
              onClick={() => onSwitchAndRetry(altEngine, m.promptText)}
            >
              <ArrowLeftRight size={11}/>
              <span>换 {altLabel}</span>
            </button>
          )}
          {isAnswer && (
            <button
              type="button"
              className="icon-button copy-answer-btn"
              title="复制回复"
              onClick={copyText}
            >
              {copied ? <Check size={12} color="var(--emerald)"/> : <Copy size={12}/>}
            </button>
          )}
        </div>
      </div>
      {isAnswer ? (
        <div className="markdown-body">
          <SafeMarkdown components={markdownComponents}>
            {m.text}
          </SafeMarkdown>
        </div>
      ) : (
        <>
          <pre className="prose">{m.text}</pre>
          {m.type === 'error' && !active && m.promptText && onSwitchAndRetry && (
            <div className="error-recovery-bar">
              <button
                type="button"
                className="error-switch-btn"
                onClick={() => onSwitchAndRetry(altEngine, m.promptText)}
              >
                <ArrowLeftRight size={13}/>
                <span>切换至 {isAgy ? 'Codex' : 'Antigravity'} 接力重试该指令</span>
              </button>
            </div>
          )}
        </>
      )}
    </article>
  );
});

const PROCESS_TYPES = new Set(['reasoning', 'plan', 'plan_text', 'command', 'change', 'tool', 'notice']);

const TurnProcessGroup = memo(function TurnProcessGroup({
  group,
  active,
  isLatest,
  onInspectFile,
}) {
  const items = group.items || [];
  const hasRunningItems = items.some(x => {
    if (x.type === 'reasoning') return !x.completed;
    if (x.type === 'command') return x.status === 'inProgress' || (!x.status && x.exitCode == null);
    if (x.type === 'change' || x.type === 'tool') return x.status === 'inProgress';
    return false;
  });
  const isProcessRunning = active && (hasRunningItems || isLatest);

  const hasReasoning = items.some(x => x.type === 'reasoning');
  const commands = items.filter(x => x.type === 'command');
  const changes = items.filter(x => x.type === 'change');
  const tools = items.filter(x => x.type === 'tool');
  const totalFiles = changes.reduce((acc, c) => acc + (c.changes?.length || 0), 0);
  const failedCmds = commands.filter(c => c.exitCode != null && c.exitCode !== 0).length;

  const tags = [];
  if (hasReasoning) tags.push('思考推演');
  if (commands.length > 0) tags.push(`${commands.length} 条命令`);
  if (totalFiles > 0) tags.push(`${totalFiles} 个文件修改`);
  else if (changes.length > 0) tags.push(`${changes.length} 处变更`);
  if (tools.length > 0) tags.push(`${tools.length} 个工具调用`);

  const tagSummary = tags.join(' · ');

  // 运行中默认自动展开显示执行过程，任务完成后全部折叠收起，便于快速查看问题与结果
  const [isOpen, setIsOpen] = useState(isProcessRunning);
  useEffect(() => {
    setIsOpen(isProcessRunning);
  }, [isProcessRunning]);

  return (
    <article
      className={`message tool-card turn-process-card ${isProcessRunning ? 'state-run' : failedCmds > 0 ? 'state-err' : 'state-ok'}`}
      id={`msg-${group.id}`}
    >
      <details open={isOpen}>
        <summary
          className="turn-process-summary"
          onClick={e => {
            e.preventDefault();
            setIsOpen(v => !v);
          }}
        >
          <IconTile
            icon={isProcessRunning ? Loader2 : Sparkles}
            tone={isProcessRunning ? 'blue' : failedCmds > 0 ? 'red' : 'teal'}
            spin={isProcessRunning}
          />
          <div className="turn-process-meta">
            <span className="turn-process-title">
              {isProcessRunning ? '执行中…' : `执行过程 (${items.length} 步)`}
            </span>
            {tagSummary && (
              <span className="turn-process-tags" title={tagSummary}>
                {tagSummary}
              </span>
            )}
          </div>
          <span className={`status-pill ${isProcessRunning ? 'run' : failedCmds > 0 ? 'err' : 'ok'}`}>
            {isProcessRunning ? '执行中' : failedCmds > 0 ? `${failedCmds} 处报错` : '全部完成'}
          </span>
        </summary>

        {isOpen && <div className="turn-process-body">
          {items.map(m => {
            if (m.type === 'reasoning') {
              return <ReasoningMessageItem key={m.id} item={m} active={active}/>;
            }
            if (m.type === 'plan') {
              return <PlanMessageItem key={m.id} item={m}/>;
            }
            if (m.type === 'plan_text') {
              return <PlanTextMessageItem key={m.id} item={m}/>;
            }
            if (m.type === 'command') {
              return <CommandMessageItem key={m.id} item={m} active={active}/>;
            }
            if (m.type === 'change') {
              return <ChangeMessageItem key={m.id} item={m} onInspectFile={onInspectFile}/>;
            }
            if (m.type === 'image') return <ImageMessageItem key={m.id} item={m}/>;
            if (m.type === 'tool') {
              return <ToolMessageItem key={m.id} item={m}/>;
            }
            if (m.type === 'notice') {
              return <div key={m.id} className="notice-chip">{m.text}</div>;
            }
            return null;
          })}
        </div>}
      </details>
    </article>
  );
});

export default function Conversation({
  session, events, job, onLoadOlder, historyBusy, onSend, onStop, onSwitchAndRetry, error, onError, sending,
  projects, projectId, onProjectChange, engine, onEngineChange, engineState, catalog,
  model, onModelChange, effort, onEffortChange, mode, onModeChange, fast, onFastChange,
  onOpenSettings, onInspectFile, composerInputRef,
}) {
  const [approvals, setApprovals] = useState([]);
  const [atBottom, setAtBottom] = useState(true);
  const [previewImg, setPreviewImg] = useState(null);
  const [visibleTurnIdx, setVisibleTurnIdx] = useState(0);
  const [scrubState, setScrubState] = useState({ active: false, index: null, topPx: 0 });
  const [maxRenderTurns, setMaxRenderTurns] = useState(6);
  const bottom = useRef(null);
  const scroll = useRef(null);
  const follow = useRef(true);
  const scrubberTrackRef = useRef(null);
  const isPointerDownRef = useRef(false);
  const lastVibratedIdxRef = useRef(-1);

  const eventParser = useRef(null);
  if (!eventParser.current) eventParser.current = createEventParser();
  const {items: content, tokenUsage, lastJobState} = useMemo(
    () => eventParser.current(events), [events],
  );
  const jobFinishedInStream = Boolean(
    job && lastJobState && lastJobState.jobId === job.id && !activeStates.has(lastJobState.status)
  );
  const localActive = Boolean(job && activeStates.has(job.status) && !jobFinishedInStream);
  const externalActive = Boolean(!localActive && session?.external_active);
  const writerLocked = false;
  const active = localActive || externalActive;
  const previousSession = useRef(session?.id);

  const displayBuilder = useRef(null);
  if (!displayBuilder.current) displayBuilder.current = createDisplayBuilder(PROCESS_TYPES);
  const displayContent = useMemo(() => displayBuilder.current(content), [content]);

  const turns = useMemo(() => {
    const list = [];
    let cur = null;
    for (const m of content) {
      if (m.type === 'user') {
        if (cur) list.push(cur);
        cur = {
          id: m.id,
          index: list.length + 1,
          question: (m.text || '').trim() || (m.images?.length ? '[图片附件]' : '提问'),
          userMsgId: m.id,
          engine: m.engine || session?.engine || 'codex',
          created: m.created,
          resultMsgId: null,
          resultSnippet: '',
          hasError: false,
        };
      } else if (cur) {
        if (m.type === 'agent') {
          cur.resultMsgId = m.id;
          if (m.engine) cur.engine = m.engine;
          if (!cur.resultSnippet && m.text) {
            const clean = m.text.replace(/[*#_`~>]+/g, '').trim().split('\n').find(Boolean);
            if (clean) cur.resultSnippet = clean.slice(0, 96);
          }
        } else if (m.type === 'change' || m.type === 'command') {
          if (!cur.resultMsgId) cur.resultMsgId = m.id;
          if (m.type === 'command' && m.exitCode && m.exitCode !== 0) cur.hasError = true;
        } else if (m.type === 'error') {
          cur.hasError = true;
          if (!cur.resultMsgId) cur.resultMsgId = m.id;
        }
      }
    }
    if (cur) list.push(cur);
    return list;
  }, [content, session?.engine]);

  const jumpToMessage = useCallback((msgId) => {
    if (!msgId) return;
    follow.current = false;
    setAtBottom(false);
    setMaxRenderTurns(Infinity);
    setTimeout(() => {
      const el = document.getElementById(`msg-${msgId}`);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.add('jump-highlight');
        setTimeout(() => {
          el.classList.remove('jump-highlight');
        }, 2000);
      }
    }, 40);
  }, []);

  const resolveScrubFromClientY = useCallback((clientY) => {
    const track = scrubberTrackRef.current;
    if (!track || !turns.length) return null;
    const rect = track.getBoundingClientRect();
    const relY = Math.max(0, Math.min(rect.height, clientY - rect.top));
    const ratio = rect.height > 0 ? relY / rect.height : 0;
    const idx = Math.min(turns.length - 1, Math.max(0, Math.floor(ratio * turns.length)));
    const clampedTop = Math.max(24, Math.min(Math.max(24, rect.height - 24), relY));
    return { idx, topPx: clampedTop };
  }, [turns.length]);

  const scrollRafId = useRef(null);

  const handleScroll = useCallback(() => {
    if (scrollRafId.current != null) return;
    scrollRafId.current = requestAnimationFrame(() => {
      scrollRafId.current = null;
      const el = scroll.current;
      if (!el) return;
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
      follow.current = nearBottom;
      setAtBottom(nearBottom);
      if (nearBottom && turns.length) {
        setVisibleTurnIdx(turns.length - 1);
        return;
      }
      if (turns.length > 1) {
        const rect = el.getBoundingClientRect();
        const probeY = rect.top + rect.height * 0.38;
        let bestIdx = 0;
        for (let i = 0; i < turns.length; i++) {
          const node = document.getElementById(`msg-${turns[i].userMsgId}`);
          if (node && node.getBoundingClientRect().top <= probeY) {
            bestIdx = i;
          }
        }
        setVisibleTurnIdx(bestIdx);
      }
    });
  }, [turns]);

  useEffect(() => {
    return () => {
      if (scrollRafId.current != null) {
        cancelAnimationFrame(scrollRafId.current);
      }
    };
  }, []);

  const handleScrubPointerDown = useCallback((e) => {
    if (e.button != null && e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {}
    isPointerDownRef.current = true;
    const hit = resolveScrubFromClientY(e.clientY);
    if (hit) {
      lastVibratedIdxRef.current = hit.idx;
      try { navigator.vibrate?.(8); } catch {}
      setScrubState({ active: true, index: hit.idx, topPx: hit.topPx });
    }
  }, [resolveScrubFromClientY]);

  const handleScrubPointerMove = useCallback((e) => {
    if (!isPointerDownRef.current && e.pointerType !== 'mouse') return;
    const hit = resolveScrubFromClientY(e.clientY);
    if (!hit) return;
    if (isPointerDownRef.current && hit.idx !== lastVibratedIdxRef.current) {
      lastVibratedIdxRef.current = hit.idx;
      try { navigator.vibrate?.(5); } catch {}
    }
    setScrubState({ active: true, index: hit.idx, topPx: hit.topPx });
  }, [resolveScrubFromClientY]);

  const handleScrubPointerUp = useCallback((e) => {
    if (!isPointerDownRef.current) return;
    isPointerDownRef.current = false;
    try {
      e.currentTarget.releasePointerCapture?.(e.pointerId);
    } catch {}
    const hit = resolveScrubFromClientY(e.clientY);
    const targetIdx = hit ? hit.idx : scrubState.index;
    const targetTurn = targetIdx != null ? turns[targetIdx] : null;
    if (targetTurn) {
      setVisibleTurnIdx(targetIdx);
      jumpToMessage(targetTurn.userMsgId);
    }
    setScrubState({ active: false, index: null, topPx: 0 });
  }, [resolveScrubFromClientY, scrubState.index, turns, jumpToMessage]);

  const handleScrubPointerCancel = useCallback(() => {
    isPointerDownRef.current = false;
    setScrubState({ active: false, index: null, topPx: 0 });
  }, []);

  const handleScrubPointerLeave = useCallback(() => {
    if (!isPointerDownRef.current) {
      setScrubState({ active: false, index: null, topPx: 0 });
    }
  }, []);

  useEffect(() => {
    previousSession.current = session?.id;
    follow.current = true;
    setAtBottom(true);
    setScrubState({ active: false, index: null, topPx: 0 });
    setMaxRenderTurns(6);
  }, [session?.id]);

  const prevScrollHeightRef = useRef(null);

  useLayoutEffect(() => {
    if (prevScrollHeightRef.current != null && scroll.current) {
      const diff = scroll.current.scrollHeight - prevScrollHeightRef.current;
      if (diff > 0) {
        scroll.current.scrollTop += diff;
        prevScrollHeightRef.current = null;
      }
    }
  }, [maxRenderTurns, events]);

  const handleExpandOlderTurns = useCallback((delta) => {
    follow.current = false;
    setAtBottom(false);
    if (scroll.current) {
      prevScrollHeightRef.current = scroll.current.scrollHeight;
    }
    setMaxRenderTurns(v => delta === Infinity ? Infinity : Math.min(turns.length, v + delta));
  }, [turns.length]);

  useEffect(() => {
    // Keep the timeline highlight on the newest turn only while the user is following the output.
    if (follow.current) setVisibleTurnIdx(Math.max(0, turns.length - 1));
  }, [turns.length]);

  useEffect(() => {
    if (follow.current) bottom.current?.scrollIntoView({block: 'end'});
  }, [content, approvals]);

  useEffect(() => {
    if (!session || job?.status !== 'waiting') {
      setApprovals([]);
      return;
    }
    let closed = false;
    async function refreshApprovals() {
      try {
        const data = await api(`sessions/${session.id}/approvals`);
        if (!closed) setApprovals(data);
      } catch (err) {
        if (!closed) onError(err.message);
      }
    }
    refreshApprovals();
    const timer = setInterval(refreshApprovals, 1500);
    return () => {
      closed = true;
      clearInterval(timer);
    };
  }, [session?.id, job?.status, onError]);

  function applyStarter(card) {
    if (card.mode) onModeChange(card.mode);
    if (composerInputRef?.current?.setPrompt) {
      composerInputRef.current.setPrompt(card.prompt);
      composerInputRef.current.focus?.();
    }
  }

  const models = catalog?.[engine]?.models || [];
  const activeModelObj = models.find(m => m.id === model) || models.find(m => m.isDefault) || models[0];
  const currentProject = projects.find(p => p.id === projectId);

  const runningJobEngine = job?.engine || engine;
  const runningJobModels = catalog?.[runningJobEngine]?.models || [];
  const runningJobModelName = runningJobModels.find(m => m.id === job?.model)?.name || job?.model;

  const visibleDisplayContent = useMemo(() => {
    if (turns.length <= maxRenderTurns) return displayContent;
    const startTurnIdx = Math.max(1, turns.length - maxRenderTurns + 1);
    return displayContent.filter(item => {
      if (item.turnIndex == null) return true;
      return item.turnIndex >= startTurnIdx;
    });
  }, [displayContent, turns.length, maxRenderTurns]);

  const composer = (
    <Composer
      key={session?.id || 'new'}
      sessionId={session?.id || 'new'}
      initialExecutionTarget={session?.execution_target || 'local'}
      initialCloudEnvironment={session?.cloud_environment || ''}
      externalRef={composerInputRef}
      onSend={onSend}
      active={localActive}
      externalActive={externalActive}
      writerLocked={writerLocked}
      sending={sending}
      onStop={onStop}
      error={error}
      onError={onError}
      projects={projects}
      projectId={projectId}
      onProjectChange={onProjectChange}
      sessionLocked={Boolean(session)}
      sessionCwd={session?.cwd}
      engine={engine}
      onEngineChange={onEngineChange}
      engineState={engineState}
      catalog={catalog}
      model={model}
      onModelChange={onModelChange}
      effort={effort}
      onEffortChange={onEffortChange}
      mode={mode}
      onModeChange={onModeChange}
      fast={fast}
      onFastChange={onFastChange}
      onOpenSettings={onOpenSettings}
    />
  );

  if (content.length === 0 && !externalActive) {
    return (
      <div className="conversation empty-conversation">
        <div className="new-task-stage">
          <div className="welcome">
            <div className="welcome-badge">
              <span className="dot"/>
              <span>{currentProject?.name || '工作区'}</span>
              <span>·</span>
              <span>{activeModelObj?.name || (engine === 'antigravity' ? 'Antigravity' : 'Codex')}</span>
              <span>·</span>
              <span>{effortBadge[effort] || effort}</span>
            </div>
            <h1>今天想构建什么？</h1>
            <p>随时驱动家里的 Mac mini 编写代码、执行测试或推演架构。</p>
          </div>

          {composer}

          <div className="starter-grid" aria-label="快捷任务模板">
            {STARTERS.map(card => {
              const Icon = card.icon;
              return (
                <button type="button" key={card.title} className={`starter-card tone-${card.tone}`} onClick={() => applyStarter(card)}>
                  <div className={`starter-icon card-icon tone-${card.tone}`}><Icon size={17}/></div>
                  <div className="starter-body">
                    <strong>{card.title}</strong>
                    <span>{card.desc}</span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    );
  }

  const hiddenTurnCount = Math.max(0, turns.length - maxRenderTurns);

  const contextPct = tokenUsage?.contextWindow
    ? Math.min(100, Math.round(((tokenUsage.lastTokens || tokenUsage.totalTokens) / tokenUsage.contextWindow) * 100))
    : null;

  const activeScrubTurn = scrubState.active && scrubState.index != null ? turns[scrubState.index] : null;
  const highlightedIdx = scrubState.active && scrubState.index != null ? scrubState.index : visibleTurnIdx;

  return (
    <ImageContext.Provider value={{sessionId: session?.id, onPreview: setPreviewImg}}>
    <div className="conversation chat-conversation">
      <div
        className="messages"
        ref={scroll}
        onScroll={handleScroll}
      >
        <div className="message-list">
          {hiddenTurnCount > 0 && (
            <div className="progressive-history-banner">
              <button
                type="button"
                className="progressive-load-btn"
                onClick={() => handleExpandOlderTurns(8)}
              >
                <span>↑ 展开更早的 {Math.min(hiddenTurnCount, 8)} 轮问答（已隐藏 {hiddenTurnCount} 轮）</span>
              </button>
              <button
                type="button"
                className="progressive-load-all-btn"
                onClick={() => handleExpandOlderTurns(Infinity)}
              >
                展开全部
              </button>
            </div>
          )}
          {hiddenTurnCount === 0 && onLoadOlder && (
            <button
              type="button"
              className="history-load-btn"
              disabled={historyBusy}
              onClick={async () => {
                follow.current = false;
                setAtBottom(false);
                if (scroll.current) {
                  prevScrollHeightRef.current = scroll.current.scrollHeight;
                }
                await onLoadOlder();
                if (scroll.current && prevScrollHeightRef.current == null) {
                  prevScrollHeightRef.current = scroll.current.scrollHeight;
                }
                setMaxRenderTurns(v => (v === Infinity ? Infinity : v + 20));
              }}
            >
              {historyBusy ? '正在拉取更早历史…' : '从服务器加载更早快照'}
            </button>
          )}
          {visibleDisplayContent.map((m, idx) => {
            if (m.type === 'cloud_task') return <CloudTaskCard key={m.id} item={m}/>;
            if (m.type === 'user') {
              return (
                <UserMessageItem
                  key={m.id}
                  item={m}
                  session={session}
                  catalog={catalog}
                  onPreviewImg={setPreviewImg}
                  onError={onError}
                />
              );
            }
            if (m.type === 'process_group') {
              const isLatest = idx === visibleDisplayContent.length - 1;
              return (
                <TurnProcessGroup
                  key={m.id}
                  group={m}
                  active={active}
                  isLatest={isLatest}
                  onInspectFile={onInspectFile}
                />
              );
            }
            if (m.type === 'reasoning') {
              return <ReasoningMessageItem key={m.id} item={m} active={active}/>;
            }
            if (m.type === 'plan') {
              return <PlanMessageItem key={m.id} item={m}/>;
            }
            if (m.type === 'plan_text') {
              return <PlanTextMessageItem key={m.id} item={m}/>;
            }
            if (m.type === 'command') {
              return <CommandMessageItem key={m.id} item={m} active={active}/>;
            }
            if (m.type === 'change') {
              return <ChangeMessageItem key={m.id} item={m} onInspectFile={onInspectFile}/>;
            }
            if (m.type === 'image') return <ImageMessageItem key={m.id} item={m}/>;
            if (m.type === 'tool') {
              return <ToolMessageItem key={m.id} item={m}/>;
            }
            return (
              <AgentOrErrorMessageItem
                key={m.id}
                item={m}
                sessionEngine={session?.engine}
                active={active}
                onSwitchAndRetry={onSwitchAndRetry}
                onError={onError}
              />
            );
          })}
        </div>

        <div className="approval-list">
          {approvals.map(a => (
            <Approval
              key={a.id}
              approval={a}
              onDone={() => setApprovals(v => v.filter(x => x.id !== a.id))}
              onError={onError}
            />
          ))}
        </div>

        {localActive && (
          <div className={`running-line status-${job.status}`}>
            <span className="running-icon">
              {job.status === 'waiting'
                ? <ShieldCheck size={14}/>
                : job.status === 'queued'
                  ? <Hourglass size={14}/>
                  : <Loader2 size={14} className="spinning"/>}
            </span>
            <span className="running-text">
              {job.status === 'waiting'
                ? '等待你批准操作（关闭页面后回来仍可继续处理）'
                : job.status === 'queued'
                  ? '任务排队中，即将在 Mac mini 上启动…'
                  : `正在${job.execution_target === 'cloud' ? '提交云端任务' : '本地运行'}${job.execution_target !== 'cloud' && runningJobModelName ? ` · ${runningJobModelName}` : ''}${job.execution_target !== 'cloud' && job.effort ? ` (${effortBadge[job.effort] || job.effort})` : ''}…`}
            </span>
            {job.status === 'running' && <span className="typing-dots" aria-hidden="true"><i/><i/><i/></span>}
          </div>
        )}

        {externalActive && (
          <div className="running-line external-sync-line status-external">
            <span className="running-icon"><RefreshCw size={14} className="spinning"/></span>
            <span className="running-text">Mac 本机 Codex 正在执行此会话 · 输出进度实时同步中…</span>
          </div>
        )}

        {session && (
          <div className="token-meter">
            {tokenUsage && <span className="token-chip"><Gauge size={12}/>累计 {tokenUsage.totalTokens.toLocaleString()} tokens</span>}
            {tokenUsage?.reasoningTokens > 0 && (
              <span className="token-chip tone-purple"><Brain size={12}/>思考 {tokenUsage.reasoningTokens.toLocaleString()}</span>
            )}
            {contextPct != null && (
              <span className={`token-chip context-chip ${contextPct >= 85 ? 'level-high' : contextPct >= 60 ? 'level-mid' : 'level-low'}`}>
                上下文 {contextPct}%
                <span className="context-bar"><span style={{width: `${contextPct}%`}}/></span>
              </span>
            )}
            <SessionQuota executionTarget={session?.execution_target} engine={localActive ? runningJobEngine : engine} model={localActive ? job?.model : activeModelObj?.id} status={job?.status} />
          </div>
        )}

        <div ref={bottom}/>
      </div>

      {turns.length >= 1 && (
        <div
          className={`edge-scrubber ${scrubState.active ? 'is-scrubbing' : ''}`}
          aria-label="问答时间轴快速滑动定位"
        >
          <div
            ref={scrubberTrackRef}
            className="edge-scrubber-track"
            onPointerDown={handleScrubPointerDown}
            onPointerMove={handleScrubPointerMove}
            onPointerUp={handleScrubPointerUp}
            onPointerCancel={handleScrubPointerCancel}
            onPointerLeave={handleScrubPointerLeave}
          >
            {turns.map((turn, idx) => {
              const dist = Math.abs(idx - highlightedIdx);
              const proximityClass = dist === 0
                ? 'is-target'
                : dist === 1
                  ? 'is-near-1'
                  : dist === 2
                    ? 'is-near-2'
                    : '';
              const toneClass = turn.hasError
                ? 'tone-err'
                : turn.engine === 'antigravity'
                  ? 'tone-agy'
                  : 'tone-codex';
              return (
                <div
                  key={turn.id}
                  className={`edge-tick-slot ${proximityClass} ${toneClass}`}
                >
                  <span className="edge-tick-bar" />
                </div>
              );
            })}

            {activeScrubTurn && (
              <div
                className="scrubber-preview-card"
                style={{ top: `${scrubState.topPx}px` }}
                aria-live="polite"
              >
                <div className="scrubber-preview-head">
                  <span className="scrubber-turn-idx">#{activeScrubTurn.index} / {turns.length}</span>
                  <span className={`scrubber-engine-tag ${activeScrubTurn.engine === 'antigravity' ? 'agy' : 'codex'}`}>
                    {activeScrubTurn.engine === 'antigravity' ? 'AGY' : 'Codex'}
                  </span>
                  <span className="scrubber-release-hint">松手定位</span>
                </div>
                <div className="scrubber-preview-row is-question">
                  <span className="scrubber-row-badge q">问</span>
                  <span className="scrubber-row-text">{activeScrubTurn.question}</span>
                </div>
                <div className="scrubber-preview-row is-answer">
                  <span className="scrubber-row-badge a">答</span>
                  <span className="scrubber-row-text">
                    {activeScrubTurn.resultSnippet
                      || (activeScrubTurn.index === turns.length && active ? '正在执行中…' : '等待或查看该轮详情')}
                  </span>
                </div>
              </div>
            )}
          </div>

          {!atBottom && (
            <button
              type="button"
              className="edge-bottom-dot"
              title="回到最新输出底部"
              aria-label="回到最新输出底部"
              onClick={() => {
                follow.current = true;
                bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
              }}
            >
              <ArrowDown size={11} />
            </button>
          )}
        </div>
      )}

      {composer}

      {previewImg && (
        <div className="modal-backdrop" onClick={() => setPreviewImg(null)}>
          <div className="image-lightbox" role="dialog" aria-label="图片预览">
            <button type="button" className="icon-button" onClick={() => setPreviewImg(null)} aria-label="关闭图片预览"><X size={20}/></button>
            <a href={previewImg} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}>打开原图</a>
            <img src={previewImg} alt="大图预览"/>
          </div>
        </div>
      )}
    </div>
    </ImageContext.Provider>
  );
}
