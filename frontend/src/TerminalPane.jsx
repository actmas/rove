import {useEffect, useRef, useState} from 'react';
import {SquareTerminal} from 'lucide-react';
import {Terminal} from '@xterm/xterm';
import {FitAddon} from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import {api, post} from './api';

const LIGHT_XTERM = {
  background: '#ffffff',
  foreground: '#18181b',
  cursor: '#2563eb',
  selectionBackground: '#dbeafe',
  black: '#18181b',
  red: '#dc2626',
  green: '#16a34a',
  yellow: '#ca8a04',
  blue: '#2563eb',
  magenta: '#9333ea',
  cyan: '#0891b2',
  white: '#e4e4e7',
};

const DARK_XTERM = {
  background: '#0d0f13',
  foreground: '#e5e7eb',
  cursor: '#60a5fa',
  selectionBackground: '#1e3a8a',
  black: '#1f2937',
  red: '#f87171',
  green: '#4ade80',
  yellow: '#facc15',
  blue: '#60a5fa',
  magenta: '#c084fc',
  cyan: '#22d3ee',
  white: '#f3f4f6',
};

function encodeUtf8Base64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

const KEYS = [
  ['Esc', '\x1b'],
  ['Tab', '\t'],
  ['Ctrl+C', '\x03'],
  ['Ctrl+D', '\x04'],
  ['Ctrl+L', '\x0c'],
  ['↑', '\x1b[A'],
  ['↓', '\x1b[B'],
  ['←', '\x1b[D'],
  ['→', '\x1b[C'],
  ['git status', 'git status\r'],
];

export default function TerminalPane({session, onError, darkTheme}) {
  const element = useRef(null);
  const current = useRef(null);
  const sendInput = useRef(null);
  const [terminal, setTerminal] = useState(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setTerminal(null);
    if (session) {
      api(`sessions/${session.id}/terminal`)
        .then(t => {if (!cancelled) setTerminal(t);})
        .catch(e => onError(e.message));
    }
    return () => {cancelled = true;};
  }, [session?.id, onError]);

  useEffect(() => {
    if (current.current) {
      current.current.options.theme = darkTheme ? DARK_XTERM : LIGHT_XTERM;
    }
  }, [darkTheme]);

  async function start() {
    setStarting(true);
    try {
      setTerminal(await post(`sessions/${session.id}/terminal`));
    } catch (e) {
      onError(e.message);
    } finally {
      setStarting(false);
    }
  }

  useEffect(() => {
    if (!terminal || !element.current) return;
    const xterm = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      lineHeight: 1.35,
      fontFamily: '"SFMono-Regular", Menlo, Monaco, Consolas, monospace',
      scrollback: 10000,
      theme: darkTheme ? DARK_XTERM : LIGHT_XTERM,
    });
    const fit = new FitAddon();
    xterm.loadAddon(fit);
    xterm.open(element.current);
    current.current = xterm;

    let cancelled = false, cursor = 0, timer, chain = Promise.resolve(), live = terminal.status !== 'exited';
    function input(kind, data) {
      if (!live) return;
      chain = chain
        .then(() => post(`terminals/${terminal.id}/input`, {kind, data}))
        .catch(e => {if (!cancelled) onError(e.message);});
    }
    sendInput.current = data => input('data', encodeUtf8Base64(data));

    function resize() {
      if (element.current?.clientWidth && !cancelled) {
        fit.fit();
        if (live) input('resize', JSON.stringify({cols: xterm.cols, rows: xterm.rows}));
      }
    }
    const listener = xterm.onData(data => input('data', encodeUtf8Base64(data)));
    const observer = new ResizeObserver(resize);
    observer.observe(element.current);
    resize();

    async function poll() {
      try {
        const out = await api(`terminals/${terminal.id}/output?after=${cursor}`);
        if (cancelled) return;
        for (const chunk of out.chunks) {
          xterm.write(Uint8Array.from(atob(chunk.data), c => c.charCodeAt(0)));
          cursor = chunk.id;
        }
        if (out.status === 'exited') {
          if (live) setTerminal(t => ({...t, status: 'exited'}));
          live = false;
        }
        if (live || out.chunks.length >= 200) {
          const delay = document.hidden ? 4000 : (out.chunks.length >= 200 ? 0 : 220);
          timer = setTimeout(poll, delay);
        }
      } catch (e) {
        if (!cancelled) {
          onError(e.message);
          timer = setTimeout(poll, document.hidden ? 5000 : 3000);
        }
      }
    }
    poll();

    function handleVisibility() {
      if (!document.hidden && !cancelled && live) {
        clearTimeout(timer);
        poll();
      }
    }
    document.addEventListener('visibilitychange', handleVisibility);

    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', handleVisibility);
      observer.disconnect();
      listener.dispose();
      xterm.dispose();
      current.current = null;
      sendInput.current = null;
    };
  }, [terminal?.id, onError]);

  const running = terminal && terminal.status !== 'exited';

  return (
    <section className="terminal-pane">
      <div className="pane-toolbar">
        <div className="button-row">
          <SquareTerminal size={15}/>
          <strong>持久 PTY 终端</strong>
          {terminal && (
            <span className={`status-pill ${running ? 'ok' : 'muted'}`}>
              {running ? '运行中 · zsh' : '已退出'}
            </span>
          )}
        </div>
        <div className="button-row">
          {running && (
            <button type="button" onClick={() => current.current?.clear()}>
              清屏
            </button>
          )}
          {running && (
            <button
              type="button"
              onClick={() => post(`terminals/${terminal.id}/input`, {kind: 'stop'}).catch(e => onError(e.message))}
            >
              结束终端
            </button>
          )}
          {session && (!terminal || terminal.status === 'exited') && (
            <button type="button" className="primary" disabled={starting} onClick={start}>
              {starting ? '启动中…' : '启动终端'}
            </button>
          )}
        </div>
      </div>

      {!session ? (
        <div className="empty-note">请先新建或选择一个会话以绑定项目目录。</div>
      ) : !terminal ? (
        <div className="empty-note">
          点击右上角“启动终端”，在 Mac mini 项目目录开启真实 zsh。关闭网页后后台进程与输出依然保留。
        </div>
      ) : null}

      <div ref={element} className="terminal-canvas"/>

      {terminal && (
        <div className="terminal-keys">
          {KEYS.map(([label, seq]) => (
            <button
              type="button"
              disabled={!running}
              key={label}
              onClick={() => {
                sendInput.current?.(seq);
                current.current?.focus();
              }}
            >
              {label}
            </button>
          ))}
          <button type="button" onClick={() => current.current?.focus()}>
            唤起键盘
          </button>
        </div>
      )}
    </section>
  );
}
