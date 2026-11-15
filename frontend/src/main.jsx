import React, {Component} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App';
import './style.css';

window.__clearPrebootWatchdog?.();

class RootErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = {hasError: false, error: null};
  }
  static getDerivedStateFromError(error) {
    return {hasError: true, error};
  }
  componentDidCatch(error, info) {
    console.error('Workstation App Error:', error, info);
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="boot">
          <div className="boot-card">
            <strong style={{color: 'var(--danger, #ef4444)'}}>工作台界面发生未捕获异常</strong>
            <p className="boot-hint">
              {this.state.error?.message || '运行时脚本中断，建议重新加载或清除缓存'}
            </p>
            <div className="boot-actions">
              <button type="button" className="primary" onClick={() => window.location.reload(true)}>
                重新载入
              </button>
              <button type="button" onClick={() => window.__purgeAndReload?.() || window.location.reload(true)}>
                清除本地缓存重试
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById('root')).render(
  <RootErrorBoundary>
    <App/>
  </RootErrorBoundary>
);
