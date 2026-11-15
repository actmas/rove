import React, {useState} from 'react';
import {
  X,
  Smartphone,
  Check,
  Copy,
  ExternalLink,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Download,
  Layers,
  Bell,
  Maximize2,
  ChevronRight,
} from 'lucide-react';
import RoveMark from './RoveMark';

export default function InstallGuideModal({isOpen, onClose, pwa}) {
  const [copiedOrigin, setCopiedOrigin] = useState(false);
  const [copiedFlag, setCopiedFlag] = useState(false);
  const [activeTab, setActiveTab] = useState('android');

  if (!isOpen) return null;

  const origin = typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:8765';
  const isHttps = typeof window !== 'undefined' && window.location.protocol === 'https:';
  const device = pwa?.device || {};
  const isAndroid = device.isAndroid;
  const isTablet = device.isTablet;
  const isIOS = device.isIOS;

  function copyText(text, setCopied) {
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  }

  async function handleTriggerInstall() {
    if (!pwa?.triggerInstall) return;
    try {
      const accepted = await pwa.triggerInstall();
      if (accepted) {
        onClose();
      }
    } catch {}
  }

  return (
    <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="install-guide-title"
        className="settings-modal install-guide-modal"
      >
        <div className="sheet-handle mobile-only" aria-hidden="true" />
        <div className="pane-toolbar">
          <div className="modal-title-group">
            <Smartphone size={18} className="install-modal-icon" />
            <h2 id="install-guide-title">
              {isTablet ? '平板端沉浸式 App 安装指南' : '独立 App 沉浸体验安装指南'}
            </h2>
          </div>
          <button type="button" className="icon-button" aria-label="关闭" onClick={onClose}>
            <X size={19} />
          </button>
        </div>

        <div className="settings-body install-guide-body">
          {/* Environment Status Card */}
          <div className="install-env-card">
            <div className="install-env-head">
              <div className="install-env-avatar">
                <RoveMark size={24} />
              </div>
              <div className="install-env-info">
                <strong>Rove · 随身编程工作台</strong>
                <span className="install-env-desc">
                  当前环境：{device.label || '当前设备'} · {isHttps ? 'HTTPS 安全连接' : '局域网 HTTP 直连'}
                </span>
              </div>
            </div>

            <div className="install-status-row">
              {pwa?.standalone ? (
                <div className="env-status-badge success">
                  <ShieldCheck size={14} />
                  <span>已运行于独立全屏 App 模式</span>
                </div>
              ) : pwa?.canInstall ? (
                <div className="env-status-badge success">
                  <Sparkles size={14} />
                  <span>浏览器已就绪原生 WebAPK 安装</span>
                </div>
              ) : isHttps ? (
                <div className="env-status-badge info">
                  <ShieldCheck size={14} />
                  <span>安全加密连接就绪</span>
                </div>
              ) : (
                <div className="env-status-badge warning">
                  <ShieldAlert size={14} />
                  <span>局域网 HTTP 访问（Android 限制了网页直接调用系统一键安装）</span>
                </div>
              )}
            </div>

            {pwa?.canInstall && !pwa?.standalone && (
              <div className="install-quick-action-box">
                <button
                  type="button"
                  className="install-hero-btn"
                  onClick={handleTriggerInstall}
                >
                  <Download size={15} />
                  <span>立即弹出系统安装器 (一键添加桌面)</span>
                </button>
              </div>
            )}
          </div>

          {/* Value Proposition Grid: Why Install? */}
          <div className="install-features-section">
            <h3 className="install-section-title">
              <Sparkles size={14} />
              <span>安装为独立 App 的沉浸体验优势</span>
            </h3>
            <div className="install-features-grid">
              <div className="install-feature-item">
                <div className="feature-icon-box tone-blue">
                  <Maximize2 size={16} />
                </div>
                <div className="feature-text">
                  <strong>无边框全屏沉浸视界</strong>
                  <p>自动隐藏浏览器顶底栏与书签栏，编辑区和终端高度提升 30%，充分发挥平板大屏优势。</p>
                </div>
              </div>
              <div className="install-feature-item">
                <div className="feature-icon-box tone-emerald">
                  <Layers size={16} />
                </div>
                <div className="feature-text">
                  <strong>多任务分屏与自由浮窗</strong>
                  <p>Android 系统原生支持将 Rove 作为独立 App 左右分屏或自由浮窗，边查资料边让 AI 编程。</p>
                </div>
              </div>
              <div className="install-feature-item">
                <div className="feature-icon-box tone-amber">
                  <Bell size={16} />
                </div>
                <div className="feature-text">
                  <strong>熄屏后台锁屏即时通知</strong>
                  <p>任务完成或需要权限审批时，即使平板熄屏切到后台，系统通知栏也能实时响铃提醒。</p>
                </div>
              </div>
            </div>
          </div>

          {/* Actionable Paths */}
          <div className="install-methods-section">
            <h3 className="install-section-title">
              <Smartphone size={14} />
              <span>三种获取平板沉浸体验的方法</span>
            </h3>

            {/* Method 1: Local Network Chrome White-list (No domain needed) */}
            <div className="install-method-card">
              <div className="method-card-header">
                <span className="method-step-number">方法 1</span>
                <div>
                  <strong>局域网 Chrome 信任白名单（极力推荐 · 局域网免域名）</strong>
                  <span className="method-tag">1 分钟配置 · 永久生效</span>
                </div>
              </div>
              <p className="method-description">
                Android Chrome 出于安全策略，默认仅允许在 HTTPS 下自动弹出 WebAPK。通过 Chrome 官方内置的允许列表，可将当前局域网地址加入受信任源，免除证书直接激活系统原生 WebAPK 安装与后台锁屏通知！
              </p>
              <div className="method-steps-list">
                <div className="method-step-row">
                  <span className="step-num-dot">1</span>
                  <div className="step-content">
                    <span>复制当前工作台访问地址：</span>
                    <div className="step-copy-row">
                      <code className="step-code-preview">{origin}</code>
                      <button
                        type="button"
                        className="step-copy-btn"
                        onClick={() => copyText(origin, setCopiedOrigin)}
                      >
                        {copiedOrigin ? <Check size={13} /> : <Copy size={13} />}
                        <span>{copiedOrigin ? '已复制' : '复制地址'}</span>
                      </button>
                    </div>
                  </div>
                </div>

                <div className="method-step-row">
                  <span className="step-num-dot">2</span>
                  <div className="step-content">
                    <span>在平板 Chrome 新标签页打开配置网址：</span>
                    <div className="step-copy-row">
                      <code className="step-code-preview">chrome://flags/#unsafely-treat-insecure-origin-as-secure</code>
                      <button
                        type="button"
                        className="step-copy-btn"
                        onClick={() => copyText('chrome://flags/#unsafely-treat-insecure-origin-as-secure', setCopiedFlag)}
                      >
                        {copiedFlag ? <Check size={13} /> : <Copy size={13} />}
                        <span>{copiedFlag ? '已复制' : '复制 Flag 网址'}</span>
                      </button>
                    </div>
                  </div>
                </div>

                <div className="method-step-row">
                  <span className="step-num-dot">3</span>
                  <div className="step-content">
                    <span>在打开页面的输入框中<strong>粘贴上述地址</strong>，并将右侧选项切换为 <strong>Enabled</strong>。</span>
                  </div>
                </div>

                <div className="method-step-row">
                  <span className="step-num-dot">4</span>
                  <div className="step-content">
                    <span>点击 Chrome 右下角弹出的蓝色 <strong>Relaunch</strong> 按钮重启浏览器。重新打开工作台即可直接一键安装原生 App！</span>
                  </div>
                </div>
              </div>
            </div>

            {/* Method 2: HTTPS Domain Access */}
            <div className="install-method-card">
              <div className="method-card-header">
                <span className="method-step-number">方法 2</span>
                <div>
                  <strong>经由 HTTPS 域名访问（全自动唤起原生系统安装器）</strong>
                  <span className="method-tag highlight">原生 WebAPK</span>
                </div>
              </div>
              <p className="method-description">
                在 HTTPS 安全域名下，Android 系统会自动识别为官方安全 PWA，直接由 Chrome 弹出系统级安装弹窗，并生成原生 WebAPK 安装包。
              </p>
              {typeof window !== 'undefined' && window.__ROVE_BOOTSTRAP__?.endpoints?.public_url && (
                <div className="method-jump-box">
                  <a
                    href={window.__ROVE_BOOTSTRAP__.endpoints.public_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="https-launch-btn"
                  >
                    <ExternalLink size={14} />
                    <span>前往 {window.__ROVE_BOOTSTRAP__.endpoints.public_url} 打开并安装</span>
                  </a>
                  <span className="method-sub-hint">
                    （注：若开启了 Cloudflare Access，首次访问输入邮箱验证码即可免密登录）
                  </span>
                </div>
              )}
            </div>

            {/* Method 3: Browser Menu Manual Add */}
            <div className="install-method-card">
              <div className="method-card-header">
                <span className="method-step-number">方法 3</span>
                <div>
                  <strong>浏览器菜单「添加到主屏幕」（免任何配置 · 10秒搞定）</strong>
                  <span className="method-tag">最快捷</span>
                </div>
              </div>
              <p className="method-description">
                所有 Android 浏览器（Chrome、Edge、三星浏览器等）都支持将网页手动生成桌面图标。通过桌面图标打开后，浏览器地址栏与导航按钮会自动隐藏，同样享受全屏沉浸体验！
              </p>
              <div className="method-steps-list">
                <div className="method-step-row">
                  <span className="step-num-dot">1</span>
                  <div className="step-content">
                    <span>点击平板浏览器右上角 <strong>「⋮」菜单</strong> 图标（或底栏「三道杠」菜单）。</span>
                  </div>
                </div>
                <div className="method-step-row">
                  <span className="step-num-dot">2</span>
                  <div className="step-content">
                    <span>在弹出菜单中，找到并点击 <strong>「安装应用」</strong> 或 <strong>「添加到主屏幕」</strong>。</span>
                  </div>
                </div>
                <div className="method-step-row">
                  <span className="step-num-dot">3</span>
                  <div className="step-content">
                    <span>点击 <strong>「添加」</strong> 确认。稍等片刻，平板桌面上就会出现带有高清图标的 <strong>「Rove」</strong> App！</span>
                  </div>
                </div>
              </div>
            </div>

            {/* iOS Safari Fallback */}
            {isIOS && (
              <div className="install-method-card">
                <div className="method-card-header">
                  <span className="method-step-number">iOS</span>
                  <div>
                    <strong>iPadOS / iOS Safari 安装方式</strong>
                  </div>
                </div>
                <div className="method-steps-list">
                  <div className="method-step-row">
                    <span className="step-num-dot">1</span>
                    <div className="step-content">
                      <span>点击 Safari 地址栏或工具栏的 <strong>「分享」图标</strong>。</span>
                    </div>
                  </div>
                  <div className="method-step-row">
                    <span className="step-num-dot">2</span>
                    <div className="step-content">
                      <span>在菜单中向下滑动，选择 <strong>「添加到主屏幕」</strong>。点击右上角「添加」。</span>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="pane-footer install-guide-footer">
          {pwa?.canInstall && !pwa?.standalone ? (
            <button
              type="button"
              className="primary"
              onClick={handleTriggerInstall}
            >
              <Download size={14} />
              立即唤起系统安装弹窗
            </button>
          ) : null}
          <button type="button" onClick={onClose}>
            完成
          </button>
        </div>
      </section>
    </div>
  );
}
