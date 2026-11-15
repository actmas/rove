"""Independent PTY broker. Input/output is durable and replayable after web restarts."""
import base64
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios

from . import store as db


def run(ident):
    terminal = db.one('SELECT * FROM terminals WHERE id=?', (ident,))
    session = db.one('SELECT * FROM sessions WHERE id=?', (terminal['session_id'],))
    project = db.one('SELECT * FROM projects WHERE id=?', (session['project_id'],))
    workdir = db.session_workdir(session, project)
    child, fd = pty.fork()
    if child == 0:
        os.chdir(workdir)
        os.environ.update(TERM='xterm-256color', COLORTERM='truecolor',
                          PATH=str(db.ROOT / '.venv/bin') + ':' + str(os.path.expanduser('~/.local/bin')) + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin')
        shell_candidates = [os.environ.get('SHELL', ''), '/bin/zsh', '/usr/bin/zsh', '/bin/bash', '/usr/bin/bash', '/bin/sh']
        chosen_shell = next((s for s in shell_candidates if s and os.path.isfile(s) and os.access(s, os.X_OK)), '/bin/sh')
        os.execv(chosen_shell, [chosen_shell, '-l'])
    db.execute("UPDATE terminals SET status='running',pid=? WHERE id=?", (os.getpid(), ident))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
    stopped = False
    idle_ticks = 0
    chunk_counter = 0
    try:
        while True:
            had_activity = False
            if idle_ticks == 0 or idle_ticks % 3 == 0:
                inputs = db.query('SELECT * FROM terminal_input WHERE terminal_id=? ORDER BY id LIMIT 100', (ident,))
                if inputs:
                    had_activity = True
                    processed_ids = []
                    for item in inputs:
                        if item['kind'] == 'data':
                            os.write(fd, base64.b64decode(item['data']))
                        elif item['kind'] == 'resize':
                            size = json.loads(item['data'])
                            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', size['rows'], size['cols'], 0, 0))
                        elif item['kind'] == 'stop':
                            stopped = True
                        processed_ids.append(item['id'])
                    if processed_ids:
                        placeholders = ','.join('?' for _ in processed_ids)
                        db.execute(f'DELETE FROM terminal_input WHERE id IN ({placeholders})', tuple(processed_ids))
            if stopped:
                break
            wait_timeout = 0.06 if idle_ticks < 20 else 0.18
            if select.select([fd], [], [], wait_timeout)[0]:
                try:
                    chunk = os.read(fd, 16384)
                except OSError:
                    break
                if not chunk:
                    break
                had_activity = True
                db.execute('INSERT INTO terminal_output(terminal_id,data) VALUES(?,?)', (ident, base64.b64encode(chunk).decode()))
                chunk_counter += 1
                if chunk_counter % 100 == 0:
                    cutoff = db.one('SELECT id FROM terminal_output WHERE terminal_id=? ORDER BY id DESC LIMIT 1 OFFSET 1500', (ident,))
                    if cutoff:
                        db.execute('DELETE FROM terminal_output WHERE terminal_id=? AND id <= ?', (ident, cutoff['id']))
            idle_ticks = 0 if had_activity else min(idle_ticks + 1, 1000)
            if os.waitpid(child, os.WNOHANG)[0]:
                break
    finally:
        os.close(fd)
        # Only this PTY's process group is signalled; other sessions are untouched.
        try:
            os.killpg(child, signal.SIGHUP)
        except ProcessLookupError:
            pass
        db.execute("UPDATE terminals SET status='exited' WHERE id=?", (ident,))


if __name__ == '__main__':
    os.umask(0o077)
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())]) if sys.platform == 'darwin' else None
    try:
        run(sys.argv[1])
    finally:
        if awake:
            awake.terminate()
            awake.wait()
