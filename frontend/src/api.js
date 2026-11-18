export const base = new URL('.', window.location.href);
export const STATE_CACHE_KEY = 'walking-workstation:state-cache';

export function clearCachedState() {
  try {
    localStorage.removeItem(STATE_CACHE_KEY);
  } catch {}
}

export async function purgeCacheAndReload() {
  clearCachedState();
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {}
  if (typeof caches !== 'undefined') {
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    } catch {}
  }
  if (typeof navigator !== 'undefined' && navigator.serviceWorker) {
    try {
      const regs = await navigator.serviceWorker.getRegistrations();
      for (const r of regs) await r.unregister();
    } catch {}
  }
  window.location.reload(true);
}

if (typeof window !== 'undefined') {
  window.__purgeAndReload = purgeCacheAndReload;
}

export async function probeCloudflareAccessRedirect() {
  if (typeof window === 'undefined' || window.location.protocol !== 'https:') return false;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3000);
  try {
    const res = await fetch(new URL('healthz', base), {
      method: 'GET',
      redirect: 'manual',
      cache: 'no-store',
      credentials: 'same-origin',
      signal: controller.signal,
    });
    return res.type === 'opaqueredirect' || res.status === 302 || res.status === 403;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

export async function api(path, options = {}) {
  const {timeoutMs = path === 'state' ? 12000 : 20000, ...fetchOpts} = options;
  const controller = new AbortController();
  const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  let response;
  let body;
  try {
    response = await fetch(new URL(`api/${path}`, base), {
      ...fetchOpts,
      signal: fetchOpts.signal || controller.signal,
      headers: {'Content-Type': 'application/json', 'X-Workstation': '1', ...fetchOpts.headers},
      credentials: 'same-origin',
    });
    if (response.redirected && new URL(response.url).hostname.endsWith('cloudflareaccess.com')) {
      const error = new Error('Cloudflare Access 登录已过期，请重新验证');
      error.isAccessAuth = true;
      throw error;
    }
    body = await response.json();
  } catch (err) {
    if (err?.name === 'AbortError') {
      const timeoutErr = new Error('连接 Mac mini 响应超时，正在自动重试…');
      timeoutErr.isTimeout = true;
      throw timeoutErr;
    }
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }

  if (!response.ok) {
    if (response.status === 401 && path !== 'login') {
      window.dispatchEvent(new Event('workstation-locked'));
    }
    const err = new Error(typeof body.detail === 'string' ? body.detail : '请求未完成，请重试');
    err.status = response.status;
    throw err;
  }
  return body;
}

export function post(path, body = {}, options = {}) {
  return api(path, {method: 'POST', body: JSON.stringify(body), ...options});
}

export function patch(path, body = {}, options = {}) {
  return api(path, {method: 'PATCH', body: JSON.stringify(body), ...options});
}

export function del(path, options = {}) {
  return api(path, {method: 'DELETE', ...options});
}

export function uploadUrl(sessionId, filename) {
  return new URL(`api/sessions/${sessionId}/uploads/${encodeURIComponent(filename)}`, base).toString();
}

export function fileUrl(filePath, {sessionId, projectId, download = false} = {}) {
  const url = new URL('api/files', base);
  url.searchParams.set('path', String(filePath || '').trim());
  if (sessionId) url.searchParams.set('session_id', sessionId);
  if (projectId) url.searchParams.set('project_id', projectId);
  if (download) url.searchParams.set('download', '1');
  return url.toString();
}

export function fileMetaApiPath(filePath, {sessionId, projectId} = {}) {
  const params = new URLSearchParams({path: String(filePath || '').trim()});
  if (sessionId) params.set('session_id', sessionId);
  if (projectId) params.set('project_id', projectId);
  return `files/meta?${params.toString()}`;
}

function parseContentDispositionFilename(headerValue) {
  if (!headerValue) return '';
  const utf8Match = headerValue.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  if (utf8Match) {
    try {
      return decodeURIComponent(utf8Match[1].trim().replace(/^"|"$/g, ''));
    } catch {}
  }
  const plainMatch = headerValue.match(/filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\s]+)/i);
  return (plainMatch?.[1] || plainMatch?.[2] || '').trim();
}

export async function downloadFile(filePath, {sessionId, projectId, filename, onError} = {}) {
  const url = fileUrl(filePath, {sessionId, projectId, download: true});
  try {
    const res = await fetch(url, {
      method: 'GET',
      credentials: 'same-origin',
      headers: {'X-Workstation': '1'},
    });
    if (res.redirected && new URL(res.url).hostname.endsWith('cloudflareaccess.com')) {
      throw new Error('Cloudflare Access 登录已过期，请重新验证');
    }
    if (!res.ok) {
      if (res.status === 401) {
        window.dispatchEvent(new Event('workstation-locked'));
      }
      let message = '下载失败，请重试';
      try {
        const body = await res.json();
        if (typeof body?.detail === 'string') message = body.detail;
      } catch {}
      throw new Error(message);
    }
    const disposition = res.headers.get('content-disposition') || '';
    if (!/attachment/i.test(disposition)) {
      throw new Error('服务器未返回有效文件，请刷新后重试');
    }
    const blob = await res.blob();
    const resolvedName = filename
      || parseContentDispositionFilename(disposition)
      || String(filePath || '').split('/').pop()
      || 'download';
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = resolvedName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1500);
    return true;
  } catch (err) {
    onError?.(err?.message || '下载文件失败');
    return false;
  }
}

export function isLocalFileHref(href) {
  if (typeof href !== 'string') return false;
  const raw = href.trim();
  if (!raw || raw.startsWith('#')) return false;
  if (/^(https?:|mailto:|tel:|data:|blob:|javascript:)/i.test(raw)) return false;
  if (/^\/(api|assets)\//i.test(raw)) return false;
  if (/^(file:\/\/|sandbox:)/i.test(raw)) return true;
  if (raw.startsWith('/') || raw.startsWith('~/') || raw.startsWith('./') || raw.startsWith('../')) return true;
  // Relative file path like "docs/report.md" or "output.pdf"
  if (!/^[a-z][a-z0-9+.-]*:/i.test(raw)) return true;
  return false;
}

export function formatFileSize(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export const getJobTurnDiff = (sessionId, jobId) =>
  api(`sessions/${encodeURIComponent(sessionId)}/jobs/${encodeURIComponent(jobId)}/diff`);

export const revertJobCheckpoint = (sessionId, jobId, force = false) =>
  post(`sessions/${encodeURIComponent(sessionId)}/jobs/${encodeURIComponent(jobId)}/revert`, {force: !!force});

export const promoteScratchSession = (sessionId, payload = {}) =>
  post(`sessions/${encodeURIComponent(sessionId)}/promote`, payload);

export const gitCommitProject = (projectId, payload) =>
  post(`projects/${encodeURIComponent(projectId)}/git/commit`, payload);

export const gitPushProject = (projectId, payload = {}) =>
  post(`projects/${encodeURIComponent(projectId)}/git/push`, payload);

export function requestId() {
  return globalThis.crypto?.randomUUID?.() || Array.from(globalThis.crypto.getRandomValues(new Uint8Array(20)), n => n.toString(16).padStart(2, '0')).join('');
}

export const statusText = {
  queued: '准备中',
  running: '思考与执行中',
  waiting: '等待审批',
  submitted: '已提交云端',
  completed: '已完成',
  failed: '执行失败',
  interrupted: '进程中断',
  stopped: '已停止',
};

export const activeStates = new Set(['queued', 'running', 'waiting']);

export const effortBadge = {
  auto: '自动强度',
  low: '极速 Low',
  medium: '均衡 Med',
  high: '深入 High',
  xhigh: '高强 X-High',
  max: '极限 Max',
  ultra: '超算 Ultra',
};

export const modeBadge = {
  standard: '标准审批',
  full: '完全访问',
  'accept-edits': '接受修改',
  plan: '只读规划',
  auto: '自动执行',
};

export async function compressImageFile(file) {
  if (!file || !file.type.startsWith('image/')) {
    throw new Error('请选择有效的图片文件（PNG / JPEG / WebP / GIF）');
  }
  const readAsDataUrl = blob => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.readAsDataURL(blob);
  });

  // Keep small native PNG/WebP/GIF files intact without re-encoding artifacts.
  if (file.size <= 720 * 1024 && ['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type)) {
    return {name: file.name || 'image.png', dataUrl: await readAsDataUrl(file)};
  }
  if (file.type === 'image/gif') {
    if (file.size > 3.8 * 1024 * 1024) throw new Error('GIF 动图请控制在 4 MB 以内');
    return {name: file.name || 'image.gif', dataUrl: await readAsDataUrl(file)};
  }

  const rawUrl = await readAsDataUrl(file);
  const img = await new Promise((resolve, reject) => {
    const el = new Image();
    el.onload = () => resolve(el);
    el.onerror = () => reject(new Error('无法解析该图片'));
    el.src = rawUrl;
  });

  const maxSide = 1600;
  let {width, height} = img;
  if (width > maxSide || height > maxSide) {
    const ratio = Math.min(maxSide / width, maxSide / height);
    width = Math.max(1, Math.round(width * ratio));
    height = Math.max(1, Math.round(height * ratio));
  }
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);
  ctx.drawImage(img, 0, 0, width, height);
  const dataUrl = canvas.toDataURL('image/jpeg', 0.84);
  return {name: (file.name || 'screenshot').replace(/\.[^.]+$/, '') + '.jpg', dataUrl};
}

export function parseUnifiedDiff(patchText) {
  if (!patchText || !patchText.trim()) return [];
  const files = [];
  const chunks = patchText.split(/^diff --git /m).filter(Boolean);
  for (const chunk of chunks) {
    const lines = chunk.split('\n');
    const header = lines[0] || '';
    const match = header.match(/a\/(.+?)\s+b\/(.+)$/);
    const path = match ? match[2] : header.trim();
    let additions = 0, deletions = 0, oldLine = 0, newLine = 0;
    const rows = [];
    for (let i = 1; i < lines.length; i++) {
      const line = lines[i];
      if (line.startsWith('--- ') || line.startsWith('+++ ') || line.startsWith('index ') || line.startsWith('new file') || line.startsWith('deleted file') || line.startsWith('similarity ') || line.startsWith('rename ')) {
        continue;
      }
      const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/);
      if (hunk) {
        oldLine = Number(hunk[1]);
        newLine = Number(hunk[2]);
        rows.push({type: 'hunk', text: line, oldNum: '', newNum: ''});
        continue;
      }
      if (!rows.length && !line) continue;
      if (line.startsWith('+')) {
        additions++;
        rows.push({type: 'added', text: line, oldNum: '', newNum: newLine++});
      } else if (line.startsWith('-')) {
        deletions++;
        rows.push({type: 'removed', text: line, oldNum: oldLine++, newNum: ''});
      } else if (line.startsWith('\\')) {
        rows.push({type: 'meta', text: line, oldNum: '', newNum: ''});
      } else {
        if (i === lines.length - 1 && line === '') continue;
        rows.push({type: 'context', text: line, oldNum: oldLine ? oldLine++ : '', newNum: newLine ? newLine++ : ''});
      }
    }
    files.push({path, additions, deletions, rows});
  }
  return files;
}

export function downloadMarkdown(filename, content) {
  const blob = new Blob([content], {type: 'text/markdown;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function getStorageStats() {
  return api('system/storage');
}

export function pruneStorage(options = {}) {
  return post('system/storage/prune', options);
}

export function getAgentsHub() {
  return api('agents');
}

export function upsertAgent(payload) {
  return post('agents', payload);
}

export function deleteAgent(agentId) {
  return del(`agents/${encodeURIComponent(agentId)}`);
}

export function checkAgent(agentId) {
  return post(`agents/${encodeURIComponent(agentId)}/check`);
}

export function getClusterStatus() {
  return api('cluster/status');
}

export function updateClusterConfig(payload) {
  return patch('cluster/config', payload);
}

export function probeClusterPeer() {
  return post('cluster/probe');
}

export function createClusterSnapshot(options = {}) {
  return post('cluster/snapshot', options, {timeoutMs: 120000});
}

export function restoreClusterSnapshot(options = {}) {
  return post('cluster/restore', options, {timeoutMs: 120000});
}

export function pushClusterSnapshot(options = {}) {
  return post('cluster/push', options, {timeoutMs: 180000});
}

export function pullClusterSnapshot(options = {}) {
  return post('cluster/pull', options, {timeoutMs: 180000});
}

export function getSessionQueue(sessionId) {
  return api(`sessions/${encodeURIComponent(sessionId)}/queue`);
}

export function enqueueSessionMessage(sessionId, payload) {
  return post(`sessions/${encodeURIComponent(sessionId)}/queue`, payload);
}

export function updateQueuedMessage(queueId, payload) {
  return patch(`queue/${encodeURIComponent(queueId)}`, payload);
}

export function cancelQueuedMessage(queueId) {
  return del(`queue/${encodeURIComponent(queueId)}`);
}

export function pauseSessionQueue(sessionId, paused) {
  return post(`sessions/${encodeURIComponent(sessionId)}/queue/pause`, {paused: !!paused});
}

export function getHandoffPreview(sessionId, targetEngine, targetModel = '') {
  const qs = new URLSearchParams({target_engine: targetEngine});
  if (targetModel) qs.set('target_model', targetModel);
  return api(`sessions/${encodeURIComponent(sessionId)}/handoff-preview?${qs.toString()}`);
}

export function getWorkstationDiagnostics() {
  return api('diagnostics');
}

export function putProjectFile(projectId, payload) {
  return api(`projects/${encodeURIComponent(projectId)}/file`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function suggestGitCommit(projectId, payload = {}) {
  return post(`projects/${encodeURIComponent(projectId)}/git/suggest-commit`, payload);
}

export function createGitPr(projectId, payload = {}) {
  return post(`projects/${encodeURIComponent(projectId)}/git/pr`, payload);
}

export function createPairingToken() {
  return post('auth/pairing-token', {});
}

export function getPreviewPorts() {
  return api('preview/ports');
}

export function startArenaRun(payload) {
  return post('arena/start', payload);
}

export function getArenaStatus(arenaId) {
  return api(`arena/${encodeURIComponent(arenaId)}`);
}

export function startOrchestration(sessionId, payload) {
  return post(`sessions/${encodeURIComponent(sessionId)}/orchestrations`, payload);
}

export function getSessionOrchestrations(sessionId) {
  return api(`sessions/${encodeURIComponent(sessionId)}/orchestrations`);
}

export function getOrchestration(runId) {
  return api(`orchestrations/${encodeURIComponent(runId)}`);
}

export function actOrchestration(runId, payload) {
  return post(`orchestrations/${encodeURIComponent(runId)}/actions`, payload);
}

export function adoptArenaWinner(arenaId, winnerSessionId) {
  return post(`arena/${encodeURIComponent(arenaId)}/adopt`, {winner_session_id: winnerSessionId});
}

export function previewAutoSelect(payload) {
  return post('models/auto-select', payload);
}

export function getProjectCommands(projectId) {
  return api(`projects/${encodeURIComponent(projectId)}/commands`);
}

export function compactSessionContext(sessionId) {
  return post(`sessions/${encodeURIComponent(sessionId)}/compact`, {});
}

