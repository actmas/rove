"""Build using an installed Node or this Mac's bundled Codex runtime, and stamp App-Shell precache manifest into dist/sw.js."""
import os
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
node = shutil.which('node')
if not node:
    for candidate in (
        Path('/opt/homebrew/bin/node'),
        Path('/usr/local/bin/node'),
        Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node',
    ):
        if candidate.is_file():
            node = str(candidate)
            break
if not node:
    raise SystemExit('需要 Node.js 22+。当前 Mac 没有找到可用运行时。')
env = {**os.environ, 'PATH': str(Path(node).parent) + ':' + os.environ.get('PATH', '')}
vite = root / 'frontend/node_modules/vite/bin/vite.js'
if not vite.is_file():
    raise SystemExit('请先在 frontend 安装 pnpm-lock.yaml 中的依赖。')
subprocess.run([node, str(vite), 'build'], cwd=root / 'frontend', env=env, check=True)
