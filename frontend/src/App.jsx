import {readHistory, writeHistory} from './historyCache';
import {appendEvents} from './eventParser';
import {lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  Code2, SquareTerminal, SquarePen, Search, Settings, Menu, X, Folder, Plus,
  PanelLeftClose, PanelLeftOpen, ChevronDown, ArrowUpRight, Pin, Pencil, Trash2,
  Download, Sun, Moon, Monitor, MessageSquare, Check, Copy, Layers, RefreshCw, Bot, Zap, Smartphone,
  ShieldCheck, Hourglass, Sparkles, CheckCircle2, AlertCircle, Brain, GitBranch, MoreHorizontal,
  Lock, Wifi,
} from 'lucide-react';
import {
  api, post, patch, del, base, requestId, statusText, activeStates,
  downloadMarkdown, effortBadge, modeBadge, STATE_CACHE_KEY, clearCachedState, probeCloudflareAccessRedirect,
  purgeCacheAndReload, revertJobCheckpoint, promoteScratchSession,
  getSessionQueue, enqueueSessionMessage, updateQueuedMessage, cancelQueuedMessage,
  pauseSessionQueue, getHandoffPreview, getWorkstationDiagnostics,
  startArenaRun, getArenaStatus, adoptArenaWinner,
  getSessionOrchestrations, compactSessionContext,
} from './api';
import {usePwaManager, clearAppBadgeSafe} from './pwa';
import {deleteHistory} from './historyCache';
import Conversation, {parseEvents, buildMarkdownTranscript} from './Conversation';
const SettingsPanel = lazy(() => import('./SettingsPanel'));
const QuotaModal = lazy(() => import('./QuotaModal'));
const ProjectMemoriesModal = lazy(() => import('./ProjectMemoriesModal'));
const WorktreeModal = lazy(() => import('./WorktreeModal'));
const PermissionModal = lazy(() => import('./PermissionModal'));
import InstallBanner from './InstallBanner';
const InstallGuideModal = lazy(() => import('./InstallGuideModal'));
import Login from './Login';
import RoveMark from './RoveMark';
import {acquireWakeLock, releaseWakeLock} from './wakelock';

const TerminalPane = lazy(() => import('./TerminalPane'));
const CodePane = lazy(() => import('./CodePane'));
const PreviewPane = lazy(() => import('./PreviewPane'));

const ALL_PROJECTS = '__all__';

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem('walking-workstation:prefs') || '{}');
  } catch {
    return {};
  }
}

function loadCachedState() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STATE_CACHE_KEY) || 'null');
    if (parsed && Array.isArray(parsed.projects) && Array.isArray(parsed.sessions) && parsed.engines) {
      return parsed;
    }
  } catch {}
  return null;
}

function saveCachedState(result) {
  try {
    const compact = {
      ...result,
      _cached_at: Date.now(),
      sessions: (result.sessions || []).slice(0, 120),
      jobs: (result.jobs || []).slice(0, 60),
    };
    localStorage.setItem(STATE_CACHE_KEY, JSON.stringify(compact));
  } catch {}
}

function hasSignificantStateChange(prev, next) {
  if (!prev || !next) return true;
  if (JSON.stringify(prev.catalog) !== JSON.stringify(next.catalog)) return true;
  if (prev.sessions?.length !== next.sessions?.length) return true;
  if (prev.jobs?.length !== next.jobs?.length) return true;
  if (prev.projects?.length !== next.projects?.length) return true;
  if (prev.agents?.length !== next.agents?.length) return true;
  if (Boolean(prev.codex_sync) !== Boolean(next.codex_sync)) return true;
  if (Boolean(prev.antigravity_sync) !== Boolean(next.antigravity_sync)) return true;
  if (prev.web_push?.enabled !== next.web_push?.enabled || prev.web_push?.subscription_count !== next.web_push?.subscription_count) return true;

  const pJ = prev.jobs || [], nJ = next.jobs || [];
  for (let i = 0; i < nJ.length; i++) {
    const nj = nJ[i], pj = pJ[i];
    if (!pj || nj.id !== pj.id || nj.status !== pj.status || nj.model !== pj.model) return true;
  }

  const pS = prev.sessions || [], nS = next.sessions || [];
  for (let i = 0; i < nS.length; i++) {
    const ns = nS[i], ps = pS[i];
    if (!ps ||
        ns.id !== ps.id ||
        ns.updated !== ps.updated ||
        ns.title !== ps.title ||
        ns.pinned !== ps.pinned ||
        ns.model !== ps.model ||
        ns.effort !== ps.effort ||
        ns.mode !== ps.mode ||
        ns.fast !== ps.fast ||
        ns.engine !== ps.engine ||
        ns.execution_target !== ps.execution_target ||
        ns.use_worktree !== ps.use_worktree ||
        ns.external_active !== ps.external_active ||
        ns.writer_locked !== ps.writer_locked) {
      return true;
    }
  }

  const pP = prev.projects || [], nP = next.projects || [];
  for (let i = 0; i < nP.length; i++) {
    const np = nP[i], pp = pP[i];
    if (!pp || np.id !== pp.id || np.name !== pp.name || np.branch !== pp.branch) return true;
  }
  return false;
}

function loadBootstrap() {
  const cached = loadCachedState();
  try {
    const boot = typeof window !== 'undefined' ? window.__ROVE_BOOTSTRAP__ : null;
    if (boot && typeof boot === 'object') {
      const bootTs = (boot.ts || 0) * 1000;
      const cachedTs = cached?._cached_at || 0;
      // If cached state from previous user operations is fresher than the cached HTML shell's bootstrap, prefer cached
      if (cached && cachedTs > bootTs && cachedTs - bootTs > 5000) {
        return {locked: false, state: cached, fromServer: false};
      }
      if (boot.state && Array.isArray(boot.state.projects) && Array.isArray(boot.state.sessions) && boot.state.engines) {
        saveCachedState(boot.state);
        return {locked: false, state: boot.state, fromServer: true};
      }
      if (boot.locked === true) {
        return {locked: true, state: null, fromServer: true};
      }
    }
  } catch {}
  return {locked: cached ? false : null, state: cached, fromServer: false};
}

export default function App() {
  const initialPrefs = useMemo(loadPrefs, []);
  const bootstrap = useMemo(loadBootstrap, []);
  const initialCachedState = bootstrap.state;
  const [themeMode, setThemeMode] = useState(() => {
    try {return localStorage.getItem('walking-workstation:theme') || 'system';} catch {return 'system';}
  });
  const [systemDark, setSystemDark] = useState(() => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false);

  const [locked, setLocked] = useState(() => bootstrap.locked);
  const [state, setState] = useState(() => initialCachedState);
  const [reachable, setReachable] = useState(() => Boolean(bootstrap.fromServer || !initialCachedState));
  const [bootError, setBootError] = useState('');
  const [bootElapsed, setBootElapsed] = useState(false);
  const [cfAuthNeeded, setCfAuthNeeded] = useState(false);

  const [projectFilter, setProjectFilter] = useState(ALL_PROJECTS);
  const [projectId, setProjectId] = useState('');
  const [selected, setSelected] = useState('');
  const [historyPage, setHistoryPage] = useState(null);
  const [historyBusy, setHistoryBusy] = useState(false);
  const historySessionRef = useRef(selected);
  historySessionRef.current = selected;
  const [showAutomations, setShowAutomations] = useState(false);
  const [syncingCodex, setSyncingCodex] = useState(false);
  const [engine, setEngine] = useState(initialPrefs.engine || 'codex');
  const [model, setModel] = useState(initialPrefs.model || 'gpt-6.1-sol');
  const [effort, setEffort] = useState(initialPrefs.effort || 'medium');
  const [mode, setMode] = useState(initialPrefs.mode || 'standard');
  const [fast, setFast] = useState(Boolean(initialPrefs.fast));

  const [inspectTarget, setInspectTarget] = useState('');

  const [tab, setTab] = useState('chat');
  const [drawer, setDrawer] = useState(false);
  const [settings, setSettings] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [projectsOpen, setProjectsOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState('');

  const [editingId, setEditingId] = useState('');
  const [editingTitle, setEditingTitle] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState('');
  const [copiedNative, setCopiedNative] = useState(false);
  const [quotedRef, setQuotedRef] = useState(null);
  const [lastPrompt, setLastPrompt] = useState('');
  const [sessionQueue, setSessionQueue] = useState([]);
  const [queuePaused, setQueuePaused] = useState(false);
  const [supportsLiveSteer, setSupportsLiveSteer] = useState(false);
  const [sendStatus, setSendStatus] = useState(null);
  const [failedOutbox, setFailedOutbox] = useState(null);
  const [handoffPreview, setHandoffPreview] = useState(null);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [diagnosticsData, setDiagnosticsData] = useState(null);
  const [arenaOpen, setArenaOpen] = useState(false);
  const [arenaPrompt, setArenaPrompt] = useState('');
  const [arenaEngineA, setArenaEngineA] = useState('codex');
  const [arenaEngineB, setArenaEngineB] = useState('antigravity');
  const [arenaRun, setArenaRun] = useState(null);
  const [arenaBusy, setArenaBusy] = useState(false);
  const [quotaOpen, setQuotaOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [worktreeOpen, setWorktreeOpen] = useState(false);
  const [modeModalOpen, setModeModalOpen] = useState(false);
  const [installGuideOpen, setInstallGuideOpen] = useState(false);
  const [enableWakeLock, setEnableWakeLock] = useState(() => {
    try {
      const v = localStorage.getItem('walking-workstation:enable-wake-lock');
      return v !== null ? v === 'true' : true;
    } catch {
      return true;
    }
  });

  const [error, setError] = useState('');
  const [events, setEvents] = useState([]);
  const [connected, setConnected] = useState(false);
  const [sending, setSending] = useState(false);

  const pendingRequest = useRef(null);
  if (!pendingRequest.current) {
    try { pendingRequest.current = JSON.parse(sessionStorage.getItem('rove-pending-request') || 'null'); } catch {}
  }
  const initialized = useRef(false);
  const refreshInFlight = useRef(null);
  const searchInput = useRef(null);
  const composerInputRef = useRef(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  const lastSavedTime = useRef(0);

  const darkTheme = themeMode === 'dark' || (themeMode === 'system' && systemDark);

  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
    if (!mq) return;
    const handler = e => setSystemDark(e.matches);
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = darkTheme ? 'dark' : 'light';
    const color = darkTheme ? '#0d0f13' : '#ffffff';
    document.querySelectorAll('meta[name="theme-color"]').forEach(meta => {
      meta.setAttribute('content', color);
    });
    try {localStorage.setItem('walking-workstation:theme', themeMode);} catch {}
  }, [darkTheme, themeMode]);

  useEffect(() => {
    try {
      localStorage.setItem('walking-workstation:prefs', JSON.stringify({engine, model, effort, mode, fast}));
    } catch {}
  }, [engine, model, effort, mode, fast]);

  const onError = useCallback(text => setError(text), []);

  const syncControlsFromSession = useCallback((s, catalog) => {
    if (!s) return;
    const eng = s.engine || 'codex';
    setEngine(eng);
    if (eng === 'auto') {
      setModel(s.model || 'auto');
      setEffort(s.effort || 'auto');
      setMode(s.mode || 'standard');
      setFast(Boolean(s.fast));
      return;
    }
    const engineModels = catalog?.[eng]?.models || [];
    const otherModels = Object.entries(catalog || {})
      .filter(([k, v]) => k !== eng && Array.isArray(v?.models))
      .flatMap(([, v]) => v.models);
    const fallbackModel = engineModels.find(m => m.isDefault) || engineModels[0];
    // A model id that only exists in another engine's catalog (stale relay state) must not be shown or sent.
    const foreignModel = Boolean(s.model)
      && s.model !== 'auto'
      && !engineModels.some(m => m.id === s.model)
      && otherModels.some(m => m.id === s.model);
    const resolvedModel = s.model === 'auto'
      ? 'auto'
      : ((foreignModel ? '' : s.model) || fallbackModel?.id || '');
    const modelObj = engineModels.find(m => m.id === resolvedModel) || fallbackModel;
    setModel(resolvedModel);
    const resolvedEffort = s.effort === 'auto'
      ? 'auto'
      : ((foreignModel ? '' : s.effort) || modelObj?.defaultEffort || 'medium');
    setEffort(resolvedEffort);
    setMode(s.mode || 'standard');
    setFast(Boolean(s.fast));
  }, []);

  const hydrateInitialSelection = useCallback(result => {
    if (initialized.current || !result) return;
    initialized.current = true;
    clearAppBadgeSafe();

    const params = new URLSearchParams(window.location.search);
    const urlSession = params.get('session');
    const urlAction = params.get('action');
    const urlTab = params.get('tab');
    const urlModal = params.get('modal');
    const urlCfAuth = params.get('cf_auth');
    if (urlSession || urlAction || urlTab || urlModal || urlCfAuth) {
      try {
        window.history.replaceState({}, '', window.location.pathname);
      } catch {}
    }

    const defProj = result.projects.find(p => p.name === '兴趣项目')
      || result.projects.find(p => p.name === 'mobile-workstation')
      || result.projects[0];

    if (urlAction === 'new') {
      setProjectId(defProj?.id || '');
      setSelected('');
      setTab('chat');
    } else {
      let previous;
      try {previous = localStorage.getItem('walking-workstation:last-session');} catch {}
      const nonAuto = result.sessions.filter(item => item.thread_kind !== 'automation');
      const target = (urlSession && result.sessions.find(item => item.id === urlSession))
        || result.sessions.find(item => item.id === previous)
        || nonAuto[0]
        || result.sessions[0];
      if (target) {
        setProjectId(target.project_id);
        setSelected(target.id);
        syncControlsFromSession(target, result.catalog);
      } else {
        setProjectId(defProj?.id || '');
      }
    }

    if (urlTab === 'terminal' || urlTab === 'code' || urlTab === 'chat') {
      setTab(urlTab);
    }
    if (urlModal === 'quota') {
      setQuotaOpen(true);
    }
  }, [syncControlsFromSession]);

  useEffect(() => {
    if (initialCachedState) {
      hydrateInitialSelection(initialCachedState);
    }
  }, [hydrateInitialSelection, initialCachedState]);

  useEffect(() => {
    if (locked === null || !state) {
      const timer = setTimeout(() => setBootElapsed(true), 3200);
      return () => clearTimeout(timer);
    }
  }, [locked, state]);

  const handleNetworkFailure = useCallback(async err => {
    if (err?.status === 401) {
      setLocked(true);
      return;
    }
    if (err?.isAccessAuth) { setCfAuthNeeded(true); setReachable(false); return; }
    setReachable(false);
    setBootError(err?.message || '连接 Mac mini 超时');
    const needsCf = await probeCloudflareAccessRedirect();
    if (needsCf) {
      setCfAuthNeeded(true);
    }
  }, []);

  const refresh = useCallback(async () => {
    if (refreshInFlight.current) return refreshInFlight.current;
    const task = (async () => {
      const result = await api('state');
      const changed = hasSignificantStateChange(stateRef.current, result);
      if (changed) {
        setState(result);
      }
      const now = Date.now();
      if (!lastSavedTime.current || now - lastSavedTime.current > 3000) {
        saveCachedState(result);
        lastSavedTime.current = now;
      }
      setLocked(false);
      setReachable(true);
      setBootError('');
      setCfAuthNeeded(false);
      hydrateInitialSelection(result);
      return result;
    })();
    refreshInFlight.current = task;
    try {
      return await task;
    } finally {
      refreshInFlight.current = null;
    }
  }, [hydrateInitialSelection]);

  const pwa = usePwaManager(state?.web_push, refresh);

  useEffect(() => {
    if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
    function onSwMessage(event) {
      if (event.data?.type !== 'WORKSTATION_OPEN_TARGET') return;
      clearAppBadgeSafe();
      const targetId = event.data.sessionId;
      refresh().then(() => {
        if (targetId) {
          setSelected(targetId);
          setTab('chat');
        }
        setSettings(false);
        setQuotaOpen(false);
        setDrawer(false);
      }).catch(() => {});
    }
    navigator.serviceWorker.addEventListener('message', onSwMessage);
    return () => navigator.serviceWorker.removeEventListener('message', onSwMessage);
  }, [refresh]);

  useEffect(() => {
    function lock() {
      setLocked(true);
    }
    window.addEventListener('workstation-locked', lock);
    if (bootstrap.locked !== true) {
      refresh().catch(handleNetworkFailure);
    }
    return () => window.removeEventListener('workstation-locked', lock);
  }, [bootstrap.locked, handleNetworkFailure, refresh]);

  useEffect(() => {
    if (locked === true) return;
    let cancelled = false;
    let timer = null;

    async function pollLoop() {
      if (cancelled) return;
      if (document.visibilityState === 'visible') {
        try {
          await refresh();
        } catch (e) {
          if (!cancelled) {
            await handleNetworkFailure(e);
            if (stateRef.current) setError(e.message);
          }
        }
      }
      if (!cancelled) {
        const hasActiveWork = (stateRef.current?.jobs || []).some(j => activeStates.has(j.status));
        const delay = connected ? 12000 : (hasActiveWork ? 2500 : 8000);
        timer = setTimeout(pollLoop, delay);
      }
    }

    timer = setTimeout(pollLoop, 2500);
    let visTimer = null;
    function visible() {
      if (document.visibilityState === 'visible') {
        clearAppBadgeSafe();
        clearTimeout(visTimer);
        visTimer = setTimeout(() => {
          if (!cancelled && document.visibilityState === 'visible') {
            refresh().catch(err => {
              setReachable(false);
              probeCloudflareAccessRedirect().then(needsCf => {
                if (needsCf) setCfAuthNeeded(true);
              });
            });
          }
        }, 300);
      }
    }
    document.addEventListener('visibilitychange', visible);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (visTimer) clearTimeout(visTimer);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [connected, handleNetworkFailure, locked, refresh]);

  useEffect(() => {
    setEvents([]);
    setHistoryPage(null);
    setError('');
    setConnected(false);
    if (!selected || locked) return;
    let dead = false, ws, timer, watchdog, cursor = 0;
    let lastMessage = Date.now(), openedAt = 0, failures = 0;
    let polling = false, cacheTimer;
    let history = [];
    let historyIds = new Set();
    function store(next, nextCursor, appended = false) {
      history = next;
      if (!appended) historyIds = new Set(next.map(event => event.id));
      cursor = nextCursor;
      setEvents(next);
      clearTimeout(cacheTimer);
      cacheTimer = setTimeout(() => {
        if (!dead) writeHistory(selected, {events: history, cursor, saved: Date.now()});
      }, 1500);
    }
    function accept(batch) {
      if (!batch?.length) return;
      const fresh = batch.filter(event => {
        if (historyIds.has(event.id)) return false;
        historyIds.add(event.id);
        return true;
      });
      store(appendEvents(history, fresh), Math.max(cursor, batch.at(-1).id), true);
      if (batch.some(e => (e.kind === 'job_status' || e.kind === 'approval'))) refresh().catch(() => {});
    }
    async function poll() {
      if (dead || polling || document.hidden) return;
      polling = true;
      try {
        const batch = await api(`sessions/${selected}/events?after=${cursor}`);
        if (!dead) { accept(batch); setConnected(true); }
      } catch (error) { if (!dead) handleNetworkFailure(error); }
      finally {
        polling = false;
        if (!dead) timer = setTimeout(poll, 2500 + Math.random() * 500);
      }
    }
    function fallback() {
      if (dead) return;
      ws.onclose = null;
      ws.close();
      clearInterval(watchdog);
      setConnected(false);
      poll();
    }
    function connect() {
      if (dead) return;
      const url = new URL(`api/sessions/${selected}/stream?after=${cursor}`, base);
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      openedAt = 0;
      lastMessage = Date.now();
      ws = new WebSocket(url);
      ws.onopen = () => { openedAt = Date.now(); setConnected(true); };
      ws.onmessage = event => {
        if (dead) return;
        lastMessage = Date.now();
        try {
          const msg = JSON.parse(event.data);
          if (msg.reset && msg.snapshot) {
            historyIds.clear();
            for (const ev of (msg.snapshot.events || [])) historyIds.add(ev.id);
            setHistoryPage(msg.snapshot);
            store(msg.snapshot.events || [], msg.snapshot.cursor || 0, true);
            return;
          }
          accept(msg.events);
        } catch { fallback(); }
      };
      ws.onclose = event => {
        if (dead) return;
        setConnected(false);
        clearInterval(watchdog);
        if (event.code === 4401) { setLocked(true); return; }
        if (!openedAt || Date.now() - openedAt < 10000) failures++;
        else failures = 0;
        if (failures >= 2) { poll(); return; }
        timer = setTimeout(connect, 1000 + Math.random() * 1000);
      };
      watchdog = setInterval(() => {
        if (!document.hidden && Date.now() - lastMessage > (openedAt ? 35000 : 5000)) fallback();
      }, 1000);
    }
    async function boot() {
      const cached = await Promise.race([readHistory(selected), new Promise(resolve => setTimeout(() => resolve(null), 300))]);
      if (dead) return;
      if (cached) { store(cached.events, cached.cursor); }
      try {
        const snapshot = await api(`sessions/${selected}/snapshot`);
        if (dead) return;
        store(snapshot.events, snapshot.cursor);
        setHistoryPage(snapshot);
        connect();
      } catch (error) {
        if (!dead) { handleNetworkFailure(error); poll(); }
      }
    }
    function visible() {
      if (document.hidden || dead) return;
      if (ws?.readyState === WebSocket.OPEN && Date.now() - lastMessage < 15000) return;
      clearTimeout(timer);
      if (ws && ws.readyState !== WebSocket.CLOSED) fallback();
      else poll();
    }
    boot();
    const upgradeTimer = setInterval(() => {
      if (!dead && !document.hidden && failures >= 2 && ws?.readyState === WebSocket.CLOSED) connect();
    }, 60000);
    function prepend(event) {
      if (event.detail.session !== selected) return;
      const ids = new Set(history.map(e => e.id));
      store([...event.detail.events.filter(e => !ids.has(e.id)), ...history], cursor);
    }
    window.addEventListener('rove-older-history', prepend);
    document.addEventListener('visibilitychange', visible);
    return () => {
      dead = true;
      clearInterval(upgradeTimer);
      window.removeEventListener('rove-older-history', prepend);
      clearTimeout(timer);
      clearTimeout(cacheTimer);
      clearInterval(watchdog);
      document.removeEventListener('visibilitychange', visible);
      ws?.close();
    };
  }, [selected, locked]);

  useEffect(() => {
    if (selected) {
      try {localStorage.setItem('walking-workstation:last-session', selected);} catch {}
    }
  }, [selected]);

  useEffect(() => {
    if (searching) searchInput.current?.focus();
  }, [searching]);

  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape') {
        setDrawer(false);
        setSettings(false);
        setSearching(false);
        setEditingId('');
        setConfirmDeleteId('');
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  const current = state?.sessions.find(s => s.id === selected);
  const project = state?.projects.find(p => p.id === projectId);
  const activeProject = state?.projects.find(p => p.id === (current?.project_id || projectId)) || project || state?.projects?.[0];
  const job = state?.jobs.find(j => j.session_id === selected);
  const active = Boolean(job && activeStates.has(job.status));
  const catalog = state?.catalog;

  useEffect(() => {
    try {
      localStorage.setItem('walking-workstation:enable-wake-lock', String(enableWakeLock));
    } catch {}
  }, [enableWakeLock]);

  useEffect(() => {
    const shouldLock = enableWakeLock && active;
    if (shouldLock) {
      acquireWakeLock();
    } else {
      releaseWakeLock();
    }
  }, [active, enableWakeLock]);

  function applyEngineDefaults(nextEngine) {
    const wasAutoEngine = engine === 'auto';
    setEngine(nextEngine);
    if (nextEngine === 'auto') {
      setModel('auto');
      setEffort('auto');
      setFast(false);
      return;
    }
    const list = catalog?.[nextEngine]?.models || [];
    const def = list.find(m => m.isDefault) || list[0];
    if (def) {
      if (wasAutoEngine || model !== 'auto') {
        setModel(def.id);
      }
      if (wasAutoEngine || effort !== 'auto') {
        setEffort(def.defaultEffort || 'medium');
      }
      if (!def.supportsFast) setFast(false);
    } else if (wasAutoEngine) {
      setModel('');
      setEffort('medium');
    }
    if (nextEngine !== 'codex') setFast(false);
  }

  function newTask() {
    setSelected('');
    setTab('chat');
    setDrawer(false);
    setError('');
    setConfirmDeleteId('');
    pendingRequest.current = null;
  }

  function selectProjectFilter(id) {
    setProjectFilter(id);
    setProjectsOpen(false);
    if (id !== ALL_PROJECTS) {
      setProjectId(id);
      newTask();
    }
  }

  function selectComposerProject(id) {
    setProjectId(id);
    if (projectFilter !== ALL_PROJECTS) {
      setProjectFilter(id);
    }
  }

  async function quickSyncCodex() {
    if (syncingCodex) return;
    setSyncingCodex(true);
    try {
      await post('sync', {session_id: selected || null});
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setSyncingCodex(false);
    }
  }

  function selectSession(s) {
    setSelected(s.id);
    setProjectId(s.project_id);
    syncControlsFromSession(s, catalog);
    setTab('chat');
    setDrawer(false);
    setConfirmDeleteId('');
  }

  function changeEngine(value) {
    const wasAutoEngine = engine === 'auto';
    applyEngineDefaults(value);
    if (value === 'auto') {
      if (selected) {
        persistSessionPrefs({ engine: 'auto', model: 'auto', effort: 'auto', fast: false });
      } else {
        newTask();
      }
      return;
    }
    const list = catalog?.[value]?.models || [];
    const def = list.find(m => m.isDefault) || list[0];
    const nextModel = (!wasAutoEngine && model === 'auto') ? 'auto' : (def?.id || (model === 'auto' ? '' : model));
    const nextEffort = (!wasAutoEngine && effort === 'auto') ? 'auto' : (def?.defaultEffort || 'medium');
    if (selected) {
      persistSessionPrefs({ engine: value, model: nextModel, effort: nextEffort, ...(value !== 'codex' ? {fast: false} : {}) });
      if (events.length > 0 && nextModel && nextModel !== 'auto') {
        getHandoffPreview(selected, value, nextModel)
          .then(res => { if (res?.needed) setHandoffPreview(res); })
          .catch(() => {});
      }
    } else {
      newTask();
    }
  }

  async function persistSessionPrefs(updates) {
    if (!selected) return;
    try {
      await patch(`sessions/${selected}`, updates);
      refresh().catch(() => {});
    } catch {}
  }

  function handleModelChange(nextModel) {
    setModel(nextModel);
    if (nextModel === 'auto') {
      setFast(false);
      persistSessionPrefs({model: 'auto', fast: false});
      return;
    }
    const activeEngine = engine || current?.engine || 'codex';
    const list = catalog?.[activeEngine]?.models || [];
    const found = list.find(m => m.id === nextModel);
    let nextEffort = effort;
    let nextFast = fast;
    if (found) {
      if (activeEngine === 'antigravity' || (effort !== 'auto' && !found.efforts?.includes(effort))) {
        nextEffort = found.defaultEffort || found.efforts?.[0] || 'medium';
      }
      if (!found.supportsFast) nextFast = false;
    }
    setEffort(nextEffort);
    setFast(nextFast);
    persistSessionPrefs({model: nextModel, effort: nextEffort, fast: nextFast});
  }

  function handleEffortChange(nextEffort) {
    setEffort(nextEffort);
    persistSessionPrefs({effort: nextEffort});
  }

  function handleModeChange(nextMode) {
    setMode(nextMode);
    persistSessionPrefs({mode: nextMode});
  }

  function handleFastChange(nextFast) {
    setFast(nextFast);
    persistSessionPrefs({fast: nextFast});
  }

  const [orchestration, setOrchestration] = useState('off');
  const [orchestrationRuns, setOrchestrationRuns] = useState([]);

  useEffect(() => {
    setOrchestration(current?.orchestration || 'off');
  }, [current?.id, current?.orchestration]);

  function handleOrchestrationChange(nextOrch) {
    setOrchestration(nextOrch);
    persistSessionPrefs({orchestration: nextOrch});
  }

  const loadOrchestrations = useCallback(async () => {
    if (!selected) {
      setOrchestrationRuns([]);
      return;
    }
    try {
      const res = await getSessionOrchestrations(selected);
      setOrchestrationRuns(res?.runs || []);
    } catch {}
  }, [selected]);

  useEffect(() => {
    loadOrchestrations();
    if (!selected) return;
    const hasActiveRun = orchestrationRuns.some(r => !['completed', 'failed', 'cancelled'].includes(r.status));
    if (!hasActiveRun && orchestration === 'off') return;
    const timer = setInterval(loadOrchestrations, 3000);
    return () => clearInterval(timer);
  }, [loadOrchestrations, selected, events.length, orchestration]);

  async function createSession() {
    const result = await post('sessions', {
      project_id: projectId,
      engine,
      model,
      effort,
      mode,
      fast,
    });
    await refresh();
    setSelected(result.id);
    return result;
  }

  async function send(text, attachedImages = [], overrides = {}) {
    setSending(true);
    setError('');
    setSendStatus({state: 'sending'});
    const targetEngine = overrides.engine || engine;
    const targetModel = overrides.model || model;
    const targetEffort = overrides.effort || effort;
    try {
      const s = current || await createSession();
      if (!pendingRequest.current || pendingRequest.current.text !== text || pendingRequest.current.session !== s.id) {
        pendingRequest.current = {text, session: s.id, id: requestId()};
        try { sessionStorage.setItem('rove-pending-request', JSON.stringify(pendingRequest.current)); } catch {}
      }
      await post(`sessions/${s.id}/messages`, {
        text,
        request_id: pendingRequest.current.id,
        execution_target: overrides.execution_target || 'local',
        cloud_environment: overrides.cloud_environment || '',
        engine: targetEngine,
        model: targetModel,
        effort: targetEffort,
        mode,
        fast,
        orchestration: overrides.orchestration || orchestration,
        images: attachedImages.map(img => img.dataUrl),
      });
      await loadOrchestrations();
      pendingRequest.current = null;
      if (text && text.trim()) setLastPrompt(text);
      setFailedOutbox(null);
      setSendStatus({state: 'acked'});
      setTimeout(() => setSendStatus(prev => (prev?.state === 'acked' ? null : prev)), 2400);
      try { sessionStorage.removeItem('rove-pending-request'); } catch {}
      await refresh();
      return true;
    } catch (e) {
      setError(e.message);
      setFailedOutbox({text, attachedImages, overrides});
      setSendStatus({state: 'failed', error: e.message});
      return false;
    } finally {
      setSending(false);
    }
  }

  const loadQueue = useCallback(async () => {
    if (!selected) {
      setSessionQueue([]);
      setQueuePaused(false);
      return;
    }
    try {
      const res = await getSessionQueue(selected);
      setSessionQueue(res.queue || []);
      setQueuePaused(Boolean(res.paused));
      setSupportsLiveSteer(Boolean(res.supports_live_steer));
    } catch {}
  }, [selected]);

  useEffect(() => {
    loadQueue();
    if (!selected) return;
    const interval = setInterval(loadQueue, 5000);
    return () => clearInterval(interval);
  }, [loadQueue, job?.id, job?.status, selected]);

  async function handleEnqueue(text, steer = false) {
    if (!selected || !text) return;
    try {
      await enqueueSessionMessage(selected, {text, steer, engine, model, effort, mode, fast});
      await loadQueue();
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleUpdateQueue(queueId, text) {
    try {
      await updateQueuedMessage(queueId, {text});
      await loadQueue();
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleDeleteQueue(queueId) {
    try {
      await cancelQueuedMessage(queueId);
      await loadQueue();
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleToggleQueuePause() {
    if (!selected) return;
    try {
      await pauseSessionQueue(selected, !queuePaused);
      await loadQueue();
      await refresh();
    } catch (e) {
      setError(e.message);
    }
  }

  async function openDiagnostics() {
    setDiagnosticsOpen(true);
    try {
      const data = await getWorkstationDiagnostics();
      setDiagnosticsData(data);
    } catch (e) {
      setError(e.message);
    }
  }

  async function switchEngineAndRetry(targetEngine, promptText) {
    if (!promptText || sending || active) return;
    applyEngineDefaults(targetEngine);
    const list = catalog?.[targetEngine]?.models || [];
    const def = list.find(m => m.isDefault) || list[0];
    const nextModel = def?.id || model;
    const nextEffort = def?.defaultEffort || 'medium';
    await send(promptText, [], {
      engine: targetEngine,
      model: nextModel,
      effort: nextEffort,
    });
  }

  async function ensureTerminal() {
    try {
      if (!current) await createSession();
      setTab(v => v === 'terminal' ? 'chat' : 'terminal');
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleRevertJob(jobId) {
    if (!selected || !jobId) return;
    try {
      let res = await revertJobCheckpoint(selected, jobId, false);
      if (res?.requires_confirm) {
        if (!window.confirm(res.message || '检测到后续改动或并发会话占用，回滚将永久清除后续对话历史并覆盖工作区改动，确认强制回滚吗？')) {
          return;
        }
        res = await revertJobCheckpoint(selected, jobId, true);
      } else {
        if (!window.confirm('回滚将使工作区代码还原到该轮执行前，并永久清除该轮之后的记录，确认继续吗？')) {
          return;
        }
      }
      const snap = await api(`sessions/${selected}/snapshot`);
      setHistoryPage(snap);
      setEvents(snap.events || []);
      await refresh();
    } catch (e) {
      setError(e.message);
    }
  }

  async function createScratchSession() {
    try {
      const created = await post('sessions', {
        scratch: true,
        engine: newEngine,
        model: newModel,
        effort: newEffort,
        mode: newMode,
        fast: newFast,
      });
      await refresh();
      setSelected(created.id);
      setSidebarOpen(false);
    } catch (e) {
      setError(e.message);
    }
  }

  async function handlePromoteScratch() {
    if (!selected) return;
    try {
      await promoteScratchSession(selected, {init_git: true});
      await refresh();
    } catch (e) {
      setError(e.message);
    }
  }
  async function stop() {
    try {
      await post(`sessions/${selected}/stop`);
      await refresh();
    } catch (e) {
      setError(e.message);
    }
  }

  async function logout() {
    try {
      await post('logout');
      clearCachedState();
      if (typeof window !== 'undefined' && 'caches' in window) {
        try {
          const keys = await caches.keys();
          for (const key of keys) {
            const cache = await caches.open(key);
            await cache.delete('/');
            await cache.delete('/index.html');
          }
        } catch {}
      }
      setLocked(true);
      setState(null);
      setSettings(false);
    } catch (e) {
      setError(e.message);
    }
  }

  async function togglePin(e, s) {
    e.stopPropagation();
    try {
      await patch(`sessions/${s.id}`, {pinned: !s.pinned});
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  }

  function startRename(e, s) {
    e.stopPropagation();
    setEditingId(s.id);
    setEditingTitle(s.title);
  }

  async function saveRename(s) {
    const cleaned = editingTitle.trim();
    setEditingId('');
    if (!cleaned || cleaned === s.title) return;
    try {
      await patch(`sessions/${s.id}`, {title: cleaned});
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  }

  async function removeSession(e, s) {
    e.stopPropagation();
    if (confirmDeleteId !== s.id) {
      setConfirmDeleteId(s.id);
      return;
    }
    setConfirmDeleteId('');
    try {
      await del(`sessions/${s.id}`);
      deleteHistory(s.id);
      if (selected === s.id) newTask();
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  }

  function exportCurrentSession() {
    if (!current || !events.length) return;
    const {items} = parseEvents(events);
    const md = buildMarkdownTranscript(current, project, items);
    const safeName = (current.title || 'session').replace(/[\\/:*?"<>|]+/g, '-').slice(0, 50);
    downloadMarkdown(`${safeName}.md`, md);
  }

  function handleInspectFile(filePath) {
    let rel = filePath || '';
    const bases = [current?.cwd, project?.path].filter(Boolean);
    for (const b of bases) {
      const prefix = b.endsWith('/') ? b : `${b}/`;
      if (rel.startsWith(prefix)) {
        rel = rel.slice(prefix.length);
        break;
      }
    }
    setInspectTarget(rel);
    setTab('code');
  }

  function handleMentionFile(filePath) {
    setTab('chat');
    if (composerInputRef.current) {
      if (composerInputRef.current.appendPrompt) {
        composerInputRef.current.appendPrompt(`\`${filePath}\` `);
      } else if (composerInputRef.current.value !== undefined) {
        const prev = composerInputRef.current.value;
        const sep = prev && !prev.endsWith(' ') && !prev.endsWith('\n') ? ' ' : '';
        composerInputRef.current.value = `${prev}${sep}\`${filePath}\` `;
      }
      requestAnimationFrame(() => composerInputRef.current?.focus?.());
    }
  }

  function cycleTheme() {
    setThemeMode(prev => prev === 'system' ? 'light' : prev === 'light' ? 'dark' : 'system');
  }

  if (locked === null || !state) {
    if (locked === true) return <Login onUnlock={refresh}/>;
    const isHttps = typeof window !== 'undefined' && window.location.protocol === 'https:';

    return (
      <div className="boot">
        <div className="boot-card">
          <div className="boot-spinner" style={bootError ? {borderColor: 'rgba(239, 68, 68, 0.3)', borderTopColor: '#ef4444'} : undefined}/>
          <strong>
            {cfAuthNeeded
              ? '需要重新验证网关'
              : (bootError ? '连接工作台未完成' : (bootElapsed ? '连接耗时较长…' : '正在连接 Mac mini 工作台…'))}
          </strong>
          <p className="boot-hint">
            {cfAuthNeeded
              ? '网关登录态已过期，点击下方重新验证。'
              : (bootError || (bootElapsed ? '网络响应较慢，可直接密码登录或切换局域网。' : '正在同步工作区状态与引擎连接…'))}
          </p>
          <div className="boot-actions">
            {cfAuthNeeded ? (
              <a className="boot-link-btn primary" href="/healthz?cf_auth=1" style={{fontWeight: 600}}>
                <ArrowUpRight size={14}/>重新验证 Cloudflare 网关
              </a>
            ) : (
              <button type="button" className="primary" onClick={() => { setLocked(true); }}>
                <Lock size={14}/>输入密码登录
              </button>
            )}
            <button type="button" onClick={() => { setBootError(''); setCfAuthNeeded(false); refresh().catch(handleNetworkFailure); }}>
              <RefreshCw size={14}/>重试连接
            </button>
            {isHttps && window.__ROVE_BOOTSTRAP__?.endpoints?.lan_url && (
              <a className="boot-link-btn" href={window.__ROVE_BOOTSTRAP__.endpoints.lan_url}>
                <Wifi size={14}/>局域网直连 ({window.__ROVE_BOOTSTRAP__.endpoints.lan_url.replace(/^https?:\/\//, '')})
              </a>
            )}
          </div>
        </div>
      </div>
    );
  }
  if (locked) return <Login onUnlock={refresh}/>;

  const projectMap = Object.fromEntries(state.projects.map(p => [p.id, p]));
  const filterProjectObj = projectFilter === ALL_PROJECTS ? null : projectMap[projectFilter];
  const visiblePool = state.sessions.filter(s => showAutomations || s.thread_kind !== 'automation' || s.id === selected);
  const automationCount = state.sessions.filter(s => s.thread_kind === 'automation').length;
  const projectCounts = {};
  for (const s of visiblePool) {
    projectCounts[s.project_id] = (projectCounts[s.project_id] || 0) + 1;
  }
  const workspaceProjects = state.projects.filter(p => p.is_workspace);
  const repoProjects = state.projects.filter(p => !p.is_workspace);

  const filteredSessions = visiblePool.filter(s =>
    searching && query.trim()
      ? s.title.toLowerCase().includes(query.toLowerCase()) || (projectMap[s.project_id]?.name || '').toLowerCase().includes(query.toLowerCase())
      : (projectFilter === ALL_PROJECTS || s.project_id === projectFilter)
  );
  const pinnedSessions = filteredSessions.filter(s => s.pinned);
  const regularSessions = filteredSessions.filter(s => !s.pinned);

  const activeEngine = engine || current?.engine || 'codex';
  const activeModels = catalog?.[activeEngine]?.models || [];
  const activeModelName = (activeEngine === 'auto' || model === 'auto')
    ? '智能自动'
    : (activeModels.find(m => m.id === model)?.name || model);
  const resumeCmd = current?.native_id
    ? `${current.engine === 'codex' ? 'codex resume' : 'agy --conversation'} ${current.native_id}`
    : '';

  function renderSessionRow(s) {
    const j = state.jobs.find(item => item.session_id === s.id);
    const isWaiting = Boolean(j && j.status === 'waiting');
    const isRunning = Boolean(j && (j.status === 'running' || j.status === 'queued'));
    const isExternalRunning = Boolean(!isRunning && !isWaiting && s.external_active);
    const isFailed = Boolean(j && j.status === 'failed');
    const isCompleted = Boolean(j && (j.status === 'completed' || j.status === 'submitted'));
    const isEditing = editingId === s.id;
    const isDeleting = confirmDeleteId === s.id;
    const projName = projectMap[s.project_id]?.name;
    const originTag = s.originator === 'Codex Desktop'
      ? '本机'
      : (s.originator && s.originator.includes('feishu'))
        ? '飞书'
        : null;

    if (isEditing) {
      return (
        <div key={s.id} className="session-edit-row">
          <input
            autoFocus
            value={editingTitle}
            onChange={e => setEditingTitle(e.target.value)}
            onBlur={() => saveRename(s)}
            onKeyDown={e => {
              if (e.key === 'Enter') saveRename(s);
              if (e.key === 'Escape') setEditingId('');
            }}
          />
        </div>
      );
    }

    return (
      <div
        key={s.id}
        role="button"
        tabIndex={0}
        className={`session-row ${s.id === selected ? 'selected' : ''} ${isWaiting ? 'row-waiting' : isRunning ? 'row-running' : ''}`}
        onClick={() => selectSession(s)}
        onKeyDown={e => {if (e.key === 'Enter') selectSession(s);}}
        title={`${s.title} · ${projName || ''} · ${s.engine}${s.model ? ` · ${s.model}` : ''}`}
      >
        <div className="session-main">
          <div className="session-title-line">
            {Boolean(s.pinned) && <Pin size={11} className="pin-indicator"/>}
            <span className="session-title-text">{s.title}</span>
            {isWaiting && <span className="dot dot-waiting" title="任务等待审批"/>}
            {isRunning && <span className="dot dot-running" title="工作台任务执行中"/>}
            {isExternalRunning && <span className="dot sync-dot" title="Mac 本机 Codex 正在执行"/>}
            {isFailed && <span className="dot dot-failed" title="任务执行失败"/>}
          </div>
          <div className="session-sub-line">
            <span className={`session-engine-badge ${s.engine}`}>
              {s.engine === 'antigravity' ? <Sparkles size={10}/> : <Bot size={10}/>}
              <span>
                {s.engine === 'antigravity'
                  ? 'Antigravity'
                  : s.engine === 'codex'
                    ? 'Codex'
                    : (state.agents?.find(a => a.id === s.engine)?.short_name || s.engine)}
              </span>
            </span>
            {Boolean((s.native_codex_id && s.native_antigravity_id) || (s.last_engine && s.last_engine !== s.engine)) && (
              <span className="session-relay-badge" title="跨 Agent 协同接力会话">⇄ 接力</span>
            )}
            {(projectFilter === ALL_PROJECTS || searching) && projName && (
              <span className="session-proj-badge">{projName}</span>
            )}
            {isWaiting ? (
              <span className="session-status-tag waiting">待审批</span>
            ) : isRunning ? (
              <span className="session-status-tag running">执行中</span>
            ) : isExternalRunning ? (
              <span className="session-status-tag external">Mac 运行中</span>
            ) : isFailed ? (
              <span className="session-status-tag failed">失败</span>
            ) : (
              <>
                {originTag && <span>· {originTag}</span>}
                {s.model && <span>· {s.model}</span>}
              </>
            )}
          </div>
        </div>
        <div className="session-actions" onClick={e => e.stopPropagation()}>
          <button
            type="button"
            className="mini-action"
            title={s.pinned ? '取消置顶' : '置顶会话'}
            aria-label={s.pinned ? '取消置顶' : '置顶会话'}
            onClick={e => togglePin(e, s)}
          >
            <Pin size={12}/>
          </button>
          <button
            type="button"
            className="mini-action"
            title="重命名会话"
            aria-label="重命名会话"
            onClick={e => startRename(e, s)}
          >
            <Pencil size={12}/>
          </button>
          {!isRunning && !isWaiting && !isExternalRunning && (
            <button
              type="button"
              className={`mini-action ${isDeleting ? 'danger-confirm' : ''}`}
              title={isDeleting ? '再次点击确认移除' : '从工作台移除会话'}
              aria-label="移除会话"
              onClick={e => removeSession(e, s)}
            >
              {isDeleting ? '确认' : <Trash2 size={12}/>}
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={`app-shell ${collapsed ? 'sidebar-collapsed' : ''}`}>
      {drawer && <button type="button" className="drawer-backdrop" aria-label="关闭导航抽屉" onClick={() => setDrawer(false)}/>}

      <aside className={`sidebar ${drawer ? 'open' : ''}`} aria-label="项目与会话导航">
        <div className="brand">
          <div className="brand-mark rove-brand-mark"><RoveMark size={20}/></div>
          <h1>Rove</h1>
          <button
            type="button"
            className="icon-button sidebar-toggle desktop-only"
            aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
            title={collapsed ? '展开侧栏' : '收起侧栏'}
            onClick={() => setCollapsed(v => !v)}
          >
            {collapsed ? <PanelLeftOpen size={16}/> : <PanelLeftClose size={16}/>}
          </button>
          <button type="button" className="icon-button drawer-close-btn" onClick={() => setDrawer(false)} aria-label="关闭菜单">
            <X size={19}/>
          </button>
        </div>

        <nav className="sidebar-actions">
          <button type="button" className={!selected ? 'selected' : ''} onClick={newTask} title="新任务">
            <SquarePen size={17}/>
            <span>新建任务</span>
          </button>
          <button
            type="button"
            className={searching ? 'selected' : ''}
            onClick={() => {setSearching(v => !v); setQuery('');}}
            title="搜索会话"
          >
            <Search size={17}/>
            <span>搜索会话</span>
          </button>
        </nav>

        <div className="sidebar-scroll">
          <div className="section-label">
            <span>工作区与项目</span>
            <button type="button" className="icon-button" aria-label="登记新项目" title="登记新项目" onClick={() => setSettings(true)}>
              <Plus size={15}/>
            </button>
          </div>

          <button
            type="button"
            className="project-row selected"
            onClick={() => setProjectsOpen(v => !v)}
            title={filterProjectObj ? filterProjectObj.path : '跨项目浏览全部 Mac 同步会话'}
          >
            {projectFilter === ALL_PROJECTS ? <Layers size={16}/> : <Folder size={16}/>}
            <span>{projectFilter === ALL_PROJECTS ? '全部项目' : (filterProjectObj?.name || '选择项目')}</span>
            <em className="project-count">{projectFilter === ALL_PROJECTS ? visiblePool.length : (projectCounts[projectFilter] || 0)}</em>
            <ChevronDown size={13} className={projectsOpen ? 'rotated' : ''}/>
          </button>

          {projectsOpen && (
            <div className="project-options">
              {projectFilter !== ALL_PROJECTS && (
                <button type="button" className="project-row" onClick={() => selectProjectFilter(ALL_PROJECTS)}>
                  <Layers size={15}/>
                  <span>全部项目（全部同步会话）</span>
                  <em className="project-count">{visiblePool.length}</em>
                </button>
              )}
              {workspaceProjects.length > 0 && (
                <div className="project-group-label">Codex 工作区</div>
              )}
              {workspaceProjects.filter(p => p.id !== projectFilter).map(p => (
                <button type="button" className="project-row" key={p.id} onClick={() => selectProjectFilter(p.id)} title={p.path}>
                  <Folder size={15}/>
                  <span>{p.name}</span>
                  {projectCounts[p.id] > 0 && <em className="project-count">{projectCounts[p.id]}</em>}
                </button>
              ))}
              {repoProjects.length > 0 && (
                <div className="project-group-label">子项目仓库</div>
              )}
              {repoProjects.filter(p => p.id !== projectFilter).map(p => (
                <button type="button" className="project-row" key={p.id} onClick={() => selectProjectFilter(p.id)} title={p.path}>
                  <Folder size={15}/>
                  <span>{p.name}</span>
                  {projectCounts[p.id] > 0 && <em className="project-count">{projectCounts[p.id]}</em>}
                </button>
              ))}
            </div>
          )}

          {searching && (
            <div className="session-search-wrap">
              <Search size={13}/>
              <input
                ref={searchInput}
                className="session-search"
                aria-label="搜索会话标题"
                placeholder="跨项目搜索会话…"
                value={query}
                onChange={e => setQuery(e.target.value)}
              />
              {query && (
                <button type="button" className="icon-button" onClick={() => setQuery('')} aria-label="清空搜索">
                  <X size={13}/>
                </button>
              )}
            </div>
          )}

          {pinnedSessions.length > 0 && (
            <>
              <div className="section-label recent-label"><span>置顶会话</span></div>
              <nav className="session-list" aria-label="置顶会话列表">
                {pinnedSessions.map(renderSessionRow)}
              </nav>
            </>
          )}

          <div className="section-label recent-label">
            <span>{searching ? '匹配结果' : '同步会话'}</span>
            <div className="section-tools">
              {automationCount > 0 && (
                <button
                  type="button"
                  className={`auto-filter-btn ${showAutomations ? 'active' : ''}`}
                  onClick={() => setShowAutomations(v => !v)}
                  title={showAutomations ? '隐藏 Codex 定时自动化会话' : `显示 ${automationCount} 条 Codex 定时自动化记录`}
                >
                  <Bot size={12}/>
                  <span>{showAutomations ? '含自动化' : `自动化 ${automationCount}`}</span>
                </button>
              )}
              {state.codex_sync?.enabled && (
                <button
                  type="button"
                  className="icon-button sync-mini-btn"
                  onClick={quickSyncCodex}
                  disabled={syncingCodex}
                  aria-label="同步本机 Codex"
                  title="立即同步 Mac 本机 Codex 项目与对话进度"
                >
                  <RefreshCw size={13} className={syncingCodex ? 'spinning' : ''}/>
                </button>
              )}
            </div>
          </div>
          <nav className="session-list" aria-label="会话列表">
            {regularSessions.map(renderSessionRow)}
            {!filteredSessions.length && (
              <p className="sidebar-empty">{query ? '没有找到匹配的会话' : '暂无历史会话，开始一个新任务吧。'}</p>
            )}
          </nav>
        </div>

        <div className="sidebar-bottom">
          <div className="host" title={reachable ? `${state.host || 'Mac mini'} 在线 · 进度实时同步` : '网络重连中'}>
            <span className={`dot ${reachable ? '' : 'offline'}`}/>
            <span>
              {state.host || 'Mac mini'} · {reachable ? (state.codex_sync?.enabled ? '本机已同步' : '在线') : '重连中'}
            </span>
          </div>
          <div className="sidebar-footer-btns">
            <button
              type="button"
              className="footer-btn"
              onClick={() => {setSettings(true); setDrawer(false);}}
              title="工作台设置"
            >
              <Settings size={16}/>
              <span>设置</span>
            </button>
            <button
              type="button"
              className="icon-button theme-quick-btn"
              onClick={cycleTheme}
              title={`外观：${themeMode === 'system' ? '跟随系统' : themeMode === 'dark' ? '深色' : '浅色'}（点击切换）`}
              aria-label="切换主题外观"
            >
              {themeMode === 'system' ? <Monitor size={15}/> : darkTheme ? <Moon size={15}/> : <Sun size={15}/>}
            </button>
          </div>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              type="button"
              className="icon-button nav-menu-btn"
              aria-label="项目与会话菜单"
              onClick={() => setDrawer(true)}
            >
              <Menu size={19}/>
              {current?.external_active && <span className="menu-active-dot" title="Mac 运行中"/>}
            </button>
            <button
              type="button"
              className="topbar-rove-mark rove-brand-mark"
              aria-label="Rove 菜单"
              onClick={() => setDrawer(v => !v)}
              title="Rove"
            >
              <RoveMark size={16}/>
            </button>
            <div className="breadcrumb-info">
              <button type="button" className="breadcrumb-project" onClick={newTask} title={current?.cwd || project?.path || '点击新建该项目的任务'}>
                {project?.name || '选择项目'}
              </button>
              <span className="breadcrumb-slash">/</span>
              <span className="breadcrumb-title" title={current?.title}>{current?.title || '新建任务'}</span>
            </div>
            <button
              type="button"
              className={`topbar-mode-pill mode-badge-${mode}`}
              onClick={() => setModeModalOpen(true)}
              title={`当前权限策略：${modeBadge[mode] || mode}（点击修改执行与网络权限）`}
              aria-label="修改会话权限"
            >
              <ShieldCheck size={13}/>
              <span>{modeBadge[mode] || mode}</span>
            </button>
            <span className="topbar-meta-pill desktop-only" title="当前生效模型与思考档位">
              {activeModelName} · {effortBadge[effort] || effort}
            </span>
          </div>

          <div className="topbar-actions">
            {cfAuthNeeded && (
              <a className="cf-auth-badge" href="/healthz?cf_auth=1" title="Cloudflare 网关凭据已过期，点击重新验证">
                网关需验证
              </a>
            )}
            {selected && !connected && <span className="connection-note">同步中…</span>}
            {active ? (
              <span className={`job-status status-${job.status}`}>
                {job.status === 'waiting' ? (
                  <ShieldCheck size={13}/>
                ) : job.status === 'queued' ? (
                  <Hourglass size={13}/>
                ) : (
                  <span className="dot dot-running"/>
                )}
                <span>{statusText[job.status]}</span>
              </span>
            ) : current?.external_active ? (
              <span className="job-status external-status" title="Mac 本机 Codex 正在执行此会话，进度实时同步中">
                <span className="dot sync-dot"/>
                <span>Mac 运行中</span>
              </span>
            ) : null}

            <div className="segmented view-switcher" role="tablist" aria-label="工作区视图">
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'chat'}
                className={tab === 'chat' ? 'active' : ''}
                onClick={() => setTab('chat')}
                title="对话"
              >
                <MessageSquare size={15}/>
                <span>对话</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'terminal'}
                className={tab === 'terminal' ? 'active' : ''}
                onClick={ensureTerminal}
                aria-label="终端"
                title="终端"
              >
                <SquareTerminal size={15}/>
                <span>终端</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'code'}
                className={tab === 'code' ? 'active' : ''}
                onClick={() => setTab('code')}
                aria-label="代码修改"
                title="代码修改"
              >
                <Code2 size={15}/>
                <span>代码</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'preview'}
                className={tab === 'preview' ? 'active' : ''}
                onClick={() => setTab('preview')}
                aria-label="实时预览"
                title="本地开发服务器实时预览"
              >
                <Monitor size={15}/>
                <span>预览</span>
              </button>
            </div>

            {current?.use_worktree && (
              <button
                type="button"
                className="topbar-worktree-btn active"
                onClick={() => setWorktreeOpen(true)}
                title={`当前会话位于独立工作树 ${current.git_branch || ''}，与其他任务完全并行`}
                aria-label="独立工作树状态"
              >
                <GitBranch size={13} />
                <span className="desktop-only">{current.git_branch || '工作树'}</span>
              </button>
            )}

            <button
              type="button"
              className="topbar-quota-btn desktop-only"
              onClick={() => setQuotaOpen(true)}
              title="查看 Codex 与 Antigravity 实时额度、5小时/周配额及刷新倒计时"
              aria-label="查看模型额度"
            >
              <Zap size={14} className="quota-pill-icon"/>
              <span>额度</span>
            </button>

            <details className="topbar-more-dropdown">
              <summary
                className="icon-button topbar-more-btn"
                title="项目工具箱与更多操作（额度 / 记忆 / Worktree / 外观 / 导出）"
                aria-label="项目工具箱与操作菜单"
              >
                <MoreHorizontal size={17}/>
                {typeof activeProject?.memories_count === 'number' && activeProject.memories_count > 0 && (
                  <span className="topbar-more-dot" title={`已沉淀 ${activeProject.memories_count} 条项目持久记忆`}/>
                )}
              </summary>
              <div className="topbar-more-popover" onClick={e => {
                // Auto close details on action item click
                if (e.target.closest('button')) {
                  e.currentTarget.closest('details')?.removeAttribute('open');
                }
              }}>
                <div className="popover-section-label">项目与 AI 能力</div>

                <button
                  type="button"
                  className="popover-item"
                  onClick={() => setQuotaOpen(true)}
                >
                  <Zap size={15} style={{color: 'var(--amber)'}}/>
                  <div className="popover-item-text">
                    <span className="popover-item-title">模型配额与倒计时</span>
                    <span className="popover-item-sub">Codex 5小时/周配额与 Antigravity</span>
                  </div>
                </button>

                <button
                  type="button"
                  className="popover-item"
                  onClick={openDiagnostics}
                >
                  <Wifi size={15} style={{color: 'var(--emerald)'}}/>
                  <div className="popover-item-text">
                    <span className="popover-item-title">待处理中心与连接诊断</span>
                    <span className="popover-item-sub">集中处理审批、提问、失败任务与引擎状态</span>
                  </div>
                </button>

                {activeProject && (
                  <button
                    type="button"
                    className="popover-item"
                    onClick={() => setArenaOpen(true)}
                  >
                    <Layers size={15} style={{color: 'var(--accent)'}}/>
                    <div className="popover-item-text">
                      <span className="popover-item-title">双引擎对比跑 (Arena)</span>
                      <span className="popover-item-sub">同一需求同时交由两个引擎在独立 Worktree 对比实现</span>
                    </div>
                  </button>
                )}

                {activeProject && (
                  <button
                    type="button"
                    className="popover-item"
                    onClick={() => setMemoryOpen(true)}
                  >
                    <Brain size={15} style={{color: 'var(--accent)'}}/>
                    <div className="popover-item-text">
                      <span className="popover-item-title">项目持久记忆 (claude-mem)</span>
                      <span className="popover-item-sub">
                        {activeProject.memories_count > 0 ? `已沉淀 ${activeProject.memories_count} 条跨会话约定` : '沉淀开发规范与架构决策'}
                      </span>
                    </div>
                    {typeof activeProject.memories_count === 'number' && activeProject.memories_count > 0 && (
                      <span className="memory-count-badge">{activeProject.memories_count}</span>
                    )}
                  </button>
                )}

                {activeProject && (
                  <button
                    type="button"
                    className="popover-item"
                    onClick={() => setWorktreeOpen(true)}
                  >
                    <GitBranch size={15} style={{color: '#10b981'}}/>
                    <div className="popover-item-text">
                      <span className="popover-item-title">Git Worktree 并行隔离</span>
                      <span className="popover-item-sub">
                        {current?.use_worktree ? `当前分支：${current.git_branch || '已隔离'}` : '派生独立分支，解锁多任务并发'}
                      </span>
                    </div>
                    {current?.use_worktree && (
                      <span className="current-mode-tag" style={{background: '#10b981'}}>已开启</span>
                    )}
                  </button>
                )}

                {activeProject?.is_scratch ? (
                  <button
                    type="button"
                    className="popover-item"
                    onClick={handlePromoteScratch}
                  >
                    <Folder size={15} style={{color: '#3b82f6'}}/>
                    <div className="popover-item-text">
                      <span className="popover-item-title">升级为正式 Git 项目</span>
                      <span className="popover-item-sub">初始化 Git 仓库并解锁 Diff 与 Worktree</span>
                    </div>
                  </button>
                ) : (
                  <button
                    type="button"
                    className="popover-item"
                    onClick={createScratchSession}
                  >
                    <Sparkles size={15} style={{color: '#f59e0b'}}/>
                    <div className="popover-item-text">
                      <span className="popover-item-title">新建零项目临时会话 (Scratch)</span>
                      <span className="popover-item-sub">无需预先创建目录，直接开聊并可随时转正</span>
                    </div>
                  </button>
                )}

                <div className="popover-divider"/>

                <button
                  type="button"
                  className="popover-item"
                  onClick={cycleTheme}
                >
                  {themeMode === 'system' ? <Monitor size={15}/> : darkTheme ? <Moon size={15}/> : <Sun size={15}/>}
                  <div className="popover-item-text">
                    <span className="popover-item-title">外观主题</span>
                    <span className="popover-item-sub">当前：{themeMode === 'system' ? '跟随系统' : themeMode === 'dark' ? '深色模式' : '浅色模式'}</span>
                  </div>
                </button>

                {current && events.length > 0 && (
                  <button
                    type="button"
                    className="popover-item"
                    onClick={exportCurrentSession}
                  >
                    <Download size={15}/>
                    <div className="popover-item-text">
                      <span className="popover-item-title">导出会话记录</span>
                      <span className="popover-item-sub">导出为带排版的 Markdown</span>
                    </div>
                  </button>
                )}

                {current?.native_id && (
                  <div className="popover-native-box">
                    <div className="popover-native-header">
                      <ArrowUpRight size={14}/>
                      <span>Mac 本机 CLI 接力</span>
                    </div>
                    {current.cwd && <code className="popover-native-cwd">{current.cwd}</code>}
                    <div className="native-cmd-box" style={{marginTop: 6}}>
                      <code>{resumeCmd}</code>
                      <button
                        type="button"
                        className="icon-button"
                        aria-label="复制恢复命令"
                        onClick={(e) => {
                          e.stopPropagation();
                          navigator.clipboard?.writeText(resumeCmd).then(() => {
                            setCopiedNative(true);
                            setTimeout(() => setCopiedNative(false), 1500);
                          }).catch(() => {});
                        }}
                      >
                        {copiedNative ? <Check size={13}/> : <Copy size={13}/>}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </details>
          </div>
        </header>

        <InstallBanner pwa={pwa}/>

        <div className="tab-content">
          <Suspense fallback={<p className="empty-note">正在加载工作区组件…</p>}>
            {tab === 'chat' ? (
              <Conversation
                session={current}
                events={events}
                job={job}
                onLoadOlder={historyPage?.has_older ? async () => {
                  if (historyBusy) return;
                  const id = selected;
                  setHistoryBusy(true);
                  try {
                    const page = await api(`sessions/${id}/snapshot?before=${historyPage.oldest}`);
                    if (historySessionRef.current !== id) return;
                    window.dispatchEvent(new CustomEvent('rove-older-history', {detail: {session: id, events: page.events}}));
                    setHistoryPage(page);
                  } catch (error) { setError(error.message); }
                  finally { setHistoryBusy(false); }
                } : null}
                historyBusy={historyBusy}
                onSend={send}
                onStop={stop}
                onSwitchAndRetry={switchEngineAndRetry}
                error={error}
                onError={onError}
                sending={sending}
                projects={state.projects}
                projectId={projectId}
                onProjectChange={selectComposerProject}
                engine={activeEngine}
                onEngineChange={changeEngine}
                engineState={state.engines[activeEngine]}
                catalog={catalog}
                model={model}
                onModelChange={handleModelChange}
                effort={effort}
                onEffortChange={handleEffortChange}
                mode={mode}
                onModeChange={handleModeChange}
                fast={fast}
                onFastChange={handleFastChange}
                orchestration={orchestration}
                onCompact={selected ? async () => {
                  try {
                    await compactSessionContext(selected);
                    await refresh();
                  } catch (e) { setError(e.message || '上下文压缩失败'); }
                } : null}
                onOrchestrationChange={handleOrchestrationChange}
                orchestrationRuns={orchestrationRuns}
                onOrchestrationUpdated={updated => {
                  setOrchestrationRuns(prev => {
                    const idx = prev.findIndex(r => r.id === updated.id);
                    if (idx === -1) return [updated, ...prev];
                    const next = [...prev];
                    next[idx] = updated;
                    return next;
                  });
                }}
                onOpenSettings={() => setSettings(true)}
                onInspectFile={handleInspectFile}
                composerInputRef={composerInputRef}
                quotedRef={quotedRef}
                onClearQuote={() => setQuotedRef(null)}
                onQuoteText={txt => setQuotedRef(txt)}
                lastPrompt={lastPrompt}
                onCreateScratch={createScratchSession}
                onRevertJob={handleRevertJob}
                queue={sessionQueue}
                queuePaused={queuePaused}
                supportsLiveSteer={supportsLiveSteer}
                onEnqueue={handleEnqueue}
                onUpdateQueue={handleUpdateQueue}
                onDeleteQueue={handleDeleteQueue}
                onToggleQueuePause={handleToggleQueuePause}
                sendStatus={sendStatus}
                onRetryOutbox={failedOutbox ? () => send(failedOutbox.text, failedOutbox.attachedImages, failedOutbox.overrides) : null}
              />
            ) : (
              <>
                {error && <div className="error pane-error" role="alert">{error}</div>}
                {tab === 'terminal' ? (
                  <TerminalPane session={current} onError={onError} darkTheme={darkTheme}/>
                ) : tab === 'preview' ? (
                  <PreviewPane
                    onError={onError}
                    onAskPreview={text => {
                      setTab('chat');
                      requestAnimationFrame(() => {
                        composerInputRef.current?.setPrompt?.(text);
                        composerInputRef.current?.focus?.();
                      });
                    }}
                  />
                ) : (
                  <CodePane
                    project={project}
                    sessionId={current?.id}
                    onError={onError}
                    initialFile={inspectTarget}
                    onConsumeInitialFile={() => setInspectTarget('')}
                    onMentionFile={handleMentionFile}
                    onAskDiffLine={info => {
                      setQuotedRef(`${info.path}:${info.line}\n${info.code}`);
                      setTab('chat');
                      requestAnimationFrame(() => composerInputRef.current?.focus());
                    }}
                  />
                )}
              </>
            )}
          </Suspense>
        </div>
      </main>

      <Suspense fallback={null}>
        {settings && (
          <SettingsPanel
            engines={state.engines}
            catalog={catalog}
            codexSync={state.codex_sync}
            antigravitySync={state.antigravity_sync}
            pwa={pwa}
            projectCount={state.projects.length}
            sessionCount={visiblePool.length}
            activeSessionId={selected}
            themeMode={themeMode}
            onThemeChange={setThemeMode}
            onClose={() => setSettings(false)}
            onRefresh={refresh}
            onLogout={logout}
            onProject={id => {
              selectProjectFilter(id);
              setSettings(false);
            }}
            onOpenQuota={() => setQuotaOpen(true)}
            onOpenMemory={() => setMemoryOpen(true)}
            onOpenInstallGuide={() => setInstallGuideOpen(true)}
            enableWakeLock={enableWakeLock}
            onToggleWakeLock={() => setEnableWakeLock(v => !v)}
          />
        )}

        {handoffPreview && (
          <div className="modal-backdrop" onClick={() => setHandoffPreview(null)}>
            <div className="modal-card handoff-modal" onClick={e => e.stopPropagation()}>
              <div className="modal-header">
                <h3>跨引擎接力上下文检查</h3>
                <button type="button" className="icon-button" onClick={() => setHandoffPreview(null)}><X size={16}/></button>
              </div>
              <div className="modal-body handoff-body">
                <p className="handoff-meta">
                  目标引擎：<strong>{handoffPreview.target_engine}</strong>
                  {handoffPreview.target_model ? ` · 模型：${handoffPreview.target_model}` : ''}
                  {' · '}纳入轮次：<strong>{handoffPreview.included_turns}/{handoffPreview.total_turns}</strong>
                  {' · '}约 <strong>{handoffPreview.estimated_tokens}</strong> tokens
                </p>
                {handoffPreview.omitted_count > 0 && (
                  <details className="handoff-omitted">
                    <summary>已省略 {handoffPreview.omitted_count} 条早期对话（点击查回原文）</summary>
                    <div className="handoff-omitted-list">
                      {(handoffPreview.omitted_turns || []).map((t, i) => (
                        <div key={i} className="handoff-omitted-item">
                          <div><strong>提问：</strong>{t.prompt}</div>
                          {t.reply_preview && <div className="muted"><strong>回复：</strong>{t.reply_preview}</div>}
                        </div>
                      ))}
                    </div>
                  </details>
                )}
                <pre className="handoff-preview-pre">{handoffPreview.preview_text}</pre>
              </div>
            </div>
          </div>
        )}

        {diagnosticsOpen && (
          <div className="modal-backdrop" onClick={() => setDiagnosticsOpen(false)}>
            <div className="modal-card handoff-modal" onClick={e => e.stopPropagation()}>
              <div className="modal-header">
                <h3>待处理任务中心与连接诊断</h3>
                <button type="button" className="icon-button" onClick={() => setDiagnosticsOpen(false)}><X size={16}/></button>
              </div>
              <div className="modal-body handoff-body">
                {!diagnosticsData ? (
                  <p className="empty-note">正在采集工作站状态…</p>
                ) : (
                  <>
                    <div className="diag-section">
                      <h4>引擎与登录连接</h4>
                      <div className="diag-chips">
                        <span className="diag-chip ok">鉴权会话正常</span>
                        {(diagnosticsData.engines || []).map(eng => (
                          <span key={eng.id} className={`diag-chip ${eng.installed ? 'ok' : 'warn'}`}>
                            {eng.id}: {eng.installed ? '已就绪' : '未检测到 CLI'}
                          </span>
                        ))}
                      </div>
                    </div>
                    <div className="diag-section">
                      <h4>待审批 ({(diagnosticsData.pending_approvals || []).length}) / 待回答 ({(diagnosticsData.pending_questions || []).length})</h4>
                      {(diagnosticsData.pending_approvals || []).map(a => (
                        <button key={a.id} type="button" className="diag-row" onClick={() => { setSelected(a.session_id); setDiagnosticsOpen(false); }}>
                          <span>[审批] {a.session_title}: {a.title}</span>
                        </button>
                      ))}
                      {(diagnosticsData.pending_questions || []).map(q => (
                        <button key={q.id} type="button" className="diag-row" onClick={() => { setSelected(q.session_id); setDiagnosticsOpen(false); }}>
                          <span>[提问] {q.session_title}: {q.prompt}</span>
                        </button>
                      ))}
                      {!(diagnosticsData.pending_approvals?.length || diagnosticsData.pending_questions?.length) && (
                        <p className="muted">暂无挂起的审批或提问。</p>
                      )}
                    </div>
                    <div className="diag-section">
                      <h4>近期失败任务 ({(diagnosticsData.failed_jobs || []).length})</h4>
                      {(diagnosticsData.failed_jobs || []).slice(0, 6).map(j => (
                        <button key={j.id} type="button" className="diag-row" onClick={() => { setSelected(j.session_id); setDiagnosticsOpen(false); }}>
                          <span>[{j.engine || 'agent'}] {j.session_title}: {(j.error || '执行失败').slice(0, 80)}</span>
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        )}

        {arenaOpen && (
          <div className="modal-backdrop" onClick={() => setArenaOpen(false)}>
            <div className="modal-card handoff-modal" onClick={e => e.stopPropagation()}>
              <div className="modal-header">
                <h3>双引擎并行对比跑 (Arena)</h3>
                <button type="button" className="icon-button" onClick={() => setArenaOpen(false)}><X size={16}/></button>
              </div>
              <div className="modal-body handoff-body">
                {!arenaRun ? (
                  <>
                    <p className="setting-description" style={{margin: 0}}>
                      自动为当前项目创建两棵独立 Git Worktree，同时用两个引擎执行同一任务，完成后对比代码变更行数并择优合并。
                    </p>
                    <div className="arena-select-row">
                      <label>
                        <span>引擎 A</span>
                        <select value={arenaEngineA} onChange={e => setArenaEngineA(e.target.value)}>
                          <option value="codex">Codex</option>
                          <option value="antigravity">Antigravity</option>
                          <option value="claude-code">Claude Code</option>
                          <option value="gemini-cli">Gemini CLI</option>
                        </select>
                      </label>
                      <label>
                        <span>引擎 B</span>
                        <select value={arenaEngineB} onChange={e => setArenaEngineB(e.target.value)}>
                          <option value="antigravity">Antigravity</option>
                          <option value="codex">Codex</option>
                          <option value="claude-code">Claude Code</option>
                          <option value="gemini-cli">Gemini CLI</option>
                        </select>
                      </label>
                    </div>
                    <textarea
                      className="arena-prompt-input"
                      rows={4}
                      placeholder="输入要让两个引擎同时实现的需求描述…"
                      value={arenaPrompt}
                      onChange={e => setArenaPrompt(e.target.value)}
                    />
                    <button
                      type="button"
                      className="primary"
                      disabled={arenaBusy || !arenaPrompt.trim() || !activeProject}
                      onClick={async () => {
                        setArenaBusy(true);
                        try {
                          const started = await startArenaRun({
                            project_id: activeProject.id,
                            prompt: arenaPrompt.trim(),
                            engine_a: arenaEngineA,
                            engine_b: arenaEngineB,
                          });
                          const status = await getArenaStatus(started.id);
                          setArenaRun(status);
                          await refresh();
                        } catch (e) {
                          setError(e.message);
                        } finally {
                          setArenaBusy(false);
                        }
                      }}
                    >
                      {arenaBusy ? '正在启动双 Worktree…' : '同时启动对比执行'}
                    </button>
                  </>
                ) : (
                  <>
                    <div className="arena-compare-grid">
                      {[arenaRun.candidate_a, arenaRun.candidate_b].map((cand, idx) => (
                        <div key={cand.session_id} className="arena-card">
                          <div className="arena-card-head">
                            <strong>方案 {idx === 0 ? 'A' : 'B'} · {cand.engine}</strong>
                            <span className="diag-chip">{cand.job_status}</span>
                          </div>
                          <div className="arena-card-stats">
                            <span className="stat-add">+{cand.diff?.additions || 0}</span>
                            <span className="stat-del">-{cand.diff?.deletions || 0}</span>
                          </div>
                          <div className="button-row" style={{marginTop: '8px'}}>
                            <button
                              type="button"
                              onClick={() => { setSelected(cand.session_id); setArenaOpen(false); }}
                            >
                              查看会话
                            </button>
                            <button
                              type="button"
                              className="primary"
                              disabled={arenaBusy || arenaRun.status === 'adopted'}
                              onClick={async () => {
                                setArenaBusy(true);
                                try {
                                  await adoptArenaWinner(arenaRun.id, cand.session_id);
                                  setArenaOpen(false);
                                  setArenaRun(null);
                                  await refresh();
                                } catch (e) {
                                  setError(e.message);
                                } finally {
                                  setArenaBusy(false);
                                }
                              }}
                            >
                              采纳此方案并合并
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="button-row">
                      <button
                        type="button"
                        onClick={async () => {
                          const next = await getArenaStatus(arenaRun.id);
                          setArenaRun(next);
                        }}
                      >
                        <RefreshCw size={13}/> 刷新对比结果
                      </button>
                      <button type="button" onClick={() => setArenaRun(null)}>新建对比</button>
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        )}

        {quotaOpen && (
          <QuotaModal
            isOpen={quotaOpen}
            onClose={() => setQuotaOpen(false)}
          />
        )}

        {memoryOpen && (
          <ProjectMemoriesModal
            isOpen={memoryOpen}
            onClose={() => setMemoryOpen(false)}
            project={activeProject}
            onMemoriesChange={refresh}
          />
        )}

        {worktreeOpen && (
          <WorktreeModal
            isOpen={worktreeOpen}
            onClose={() => setWorktreeOpen(false)}
            session={current}
            project={activeProject}
            onWorktreeChanged={refresh}
          />
        )}

        {modeModalOpen && (
          <PermissionModal
            isOpen={modeModalOpen}
            onClose={() => setModeModalOpen(false)}
            currentMode={mode}
            onSelectMode={handleModeChange}
            sessionTitle={current?.title}
            engine={current?.engine || engine}
            catalog={catalog}
          />
        )}

        {installGuideOpen && (
          <InstallGuideModal
            isOpen={installGuideOpen}
            onClose={() => setInstallGuideOpen(false)}
            pwa={pwa}
          />
        )}
      </Suspense>
    </div>
  );
}
