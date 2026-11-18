import {useCallback, useEffect, useState} from 'react';
import {RefreshCw, Smartphone, Monitor, ExternalLink, Globe} from 'lucide-react';
import {base, getPreviewPorts} from './api';

export default function PreviewPane({onError}) {
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
          <button type="button" className="icon-button" onClick={() => setNonce(n => n + 1)} title="刷新预览">
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
            key={`${numericPort}-${cleanSub}-${nonce}`}
            src={iframeSrc}
            title={`Local Dev Preview :${numericPort}`}
            className={`preview-iframe ${viewport === 'mobile' ? 'is-mobile' : ''}`}
          />
        ) : (
          <p className="empty-note">请输入 3000–9999 范围内的本地开发端口（排除 8765）。</p>
        )}
      </div>
    </div>
  );
}
