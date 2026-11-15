// Screen Wake Lock controller for iOS 16.4+, PWA Standalone and modern browsers
let sentinel = null;
let requested = false;

export async function acquireWakeLock() {
  requested = true;
  if (typeof navigator === 'undefined' || !('wakeLock' in navigator)) {
    return false;
  }
  try {
    if (sentinel && !sentinel.released) return true;
    sentinel = await navigator.wakeLock.request('screen');
    sentinel.addEventListener('release', () => {
      sentinel = null;
      if (requested && typeof document !== 'undefined' && document.visibilityState === 'visible') {
        acquireWakeLock();
      }
    });
    return true;
  } catch {
    return false;
  }
}

export async function releaseWakeLock() {
  requested = false;
  if (sentinel) {
    try {
      await sentinel.release();
    } catch {}
    sentinel = null;
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && requested) {
      acquireWakeLock();
    }
  });
}
