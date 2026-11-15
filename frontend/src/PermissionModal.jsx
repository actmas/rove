import React from 'react';
import {X, ShieldCheck, Zap, ShieldAlert, BookOpen, Bot, Check} from 'lucide-react';

export default function PermissionModal({
  isOpen,
  onClose,
  currentMode,
  onSelectMode,
  sessionTitle,
  engine = 'codex',
  catalog,
}) {
  if (!isOpen) return null;

  const engineModes = catalog?.modes?.[engine] || [
    {id: 'standard', label: '标准审批', description: '允许工作区读写与网络，敏感命令需手动批准确认'},
    {id: 'full', label: '完全访问 (Full)', description: '放行系统命令与网络访问（无沙箱限制），支持 SSH、跨目录与运维脚本'},
    {id: 'auto', label: '自动执行 (Auto)', description: '自动审批并放行网络，免等待手动确认，适合移动端后台全自动运行'},
    {id: 'plan', label: '只读规划 (Plan)', description: '只读沙箱，仅推演分析架构方案，不修改系统与代码'},
  ];

  const modeIcons = {
    standard: ShieldCheck,
    full: Zap,
    auto: Bot,
    plan: BookOpen,
  };

  const modeTones = {
    standard: 'blue',
    full: 'amber',
    auto: 'emerald',
    plan: 'purple',
  };

  return (
    <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <section role="dialog" aria-modal="true" aria-label="会话权限策略" className="settings-modal permission-modal">
        <header className="settings-header">
          <div className="settings-header-left">
            <ShieldCheck size={18} className="tone-blue-icon" />
            <div className="settings-title-group">
              <h2>会话权限与安全策略</h2>
              <span className="settings-subtitle">
                {sessionTitle ? `会话：${sessionTitle}` : '当前会话权限配置'}
              </span>
            </div>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </header>

        <div className="settings-body permission-modal-body">
          <div className="permission-notice-banner">
            <p>
              💡 <strong>提示</strong>：针对需要执行 SSH 连接远端服务器（如 PVE/NAS）、执行系统级命令或读取本机密钥的任务，请切换为<strong>「完全访问」</strong>模式。
            </p>
          </div>

          <div className="permission-modes-list">
            {engineModes.map(m => {
              const Icon = modeIcons[m.id] || ShieldCheck;
              const tone = modeTones[m.id] || 'blue';
              const isSelected = currentMode === m.id;
              return (
                <button
                  type="button"
                  key={m.id}
                  className={`permission-mode-card ${isSelected ? 'active' : ''} tone-${tone}`}
                  onClick={() => {
                    onSelectMode(m.id);
                    onClose();
                  }}
                >
                  <div className={`permission-card-icon card-icon tone-${tone}`}>
                    <Icon size={18} />
                  </div>
                  <div className="permission-card-text">
                    <div className="permission-card-title-row">
                      <strong>{m.label}</strong>
                      {isSelected && (
                        <span className="current-mode-tag">
                          <Check size={11} /> 当前生效
                        </span>
                      )}
                    </div>
                    <span className="permission-card-desc">{m.description}</span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      </section>
    </div>
  );
}
