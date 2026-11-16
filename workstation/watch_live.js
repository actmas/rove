(function() {
  const urlParams = new URLSearchParams(window.location.search);
  const token = urlParams.get('token') || '';
  let lastEventId = 0;
  let currentSessionId = '';
  let startedTime = null;
  let timerInterval = null;
  let pollTimer = null;
  const itemsMap = new Map();

  const statusDot = document.getElementById('status-dot');
  const statusText = document.getElementById('status-text');
  const stopwatch = document.getElementById('stopwatch');
  const sessionTitle = document.getElementById('session-title');
  const contentBox = document.getElementById('content');
  const anchor = document.getElementById('anchor');
  const emptyState = document.getElementById('empty-state');
  const approvalCard = document.getElementById('watch-approval-card');
  const approvalCmd = document.getElementById('watch-approval-cmd');
  const approvalCwd = document.getElementById('watch-approval-cwd');
  const approvalActions = document.getElementById('watch-approval-actions');
  const approvalFeedback = document.getElementById('watch-approval-feedback');
  const btnAccept = document.getElementById('watch-btn-accept');
  const btnDecline = document.getElementById('watch-btn-decline');
  let activeApprovalId = '';
  let approvalBusy = false;

  function formatTime(secs) {
    if (secs == null || isNaN(secs) || secs < 0) return '00:00';
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  }

  function updateStopwatch() {
    if (document.hidden) return;
    if (!startedTime) {
      stopwatch.textContent = '--:--';
      return;
    }
    const elapsed = (Date.now() / 1000) - startedTime;
    stopwatch.textContent = formatTime(elapsed);
  }

  function setStatus(status, engine) {
    statusDot.className = 'dot ' + (status || '');
    if (status === 'running') {
      statusText.textContent = (engine === 'antigravity' ? 'Antigravity' : 'Codex') + ' 执行中';
    } else if (status === 'waiting') {
      statusText.textContent = '等待审批';
    } else if (status === 'queued') {
      statusText.textContent = '排队中';
    } else {
      statusText.textContent = 'Mac mini 就绪';
    }
  }

  function ensureItem(id, type, title) {
    if (itemsMap.has(id)) {
      return itemsMap.get(id);
    }
    if (emptyState) emptyState.style.display = 'none';

    // Limit cards on watch screen to avoid out-of-memory
    const existingCards = contentBox.querySelectorAll('.card');
    if (existingCards.length > 25) {
      for (let i = 0; i < existingCards.length - 20; i++) {
        const oldCard = existingCards[i];
        for (const [key, val] of itemsMap.entries()) {
          if (val.el === oldCard) {
            itemsMap.delete(key);
            break;
          }
        }
        oldCard.remove();
      }
    }

    const el = document.createElement('article');
    el.className = 'card ' + type;
    el.id = 'msg-' + id;

    const header = document.createElement('div');
    header.className = 'card-header';
    header.textContent = title;

    const body = document.createElement('pre');
    body.className = 'card-body';

    el.appendChild(header);
    el.appendChild(body);
    contentBox.insertBefore(el, anchor);

    const entry = { el, body, text: '' };
    itemsMap.set(id, entry);
    return entry;
  }

  function renderEvents(events) {
    if (!events || !events.length) return;
    let hasNewCards = false;

    for (const e of events) {
      lastEventId = Math.max(lastEventId, e.id || 0);
      const p = e.payload || {};
      const kind = e.kind;

      if (kind === 'user') {
        const rawText = p.text || '';
        const text = rawText.replace(/<!--\s*WORKSTATION_BRIDGE_START[\s\S]*?WORKSTATION_BRIDGE_END\s*-->/g, '').trim();
        if (text) {
          const item = ensureItem('u-' + e.id, 'user', '👤 任务提示');
          item.body.textContent = text;
          hasNewCards = true;
        }
      } else if (kind === 'job_status' && p.error) {
        const item = ensureItem('err-' + e.id, 'terminal', '⚠️ 异常');
        item.body.textContent = p.error;
        hasNewCards = true;
      } else if (kind === 'antigravity') {
        const skind = p.step_kind;
        if (skind === 'reasoning') {
          const text = (p.text || '').trim();
          if (text) {
            const item = ensureItem(p.step_id || ('agy-think-' + e.id), 'reasoning', '🧠 思考');
            item.body.textContent = text.length > 300 ? text.slice(0, 300) + '…' : text;
            hasNewCards = true;
          }
        } else if (skind === 'agent') {
          const text = (p.text || '').trim();
          if (text) {
            const item = ensureItem(p.step_id || ('agy-agent-' + e.id), 'agent', '🤖 回复');
            item.body.textContent = text;
            hasNewCards = true;
          }
        } else if (skind === 'command') {
          const cmd = p.command || '';
          if (cmd) {
            const item = ensureItem(p.step_id || ('agy-cmd-' + e.id), 'terminal', '💻 Shell');
            item.body.textContent = cmd;
            hasNewCards = true;
          }
        } else if (skind === 'change') {
          const item = ensureItem(p.step_id || ('agy-chg-' + e.id), 'change', '📝 文件修改');
          const paths = (p.changes || []).map(c => '• ' + (c.path || '')).join('\n');
          item.body.textContent = paths || '已写入变更';
          hasNewCards = true;
        } else if (skind === 'tool') {
          const item = ensureItem(p.step_id || ('agy-tool-' + e.id), 'terminal', '🛠️ 工具执行');
          item.body.textContent = p.title || p.detail || '执行操作';
          hasNewCards = true;
        }
      } else if (kind === 'codex') {
        const m = p.method;
        const v = p.params || {};
        const it = v.item;

        if (m === 'item/agentMessage/delta') {
          const item = ensureItem(v.itemId || 'agent-live', 'agent', '🤖 回复');
          item.text += (v.delta || '');
          item.body.textContent = item.text;
          hasNewCards = true;
        } else if (m === 'item/reasoning/textDelta' || m === 'item/reasoning/summaryTextDelta') {
          const item = ensureItem(v.itemId || 'reasoning-live', 'reasoning', '🧠 深度思考');
          item.text += (v.delta || '');
          item.body.textContent = item.text.length > 300 ? item.text.slice(0, 300) + '…' : item.text;
          hasNewCards = true;
        } else if (m === 'item/commandExecution/outputDelta') {
          const item = ensureItem(v.itemId || 'cmd-live', 'terminal', '💻 Shell');
          item.text += (v.delta || '');
          item.body.textContent = item.text;
          hasNewCards = true;
        } else if (it && it.type === 'commandExecution') {
          const item = ensureItem(it.id, 'terminal', '💻 ' + (it.command || 'Shell'));
          if (it.aggregatedOutput) {
            item.body.textContent = it.aggregatedOutput;
          }
          hasNewCards = true;
        } else if (it && it.type === 'fileChange') {
          const item = ensureItem(it.id, 'change', '📝 文件修改');
          const paths = (it.changes || []).map(c => '• ' + (c.path || '')).join('\n');
          item.body.textContent = paths || '已写入变更';
          hasNewCards = true;
        } else if (it && it.type === 'agentMessage') {
          const item = ensureItem(it.id, 'agent', '🤖 回复');
          if (it.text) {
            item.text = it.text;
            item.body.textContent = it.text;
          }
          hasNewCards = true;
        }
      }
    }

    if (hasNewCards) {
      anchor.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }

  async function pollState() {
    try {
      const res = await fetch(`/api/watch/live/state?token=${encodeURIComponent(token)}&after=${lastEventId}`, {
        cache: 'no-store'
      });
      if (!res.ok) {
        if (res.status === 401) {
          statusText.textContent = 'Token 无效';
          if (emptyState) {
            emptyState.style.display = 'flex';
            emptyState.innerHTML = '<div class="empty-icon">🔒</div><div>Token 无效或已更新<br><small style="color:var(--dim)">请检查快捷指令 URL 中的 token 参数</small></div>';
          }
          return;
        }
        throw new Error('HTTP ' + res.status);
      }
      const data = await res.json();

      // Handle session change
      if (data.session_id && data.session_id !== currentSessionId) {
        currentSessionId = data.session_id;
        lastEventId = 0;
        itemsMap.clear();
        const cards = contentBox.querySelectorAll('.card');
        cards.forEach(c => c.remove());
      }

      sessionTitle.textContent = data.session_title || 'Rove 监控';

      if (data.job) {
        setStatus(data.job.status, data.job.engine);
        if (data.job.started) {
          startedTime = data.job.started;
        }
      } else {
        setStatus('idle');
        startedTime = null;
        stopwatch.textContent = '--:--';
      }

      if (approvalCard && data.approval && data.approval.id) {
        const appr = data.approval;
        const isNew = activeApprovalId !== appr.id;
        activeApprovalId = appr.id;
        approvalCard.classList.add('visible');
        approvalCmd.textContent = appr.command || appr.reason || '请求执行操作';
        approvalCwd.textContent = appr.cwd ? '目录: ' + appr.cwd : '';
        approvalCwd.style.display = appr.cwd ? 'block' : 'none';
        if (appr.is_question) {
          approvalActions.style.display = 'none';
          approvalFeedback.style.display = 'block';
          approvalFeedback.textContent = '请在 iPhone 或网页端回答此交互问题';
        } else if (!approvalBusy) {
          approvalActions.style.display = 'grid';
          approvalFeedback.style.display = 'none';
          btnAccept.disabled = false;
          btnDecline.disabled = false;
        }
        if (isNew) {
          window.scrollTo({ top: 0, behavior: 'smooth' });
        }
      } else if (approvalCard) {
        activeApprovalId = '';
        approvalBusy = false;
        approvalCard.classList.remove('visible');
      }

      if (data.events && data.events.length) {
        renderEvents(data.events);
      }

      if (itemsMap.size === 0) {
        if (emptyState) {
          emptyState.style.display = 'flex';
          const title = data.session_title || '等待新任务';
          emptyState.innerHTML = `<div class="empty-icon">🟢</div><div>Mac mini 就绪<br><small style="color:var(--dim)">${title}</small></div>`;
        }
      } else if (emptyState) {
        emptyState.style.display = 'none';
      }

      // Adaptive polling rate based on execution state: save Apple Watch battery
      const isActive = data.job && (data.job.status === 'running' || data.job.status === 'waiting');
      const nextDelay = document.hidden ? 15000 : (isActive ? 1200 : 3500);
      pollTimer = setTimeout(pollState, nextDelay);
    } catch (err) {
      statusText.textContent = '重连中…';
      pollTimer = setTimeout(pollState, document.hidden ? 15000 : 3000);
    }
  }

  // Handle Apple Watch screen sleep / wrist drop visibility change
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      updateStopwatch();
      if (pollTimer) clearTimeout(pollTimer);
      pollState();
    }
  });

  // Scroll button interaction
  const watchScrollBtn = document.getElementById('watch-scroll-btn');
  if (watchScrollBtn) {
    watchScrollBtn.addEventListener('click', () => {
      anchor.scrollIntoView({ behavior: 'smooth', block: 'end' });
      watchScrollBtn.style.display = 'none';
    });

    window.addEventListener('scroll', () => {
      const nearBottom = (window.innerHeight + window.scrollY) >= (document.body.offsetHeight - 90);
      watchScrollBtn.style.display = (!nearBottom && itemsMap.size > 2) ? 'inline-flex' : 'none';
    }, { passive: true });
  }

  async function submitWatchDecision(decision) {
    if (!activeApprovalId || approvalBusy) return;
    approvalBusy = true;
    btnAccept.disabled = true;
    btnDecline.disabled = true;
    approvalFeedback.style.display = 'block';
    approvalFeedback.textContent = decision === 'accept' ? '正在提交批准…' : '正在提交拒绝…';
    try {
      const res = await fetch(`/api/watch/approve?token=${encodeURIComponent(token)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approval_id: activeApprovalId, decision })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || ('HTTP ' + res.status));
      }
      approvalFeedback.textContent = decision === 'accept' ? '✓ 已批准执行' : '✕ 已拒绝操作';
      if (pollTimer) clearTimeout(pollTimer);
      pollState();
    } catch (err) {
      approvalBusy = false;
      btnAccept.disabled = false;
      btnDecline.disabled = false;
      approvalFeedback.textContent = '提交失败: ' + err.message;
    }
  }

  if (btnAccept) btnAccept.addEventListener('click', () => submitWatchDecision('accept'));
  if (btnDecline) btnDecline.addEventListener('click', () => submitWatchDecision('decline'));

  // Start polling and stopwatch ticker
  pollState();
  timerInterval = setInterval(updateStopwatch, 1000);
})();
