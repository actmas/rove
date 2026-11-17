"""Zero-project Scratch workspaces and one-click promotion to full Git projects."""
from __future__ import annotations

import datetime
import os
from pathlib import Path
import re
import secrets
import subprocess
from typing import Any, Dict

from . import store as db


def scratch_root() -> Path:
    root = db.DATA / 'scratch'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _slugify(text: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]+', '-', (text or '').strip()).strip('-').lower()
    if not cleaned:
        return 'session'
    return cleaned[:28]


def create_scratch_project(title_hint: str = '') -> Dict[str, Any]:
    """Create an isolated scratch directory under ~/.config/walking-workstation/scratch and register it."""
    day = datetime.datetime.now().strftime('%Y-%m-%d')
    slug = _slugify(title_hint)
    short = secrets.token_hex(4)
    folder = (scratch_root() / f'{day}-{slug}-{short}').resolve()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    proj_id = secrets.token_hex(8)
    name = f'临时工作区 · {slug}'
    db.execute(
        'INSERT INTO projects(id, name, path, is_scratch) VALUES(?, ?, ?, 1)',
        (proj_id, name, str(folder)),
    )
    return db.one('SELECT * FROM projects WHERE id=?', (proj_id,))


def promote_scratch_project(project_id: str, name: str | None = None, init_git: bool = True) -> Dict[str, Any]:
    """Promote a scratch project into a permanent Rove project, optionally initializing Git."""
    proj = db.one('SELECT * FROM projects WHERE id=?', (project_id,))
    if not proj:
        raise ValueError('未找到临时工作区项目')
    proj_path = Path(proj['path']).resolve()
    proj_path.mkdir(parents=True, exist_ok=True)
    next_name = (name or '').strip() or proj['name'].replace('临时工作区 · ', '') or proj_path.name

    git_initialized = (proj_path / '.git').exists()
    if init_git and not git_initialized:
        subprocess.run(['git', 'init'], cwd=str(proj_path), capture_output=True, check=True, timeout=15)
        subprocess.run(['git', 'add', '-A'], cwd=str(proj_path), capture_output=True, check=False, timeout=15)
        subprocess.run(
            [
                'git', '-c', 'user.name=Rove Workstation', '-c', 'user.email=rove@local',
                'commit', '--allow-empty', '-m', 'Initial commit from Rove scratch workspace',
            ],
            cwd=str(proj_path),
            capture_output=True,
            check=True,
            timeout=15,
        )
        git_initialized = True

    db.execute('UPDATE projects SET is_scratch=0, name=? WHERE id=?', (next_name, project_id))
    updated = db.one('SELECT * FROM projects WHERE id=?', (project_id,))
    if updated:
        updated['git_initialized'] = git_initialized
    return updated
