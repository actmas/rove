import {itemImages} from './images.js';

const activeStates = new Set(['queued', 'running', 'waiting']);

function stripInjectedContext(raw) {
  if (!raw || typeof raw !== 'string') return '';
  let text = raw;
  text = text.replace(/<!-- WORKSTATION_BRIDGE_START -->[\s\S]*?<!-- WORKSTATION_BRIDGE_END -->\s*/g, '');
  text = text.replace(/# \[跨引擎接力上下文与历史记忆[\s\S]*?直接精准响应用户本次的新指令[^\n]*\n*/g, '');
  text = text.replace(/# \[跨引擎接力上下文与历史记忆[\s\S]*?保持宛如同一模型连续对话的自然体验[^\n]*\n*/g, '');
  text = text.replace(/\[项目持久记忆与全局架构约定[^\]]*\][\s\S]*?请严格遵从以上项目记忆与红线约定。\s*/g, '');
  return text.trim();
}

function createAccumulator() {
  const result = [];
  const items = new Map();
  const positions = new Map();
  const cleanPositions = [];
  let cleanResult = [];
  let previousUser = null;
  let tokenUsage = null;
  let lastJobState = null;

  return function append(events) {
    const changed = new Set();
    let dirtyFrom = result.length;

    function item(id, initial) {
      if (!items.has(id)) {
        const next = {id, ...initial};
        items.set(id, next);
        positions.set(id, result.length);
        result.push(next);
        changed.add(id);
      }
      if (!changed.has(id)) {
        const current = items.get(id);
        const next = {...current};
        if (current.parts) next.parts = {...current.parts};
        items.set(id, next);
        result[positions.get(id)] = next;
        changed.add(id);
      }
      dirtyFrom = Math.min(dirtyFrom, positions.get(id));
      return items.get(id);
    }

    function settleRunningSteps(includeCommands = false) {
      for (let i = result.length - 1; i >= 0; i--) {
        const prev = result[i];
        if (prev.type === 'user') break;
        if (prev.type === 'reasoning' && !prev.completed) item(prev.id, {}).completed = true;
        if (includeCommands && prev.status === 'inProgress') item(prev.id, {}).status = 'completed';
      }
    }

    for (const e of events) {
      const p = e.payload || {};
      if (e.kind === 'user') {
        const uid = p.item_id ? `user-item-${p.item_id}` : `user-${e.id}`;
        if (items.has(uid)) continue;
        const cleanText = stripInjectedContext(p.text || '');
        if (!cleanText && !(p.images && p.images.length)) continue;
        const prevUser = previousUser;
        if (prevUser && prevUser.text.trim() === cleanText && (!e.job_id || p.source)) {
          continue;
        }
        const entry = {
          id: uid,
          type: 'user',
          text: cleanText,
          engine: p.engine,
          execution_target: p.execution_target,
          model: p.model,
          effort: p.effort,
          mode: p.mode,
          fast: p.fast,
          images: p.images || [],
          source: p.source,
          created: e.created,
        };
        previousUser = entry;
        items.set(uid, entry);
        positions.set(uid, result.length);
        changed.add(uid);
        result.push(entry);
      } else if (e.kind === 'cloud_task') {
        result.push({id: `cloud-${e.id}`, type: 'cloud_task', ...p});
      } else if (e.kind === 'notice') {
        result.push({id: `notice-${e.id}`, type: 'notice', text: p.text || ''});
      } else if (e.kind === 'job_status') {
        if (e.job_id && p.status) {
          lastJobState = { jobId: e.job_id, status: p.status };
        }
        if (p.status && !activeStates.has(p.status)) {
          settleRunningSteps(true);
        }
        if (p.error) {
          result.push({id: `err-${e.id}`, type: 'error', text: p.error});
        }
      } else if (e.kind === 'codex') {
        const v = p.params || {};
        const m = p.method;
        const it = v.item;

        if (m === 'item/agentMessage/delta') {
          settleRunningSteps(false);
          const entry = item(v.itemId, {type: 'agent', text: '', engine: 'codex'});
          entry.text += v.delta || '';
          entry.engine = 'codex';
        } else if (m === 'item/commandExecution/outputDelta') {
          item(v.itemId, {type: 'command', text: ''}).text += v.delta || '';
        } else if (m === 'item/reasoning/summaryTextDelta' || m === 'item/reasoning/textDelta') {
          const entry = item(v.itemId, {type: 'reasoning', text: '', parts: {}, completed: false});
          const key = m === 'item/reasoning/summaryTextDelta' ? `s-${v.summaryIndex ?? 0}` : `c-${v.contentIndex ?? 0}`;
          entry.parts[key] = (entry.parts[key] || '') + (v.delta || '');
          entry.text = Object.values(entry.parts).filter(Boolean).join('\n\n');
        } else if (m === 'item/plan/delta') {
          item(v.itemId, {type: 'plan_text', text: ''}).text += v.delta || '';
        } else if (m === 'turn/plan/updated') {
          const entry = item(`plan-${v.turnId || e.job_id || 'turn'}`, {type: 'plan', steps: [], explanation: ''});
          entry.steps = v.plan || [];
          if (v.explanation) entry.explanation = v.explanation;
        } else if (m === 'thread/tokenUsage/updated' && v.tokenUsage) {
          const total = v.tokenUsage.total || {};
          const last = v.tokenUsage.last || {};
          tokenUsage = {
            totalTokens: total.totalTokens || 0,
            reasoningTokens: total.reasoningOutputTokens || 0,
            lastTokens: last.totalTokens || 0,
            contextWindow: v.tokenUsage.modelContextWindow || null,
          };
        } else if (it && (m === 'item/started' || m === 'item/completed')) {
          const done = m === 'item/completed';
          if (it.type === 'agentMessage') {
            const entry = item(it.id, {type: 'agent', text: '', engine: 'codex'});
            if (it.text) entry.text = it.text;
            entry.engine = 'codex';
            entry.completed = done;
          } else if (it.type === 'reasoning') {
            const joined = [...(it.summary || []), ...(it.content || [])].filter(Boolean).join('\n\n');
            if (done && !joined && !items.has(it.id)) continue;
            const entry = item(it.id, {type: 'reasoning', text: '', parts: {}, completed: false});
            if (joined) entry.text = joined;
            entry.completed = done;
          } else if (it.type === 'plan') {
            const entry = item(it.id, {type: 'plan_text', text: ''});
            if (it.text) entry.text = it.text;
            entry.completed = done;
          } else if (it.type === 'commandExecution') {
            Object.assign(item(it.id, {type: 'command', text: ''}), {
              title: it.command,
              cwd: it.cwd,
              exitCode: it.exitCode,
              durationMs: it.durationMs,
              text: it.aggregatedOutput || items.get(it.id)?.text || '',
              status: it.status || (done ? 'completed' : 'inProgress'),
            });
          } else if (it.type === 'fileChange') {
            Object.assign(item(it.id, {type: 'change', changes: []}), {
              changes: it.changes || [],
              status: it.status || (done ? 'completed' : 'inProgress'),
            });
          } else if (it.type === 'webSearch') {
            Object.assign(item(it.id, {type: 'tool', kind: 'search'}), {
              title: `联网搜索：${it.query || ''}`,
              status: done ? 'completed' : 'inProgress',
            });
          } else if (it.type === 'mcpToolCall' || it.type === 'collabAgentToolCall') {
            Object.assign(item(it.id, {type: 'tool', kind: 'mcp'}), {
              title: it.type === 'mcpToolCall' ? `工具调用：${it.server} / ${it.tool}` : `子代理协同：${it.tool}`,
              detail: it.prompt || (it.arguments ? JSON.stringify(it.arguments, null, 2) : ''),
              durationMs: it.durationMs,
              status: it.status || (done ? 'completed' : 'inProgress'),
            });
          } else if (it.type === 'subAgentActivity') {
            const meta = [it.model, it.reasoningEffort].filter(Boolean).join(' · ');
            Object.assign(item(it.id, {type: 'tool', kind: 'mcp'}), {
              title: `子代理：${it.agentPath || 'subagent'}${meta ? ` (${meta})` : ''}`,
              status: it.kind === 'started' && !done ? 'inProgress' : 'completed',
            });
          } else if (it.type === 'imageView' || it.type === 'imageGeneration') {
            Object.assign(item(it.id, {type: 'image'}), {
              images: itemImages(it),
              title: it.type === 'imageView' ? `查看图像：${(it.path || '').split('/').pop() || it.path || ''}` : '生成视觉图像',
              detail: it.revisedPrompt || it.path || '',
              status: it.status || (done ? 'completed' : 'inProgress'),
            });
          } else if (it.type === 'contextCompaction') {
            item(it.id, {type: 'notice', text: '上下文已自动压缩整理，保留核心记忆继续执行'});
          }
        } else if (m === 'error' && !v.willRetry) {
          result.push({id: `cerr-${e.id}`, type: 'error', text: v.error?.message || 'Codex 返回错误'});
        }
      } else if (e.kind === 'antigravity') {
        if (p.event === 'agy_step') {
          if (p.step_kind === 'reasoning') {
            const entry = item(p.step_id, {type: 'reasoning', text: '', parts: {}, completed: false});
            if (p.text != null) entry.text = p.text;
            entry.completed = Boolean(p.completed);
          } else if (p.step_kind === 'agent') {
            const stepEng = p.engine || 'antigravity';
            const entry = item(p.step_id, {type: 'agent', text: '', engine: stepEng});
            if (p.text != null) entry.text = p.text;
            entry.engine = stepEng;
            entry.completed = Boolean(p.completed);
          } else if (p.step_kind === 'command') {
            Object.assign(item(p.step_id, {type: 'command', text: ''}), {
              title: p.command,
              cwd: p.cwd,
              exitCode: p.exitCode,
              text: p.output || '',
              status: p.completed ? 'completed' : 'inProgress',
            });
          } else if (p.step_kind === 'change') {
            Object.assign(item(p.step_id, {type: 'change', changes: []}), {
              changes: p.changes || [],
              status: p.completed ? 'completed' : 'inProgress',
            });
          } else if (p.step_kind === 'tool') {
            Object.assign(item(p.step_id, {type: 'tool', kind: p.tool_kind || 'mcp'}), {
              title: p.title || '工具调用',
              detail: p.detail || '',
              status: p.completed ? 'completed' : 'inProgress',
            });
          }
        } else if ( p.event === 'step_update') {
          const step = p.step_update || {};
          if (step.thought_delta || step.thinking_delta) {
            item(`agy-think-${e.job_id}`, {type: 'reasoning', text: '', parts: {}, completed: false}).text += (step.thought_delta || step.thinking_delta);
          }
          if (step.text_delta) {
            settleRunningSteps(false);
            const entry = item(`agy-${e.job_id}`, {type: 'agent', text: '', engine: 'antigravity'});
            entry.text += step.text_delta;
            entry.engine = 'antigravity';
          }
        } else if (p.event === 'result') {
          settleRunningSteps(true);
          const r = p.result || p;
          if (r.response && !p.has_structured_steps) {
            const entry = item(`agy-${e.job_id}`, {type: 'agent', text: '', engine: 'antigravity'});
            entry.text = r.response;
            entry.engine = 'antigravity';
            entry.completed = true;
          }
        }
      }
    }

    if (dirtyFrom < result.length) {
      // Recheck only the affected turn and subsequent turns. Earlier output stays stable.
      while (dirtyFrom > 0 && result[dirtyFrom].type !== 'user') dirtyFrom--;
      const prefixLength = cleanPositions[dirtyFrom] ?? cleanResult.length;
      cleanResult = cleanResult.slice(0, prefixLength);
      const seenAgentTexts = new Set();
      for (let index = dirtyFrom; index < result.length; index++) {
        cleanPositions[index] = cleanResult.length;
        const entry = result[index];
        if (entry.type === 'user') {
          seenAgentTexts.clear();
        } else if (entry.type === 'agent' && entry.text?.trim()) {
          const text = entry.text.trim();
          if (seenAgentTexts.has(text)) continue;
          seenAgentTexts.add(text);
        }
        cleanResult.push(entry);
      }
    }
    return {items: cleanResult, tokenUsage, lastJobState};
  };
}

// Track append provenance without comparing every historical event on each render.
const eventParents = new WeakMap();

export function appendEvents(previous, batch) {
  const next = [...previous, ...batch];
  eventParents.set(next, new WeakRef(previous));
  return next;
}

export function createEventParser() {
  let previous = null;
  let accumulator = createAccumulator();
  let parsed = null;
  return events => {
    if (events === previous) return parsed;
    let ancestor = events;
    while (ancestor && ancestor !== previous) ancestor = eventParents.get(ancestor)?.deref();
    const isAppend = previous !== null && ancestor === previous;
    const offset = isAppend ? previous.length : 0;
    if (!isAppend) accumulator = createAccumulator();
    parsed = accumulator(events.slice(offset));
    previous = events;
    return parsed;
  };
}

export function parseEvents(events) {
  return createAccumulator()(events);
}
