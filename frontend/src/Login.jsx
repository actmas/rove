import {useState} from 'react';
import {ArrowUpRight, Lock} from 'lucide-react';
import {post} from './api';
import RoveMark from './RoveMark';

export default function Login({onUnlock}) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await post('login', {password});
      setPassword('');
      await onUnlock();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <div className="login-brand">
        <div className="brand-mark rove-brand-mark"><RoveMark size={22}/></div>
        <span>Rove</span>
      </div>

      <form className="login-card" onSubmit={submit}>
        <div className="login-header">
          <h1>连接你的 Mac mini</h1>
          <p>远程操控本地代码库、Codex 与持久终端。</p>
        </div>

        <input type="text" name="username" defaultValue="workstation" autoComplete="username" hidden readOnly/>
        <label htmlFor="password">访问密码</label>
        <div className="login-input-wrap">
          <Lock size={15}/>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            placeholder="输入工作台访问密码…"
            required
            value={password}
            onChange={e => setPassword(e.target.value)}
            autoFocus
          />
        </div>

        {error && <p className="error" role="alert">{error}</p>}

        <button className="primary login-submit" disabled={busy}>
          <span>{busy ? '正在验证…' : '解锁工作台'}</span>
          <ArrowUpRight size={16}/>
        </button>

        <small>
          如需重置密码，请在宿主机运行 <code>python scripts/manage.py password</code>。
          {typeof window !== 'undefined' && (() => {
            const ep = window.__ROVE_BOOTSTRAP__?.endpoints || {};
            const isHttps = window.location.protocol === 'https:';
            const targetUrl = isHttps ? ep.lan_url : ep.public_url;
            if (!targetUrl) return null;
            const hostLabel = targetUrl.replace(/^https?:\/\//, '');
            return (
              <div style={{marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '4px'}}>
                <a href={targetUrl} style={{color: 'var(--accent, #38bdf8)', textDecoration: 'none'}}>
                  {isHttps ? `📡 切换至局域网直连 (${hostLabel})` : `🌐 切换至公网域名 (${hostLabel})`}
                </a>
              </div>
            );
          })()}
        </small>
      </form>

      <footer>计算与代码留在本地 · 随时随地接着构建</footer>
    </main>
  );
}
