(function() {
  'use strict';

  // 1. URL Token & Storage Initializers
  var tokenMatch = window.location.search.match(/[?&]token=([^&]*)/);
  var token = tokenMatch ? decodeURIComponent(tokenMatch[1].replace(/\+/g, ' ')) : '';

  function pad2(value) {
    return ('0' + value).slice(-2);
  }

  // State trackers
  var startedTime = null;
  var activeApproval = null;
  var lastStateDigest = '';
  var pollTimeoutId = null;
  var currentPollDelay = 3000;
  var renderedEventIds = {};
  var currentEventsList = [];
  var currentFilter = 'all'; // 'all' | 'key'
  var prevJobStatus = null;

  // DOM Elements
  var rootEl = document.documentElement;
  var engineBadge = document.getElementById('engine-badge');
  var projectBadge = document.getElementById('project-badge');
  var branchBadge = document.getElementById('branch-badge');
  var statusBadge = document.getElementById('status-badge');
  var statusText = document.getElementById('status-text');
  var sessionTitle = document.getElementById('session-title');
  var stopwatch = document.getElementById('stopwatch');
  var pollingTag = document.getElementById('polling-tag');

  var fontDecBtn = document.getElementById('font-dec-btn');
  var fontIncBtn = document.getElementById('font-inc-btn');
  var themeBtn = document.getElementById('theme-btn');
  var autoCleanBtn = document.getElementById('auto-clean-btn');
  var cleanScreenBtn = document.getElementById('clean-screen-btn');

  var approvalBox = document.getElementById('approval-box');
  var approvalType = document.getElementById('approval-type');
  var approvalCommand = document.getElementById('approval-command');
  var approvalCwd = document.getElementById('approval-cwd');
  var approvalTimeTag = document.getElementById('approval-time-tag');
  var btnApprove = document.getElementById('btn-approve');
  var btnReject = document.getElementById('btn-reject');

  var stepTag = document.getElementById('step-tag');
  var stepDesc = document.getElementById('step-desc');
  var stepDetail = document.getElementById('step-detail');
  var filterAllBtn = document.getElementById('filter-all-btn');
  var filterKeyBtn = document.getElementById('filter-key-btn');
  var eventsStream = document.getElementById('events-stream');

  var lastSyncTime = document.getElementById('last-sync-time');
  var viewportResEl = document.getElementById('viewport-resolution');

  // ==========================================================================
  // E-ink Ghosting & Screen Refresh Management
  // ==========================================================================
  function cleanScreen(thorough) {
    document.body.classList.add('invert-flash-phase1');
    setTimeout(function() {
      document.body.classList.remove('invert-flash-phase1');
      if (thorough) {
        document.body.classList.add('invert-flash-phase2');
        setTimeout(function() {
          document.body.classList.remove('invert-flash-phase2');
        }, 60);
      }
    }, 110);
  }

  if (cleanScreenBtn) {
    cleanScreenBtn.addEventListener('click', function() {
      cleanScreen(true);
    });
  }

  // Auto Clean Ghosting Preference (Saved in localStorage)
  var autoCleanEnabled = localStorage.getItem('rove_eink_autoclean') !== '0';
  function updateAutoCleanUI() {
    if (autoCleanBtn) {
      if (autoCleanEnabled) {
        autoCleanBtn.classList.add('active-state');
        autoCleanBtn.textContent = 'Auto ↻ ON';
        autoCleanBtn.title = '自动除残影: 已开启 (状态变更时自动刷新)';
      } else {
        autoCleanBtn.classList.remove('active-state');
        autoCleanBtn.textContent = 'Auto ↻ OFF';
        autoCleanBtn.title = '自动除残影: 已关闭';
      }
    }
  }
  updateAutoCleanUI();
  if (autoCleanBtn) {
    autoCleanBtn.addEventListener('click', function() {
      autoCleanEnabled = !autoCleanEnabled;
      localStorage.setItem('rove_eink_autoclean', autoCleanEnabled ? '1' : '0');
      updateAutoCleanUI();
    });
  }

  // ==========================================================================
  // Dual-Theme Switching (Paper White ☼ vs Deep Ink ☾)
  // ==========================================================================
  var savedTheme = localStorage.getItem('rove_eink_theme') || 'light';
  function applyTheme(theme) {
    rootEl.setAttribute('data-theme', theme);
    if (themeBtn) {
      themeBtn.textContent = theme === 'dark' ? '☾ 墨黑' : '☼ 纸白';
    }
  }
  applyTheme(savedTheme);

  function toggleTheme() {
    var nextTheme = rootEl.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    localStorage.setItem('rove_eink_theme', nextTheme);
    applyTheme(nextTheme);
    cleanScreen(false);
  }
  if (themeBtn) {
    themeBtn.addEventListener('click', toggleTheme);
  }

  // ==========================================================================
  // Font Scaling Scale (13px, 15px, 17px, 19px) for E-ink Viewing Distance
  // ==========================================================================
  var FONT_SCALES = [13, 15, 17, 19, 21];
  var currentFontIndex = parseInt(localStorage.getItem('rove_eink_font_idx') || '1', 10);
  if (isNaN(currentFontIndex) || currentFontIndex < 0 || currentFontIndex >= FONT_SCALES.length) {
    currentFontIndex = 1; // Default 15px
  }

  function applyFontScale() {
    var size = FONT_SCALES[currentFontIndex];
    rootEl.style.setProperty('--font-scale', size + 'px');
    localStorage.setItem('rove_eink_font_idx', String(currentFontIndex));
  }
  applyFontScale();

  function adjustFont(delta) {
    var next = currentFontIndex + delta;
    if (next >= 0 && next < FONT_SCALES.length) {
      currentFontIndex = next;
      applyFontScale();
    }
  }
  if (fontDecBtn) fontDecBtn.addEventListener('click', function() { adjustFont(-1); });
  if (fontIncBtn) fontIncBtn.addEventListener('click', function() { adjustFont(1); });

  // ==========================================================================
  // Viewport Resolution Detection
  // ==========================================================================
  function updateViewportInfo() {
    if (viewportResEl) {
      var w = window.innerWidth || document.documentElement.clientWidth;
      var h = window.innerHeight || document.documentElement.clientHeight;
      viewportResEl.textContent = w + '×' + h + ' 视窗';
    }
  }
  window.addEventListener('resize', updateViewportInfo);
  updateViewportInfo();

  // Fullscreen on user tap
  document.addEventListener('click', function() {
    try {
      if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
        var p = document.documentElement.requestFullscreen();
        if (p && p.catch) p.catch(function() {});
      }
    } catch (e) {}
  }, { once: true });

  // ==========================================================================
  // Stopwatch Formatter
  // ==========================================================================
  function formatTime(secs) {
    if (secs == null || isNaN(secs) || secs < 0) return '00:00';
    var m = Math.floor(secs / 60);
    var s = Math.floor(secs % 60);
    return pad2(m) + ':' + pad2(s);
  }

  function updateStopwatch() {
    if (!startedTime) {
      if (stopwatch.textContent !== '--:--') stopwatch.textContent = '--:--';
      return;
    }
    var elapsed = Math.floor((Date.now() / 1000) - startedTime);
    var next = formatTime(elapsed);
    if (stopwatch.textContent !== next) stopwatch.textContent = next;
  }
  setInterval(updateStopwatch, 1000);

  // ==========================================================================
  // Hardware Physical Keyboard / Remote Clicker Navigation
  // ==========================================================================
  document.addEventListener('keydown', function(e) {
    var key = e.key.toLowerCase();
    // Approval keys
    if (activeApproval && (key === 'enter' || key === 'a')) {
      e.preventDefault();
      submitDecision('accept');
    } else if (activeApproval && (key === 'escape' || key === 'x')) {
      e.preventDefault();
      submitDecision('decline');
    } else if (key === 'r' || key === 'c') {
      e.preventDefault();
      cleanScreen(true);
    } else if (key === 't') {
      e.preventDefault();
      toggleTheme();
    } else if (key === '=' || key === '+') {
      e.preventDefault();
      adjustFont(1);
    } else if (key === '-' || key === '_') {
      e.preventDefault();
      adjustFont(-1);
    } else if (key === 'pageup' || key === 'k') {
      window.scrollBy({ top: -300, behavior: 'instant' });
    } else if (key === 'pagedown' || key === 'j') {
      window.scrollBy({ top: 300, behavior: 'instant' });
    }
  });

  // ==========================================================================
  // Event Filter Controls
  // ==========================================================================
  function setEventFilter(filter) {
    currentFilter = filter;
    if (filter === 'key') {
      filterKeyBtn.classList.add('active');
      filterAllBtn.classList.remove('active');
    } else {
      filterAllBtn.classList.add('active');
      filterKeyBtn.classList.remove('active');
    }
    renderEventsList(currentEventsList, true);
  }
  if (filterAllBtn) filterAllBtn.addEventListener('click', function() { setEventFilter('all'); });
  if (filterKeyBtn) filterKeyBtn.addEventListener('click', function() { setEventFilter('key'); });

  // ==========================================================================
  // Approval Decision Dispatcher
  // ==========================================================================
  function submitDecision(decision) {
    if (!activeApproval || !activeApproval.id) return;
    btnApprove.disabled = true;
    btnReject.disabled = true;
    btnApprove.textContent = '提交决策中…';
    btnReject.textContent = '提交决策中…';

    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/eink/approve?token=' + encodeURIComponent(token), true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = function() {
      if (xhr.readyState !== 4) return;
      btnApprove.disabled = false;
      btnReject.disabled = false;
      btnApprove.textContent = '【 ✓ 批准并执行 】';
      btnReject.textContent = '【 ✕ 拒绝取消 】';
      if (xhr.status >= 200 && xhr.status < 300) {
        approvalBox.style.display = 'none';
        stepDesc.textContent = '已提交决策 (' + (decision === 'accept' ? '已批准' : '已驳回') + ')，等待工作台执行…';
        activeApproval = null;
        cleanScreen(true);
      } else {
        var detail = xhr.statusText;
        try {
          var err = JSON.parse(xhr.responseText || '{}');
          if (err && err.detail) detail = err.detail;
        } catch (e) {}
        alert('审批提交失败: ' + detail);
      }
      fetchState();
    };
    xhr.onerror = function() {
      btnApprove.disabled = false;
      btnReject.disabled = false;
      btnApprove.textContent = '【 ✓ 批准并执行 】';
      btnReject.textContent = '【 ✕ 拒绝取消 】';
      alert('与 Mac mini 墨水台网络通信中断');
    };
    xhr.send(JSON.stringify({
      approval_id: activeApproval.id,
      decision: decision
    }));
  }

  btnApprove.addEventListener('click', function() { submitDecision('accept'); });
  btnReject.addEventListener('click', function() { submitDecision('decline'); });

  // Escape HTML helper
  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // ==========================================================================
  // Event Row Formatter & Incremental DOM Renderer
  // ==========================================================================
  function parseEventData(ev) {
    var ekind = ev.kind || 'system';
    var ep = ev.payload || {};
    var label = '事件';
    var bodyText = '';
    var rowClass = ekind;
    var isKey = false;

    if (ekind === 'user') {
      label = '任务提示';
      var rawUser = ep.text || '';
      bodyText = rawUser.replace(/<!--\s*WORKSTATION_BRIDGE_START[\s\S]*?WORKSTATION_BRIDGE_END\s*-->/g, '').trim();
      rowClass = 'user';
      isKey = true;
    } else if (ekind === 'antigravity') {
      var sk = ep.step_kind;
      if (sk === 'reasoning') {
        label = '思考摘要';
        bodyText = (ep.text || '').slice(0, 260) + (ep.text && ep.text.length > 260 ? '…' : '');
        rowClass = 'reasoning';
      } else if (sk === 'command' || sk === 'tool') {
        label = '指令 / 工具';
        bodyText = ep.command || ep.title || ep.detail || '';
        rowClass = 'terminal';
        isKey = true;
      } else if (sk === 'change') {
        label = '代码改写';
        bodyText = (ep.changes || []).map(function(c) { return '• ' + (c.path || ''); }).join('\n');
        rowClass = 'change';
        isKey = true;
      } else if (sk === 'agent') {
        label = '回复产出';
        bodyText = (ep.text || '').slice(0, 240);
        isKey = true;
      }
    } else if (ekind === 'codex') {
      var m = ep.method || '';
      var v = ep.params || {};
      var itm = v.item || {};
      var itmType = itm.type || '';
      if (m.indexOf('reasoning') !== -1 || itmType === 'reasoning') {
        label = 'Codex 思考';
        var rawText = v.delta || itm.text || itm.summary || '';
        bodyText = Array.isArray(rawText) ? rawText.join('\n') : (typeof rawText === 'string' ? rawText : String(rawText || ''));
        rowClass = 'reasoning';
      } else if (m.indexOf('commandExecution') !== -1 || itmType === 'commandExecution') {
        label = 'Codex 终端';
        var rawCmd = v.delta || itm.command || itm.aggregatedOutput || '';
        bodyText = Array.isArray(rawCmd) ? rawCmd.join('\n') : (typeof rawCmd === 'string' ? rawCmd : String(rawCmd || ''));
        rowClass = 'terminal';
        isKey = true;
      } else if (m.indexOf('agentMessage') !== -1 || itmType === 'agentMessage') {
        label = 'Codex 回复';
        var rawMsg = v.delta || itm.text || '';
        bodyText = Array.isArray(rawMsg) ? rawMsg.join('\n') : (typeof rawMsg === 'string' ? rawMsg : String(rawMsg || ''));
        isKey = true;
      }
    } else if (ekind === 'reasoning') {
      label = '思考摘要';
      bodyText = (ep.text || '').slice(0, 240);
      rowClass = 'reasoning';
    } else if (ekind === 'terminal') {
      label = '终端输出';
      bodyText = (ep.data || '').trim().slice(-220);
      rowClass = 'terminal';
    } else if (ekind === 'approval') {
      label = '审批动作';
      bodyText = ep.params ? (ep.params.command || ep.params.reason || '等待批准') : '已记录授权请求';
      rowClass = 'approval';
      isKey = true;
    } else if (ekind === 'agent') {
      label = '回复产出';
      bodyText = (ep.text || '').slice(0, 240);
      isKey = true;
    }

    var textStr = typeof bodyText === 'string' ? bodyText : (Array.isArray(bodyText) ? bodyText.join('\n') : String(bodyText || ''));
    return {
      id: ev.id,
      label: label,
      text: textStr.trim(),
      rowClass: rowClass,
      created: ev.created,
      isKey: isKey
    };
  }

  function renderEventsList(events, forceFullRebuild) {
    if (!events || events.length === 0) return;
    currentEventsList = events;

    var filtered = [];
    for (var i = 0; i < events.length; i++) {
      var parsed = parseEventData(events[i]);
      if (parsed.text) {
        if (currentFilter === 'key' && !parsed.isKey) continue;
        filtered.push(parsed);
      }
    }

    var displaySlice = filtered.slice(-10);
    var html = '';
    for (var j = 0; j < displaySlice.length; j++) {
      var item = displaySlice[j];
      var timeStr = '';
      if (item.created) {
        var dt = new Date(item.created * 1000);
        timeStr = pad2(dt.getHours()) + ':' + pad2(dt.getMinutes()) + ':' + pad2(dt.getSeconds());
      }
      html += '<div class="event-row ' + item.rowClass + '" data-evid="' + item.id + '">' +
        '<div class="event-header">' +
          '<span class="event-label">' + item.label + '</span>' +
          (timeStr ? '<span class="event-timestamp">' + timeStr + '</span>' : '') +
        '</div>' +
        '<div class="event-text">' + escapeHtml(item.text) + '</div>' +
      '</div>';
    }

    if (eventsStream.innerHTML !== html) {
      eventsStream.innerHTML = html;
    }
  }

  // ==========================================================================
  // Core State Render Machine
  // ==========================================================================
  function renderState(data) {
    if (!data) return;

    var job = data.job;
    var isWaiting = job && job.status === 'waiting';
    var isRunning = job && job.status === 'running';
    var isQueued = job && job.status === 'queued';
    var active = Boolean(data.active || isWaiting || isRunning || isQueued);
    var currentStatus = job ? job.status : 'idle';

    // Auto-clean screen on state transition to eliminate ghosting
    if (prevJobStatus !== currentStatus) {
      if (prevJobStatus && autoCleanEnabled) {
        cleanScreen(true);
      }
      prevJobStatus = currentStatus;
    }

    // Digest comparison to prevent unneeded E-ink repainting
    var evts = data.events || [];
    var lastEvtId = evts.length ? evts[evts.length - 1].id : 0;
    var currentDigest = [
      active ? '1' : '0',
      data.session_id || '',
      data.session_title || '',
      currentStatus,
      data.project_name || '',
      data.git_branch || '',
      lastEvtId,
      data.approval ? data.approval.id : 'no_appr'
    ].join('|');

    if (lastStateDigest === currentDigest && !active) {
      return;
    }
    lastStateDigest = currentDigest;

    // Header context chips
    var engine = (data.engine || 'codex').toUpperCase();
    if (engineBadge.textContent !== engine) engineBadge.textContent = engine;

    if (data.project_name) {
      projectBadge.style.display = 'inline-flex';
      projectBadge.textContent = data.project_name;
    } else {
      projectBadge.style.display = 'none';
    }

    if (data.git_branch) {
      branchBadge.style.display = 'inline-flex';
      branchBadge.textContent = '⎇ ' + data.git_branch;
    } else {
      branchBadge.style.display = 'none';
    }

    var displayTitle = data.session_title || '未命名工作台任务';
    if (sessionTitle.textContent !== displayTitle) {
      sessionTitle.textContent = displayTitle;
      sessionTitle.title = displayTitle;
    }

    // Stopwatch tracking
    if (job && job.started) {
      startedTime = job.started;
    } else {
      startedTime = null;
    }

    // Status pill & Step tag states
    if (isWaiting) {
      statusText.textContent = '等待审批确认';
      statusBadge.style.color = 'var(--text-primary)';
      stepTag.textContent = '审批';
      stepTag.style.background = 'var(--surface-inverse)';
      pollingTag.textContent = '审批等待';
    } else if (isRunning) {
      statusText.textContent = '执行推演中';
      stepTag.textContent = 'RUN';
      stepTag.style.background = 'var(--surface-inverse)';
      pollingTag.textContent = '高频同步';
    } else if (isQueued) {
      statusText.textContent = '排队就绪';
      stepTag.textContent = 'QUEUE';
      stepTag.style.background = 'var(--border-medium)';
      pollingTag.textContent = '排队中';
    } else {
      statusText.textContent = '就绪待命';
      stepTag.textContent = 'IDLE';
      stepTag.style.background = 'var(--border-medium)';
      stepDesc.textContent = 'Mac mini 工作台空闲，随时可由主端派发任务';
      stepDetail.style.display = 'none';
      pollingTag.textContent = '低频节能';
    }

    // Handle Approvals
    if (data.approval && isWaiting) {
      var prevApprId = activeApproval ? activeApproval.id : null;
      activeApproval = data.approval;
      approvalBox.style.display = 'flex';

      var method = activeApproval.method || '';
      var typeLabel = '系统安全确认';
      if ((method.indexOf('command') !== -1) || method === 'antigravity/permission') {
        typeLabel = '系统命令执行';
      } else if ((method.indexOf('file') !== -1) || (method.indexOf('Patch') !== -1)) {
        typeLabel = '文件 / 代码改写';
      }
      approvalType.textContent = typeLabel;
      approvalCommand.textContent = activeApproval.command || activeApproval.reason || '允许 AI 引擎进行该回合操作？';
      approvalCwd.textContent = activeApproval.cwd ? ('工作目录: ' + activeApproval.cwd) : '工作目录: 默认根路径';

      if (activeApproval.created) {
        var appDt = new Date(activeApproval.created * 1000);
        approvalTimeTag.textContent = pad2(appDt.getHours()) + ':' + pad2(appDt.getMinutes()) + ':' + pad2(appDt.getSeconds());
      } else {
        approvalTimeTag.textContent = '刚刚';
      }

      // If a brand new approval intercept appears, run deep clean immediately
      if (prevApprId !== activeApproval.id) {
        cleanScreen(true);
      }
    } else {
      activeApproval = null;
      approvalBox.style.display = 'none';
    }

    // Current running step description
    if (evts.length > 0 && isRunning) {
      var latest = evts[evts.length - 1];
      var p = latest.payload || {};
      var kind = latest.kind;

      if (kind === 'antigravity') {
        var sk = p.step_kind;
        if (sk === 'reasoning') {
          stepDesc.textContent = 'Antigravity 深度思考推演中…';
          if (p.text) {
            stepDetail.style.display = 'block';
            stepDetail.textContent = p.text.trim().slice(-300);
          }
        } else if (sk === 'command' || sk === 'tool') {
          stepDesc.textContent = 'Antigravity 执行: ' + (p.title || p.command || '系统命令');
          if (p.command || p.detail) {
            stepDetail.style.display = 'block';
            stepDetail.textContent = (p.command || p.detail).trim().slice(-300);
          }
        } else if (sk === 'change') {
          stepDesc.textContent = 'Antigravity 写入代码文件变更…';
          var changeList = p.changes || [];
          var paths = changeList.map(function(c) { return '• ' + (c.path || ''); }).join('\n');
          if (paths) {
            stepDetail.style.display = 'block';
            stepDetail.textContent = paths;
          }
        }
      } else if (kind === 'codex') {
        var m = p.method || '';
        if (m.indexOf('reasoning') !== -1) {
          stepDesc.textContent = 'Codex 架构思考推演中…';
        } else if (m.indexOf('commandExecution') !== -1) {
          stepDesc.textContent = 'Codex 执行终端指令中…';
        } else if (m.indexOf('fileChange') !== -1 || m.indexOf('patch') !== -1) {
          stepDesc.textContent = 'Codex 写入代码文件中…';
        }
      } else if (kind === 'terminal') {
        stepDesc.textContent = '终端命令执行输出';
        if (p.data) {
          stepDetail.style.display = 'block';
          stepDetail.textContent = p.data.trim().slice(-300);
        }
      } else if (kind === 'user') {
        stepDesc.textContent = '主端派发了新任务提示';
        stepDetail.style.display = 'none';
      }
    }

    // Render Event stream list
    renderEventsList(evts, false);

    var d = new Date();
    lastSyncTime.textContent = '同步: ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  // ==========================================================================
  // Adaptive Power-Saving Polling Engine
  // ==========================================================================
  function scheduleNextPoll(delayMs) {
    if (pollTimeoutId) clearTimeout(pollTimeoutId);
    pollTimeoutId = setTimeout(fetchState, delayMs);
  }

  function fetchState() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/eink/state?token=' + encodeURIComponent(token), true);
    xhr.onreadystatechange = function() {
      if (xhr.readyState !== 4) return;
      if (xhr.status === 401) {
        statusText.textContent = 'Token 无效';
        stepDesc.textContent = '墨水屏授权 Token 无效或已重置，请在 Mac mini 查看 watch_token';
        pollingTag.textContent = '已停机';
        return;
      }

      var nextDelay = 3000;
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          var data = JSON.parse(xhr.responseText);
          renderState(data);

          // Calculate adaptive delay based on state & visibility
          if (document.hidden) {
            nextDelay = 30000; // Background sleep
            pollingTag.textContent = '熄屏休眠 30s';
          } else if (data.approval && data.job && data.job.status === 'waiting') {
            nextDelay = 2000; // Immediate response for human approvals
            pollingTag.textContent = '审批响应 2s';
          } else if (data.active || (data.job && data.job.status === 'running')) {
            nextDelay = 3000; // Active execution
            pollingTag.textContent = '实时同步 3s';
          } else {
            nextDelay = 10000; // Idle power saving mode
            pollingTag.textContent = '节能待机 10s';
          }
        } catch (e) {
          nextDelay = 5000;
        }
      } else {
        statusText.textContent = '网络重连中';
        pollingTag.textContent = '离线退避';
        nextDelay = 6000;
      }

      scheduleNextPoll(nextDelay);
    };

    xhr.onerror = function() {
      statusText.textContent = '网络重连中';
      pollingTag.textContent = '网络故障';
      scheduleNextPoll(8000);
    };

    xhr.send();
  }

  // Handle visibility change for battery savings on e-readers
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) {
      fetchState();
    }
  });

  // Start initial polling
  fetchState();
})();
