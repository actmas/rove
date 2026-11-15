import React, {useState, useEffect} from 'react';
import {Download, X} from 'lucide-react';

const DISMISS_KEY = 'rove:dismiss-install-fab';

export default function InstallBanner({pwa}) {
  const [dismissed, setDismissed] = useState(true);
  const [tip, setTip] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    try {
      const stored = sessionStorage.getItem(DISMISS_KEY);
      if (!stored) setDismissed(false);
    } catch {
      setDismissed(false);
    }
  }, []);

  // ONLY show on Android devices that are NOT in standalone mode
  const isAndroid = Boolean(pwa?.device?.isAndroid);
  if (!isAndroid || pwa?.standalone || dismissed) {
    return null;
  }

  function handleDismiss(e) {
    e?.stopPropagation?.();
    setDismissed(true);
    try {
      sessionStorage.setItem(DISMISS_KEY, '1');
    } catch {}
  }

  async function handleInstall() {
    if (pwa?.triggerInstall) {
      try {
        const accepted = await pwa.triggerInstall();
        if (accepted) {
          setDismissed(true);
          return;
        }
      } catch {}
    }
    setTip(true);
    setTimeout(() => setTip(false), 3500);
  }

  return (
    <aside className="install-fab" aria-label="安装应用">
      {tip && (
        <div className="install-fab-tip">
          请点击浏览器右上角「⋮」选择「安装应用」或「添加到主屏幕」
        </div>
      )}
      <button
        type="button"
        className="install-fab-btn"
        onClick={handleInstall}
        title="安装应用"
      >
        <Download size={15}/>
        <span>安装应用</span>
      </button>
      <button
        type="button"
        className="install-fab-close"
        onClick={handleDismiss}
        title="关闭"
        aria-label="关闭"
      >
        <X size={13}/>
      </button>
    </aside>
  );
}
