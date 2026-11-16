import {useEffect, useState} from 'react';
import {
  X, RefreshCw, ExternalLink, LogOut, Sun, Moon, Monitor, Brain, Plus, FolderSync,
  RotateCcw, Zap, Smartphone, Bell, BellOff, Send, Share, Trash2, CheckCircle2,
  Sparkles, Activity, HardDrive, Database, Plug, Server, ShieldAlert, Download, UploadCloud, DownloadCloud, Copy, Check,
} from 'lucide-react';
import {
  api, post, del, base, getStorageStats, pruneStorage,
  getAgentsHub, upsertAgent, deleteAgent, checkAgent,
  getClusterStatus, updateClusterConfig, probeClusterPeer,
  createClusterSnapshot, restoreClusterSnapshot, pushClusterSnapshot, pullClusterSnapshot,
} from './api';
import RoveMark from './RoveMark';

const labels = {
  ready: '可用',
  blocked: '资格受限',
  unavailable: '暂不可用',
  unchecked: '尚未检查',
};

export default function SettingsPanel({
  engines, catalog, codexSync, antigravitySync, pwa, projectCount = 0, sessionCount = 0, activeSessionId,
  themeMode, onThemeChange, onClose, onRefresh, onLogout, onProject, onOpenQuota, onOpenMemory,
  enableWakeLock = true, onToggleWakeLock, onOpenInstallGuide,
}) {
  const [path, setPath] = useState('');
  const [busy, setBusy] = useState(false);
  const [syncingModels, setSyncingModels] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncNotice, setSyncNotice] = useState('');
  const [pushBusy, setPushBusy] = useState(false);
  const [testingPush, setTestingPush] = useState(false);
  const [pwaNotice, setPwaNotice] = useState('');
  const [watchConfig, setWatchConfig] = useState(null);
  const [watchBusy, setWatchBusy] = useState(false);
  const [watchNotice, setWatchNotice] = useState('');
  const [copiedWatchKey, setCopiedWatchKey] = useState('');
  const [error, setError] = useState('');
  const [checked, setChecked] = useState(null);
  const antigravity = checked || engines.antigravity;

  const [storageStats, setStorageStats] = useState(null);
  const [loadingStorage, setLoadingStorage] = useState(false);
  const [pruningStorage, setPruningStorage] = useState(false);
  const [storageNotice, setStorageNotice] = useState('');
  const [keepDays, setKeepDays] = useState(14);
  const [pruneOldSessions, setPruneOldSessions] = useState(false);

  // Universal Multi-Agent Hub state
  const [agentsHub, setAgentsHub] = useState({agents: catalog?.agents || [], presets: []});
  const [showAgentForm, setShowAgentForm] = useState(false);
  const [agentForm, setAgentForm] = useState({
    id: '',
    name: '',
    short_name: '',
    adapter: 'cli',
    binary: '',
    base_url: 'https://api.deepseek.com/v1',
    api_key: '',
    api_key_env: 'DEEPSEEK_API_KEY',
    models_text: '',
  });
  const [agentBusy, setAgentBusy] = useState(false);

  // Multi-subscription account pool state
  const [accounts, setAccounts] = useState([]);
  const [acctBusy, setAcctBusy] = useState(false);
  const [acctNotice, setAcctNotice] = useState('');
  const [pendingLogin, setPendingLogin] = useState(null);

  async function refreshAccounts() {
    try {
      const res = await api('accounts');
      setAccounts(res.accounts || []);
    } catch {}
  }

  useEffect(() => {
    refreshAccounts();
  }, []);

  async function handleQuickLogin(engine) {
    if (acctBusy) return;
    setAcctBusy(true);
    setAcctNotice('');
    setPendingLogin(null);
    try {
      const created = await post('accounts/quick-login', {engine});
      if (created.login_url) {
        setPendingLogin({
          label: created.label,
          url: created.login_url,
          code: created.device_code || '',
        });
        setAcctNotice(`已获取「${created.label}」授权链接，若未自动弹出请点击下方「打开登录授权页」。`);
        try { window.open(created.login_url, '_blank', 'noopener,noreferrer'); } catch {}
      } else {
        setAcctNotice(created.login_note || `已创建「${created.label}」，请完成授权后点击刷新。`);
      }
      await refreshAccounts();
    } catch (err) {
      setAcctNotice(err.message || '启动登录失败');
    } finally {
      setAcctBusy(false);
    }
  }

  async function handleDeleteAccount(id, label) {
    setAcctBusy(true);
    setAcctNotice('');
    try {
      await del(`accounts/${id}?purge_dir=true`);
      setAcctNotice(`已移除账号「${label}」及其隔离目录。`);
      await refreshAccounts();
    } catch (err) {
      setAcctNotice(err.message || '移除账号失败');
    } finally {
      setAcctBusy(false);
    }
  }
  const [agentNotice, setAgentNotice] = useState('');

  // PVE Standby & Disaster Recovery state
  const [clusterStatus, setClusterStatus] = useState(null);
  const [clusterBusy, setClusterBusy] = useState(false);
  const [clusterNotice, setClusterNotice] = useState('');
  const [pveFormOpen, setPveFormOpen] = useState(false);
  const [pveForm, setPveForm] = useState({
    node_name: '',
    peer_url: '',
    pve_ssh_host: 'pve-home',
    pve_ctid: '',
    remote_data_dir: '/var/lib/walking-workstation',
    pve_secondary_backup_dir: '/mnt/pve/backup/walking-workstation-snapshots',
  });
  const [copiedDeployCmd, setCopiedDeployCmd] = useState(false);

  useEffect(() => {
    let mounted = true;
    getAgentsHub().then(data => {
      if (mounted && data) setAgentsHub(data);
    }).catch(() => {});
    api('watch/config').then(data => {
      if (mounted && data) setWatchConfig(data);
    }).catch(() => {});
    getClusterStatus().then(data => {
      if (mounted && data) {
        setClusterStatus(data);
        setPveForm({
          node_name: data.config?.node_name || '',
          peer_url: data.config?.peer_url || '',
          pve_ssh_host: data.config?.pve_ssh_host || 'pve-home',
          pve_ctid: data.config?.pve_ctid || '',
          remote_data_dir: data.config?.remote_data_dir || '/var/lib/walking-workstation',
          pve_secondary_backup_dir: data.config?.pve_secondary_backup_dir || '/mnt/pve/backup/walking-workstation-snapshots',
        });
      }
    }).catch(() => {});
    return () => { mounted = false; };
  }, []);

  function applyPresetToForm(preset) {
    setShowAgentForm(true);
    setAgentNotice('');
    setAgentForm({
      id: preset.id,
      name: preset.name,
      short_name: preset.short_name || preset.name,
      adapter: preset.adapter || 'cli',
      binary: preset.detected_binary || preset.binary || preset.id,
      base_url: preset.base_url || 'https://api.deepseek.com/v1',
      api_key: '',
      api_key_env: preset.api_key_env || 'OPENAI_API_KEY',
      models_text: (preset.models || []).map(m => m.id || m).join(', '),
    });
  }

  async function handleSaveAgent(e) {
    e.preventDefault();
    setAgentBusy(true);
    setError('');
    setAgentNotice('');
    try {
      const modelsList = agentForm.models_text
        .split(',')
        .map(s => s.trim())
        .filter(Boolean);
      const payload = {
        id: agentForm.id.trim().toLowerCase(),
        name: agentForm.name.trim() || agentForm.id.trim(),
        short_name: agentForm.short_name.trim() || agentForm.name.trim() || agentForm.id.trim(),
        adapter: agentForm.adapter,
        cloud_url: agentForm.cloud_url || '',
        ...(agentForm.adapter === 'cli'
          ? {binary: agentForm.binary.trim() || agentForm.id.trim()}
          : {
              base_url: agentForm.base_url.trim(),
              api_key: agentForm.api_key.trim(),
              api_key_env: agentForm.api_key_env.trim() || 'OPENAI_API_KEY',
            }),
        ...(modelsList.length > 0 ? {models: modelsList} : {}),
      };
      const res = await upsertAgent(payload);
      setAgentsHub(prev => ({...prev, agents: res.agents || prev.agents}));
      setShowAgentForm(false);
      setAgentNotice(`已成功接入 ${res.agent?.name || payload.name}！现在可在底部提问栏直接切换或跨引擎接力协作。`);
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setAgentBusy(false);
    }
  }

  async function handleDeleteAgent(agentId, name) {
    setAgentBusy(true);
    setError('');
    setAgentNotice('');
    try {
      const res = await deleteAgent(agentId);
      setAgentsHub(prev => ({...prev, agents: res.agents || prev.agents}));
      setAgentNotice(`已移除自定义 Agent：${name}`);
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setAgentBusy(false);
    }
  }

  async function handleCheckCustomAgent(agentId) {
    setAgentBusy(true);
    setError('');
    setAgentNotice('');
    try {
      const outcome = await checkAgent(agentId);
      setAgentNotice(`${agentId}: ${outcome.detail || outcome.status}`);
      const hub = await getAgentsHub();
      setAgentsHub(hub);
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setAgentBusy(false);
    }
  }

  async function handleCreateSnapshot() {
    setClusterBusy(true);
    setError('');
    setClusterNotice('');
    try {
      const res = await createClusterSnapshot({include_projects: true, label: 'manual'});
      setClusterStatus(res.status);
      setClusterNotice(`已生成全量自愈快照：${res.snapshot?.filename} (${res.snapshot?.size_formatted})，保存在项目外部安全目录。`);
    } catch (err) {
      setError(err.message);
    } finally {
      setClusterBusy(false);
    }
  }

  async function handleSelfHealMissingFiles() {
    setClusterBusy(true);
    setError('');
    setClusterNotice('');
    try {
      const res = await restoreClusterSnapshot({
        restore_db: false,
        restore_projects: true,
        only_missing_files: true,
      });
      setClusterStatus(res.status);
      setClusterNotice(`一键自愈完成！已从快照恢复 ${res.restored_files_total || 0} 个缺失文件（未覆盖任何已有修改）。`);
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setClusterBusy(false);
    }
  }

  async function handlePushToPve() {
    setClusterBusy(true);
    setError('');
    setClusterNotice('');
    try {
      const res = await pushClusterSnapshot({include_projects: true});
      setClusterStatus(res.status);
      if (res.ok === false) {
        setError(res.detail || '快照已传输，远端备份或恢复未全部完成');
        return;
      }
      setClusterNotice(res.detail || `已将全量快照与项目源码同步至 PVE 通用备用节点 (${res.snapshot?.size_formatted})！`);
    } catch (err) {
      setError(err.message);
    } finally {
      setClusterBusy(false);
    }
  }

  async function handlePullFromPve(onlyMissing = false) {
    setClusterBusy(true);
    setError('');
    setClusterNotice('');
    try {
      const res = await pullClusterSnapshot({
        restore_db: !onlyMissing,
        restore_projects: true,
        only_missing_files: onlyMissing,
      });
      setClusterStatus(res.status);
      setClusterNotice(`已从 PVE 节点拉取并恢复快照 (${res.snapshot})，共恢复 ${res.restored_files_total || 0} 个文件。`);
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setClusterBusy(false);
    }
  }

  async function handleProbePve() {
    setClusterBusy(true);
    setError('');
    setClusterNotice('');
    try {
      const res = await probeClusterPeer();
      setClusterNotice(res.detail || (res.reachable ? 'PVE 节点连接正常' : '未连通 PVE 节点'));
    } catch (err) {
      setError(err.message);
    } finally {
      setClusterBusy(false);
    }
  }

  async function handleSavePveConfig(e) {
    e.preventDefault();
    setClusterBusy(true);
    setError('');
    setClusterNotice('');
    try {
      const status = await updateClusterConfig(pveForm);
      setClusterStatus(status);
      setPveFormOpen(false);
      setClusterNotice('已保存 PVE 节点与集群同步配置。');
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setClusterBusy(false);
    }
  }

  async function loadStorage() {
    setLoadingStorage(true);
    setStorageNotice('');
    try {
      const data = await getStorageStats();
      setStorageStats(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoadingStorage(false);
    }
  }

  async function handlePrune(isDryRun = false) {
    setPruningStorage(true);
    setStorageNotice('');
    setError('');
    try {
      const res = await pruneStorage({
        keep_days: keepDays,
        compact_events: true,
        compact_all: true,
        prune_sessions: pruneOldSessions,
        vacuum: true,
        backup_before: !isDryRun,
        dry_run: isDryRun,
      });
      if (res.dry_run) {
        const deltas = res.compact?.total_events_pruned || 0;
        const sessions = res.sessions?.pruned_sessions || 0;
        setStorageNotice(`【演练评估】可精简 ${deltas} 条过程事件，可清理 ${sessions} 个旧会话。未实际修改数据。`);
      } else {
        const freed = res.freed_formatted || '0 B';
        const deltas = res.compact?.total_events_pruned || 0;
        const sessions = res.sessions?.pruned_sessions || 0;
        setStorageNotice(`存储治理完成！已精简 ${deltas} 条过程事件，清理 ${sessions} 个旧会话，物理磁盘释放 ${freed}（${res.size_before_formatted} -> ${res.size_after_formatted}）。`);
        setStorageStats(res.stats);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setPruningStorage(false);
    }
  }

  async function handleInstallApp() {
    if (!pwa?.triggerInstall) return false;
    setError('');
    setPwaNotice('');
    const installed = await pwa.triggerInstall();
    if (installed) {
      setPwaNotice('已提交系统桌面应用安装，稍后可从主屏幕全屏启动。');
    }
    return installed;
  }

  async function handleTogglePush() {
    if (!pwa) return;
    setPushBusy(true);
    setError('');
    setPwaNotice('');
    try {
      if (pwa.isSubscribed) {
        await pwa.disablePush();
        setPwaNotice('已关闭当前设备的锁屏消息推送。');
      } else {
        await pwa.enablePush();
        setPwaNotice(`已开启 ${pwa.device?.label || '本机'} 锁屏推送！任务完成或需要审批时将第一时间通知你。`);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setPushBusy(false);
    }
  }

  async function handleTestPush(kind = 'test') {
    if (!pwa) return;
    setTestingPush(true);
    setError('');
    setPwaNotice('');
    setWatchNotice('');
    try {
      const res = await pwa.sendTestPush(activeSessionId, kind);
      const isApproval = kind === 'approval';
      if (res.sent > 0) {
        const msg = isApproval
          ? `模拟审批推送已发送至 ${res.sent} 台已绑定设备（锁屏 iPhone 将自动镜像至配对 Apple Watch）。`
          : `测试推送已送达（${res.sent} 台设备），请查看系统通知栏或锁屏界面。`;
        setPwaNotice(msg);
        if (isApproval) setWatchNotice(msg);
      } else if (res.failed > 0 || (res.errors && res.errors.length > 0)) {
        const errDetail = (res.errors || []).join('；') || '推送网关拒绝请求';
        setError(`推送发送失败（${res.failed || 1} 台设备）：${errDetail}`);
      } else if (res.removed > 0) {
        setPwaNotice('原订阅端点已失效并自动清理，请重新点击「开启本机锁屏推送」。');
      } else {
        const msg = '当前没有已绑定的手机推送设备，请先在 iPhone 主屏幕 Rove App 中开启锁屏推送。';
        setPwaNotice(msg);
        if (isApproval) setWatchNotice(msg);
      }
      if (onRefresh) await onRefresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setTestingPush(false);
    }
  }

  async function handleRotateWatchToken() {
    setWatchBusy(true);
    setError('');
    setWatchNotice('');
    try {
      const next = await post('watch/rotate');
      setWatchConfig(next);
      setWatchNotice('已生成新的 Watch / 墨水屏访问 Token，原链接已失效。');
    } catch (e) {
      setError(e.message);
    } finally {
      setWatchBusy(false);
    }
  }

  function copyWatchText(key, value) {
    if (!value) return;
    navigator.clipboard?.writeText(value);
    setCopiedWatchKey(key);
    setTimeout(() => setCopiedWatchKey(prev => (prev === key ? '' : prev)), 1800);
  }

  async function handleRemovePushDevice(id) {
    if (!pwa) return;
    setError('');
    try {
      await pwa.removeDeviceById(id);
    } catch (e) {
      setError(e.message);
    }
  }

  async function syncAllNow(restoreDismissed = false) {
    setSyncing(true);
    setError('');
    setSyncNotice('');
    try {
      const res = await post('sync', {
        restore_dismissed: restoreDismissed,
        session_id: activeSessionId || null,
      });
      await onRefresh();
      const codexSynced = res.codex?.sessions_synced ?? 0;
      const agySynced = res.antigravity?.synced_count ?? 0;
      setSyncNotice(`已同步：Codex ${codexSynced} 会话 · Antigravity ${agySynced} 会话`);
    } catch (e) {
      setError(e.message);
    } finally {
      setSyncing(false);
    }
  }

  async function check() {
    setBusy(true);
    setError('');
    try {
      const result = await post('engines/antigravity/check');
      setChecked(result);
      await onRefresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function syncModels() {
    setSyncingModels(true);
    setError('');
    try {
      await post('models/refresh');
      await onRefresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setSyncingModels(false);
    }
  }

  async function add(e) {
    e.preventDefault();
    setError('');
    try {
      const result = await post('projects', {path});
      await onRefresh();
      onProject(result.id);
    } catch (e) {
      setError(e.message);
    }
  }

  const codexCount = catalog?.codex?.models?.length || 0;
  const agyCount = catalog?.antigravity?.models?.length || 0;
  const modelSource = catalog?.codex?.source === 'live' || catalog?.antigravity?.source === 'live'
    ? '已从本机 CLI 实时同步'
    : '内置目录（点击下方可从本机 CLI 实时拉取）';

  const pushBadgeClass = pwa?.isSubscribed ? 'ready' : (!pwa?.secureContext ? 'unavailable' : '');
  const pushBadgeText = pwa?.isSubscribed
    ? '本机已开启'
    : (!pwa?.secureContext
      ? '需 HTTPS 域名'
      : (pwa?.device?.isIOS && !pwa?.standalone ? '需从主屏幕打开' : '未开启'));

  return (
    <div className="modal-backdrop" onClick={e => {if (e.target === e.currentTarget) onClose();}}>
      <section role="dialog" aria-modal="true" aria-labelledby="settings-title" className="settings-modal">
        <div className="sheet-handle mobile-only" aria-hidden="true"/>
        <div className="pane-toolbar">
          <h2 id="settings-title">工作台设置</h2>
          <button type="button" className="icon-button" aria-label="关闭设置" onClick={onClose}>
            <X size={19}/>
          </button>
        </div>

        <div className="settings-body">
          <section>
            <h3>界面外观</h3>
            <div className="segmented theme-segmented">
              <button
                type="button"
                className={themeMode === 'system' ? 'active' : ''}
                onClick={() => onThemeChange('system')}
              >
                <Monitor size={14}/>跟随系统
              </button>
              <button
                type="button"
                className={themeMode === 'light' ? 'active' : ''}
                onClick={() => onThemeChange('light')}
              >
                <Sun size={14}/>浅色
              </button>
              <button
                type="button"
                className={themeMode === 'dark' ? 'active' : ''}
                onClick={() => onThemeChange('dark')}
              >
                <Moon size={14}/>深色
              </button>
            </div>
          </section>

          {pwa && (
            <section className="pwa-settings-section">
              <div className="pwa-app-card">
                <div className="brand-mark rove-brand-mark pwa-app-avatar"><RoveMark size={28}/></div>
                <div className="pwa-app-info">
                  <strong>Rove</strong>
                  <small>随身连接 Mac mini 的全功能双引擎编程工作台</small>
                </div>
              </div>

              <div className="engine-line">
                <span>
                  <Smartphone size={14} style={{verticalAlign: '-2px', marginRight: 6}}/>
                  当前运行形态 · {pwa.device?.label || '移动设备'}
                </span>
                <span className={`engine-label ${pwa.standalone ? 'ready' : ''}`}>
                  {pwa.standalone ? '独立 App 模式' : '浏览器标签页'}
                </span>
              </div>

              <div className="engine-line">
                <span>
                  <Bell size={14} style={{verticalAlign: '-2px', marginRight: 6}}/>
                  系统级锁屏消息推送
                </span>
                <span className={`engine-label ${pushBadgeClass}`}>
                  {pushBadgeText}
                </span>
              </div>

              {!pwa.secureContext ? (
                <div className="pwa-guide-card">
                  <strong>提示：当前通过局域网 HTTP 访问</strong>
                  <p>
                    浏览器安全规范仅在 HTTPS 安全域名（或本机 <code>localhost</code>）下开放 WebAPK 安装与系统级锁屏推送；局域网直连时对话、终端与文件同步不受影响。
                  </p>
                </div>
              ) : pwa.device?.isIOS && !pwa.standalone ? (
                <div className="pwa-guide-card">
                  <strong><Share size={13} style={{verticalAlign: '-2px', marginRight: 5}}/>iPhone / iPad 升级为独立 App（2 步开启锁屏推送）</strong>
                  <ol className="pwa-steps">
                    <li>点击 Safari 地址栏的 <strong>「分享」图标</strong>，选择 <strong>「添加到主屏幕」</strong>（生成高清桌面图标，隐藏浏览器地址栏）。</li>
                    <li>从主屏幕打开 <strong>「Rove」</strong> App，回到此处即可一键开启 iOS 原生锁屏通知推送。</li>
                  </ol>
                </div>
              ) : (
                <p className="setting-description">
                  {pwa.standalone
                    ? '已运行于无边框全屏独立应用模式；开启锁屏推送后，即便熄屏切后台，任务审批请求与完成通知也会实时直达通知中心。'
                    : '支持在 Android 平板/手机编译安装为原生级 WebAPK 独立应用（支持分屏多任务与桌面长按快捷入口），并支持系统级锁屏消息推送。'}
                  {pwa.device?.isAndroid && '（注：若安卓开启了分应用代理，请把安装后的「Rove」App 一并加入代理列表以确保跨境秒连。）'}
                </p>
              )}

              <div className="button-row">
                {!pwa.standalone && (
                  <button
                    type="button"
                    className="primary"
                    onClick={async () => {
                      if (pwa.canInstall) {
                        const installed = await handleInstallApp();
                        if (!installed && onOpenInstallGuide) onOpenInstallGuide();
                      } else if (onOpenInstallGuide) {
                        onOpenInstallGuide();
                      }
                    }}
                  >
                    <Smartphone size={14}/>
                    {pwa.canInstall ? '安装独立桌面应用 (WebAPK)' : '平板/移动端安装指引'}
                  </button>
                )}

                {pwa.secureContext && !(pwa.device?.isIOS && !pwa.standalone) && (
                  <button
                    type="button"
                    className={pwa.isSubscribed ? '' : 'primary'}
                    onClick={handleTogglePush}
                    disabled={pushBusy || !pwa.pushSupported}
                  >
                    {pwa.isSubscribed ? <BellOff size={14}/> : <Bell size={14}/>}
                    {pushBusy
                      ? '正在配置推送…'
                      : (pwa.isSubscribed ? '关闭本机锁屏推送' : '开启本机锁屏推送')}
                  </button>
                )}

                {(pwa.isSubscribed || (pwa.serverSubscriptions?.length > 0)) && (
                  <>
                    <button type="button" onClick={() => handleTestPush('test')} disabled={testingPush}>
                      <Send size={14}/>
                      {testingPush ? '正在发送…' : '发送测试通知'}
                    </button>
                    <button type="button" onClick={() => handleTestPush('approval')} disabled={testingPush}>
                      <Bell size={14}/>
                      {testingPush ? '正在发送…' : '发送审批测试推送'}
                    </button>
                  </>
                )}
              </div>

              {pwaNotice && (
                <small className="sync-feedback">
                  <CheckCircle2 size={12} style={{verticalAlign: '-2px', marginRight: 4}}/>
                  {pwaNotice}
                </small>
              )}

              {pwa.serverSubscriptions?.length > 0 && (
                <div className="pwa-device-list" aria-label="已订阅推送设备">
                  <div className="pwa-device-header">
                    已绑定推送设备 ({pwa.serverSubscriptions.length})
                  </div>
                  {pwa.serverSubscriptions.map(sub => {
                    const isCurrent = Boolean(pwa.localEndpointHash && sub.endpoint_hash === pwa.localEndpointHash);
                    return (
                      <div key={sub.id} className="pwa-device-row">
                        <div className="pwa-device-meta">
                          <strong>{sub.device_label}</strong>
                          {isCurrent && <span className="pwa-current-pill">本机</span>}
                          <span className="pwa-host-hint">{sub.endpoint_host}</span>
                          {sub.last_error && (
                            <small className="pwa-host-hint" style={{color: 'var(--danger)'}}>
                              最近错误：{sub.last_error}
                            </small>
                          )}
                        </div>
                        <button
                          type="button"
                          className="icon-button pwa-remove-btn"
                          title="移除此设备的推送订阅"
                          aria-label={`移除 ${sub.device_label} 推送订阅`}
                          onClick={() => handleRemovePushDevice(sub.id)}
                        >
                          <Trash2 size={13}/>
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          )}

          <section>
            <h3>⌚ Apple Watch 与手机快捷审批</h3>
            <p className="setting-description">
              注：iOS / watchOS 系统限制网页推送镜像到手表时仅显示通知正文（无法在系统通知卡片直接出按钮）。要在 Apple Watch 上<strong>直接审批</strong>，推荐在 iPhone「快捷指令」新建一步 <code>POST</code> 快捷指令并同步到手表/操作按钮，或使用 <code>ios-companion</code> 中的原生 <code>RoveWatch</code> 手表应用。
            </p>
            <div className="engine-line">
              <span>已绑定手机推送设备</span>
              <span className={`engine-label ${(pwa?.serverSubscriptions?.length || 0) > 0 ? 'ready' : ''}`}>
                {pwa?.serverSubscriptions?.length || 0} 台
              </span>
            </div>
            {watchConfig ? (
              <>
                <div className="pve-deploy-hint">
                  <div className="engine-line">
                    <strong>快捷指令 1 步审批接口 (POST URL，可省略 approval_id 自动批准最新请求)：</strong>
                    <button
                      type="button"
                      className="icon-button"
                      title="复制快捷指令审批接口"
                      aria-label="复制快捷指令审批接口"
                      onClick={() => copyWatchText('approve', watchConfig.approve_url)}
                    >
                      {copiedWatchKey === 'approve' ? <Check size={13}/> : <Copy size={13}/>}
                    </button>
                  </div>
                  <code>{watchConfig.approve_url}</code>
                  <div className="button-row" style={{marginTop: 6}}>
                    <button
                      type="button"
                      onClick={() => copyWatchText('json_accept', '{"decision":"accept"}')}
                    >
                      {copiedWatchKey === 'json_accept' ? <Check size={13}/> : <Copy size={13}/>}
                      复制批准 JSON
                    </button>
                    <button
                      type="button"
                      onClick={() => copyWatchText('json_decline', '{"decision":"decline"}')}
                    >
                      {copiedWatchKey === 'json_decline' ? <Check size={13}/> : <Copy size={13}/>}
                      复制拒绝 JSON
                    </button>
                  </div>
                </div>
                <div className="pve-deploy-hint" style={{marginTop: 8}}>
                  <div className="engine-line">
                    <strong>Watch 实时监控与快速审批页 (Watch Token: <code>{watchConfig.token}</code>)：</strong>
                    <button
                      type="button"
                      className="icon-button"
                      title="复制 Watch 审批页链接"
                      aria-label="复制 Watch 审批页链接"
                      onClick={() => copyWatchText('live', watchConfig.live_url)}
                    >
                      {copiedWatchKey === 'live' ? <Check size={13}/> : <Copy size={13}/>}
                    </button>
                  </div>
                  <code>{watchConfig.live_url}</code>
                </div>
                <div className="pve-deploy-hint" style={{marginTop: 8}}>
                  <div className="engine-line">
                    <strong>快捷指令待审批查询接口 (JSON / Text)：</strong>
                    <button
                      type="button"
                      className="icon-button"
                      title="复制快捷指令查询接口"
                      aria-label="复制快捷指令查询接口"
                      onClick={() => copyWatchText('pending', watchConfig.pending_url)}
                    >
                      {copiedWatchKey === 'pending' ? <Check size={13}/> : <Copy size={13}/>}
                    </button>
                  </div>
                  <code>{watchConfig.pending_url}</code>
                </div>
                <div className="button-row" style={{marginTop: 10}}>
                  <button
                    type="button"
                    className="primary"
                    onClick={() => window.open(watchConfig.live_url, '_blank', 'noopener,noreferrer')}
                  >
                    <ExternalLink size={14}/>
                    打开 Watch 审批页
                  </button>
                  <button
                    type="button"
                    onClick={() => handleTestPush('approval')}
                    disabled={testingPush || !(pwa?.serverSubscriptions?.length > 0)}
                    title={pwa?.serverSubscriptions?.length > 0 ? '向已绑定手机发送模拟审批推送（锁屏时自动镜像至 Apple Watch）' : '请先在 iPhone 主屏幕 App 开启锁屏推送'}
                  >
                    <Bell size={14}/>
                    {testingPush ? '正在发送…' : '测试手机/手表审批推送'}
                  </button>
                  <button
                    type="button"
                    onClick={() => window.open(watchConfig.eink_url, '_blank', 'noopener,noreferrer')}
                  >
                    <ExternalLink size={14}/>
                    墨水屏审批台
                  </button>
                  <button
                    type="button"
                    onClick={handleRotateWatchToken}
                    disabled={watchBusy}
                  >
                    <RotateCcw size={14} className={watchBusy ? 'spinning' : ''}/>
                    轮换 Watch Token
                  </button>
                </div>
              </>
            ) : (
              <p className="setting-description">正在加载 Watch 审批配置…</p>
            )}
            {watchNotice && (
              <small className="sync-feedback">
                <CheckCircle2 size={12} style={{verticalAlign: '-2px', marginRight: 4}}/>
                {watchNotice}
              </small>
            )}
          </section>

          <section>
            <h3>📱 屏幕显示与防休眠</h3>
            <div className="engine-line">
              <span>
                <Moon size={14} style={{verticalAlign: '-2px', marginRight: 6}}/>
                前台任务防休眠 (Screen Wake Lock)
              </span>
              <span className={`engine-label ${enableWakeLock ? 'ready' : ''}`}>
                {enableWakeLock ? '防休眠已开启' : '跟随系统休眠'}
              </span>
            </div>
            <p className="setting-description">
              在前台打开且正在执行长任务（深度思考、执行命令、改写代码）时，保持屏幕常亮不黑屏；任务结束后自动恢复跟随系统休眠。
            </p>
            <div className="button-row">
              <button
                type="button"
                className={enableWakeLock ? '' : 'primary'}
                onClick={onToggleWakeLock}
              >
                <Moon size={14}/>
                {enableWakeLock ? '关闭防休眠 (恢复自动休眠)' : '开启前台防休眠'}
              </button>
            </div>
          </section>

          <section>
            <h3>本机内容同步与模型额度</h3>
            <div className="engine-line">
              <span><FolderSync size={14} style={{verticalAlign: '-2px', marginRight: 6}}/>Codex 内容同步</span>
              <span className={`engine-label ${codexSync?.enabled ? 'ready' : ''}`}>
                {codexSync?.enabled ? '已开启' : '未启用'}
              </span>
            </div>
            <div className="engine-line">
              <span><FolderSync size={14} style={{verticalAlign: '-2px', marginRight: 6}}/>Antigravity 内容同步</span>
              <span className={`engine-label ${antigravitySync?.enabled ? 'ready' : ''}`}>
                {antigravitySync?.enabled ? '已开启' : '未启用'}
              </span>
            </div>
            <p className="setting-description">
              自动实时同步 Mac mini 本机 Codex 与 Antigravity 的项目、历史对话、思考摘要与工具执行进度；在桌面端或网页端任一处继续提问，另一端均实时同步更新。
            </p>
            <div className="button-row">
              <button type="button" onClick={() => syncAllNow(false)} disabled={syncing}>
                <RefreshCw size={14} className={syncing ? 'spinning' : ''}/>
                {syncing ? '正在同步双引擎内容…' : '立即双向同步 (Codex & Antigravity)'}
              </button>
              {onOpenQuota && (
                <button type="button" onClick={() => { onOpenQuota(); onClose(); }}>
                  <Zap size={14}/>
                  查看实时模型额度与刷新倒计时
                </button>
              )}
              {onOpenMemory && (
                <button type="button" onClick={() => { onOpenMemory(); onClose(); }}>
                  <Brain size={14}/>
                  管理项目持久记忆与开发约定
                </button>
              )}
              <button type="button" onClick={() => syncAllNow(true)} disabled={syncing} title="恢复曾在网页端移除隐藏的本机会话">
                <RotateCcw size={14}/>
                恢复已忽略会话
              </button>
            </div>
            {syncNotice && <small className="sync-feedback">{syncNotice}</small>}
          </section>

          <section>
            <h3>模型与推理目录</h3>
            <div className="engine-line">
              <span><Brain size={14} style={{verticalAlign: '-2px', marginRight: 6}}/>已加载模型</span>
              <span className="engine-label ready">Codex {codexCount} · Antigravity {agyCount}</span>
            </div>
            <p className="setting-description">{modelSource}。拉取模型元数据不会提交任何提示词，不消耗对话额度。</p>
            <button type="button" onClick={syncModels} disabled={syncingModels}>
              <RefreshCw size={14} className={syncingModels ? 'spinning' : ''}/>
              {syncingModels ? '正在从本机 CLI 同步…' : '同步最新模型列表'}
            </button>
          </section>

          <section>
            <h3>本机引擎状态</h3>
            <div className="engine-line">
              <strong>OpenAI Codex</strong>
              <span className={`engine-label ${engines.codex.installed ? 'ready' : ''}`}>
                {engines.codex.installed ? '已就绪 · 复用本机 ChatGPT 登录' : '未安装'}
              </span>
            </div>
            <p className="setting-description">支持多模型切换、6 档推理强度（Low ~ Ultra）、只读规划/自动执行模式及 2x 极速通道。</p>

            <div className="engine-line">
              <strong>Google Antigravity</strong>
              <span className={`engine-label ${antigravity.status}`}>
                {engines.antigravity.installed ? labels[antigravity.status] || '尚未检查' : '未安装'}
              </span>
            </div>
            <p className="setting-description">
              {antigravity.detail || '手动执行一次轻量模型探针，确认账号登录态与地区调用资格。'}
            </p>
            {antigravity.help_url && (
              <a href={antigravity.help_url} target="_blank" rel="noreferrer">
                Google 官方排障文档 <ExternalLink size={12}/>
              </a>
            )}
            <button type="button" className="check-engine" onClick={check} disabled={busy}>
              <RefreshCw size={14} className={busy ? 'spinning' : ''}/>
              {busy ? '正在检查连接…' : '检查 Antigravity 连通性'}
            </button>
            <small>仅在手动点击时运行一次检测，绝不在后台定时轮询模型。</small>
          </section>

          <section className="multi-agent-hub-section">
            <h3>🧩 通用多 Agent 扩展中心 (Multi-Agent Hub)</h3>
            <p className="setting-description">
              除内置 Codex 与 Antigravity 外，可一键接入 Claude Code、Gemini CLI、Aider、OpenCode 或任意 OpenAI 兼容 API（DeepSeek / Ollama / OpenRouter），在同一会话内自由切换并共享跨引擎接力记忆。
            </p>

            <div className="agent-preset-pills">
              {(agentsHub.presets || []).map(preset => (
                <button
                  key={preset.id}
                  type="button"
                  className="preset-pill-btn"
                  onClick={() => applyPresetToForm(preset)}
                  title={preset.description}
                >
                  <Plus size={12}/>
                  <span>{preset.name}</span>
                </button>
              ))}
              <button
                type="button"
                className="preset-pill-btn"
                onClick={() => {
                  setShowAgentForm(v => !v);
                  setAgentForm({
                    id: '',
                    name: '',
                    short_name: '',
                    adapter: 'cli',
                    binary: '',
                    base_url: 'https://api.deepseek.com/v1',
                    api_key: '',
                    api_key_env: 'OPENAI_API_KEY',
                    models_text: '',
                  });
                }}
              >
                <Plug size={12}/>
                <span>{showAgentForm ? '收起配置表单' : '自定义 Agent'}</span>
              </button>
            </div>

            {showAgentForm && (
              <form className="agent-config-form" onSubmit={handleSaveAgent}>
                <div className="form-grid-2">
                  <div>
                    <label>Agent 标识 ID (英文小写)</label>
                    <input
                      placeholder="例如: claude-code 或 deepseek"
                      value={agentForm.id}
                      onChange={e => setAgentForm(f => ({...f, id: e.target.value}))}
                      required
                    />
                  </div>
                  <div>
                    <label>显示名称</label>
                    <input
                      placeholder="例如: Claude Code"
                      value={agentForm.name}
                      onChange={e => setAgentForm(f => ({...f, name: e.target.value}))}
                      required
                    />
                  </div>
                </div>

                <div className="form-grid-2">
                  <div>
                    <label>接入协议类型</label>
                    <select
                      value={agentForm.adapter}
                      onChange={e => setAgentForm(f => ({...f, adapter: e.target.value}))}
                    >
                      <option value="cli">CLI 命令行工具 (Claude / Gemini / Aider 等)</option>
                      <option value="openai_http">OpenAI 兼容 HTTP API (DeepSeek / Ollama / vLLM)</option>
                    </select>
                  </div>
                  <div><label>云端入口（可选）</label><input type="url" placeholder="https://…"
                    value={agentForm.cloud_url || ''} onChange={e => setAgentForm(f => ({...f, cloud_url: e.target.value}))}/>
                    <small>配置后显示本地 / 云端切换，云端打开服务商页面</small></div>
                  {agentForm.adapter === 'cli' ? (
                    <div>
                      <label>可执行命令名或绝对路径</label>
                      <input
                        placeholder="例如: claude 或 /usr/local/bin/claude"
                        value={agentForm.binary}
                        onChange={e => setAgentForm(f => ({...f, binary: e.target.value}))}
                        required
                      />
                    </div>
                  ) : (
                    <div>
                      <label>API Base URL</label>
                      <input
                        placeholder="https://api.deepseek.com/v1"
                        value={agentForm.base_url}
                        onChange={e => setAgentForm(f => ({...f, base_url: e.target.value}))}
                        required
                      />
                    </div>
                  )}
                </div>

                {agentForm.adapter === 'openai_http' && (
                  <div className="form-grid-2">
                    <div>
                      <label>API Key (可选，留空则读取环境变量)</label>
                      <input
                        type="password"
                        placeholder="sk-..."
                        value={agentForm.api_key}
                        onChange={e => setAgentForm(f => ({...f, api_key: e.target.value}))}
                      />
                    </div>
                    <div>
                      <label>或读取环境变量名</label>
                      <input
                        placeholder="DEEPSEEK_API_KEY"
                        value={agentForm.api_key_env}
                        onChange={e => setAgentForm(f => ({...f, api_key_env: e.target.value}))}
                      />
                    </div>
                  </div>
                )}

                <div>
                  <label>支持的模型列表 (逗号分隔，第一个为默认模型)</label>
                  <input
                    placeholder="例如: claude-sonnet-4-6, claude-opus-4-6"
                    value={agentForm.models_text}
                    onChange={e => setAgentForm(f => ({...f, models_text: e.target.value}))}
                  />
                </div>

                <div className="button-row">
                  <button type="submit" className="primary" disabled={agentBusy}>
                    <Plus size={14}/>
                    {agentBusy ? '正在保存…' : '保存并启用该 Agent'}
                  </button>
                  <button type="button" onClick={() => setShowAgentForm(false)}>取消</button>
                </div>
              </form>
            )}

            {(agentsHub.agents || []).filter(a => !a.builtin).length > 0 && (
              <div className="custom-agent-list">
                {(agentsHub.agents || []).filter(a => !a.builtin).map(ag => (
                  <div key={ag.id} className="custom-agent-row">
                    <div className="custom-agent-meta">
                      <strong>{ag.name}</strong>
                      <span className="meta-chip">{ag.adapter === 'openai_http' ? 'HTTP API' : 'CLI'}</span>
                      {ag.cloud_url && (
                        <span className="meta-chip" style={{color: '#2563eb', background: 'rgba(59, 130, 246, 0.12)'}}>
                          支持云端
                        </span>
                      )}
                      <span className={`engine-label ${ag.installed ? 'ready' : 'unavailable'}`}>
                        {ag.installed ? '已就绪' : '待安装 CLI'}
                      </span>
                      <small>{ag.adapter === 'openai_http' ? ag.base_url : (ag.resolved_binary || ag.binary)}</small>
                    </div>
                    <div className="custom-agent-actions">
                      <button
                        type="button"
                        onClick={() => handleCheckCustomAgent(ag.id)}
                        disabled={agentBusy}
                        title="检测连通性"
                      >
                        <RefreshCw size={12}/>检测
                      </button>
                      <button
                        type="button"
                        className="icon-button pwa-remove-btn"
                        onClick={() => handleDeleteAgent(ag.id, ag.name)}
                        disabled={agentBusy}
                        title="移除该 Agent"
                      >
                        <Trash2 size={13}/>
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            {agentNotice && <small className="sync-feedback">{agentNotice}</small>}
          </section>

          <section className="multi-agent-hub-section">
            <div className="engine-line">
              <h3>👤 多账号直接登录 (Codex / Google / Claude)</h3>
              <button type="button" onClick={refreshAccounts} disabled={acctBusy} title="刷新账号登录状态">
                <RefreshCw size={13}/> 刷新
              </button>
            </div>
            <p className="muted" style={{marginBottom: 10}}>
              无需任何配置，点击下方按钮直接打开浏览器登录新账号；支持随时暂停或一键删除。
            </p>
            <div className="button-row" style={{marginBottom: 10}}>
              <button type="button" className="primary" disabled={acctBusy} onClick={() => handleQuickLogin('codex')}>
                <Plus size={14}/> {acctBusy ? '正在获取授权链接…' : '登录 Codex 账号'}
              </button>
              <button type="button" className="primary" disabled={acctBusy} onClick={() => handleQuickLogin('claude-code')}>
                <Plus size={14}/> 登录 Claude 账号
              </button>
              <button type="button" disabled={acctBusy} onClick={() => handleQuickLogin('antigravity')}>
                <Plus size={14}/> 登录 Google 账号
              </button>
            </div>
            {pendingLogin && (
              <div className="rove-ui-card" style={{marginBottom: 10}}>
                <div className="rove-ui-card-title">🔐 正在授权：{pendingLogin.label}</div>
                {pendingLogin.code && (
                  <div className="engine-line">
                    <span>一次性设备验证码：<strong style={{fontSize: 16, letterSpacing: '0.06em'}}>{pendingLogin.code}</strong></span>
                    <button
                      type="button"
                      onClick={() => {
                        navigator.clipboard?.writeText(pendingLogin.code);
                        setAcctNotice(`已复制验证码 ${pendingLogin.code}，请前往授权页面粘贴。`);
                      }}
                    >
                      <Copy size={13}/> 复制验证码
                    </button>
                  </div>
                )}
                <div className="button-row">
                  <a
                    className="button-link primary"
                    href={pendingLogin.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <ExternalLink size={14}/> 打开登录授权页
                  </a>
                  <button type="button" onClick={refreshAccounts}>
                    <RefreshCw size={13}/> 我已完成授权，刷新状态
                  </button>
                </div>
              </div>
            )}
            {accounts.length > 0 && (
              <div className="agent-list">
                {accounts.map(acc => (
                  <div key={acc.id} className="agent-row">
                    <div>
                      <strong>{acc.label}</strong>{' '}
                      <span className="muted">({acc.engine})</span>{' '}
                      <span className={`engine-label ${acc.authenticated ? 'ready' : 'unchecked'}`}>
                        {acc.authenticated ? '已登录' : '待登录'}
                      </span>
                      {acc.paused ? <span className="muted"> · 已暂停</span> : null}
                    </div>
                    <div className="button-row">
                      {!acc.authenticated && (
                        <button
                          type="button"
                          disabled={acctBusy}
                          onClick={async () => {
                            setAcctBusy(true);
                            try {
                              const res = await post(`accounts/${acc.id}/login`, {});
                              if (res.login_url) {
                                setPendingLogin({label: acc.label, url: res.login_url, code: res.device_code || ''});
                                setAcctNotice(`已获取「${acc.label}」授权链接，请完成授权后刷新。`);
                                try { window.open(res.login_url, '_blank', 'noopener,noreferrer'); } catch {}
                              } else {
                                setAcctNotice(res.note || `已为「${acc.label}」启动登录。`);
                              }
                            } finally {
                              setAcctBusy(false);
                            }
                          }}
                        >
                          启动登录
                        </button>
                      )}
                      <button
                        type="button"
                        disabled={acctBusy}
                        onClick={async () => {
                          await post(`accounts/${acc.id}/pause?paused=${!acc.paused}`, {});
                          await refreshAccounts();
                        }}
                      >
                        {acc.paused ? '恢复' : '暂停'}
                      </button>
                      <button
                        type="button"
                        className="icon-button pwa-remove-btn"
                        disabled={acctBusy}
                        onClick={() => handleDeleteAccount(acc.id, acc.label)}
                        title="移除账号并清理隔离目录"
                      >
                        <Trash2 size={13}/>
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            {acctNotice && <small className="sync-feedback">{acctNotice}</small>}
          </section>

          <section className="pve-cluster-section">
            <h3>🛡️ PVE 通用节点部署与容灾自愈中心 (Standby & Self-Healing)</h3>
            <div className="engine-line">
              <span>
                <Server size={14} style={{verticalAlign: '-2px', marginRight: 6}}/>
                当前节点：{clusterStatus?.config?.node_name || 'Mac mini'}
              </span>
              <span className="engine-label ready">
                {clusterStatus?.config?.node_role === 'pve-standby' ? 'PVE 通用热备节点' : '主工作台节点'}
              </span>
            </div>
            <div className="engine-line" style={{marginTop: 6, fontSize: '12px', color: 'var(--text-muted)'}}>
              <span>
                💽 双盘冗余存储：
                <strong>盘 1</strong> (本地 NVMe: <code>{clusterStatus?.config?.remote_data_dir || '/var/lib/walking-workstation'}/snapshots</code>) +
                <strong> 盘 2</strong> (独立存储池: <code>{clusterStatus?.config?.pve_secondary_backup_dir || '/mnt/pve/backup/walking-workstation-snapshots'}</code>)
              </span>
            </div>
            <p className="setting-description">
              全量快照保存在项目目录之外的安全隔离区（含 SQLite 在线热备、自定义 Agent 配置及所有项目源码），并自动同时同步至 PVE 两个独立的物理硬盘/存储池。即使误删项目文件或单块硬盘损坏，也可随时一键自愈恢复或无缝切换至 PVE 节点继续开发。
            </p>

            {clusterStatus?.missing_projects_count > 0 && (
              <div className="pwa-guide-card" style={{borderColor: 'var(--amber)'}}>
                <strong>
                  <ShieldAlert size={14} style={{verticalAlign: '-2px', marginRight: 5}}/>
                  检测到 {clusterStatus.missing_projects_count} 个项目目录缺失或为空
                </strong>
                <p>若您不小心移动或删除了项目文件，可点击下方「一键自愈恢复缺失项目文件」从最近快照或 PVE 节点立即还原。</p>
              </div>
            )}

            <div className="button-row">
              <button type="button" className="primary" onClick={handleCreateSnapshot} disabled={clusterBusy}>
                <Database size={14}/>
                {clusterBusy ? '正在处理…' : '创建全量自愈快照 (含项目源码)'}
              </button>
              <button
                type="button"
                onClick={handleSelfHealMissingFiles}
                disabled={clusterBusy || !(clusterStatus?.snapshots?.length > 0)}
                title="仅恢复被误删或缺失的文件，绝不覆盖现有未提交修改"
              >
                <RotateCcw size={14}/>
                一键自愈恢复缺失项目文件
              </button>
              {clusterStatus?.snapshots?.length > 0 && (
                <a
                  className="button-link"
                  href={new URL(`api/cluster/snapshot/export?filename=${encodeURIComponent(clusterStatus.snapshots[0].filename)}`, base).toString()}
                  download={clusterStatus.snapshots[0].filename}
                >
                  <Download size={14}/>
                  下载最新恢复包 ({clusterStatus.snapshots[0].size_formatted})
                </a>
              )}
            </div>

            <div className="button-row" style={{marginTop: 8}}>
              <button type="button" onClick={handlePushToPve} disabled={clusterBusy}>
                <UploadCloud size={14}/>
                双盘同步快照至 PVE 节点
              </button>
              <button type="button" onClick={() => handlePullFromPve(false)} disabled={clusterBusy}>
                <DownloadCloud size={14}/>
                从 PVE 节点拉取恢复
              </button>
              <button type="button" onClick={handleProbePve} disabled={clusterBusy}>
                <Activity size={14}/>
                探测 PVE 节点连通性
              </button>
              <button type="button" onClick={() => setPveFormOpen(v => !v)}>
                <Server size={14}/>
                {pveFormOpen ? '收起 PVE 配置' : '配置 PVE 节点参数'}
              </button>
            </div>

            {pveFormOpen && (
              <form className="agent-config-form" onSubmit={handleSavePveConfig}>
                <div className="form-grid-2">
                  <div>
                    <label>当前节点名称</label>
                    <input
                      value={pveForm.node_name}
                      onChange={e => setPveForm(f => ({...f, node_name: e.target.value}))}
                      placeholder="Mac mini / PVE-Node"
                    />
                  </div>
                  <div>
                    <label>PVE 备用节点 HTTP 地址 (可选)</label>
                    <input
                      value={pveForm.peer_url}
                      onChange={e => setPveForm(f => ({...f, peer_url: e.target.value}))}
                      placeholder="例如: http://192.168.1.100:8765"
                    />
                  </div>
                </div>
                <div className="form-grid-2">
                  <div>
                    <label>PVE SSH 主机别名或 IP</label>
                    <input
                      value={pveForm.pve_ssh_host}
                      onChange={e => setPveForm(f => ({...f, pve_ssh_host: e.target.value}))}
                      placeholder="pve-node 或 192.168.1.254"
                    />
                  </div>
                  <div>
                    <label>PVE LXC 容器 ID (留空则直接部署在宿主机/VM)</label>
                    <input
                      value={pveForm.pve_ctid}
                      onChange={e => setPveForm(f => ({...f, pve_ctid: e.target.value}))}
                      placeholder="例如: 106 或 108"
                    />
                  </div>
                </div>
                <div className="form-grid-2">
                  <div>
                    <label>PVE 主盘同步目录 (盘 1 / 本地 NVMe 固态)</label>
                    <input
                      value={pveForm.remote_data_dir || ''}
                      onChange={e => setPveForm(f => ({...f, remote_data_dir: e.target.value}))}
                      placeholder="/var/lib/walking-workstation"
                    />
                  </div>
                  <div>
                    <label>PVE 备盘同步目录 (盘 2 / 独立存储池 / 双盘冗余)</label>
                    <input
                      value={pveForm.pve_secondary_backup_dir || ''}
                      onChange={e => setPveForm(f => ({...f, pve_secondary_backup_dir: e.target.value}))}
                      placeholder="/mnt/pve/backup/walking-workstation-snapshots"
                    />
                  </div>
                </div>
                <div className="pve-deploy-hint">
                  <div className="engine-line">
                    <strong>PVE 通用节点一键部署命令：</strong>
                    <button
                      type="button"
                      className="icon-button"
                      onClick={() => {
                        const cmd = `.venv/bin/python scripts/manage.py pve-deploy --host ${pveForm.pve_ssh_host || 'pve-home'}${pveForm.pve_ctid ? ` --ctid ${pveForm.pve_ctid}` : ''}`;
                        navigator.clipboard?.writeText(cmd);
                        setCopiedDeployCmd(true);
                        setTimeout(() => setCopiedDeployCmd(false), 1800);
                      }}
                    >
                      {copiedDeployCmd ? <Check size={13}/> : <Copy size={13}/>}
                    </button>
                  </div>
                  <code>
                    .venv/bin/python scripts/manage.py pve-deploy --host {pveForm.pve_ssh_host || 'pve-home'}{pveForm.pve_ctid ? ` --ctid ${pveForm.pve_ctid}` : ''}
                  </code>
                </div>
                <div className="button-row">
                  <button type="submit" className="primary" disabled={clusterBusy}>保存 PVE 节点配置</button>
                </div>
              </form>
            )}

            {clusterStatus?.config?.last_sync_status && (
              <small className="sync-feedback">{clusterStatus.config.last_sync_status}</small>
            )}
            {clusterNotice && <small className="sync-feedback">{clusterNotice}</small>}
          </section>

          <section>
            <h3>登记新项目</h3>
            <form onSubmit={add}>
              <label htmlFor="project-path">Mac mini 上的项目绝对路径（需在允许根目录内）</label>
              <div className="inline-form-row">
                <input
                  id="project-path"
                  placeholder="/Users/yourname/Projects/…"
                  value={path}
                  onChange={e => setPath(e.target.value)}
                  required
                />
                <button className="primary"><Plus size={15}/>添加</button>
              </div>
            </form>
          </section>

          <section>
            <h3>存储与数据生命周期治理 (SQLite Compaction)</h3>
            <p className="setting-description">
              自动管理 Mac mini 上的 state.sqlite3 数据库健康，安全精简历史会话流式 delta 碎片与超大图片内嵌数据，保留核心对话与代码记录，执行 VACUUM 释放物理空间。
            </p>
            {storageStats ? (
              <div className="storage-dashboard">
                <div className="storage-metric-grid">
                  <div className="storage-metric-card">
                    <span className="metric-title">数据库文件</span>
                    <span className="metric-value">{storageStats.total_size_formatted}</span>
                    <span className="metric-hint">WAL: {storageStats.wal_size_formatted}</span>
                  </div>
                  <div className="storage-metric-card">
                    <span className="metric-title">总事件记录</span>
                    <span className="metric-value">{storageStats.tables?.events?.count?.toLocaleString() || 0}</span>
                    <span className="metric-hint">{storageStats.tables?.events?.size_formatted}</span>
                  </div>
                  <div className="storage-metric-card">
                    <span className="metric-title">可精简碎片</span>
                    <span className="metric-value highlight">{storageStats.diagnostics?.compactable_events_total?.toLocaleString() || 0} 条</span>
                    <span className="metric-hint">流式增量与图片 blob</span>
                  </div>
                  <div className="storage-metric-card">
                    <span className="metric-title">超 14 天旧会话</span>
                    <span className="metric-value">{storageStats.diagnostics?.unpinned_old_14d || 0} 个</span>
                    <span className="metric-hint">置顶会话自动豁免</span>
                  </div>
                </div>

                <div className="storage-actions-box">
                  <div className="storage-options-row">
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={pruneOldSessions}
                        onChange={e => setPruneOldSessions(e.target.checked)}
                      />
                      <span>深度清理超过保留天数的未置顶旧会话</span>
                    </label>
                    <div className="select-pill-group">
                      <label htmlFor="keep-days-select">保留天数：</label>
                      <select
                        id="keep-days-select"
                        value={keepDays}
                        onChange={e => setKeepDays(Number(e.target.value))}
                      >
                        <option value={7}>7 天</option>
                        <option value={14}>14 天 (推荐)</option>
                        <option value={30}>30 天</option>
                      </select>
                    </div>
                  </div>

                  <div className="button-row">
                    <button
                      type="button"
                      className="primary"
                      onClick={() => handlePrune(false)}
                      disabled={pruningStorage}
                    >
                      <Sparkles size={14} className={pruningStorage ? 'spinning' : ''}/>
                      {pruningStorage ? '正在安全修剪并执行 VACUUM…' : '立即执行存储瘦身与 VACUUM'}
                    </button>
                    <button
                      type="button"
                      onClick={() => handlePrune(true)}
                      disabled={pruningStorage}
                      title="仅预估待清理的数据与体积，不执行实际删除"
                    >
                      安全演练评估 (Dry Run)
                    </button>
                    <button
                      type="button"
                      onClick={loadStorage}
                      disabled={loadingStorage}
                      title="刷新存储空间状态"
                    >
                      <RefreshCw size={14} className={loadingStorage ? 'spinning' : ''}/>
                      刷新数据
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <button type="button" onClick={loadStorage} disabled={loadingStorage}>
                <HardDrive size={14} className={loadingStorage ? 'spinning' : ''}/>
                {loadingStorage ? '正在读取存储健康指标…' : '查看数据库占用与存储瘦身'}
              </button>
            )}
            {storageNotice && <p className="sync-feedback storage-feedback">{storageNotice}</p>}
          </section>

          <section>
            <h3>安全与会话生命周期</h3>
            <p className="setting-description">
              关闭浏览器或切换网络后，后台任务与 PTY 终端持续运行；通过事件游标在任意设备上无缝接力。
            </p>
            <button type="button" onClick={onLogout}>
              <LogOut size={15}/>锁定并退出当前设备
            </button>
          </section>

          {error && <p className="error" role="alert">{error}</p>}
        </div>
      </section>
    </div>
  );
}
