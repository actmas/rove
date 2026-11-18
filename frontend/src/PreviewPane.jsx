import {useCallback, useEffect, useRef, useState} from 'react';
import {RefreshCw, Smartphone, Monitor, ExternalLink, Globe, Crosshair, Bug} from 'lucide-react';
import {base, getPreviewPorts} from './api';

export default function PreviewPane({onError, onAskPreview}) {
  const [ports, setPorts] = useState([]);
  const [port, setPort] = useState(() => {
    try {
      return localStorage.getItem('rove-preview-port') || '5173';
    } catch {
      return '5173';
    }
  });
  const [subpath, setSubpath] = useState('');
  const [viewport, setViewport] = useState('desktop');
  const [nonce, setNonce] = useState(0);
  const [probing, setProbing] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const [consoleErrors, setConsoleErrors] = useState([]);
  const iframeRef = useRef(null);

  useEffect(() => {
    const win = iframeRef.current?.contentWindow;
    if (win) {
      try { win.postMessage({type: 'rove-inspect-toggle', active: inspecting}, '*'); } catch {}
    }
  }, [inspecting]);

  useEffect(() => {
    function onMsg(ev) {
      const d = ev.data;
      if (!d || typeof d !== 'object') return;
      if (d.type === 'rove-preview-error' && d.message) {
        setConsoleErrors(prev => [d.message, ...prev.filter(x => x !== d.message)].slice(0, 15));
      } else if (d.type === 'rove-preview-pick') {
        setInspecting(false);
        const snippet = [
          `[预览元素拾取 · localhost:${port}/${subpath.replace(/^\/+/, '')}]`,
          `选择器: \`${d.selector || d.tag}\``,
          d.text ? `文本: "${d.text}"` : '',
          '请基于此页面元素进行修改或优化：',
        ].filter(Boolean).join('\n');
        onAskPreview?.(snippet);
      }
    }
    window.addEventListener('message', onMsg);
    return () => window.removeEventListener('message', onMsg);
  }, [port, subpath, onAskPreview]);

  const probe = useCallback(async () => {
    setProbing(true);
    try {
      const res = await getPreviewPorts();
      const found = res.ports || [];
      setPorts(found);
      if (found.length > 0 && !found.includes(Number(port))) {
        const next = String(found[0]);
        setPort(next);
        try { localStorage.setItem('rove-preview-port', next); } catch {}
      }
    } catch (e) {
      onError?.(e.message);
    } finally {
      setProbing(false);
    }
  }, [port, onError]);

  useEffect(() => {
    probe();
  }, []);

  const numericPort = Number(port);
  const validPort = Number.isInteger(numericPort) && numericPort >= 3000 && numericPort <= 9999 && numericPort !== 8765;
  const cleanSub = subpath.replace(/^\/+/, '');
  const iframeSrc = validPort
    ? new URL(`api/preview/${numericPort}/${cleanSub}${cleanSub.includes('?') ? '&' : '?'}__r=${nonce}`, base).toString()
    : '';

  return (
    <div className="preview-pane">
      <div className="preview-toolbar">
        <div className="preview-port-group">
          <Globe size={14}/>
          <span>本地端口</span>
          <input
            type="number"
            min={3000}
            max={9999}
            value={port}
            onChange={e => {
              setPort(e.target.value);
              try { localStorage.setItem('rove-preview-port', e.target.value); } catch {}
            }}
            aria-label="预览端口号"
            className="preview-port-input"
          />
          {ports.map(p => (
            <button
              key={p}
              type="button"
              className={`preview-chip ${Number(port) === p ? 'active' : ''}`}
              onClick={() => {
                setPort(String(p));
                try { localStorage.setItem('rove-preview-port', String(p)); } catch {}
              }}
            >
              :{p}
            </button>
          ))}
          <button type="button" className="icon-button" onClick={probe} title="重新扫描本地开发端口" disabled={probing}>
            <RefreshCw size={14} className={probing ? 'spin' : ''}/>
          </button>
        </div>

        <div className="preview-port-group">
          <input
            type="text"
            value={subpath}
            onChange={e => setSubpath(e.target.value)}
            placeholder="子路径，如 dashboard"
            className="preview-path-input"
            aria-label="预览子路径"
          />
          <button
            type="button"
            className={`icon-button ${viewport === 'desktop' ? 'active' : ''}`}
            onClick={() => setViewport('desktop')}
            title="桌面宽度"
          >
            <Monitor size={14}/>
          </button>
          <button
            type="button"
            className={`icon-button ${viewport === 'mobile' ? 'active' : ''}`}
            onClick={() => setViewport('mobile')}
            title="手机宽度 (390px)"
          >
            <Smartphone size={14}/>
          </button>
          <button
            type="button"
            className={`icon-button ${inspecting ? 'active' : ''}`}
            onClick={() => setInspecting(v => !v)}
            title={inspecting ? '退出元素拾取' : '点击页面元素直接回传给 AI 修改'}
          >
            <Crosshair size={14}/>
          </button>
          {consoleErrors.length > 0 && (
            <button
              type="button"
              className="icon-button preview-err-btn"
              onClick={() => {
                const msg = `[预览控制台报错 (${consoleErrors.length}) · localhost:${numericPort}]\n` + consoleErrors.slice(0, 6).map(e => `- ${e}`).join('\n') + '\n请排查并修复以上前端报错：';
                onAskPreview?.(msg);
              }}
              title={`抓取 ${consoleErrors.length} 条控制台报错并回传给 AI`}
            >
              <Bug size={14}/>
              <span>{consoleErrors.length}</span>
            </button>
          )}
          <button type="button" className="icon-button" onClick={() => { setConsoleErrors([]); setNonce(n => n + 1); }} title="刷新预览">
            <RefreshCw size={14}/>
          </button>
          {iframeSrc && (
            <a className="icon-button" href={iframeSrc} target="_blank" rel="noreferrer" title="在新标签页打开">
              <ExternalLink size={14}/>
            </a>
          )}
        </div>
      </div>

      <div className="preview-stage">
        {validPort ? (
          <iframe
            ref={iframeRef}
            key={`${numericPort}-${cleanSub}-${nonce}`}
            src={iframeSrc}
            title={`Local Dev Preview :${numericPort}`}
            sandbox="allow-scripts allow-forms allow-modals allow-popups"
            className={`preview-iframe ${viewport === 'mobile' ? 'is-mobile' : ''}`}
          />
        ) : (
          <p className="empty-note">请输入 3000–9999 范围内的本地开发端口（排除 8765）。</p>
        )}
      </div>
    </div>
  );
}
