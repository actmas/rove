import {useCallback, useEffect, useState} from 'react';
import {post} from './api';

let deferredInstallPrompt = null;
let swRegistrationPromise = null;
const installListeners = new Set();

function notifyInstallListeners() {
  for (const fn of installListeners) fn();
}

if (typeof window !== 'undefined') {
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    deferredInstallPrompt = event;
    notifyInstallListeners();
  });

  window.addEventListener('appinstalled', () => {
    deferredInstallPrompt = null;
    notifyInstallListeners();
  });
}

export function isStandaloneMode() {
  if (typeof window === 'undefined') return false;
  return Boolean(
    window.matchMedia?.('(display-mode: standalone)').matches ||
    window.matchMedia?.('(display-mode: fullscreen)').matches ||
    window.matchMedia?.('(display-mode: minimal-ui)').matches ||
    window.matchMedia?.('(display-mode: window-controls-overlay)').matches ||
    window.navigator.standalone === true,
  );
}

export function detectDeviceInfo() {
  if (typeof navigator === 'undefined') {
    return {isIOS: false, isAndroid: false, isTablet: false, label: '当前设备'};
  }
  const ua = navigator.userAgent || '';
  const isIPadOS = /Macintosh/i.test(ua) && (navigator.maxTouchPoints || 0) > 1;
  const isIPad = /iPad/i.test(ua) || isIPadOS;
  const isIPhone = /iPhone|iPod/i.test(ua);
  const isIOS = isIPad || isIPhone;
  const isAndroid = /Android/i.test(ua);
  const isAndroidTablet = isAndroid && !/Mobile/i.test(ua);

  let label = '桌面浏览器';
  if (isIPhone) label = 'iPhone (iOS)';
  else if (isIPad) label = 'iPad (iPadOS)';
  else if (isAndroidTablet) label = 'Android 平板';
  else if (isAndroid) label = 'Android 手机';
  else if (/Macintosh/i.test(ua)) label = 'Mac 桌面端';
  else if (/Windows/i.test(ua)) label = 'Windows 桌面端';

  return {
    isIOS,
    isAndroid,
    isTablet: isIPad || isAndroidTablet,
    label,
  };
}

export function registerWorkstationServiceWorker() {
  if (typeof window === 'undefined') return Promise.resolve(null);
  if (!window.isSecureContext || !('serviceWorker' in navigator)) {
    return Promise.resolve(null);
  }
  if (!swRegistrationPromise) {
    swRegistrationPromise = navigator.serviceWorker
      .register('/sw.js', {scope: '/'})
      .catch(() => null);
  }
  return swRegistrationPromise;
}

export function urlBase64ToUint8Array(base64String) {
  const cleaned = (base64String || '').trim();
  const padding = '='.repeat((4 - (cleaned.length % 4)) % 4);
  const base64 = (cleaned + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

export function arrayBufferToBase64Url(buffer) {
  if (!buffer) return '';
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return window.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export async function sha256Hex(text) {
  if (!window.crypto?.subtle) return '';
  const data = new TextEncoder().encode((text || '').trim());
  const digest = await window.crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(digest))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}

export function clearAppBadgeSafe() {
  if (typeof navigator !== 'undefined' && typeof navigator.clearAppBadge === 'function') {
    navigator.clearAppBadge().catch(() => {});
  }
}

export function usePwaManager(webPushState, onRefresh) {
  const [standalone, setStandalone] = useState(isStandaloneMode);
  const [canInstall, setCanInstall] = useState(() => Boolean(deferredInstallPrompt));
  const [swReady, setSwReady] = useState(false);
  const [localEndpoint, setLocalEndpoint] = useState('');
  const [localEndpointHash, setLocalEndpointHash] = useState('');
  const [permission, setPermission] = useState(() => (
    typeof Notification !== 'undefined' ? Notification.permission : 'unsupported'
  ));

  const secureContext = typeof window !== 'undefined' ? Boolean(window.isSecureContext) : false;
  const device = detectDeviceInfo();
  const pushSupported = Boolean(
    secureContext &&
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window,
  );

  const syncLocalSubscription = useCallback(async () => {
    if (!pushSupported) {
      setLocalEndpoint('');
      setLocalEndpointHash('');
      return null;
    }
    const reg = await registerWorkstationServiceWorker();
    if (!reg) return null;
    setSwReady(true);
    try {
      const sub = await reg.pushManager.getSubscription();
      if (sub?.endpoint) {
        setLocalEndpoint(sub.endpoint);
        const h = await sha256Hex(sub.endpoint);
        setLocalEndpointHash(h);
        return sub;
      }
    } catch {}
    setLocalEndpoint('');
    setLocalEndpointHash('');
    return null;
  }, [pushSupported]);

  useEffect(() => {
    registerWorkstationServiceWorker().then(reg => {
      if (reg) setSwReady(true);
      syncLocalSubscription();
    });

    function onPromptChange() {
      setCanInstall(Boolean(deferredInstallPrompt));
      setStandalone(isStandaloneMode());
    }
    installListeners.add(onPromptChange);

    const mq = window.matchMedia?.('(display-mode: standalone)');
    const onDisplayChange = () => setStandalone(isStandaloneMode());
    mq?.addEventListener?.('change', onDisplayChange);

    return () => {
      installListeners.delete(onPromptChange);
      mq?.removeEventListener?.('change', onDisplayChange);
    };
  }, [syncLocalSubscription]);

  const triggerInstall = useCallback(async () => {
    if (!deferredInstallPrompt) return false;
    const promptEvent = deferredInstallPrompt;
    promptEvent.prompt();
    const choice = await promptEvent.userChoice.catch(() => ({outcome: 'dismissed'}));
    if (choice?.outcome === 'accepted') {
      deferredInstallPrompt = null;
      notifyInstallListeners();
      return true;
    }
    return false;
  }, []);

  const serverSubscriptions = webPushState?.subscriptions || [];
  const registeredOnServer = Boolean(
    localEndpointHash && serverSubscriptions.some(item => item.endpoint_hash === localEndpointHash),
  );
  const isSubscribed = Boolean(localEndpoint && registeredOnServer && permission === 'granted');

  const enablePush = useCallback(async () => {
    if (!pushSupported) {
      throw new Error(
        device.isIOS && !standalone
          ? '请先点击 Safari 分享按钮并选择「添加到主屏幕」，从主屏幕打开 App 后即可开启 iOS 锁屏推送。'
          : '当前浏览器环境不支持 Web Push（请通过 HTTPS 域名访问）。',
      );
    }
    const publicKey = webPushState?.public_key;
    if (!publicKey) {
      throw new Error('服务端尚未返回 VAPID 公钥，请刷新后重试。');
    }

    const perm = await Notification.requestPermission();
    setPermission(perm);
    if (perm !== 'granted') {
      throw new Error('通知权限未开启，请在系统设置中允许「Rove」发送通知。');
    }

    const reg = await registerWorkstationServiceWorker();
    if (!reg) {
      throw new Error('Service Worker 尚未就绪，请刷新页面后重试。');
    }
    await navigator.serviceWorker.ready;

    let sub = await reg.pushManager.getSubscription();
    if (sub) {
      const existingKey = arrayBufferToBase64Url(sub.options?.applicationServerKey);
      if (existingKey && existingKey !== publicKey) {
        await sub.unsubscribe().catch(() => {});
        sub = null;
      }
    }

    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });
    }

    const p256dh = arrayBufferToBase64Url(sub.getKey('p256dh'));
    const auth = arrayBufferToBase64Url(sub.getKey('auth'));
    const res = await post('push/subscribe', {
      endpoint: sub.endpoint,
      keys: {p256dh, auth},
      device_label: device.label,
    });
    setLocalEndpoint(sub.endpoint);
    setLocalEndpointHash(res.endpoint_hash || await sha256Hex(sub.endpoint));
    if (onRefresh) await onRefresh();
    return res;
  }, [device.isIOS, device.label, onRefresh, pushSupported, standalone, webPushState?.public_key]);

  const disablePush = useCallback(async () => {
    const reg = await registerWorkstationServiceWorker();
    const sub = await reg?.pushManager?.getSubscription().catch(() => null);
    const endpoint = sub?.endpoint || localEndpoint;
    if (endpoint) {
      await post('push/unsubscribe', {endpoint}).catch(() => {});
    }
    if (sub) {
      await sub.unsubscribe().catch(() => {});
    }
    setLocalEndpoint('');
    setLocalEndpointHash('');
    if (onRefresh) await onRefresh();
  }, [localEndpoint, onRefresh]);

  const removeDeviceById = useCallback(async id => {
    const target = serverSubscriptions.find(item => item.id === id);
    await post('push/unsubscribe', {id});
    if (target && target.endpoint_hash === localEndpointHash) {
      const reg = await registerWorkstationServiceWorker();
      const sub = await reg?.pushManager?.getSubscription().catch(() => null);
      if (sub) await sub.unsubscribe().catch(() => {});
      setLocalEndpoint('');
      setLocalEndpointHash('');
    }
    if (onRefresh) await onRefresh();
  }, [localEndpointHash, onRefresh, serverSubscriptions]);

  const sendTestPush = useCallback(async activeSessionId => {
    return await post('push/test', {
      endpoint: localEndpoint || null,
      session_id: activeSessionId || null,
    });
  }, [localEndpoint]);

  return {
    standalone,
    secureContext,
    swReady,
    canInstall,
    triggerInstall,
    device,
    pushSupported,
    permission,
    isSubscribed,
    localEndpointHash,
    serverSubscriptions,
    enablePush,
    disablePush,
    removeDeviceById,
    sendTestPush,
  };
}
