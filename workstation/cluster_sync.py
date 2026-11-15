"""PVE universal standby node synchronization, self-healing workspace snapshots, and cross-platform recovery.

Addresses two core resilience requirements:
1. Disaster Recovery / Accidental Deletion Protection:
   Creates self-contained snapshots (SQLite online hot backup + custom agents + project source trees)
   stored outside the project directory (`~/.config/walking-workstation/snapshots`) and syncable to a
   PVE standby node. If project files are accidentally deleted on the computer, one-click self-healing
   restores missing files or entire workspaces immediately.
2. Seamless Mac ⇄ PVE Failover:
   Automatically remaps workspace paths between macOS (`/Users/...`) and Linux/PVE (`/var/lib/walking-workstation/workspaces/...`
   or custom roots) when importing a snapshot on a standby PVE node or restoring back to Mac.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import store as db

SAFE_SSH_HOST_RE = re.compile(r'^[a-zA-Z0-9_.-]+(@[a-zA-Z0-9_.-]+)?(:[0-9]+)?$')


def validate_ssh_host(host: str | None) -> str | None:
    if not host:
        return None
    h = host.strip()
    if not h:
        return None
    if h.startswith('-') or not SAFE_SSH_HOST_RE.match(h):
        raise ValueError(f'非法的 SSH 主机名格式：{h}')
    return h


def validate_peer_url(url: str | None) -> str | None:
    if not url:
        return None
    cleaned = url.strip().rstrip('/')
    if not cleaned:
        return None
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError(f'非法的节点 URL 格式：{url}，必须以 http:// 或 https:// 开头')
    return cleaned

SNAPSHOT_EXCLUDE_DIRS = {
    '.venv', 'venv', 'env', 'node_modules', '__pycache__',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', '.next',
    '.nuxt', 'target', 'build', '.gradle', '.worktrees',
    'backups', 'snapshots', 'uploads', 'logs', '.cache', 'cache', 'tmp',
}
SNAPSHOT_EXCLUDE_SUFFIXES = (
    '.pyc', '.pyo', '.DS_Store', '.swp', '.tar', '.tar.gz', '.tgz',
    '.zip', '.7z', '.rar', '.dmg', '.iso', '.apk', '.ipa', '.mp4',
    '.mov', '.mkv', '.avi', '.gguf', '.safetensors', '.onnx', '.pt', '.pth',
    '.sqlite3-wal', '.sqlite3-shm',
)
MAX_FILE_BYTES_IN_SNAPSHOT = 2 * 1024 * 1024  # Skip single binary blobs > 2MB in workspace tar


def _cluster_config_path() -> Path:
    return db.DATA / 'cluster.json'


def snapshots_dir() -> Path:
    d = db.DATA / 'snapshots'
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def default_node_name() -> str:
    env_name = os.environ.get('WORKSTATION_NODE_NAME', '').strip()
    if env_name:
        return env_name
    host = socket.gethostname() or 'workstation'
    if sys.platform == 'darwin':
        return f'Mac ({host})'
    return f'PVE-Node ({host})'


def get_cluster_config() -> Dict[str, Any]:
    path = _cluster_config_path()
    defaults: Dict[str, Any] = {
        'node_name': default_node_name(),
        'node_role': os.environ.get('WORKSTATION_ROLE', 'primary' if sys.platform == 'darwin' else 'pve-standby'),
        'peer_url': '',
        'pve_ssh_host': 'pve-home',
        'pve_ctid': '',
        'remote_data_dir': '/var/lib/walking-workstation',
        'pve_secondary_backup_dir': '/mnt/pve/backup/walking-workstation-snapshots',
        'remote_workspace_root': '/var/lib/walking-workstation/workspaces',
        'cluster_token': '',
        'max_snapshots': 8,
        'last_sync_at': None,
        'last_sync_status': None,
    }
    if path.is_file():
        with contextlib.suppress(Exception):
            loaded = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                defaults.update(loaded)
    if not defaults.get('cluster_token'):
        defaults['cluster_token'] = secrets.token_urlsafe(24)
        with contextlib.suppress(Exception):
            db.write_private(path, json.dumps(defaults, ensure_ascii=False, indent=2))
    return defaults


def update_cluster_config(patch_data: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_cluster_config()
    allowed_keys = {
        'node_name', 'node_role', 'peer_url', 'pve_ssh_host', 'pve_ctid',
        'remote_data_dir', 'pve_secondary_backup_dir', 'remote_workspace_root', 'cluster_token',
        'max_snapshots', 'last_sync_at', 'last_sync_status',
    }
    for k, v in patch_data.items():
        if k in allowed_keys and v is not None:
            if isinstance(v, str):
                cfg[k] = v.strip()
            else:
                cfg[k] = v
    if 'peer_url' in cfg and cfg['peer_url']:
        cfg['peer_url'] = validate_peer_url(cfg['peer_url'])
    if 'pve_ssh_host' in cfg and cfg['pve_ssh_host']:
        cfg['pve_ssh_host'] = validate_ssh_host(cfg['pve_ssh_host'])
    db.write_private(_cluster_config_path(), json.dumps(cfg, ensure_ascii=False, indent=2))
    return cfg


def verify_cluster_token(token: Optional[str]) -> bool:
    if not token or not isinstance(token, str):
        return False
    expected = get_cluster_config().get('cluster_token') or ''
    return bool(expected and secrets.compare_digest(token.strip(), expected))


def _should_exclude_path(rel_parts: tuple[str, ...]) -> bool:
    for part in rel_parts:
        if part in SNAPSHOT_EXCLUDE_DIRS:
            return True
        if part.endswith(SNAPSHOT_EXCLUDE_SUFFIXES):
            return True
    return False


def _add_directory_to_tar(
    tar: tarfile.TarFile,
    src_dir: Path,
    arc_prefix: str,
    skip_subpaths: Optional[set[Path]] = None,
) -> int:
    added_files = 0
    if not src_dir.is_dir():
        return 0
    src_resolved = src_dir.resolve()
    skip_set = skip_subpaths or set()
    for root, dirs, files in os.walk(src_resolved):
        root_path = Path(root).resolve()
        rel_root = root_path.relative_to(src_resolved)
        # Prune excluded directories and nested child project directories in-place
        dirs[:] = [
            d for d in dirs
            if d not in SNAPSHOT_EXCLUDE_DIRS
            and not Path(root, d).is_symlink()
            and (root_path / d).resolve() not in skip_set
        ]
        if _should_exclude_path(rel_root.parts):
            continue
        for fname in files:
            if fname.endswith(SNAPSHOT_EXCLUDE_SUFFIXES):
                continue
            fpath = Path(root) / fname
            if fpath.is_symlink():
                continue
            try:
                st = fpath.stat()
                if st.st_size > MAX_FILE_BYTES_IN_SNAPSHOT:
                    continue
                rel_file = fpath.relative_to(src_resolved)
                arcname = f'{arc_prefix}/{rel_file.as_posix()}'
                tar.add(str(fpath), arcname=arcname, recursive=False)
                added_files += 1
            except OSError:
                continue
    return added_files


def _compute_relative_workspace_subpath(project_path: str, allowed_roots: List[str]) -> str:
    p = Path(project_path)
    for root_str in allowed_roots:
        with contextlib.suppress( Exception):
            r = Path(root_str).resolve()
            pr = p.resolve()
            if pr.is_relative_to(r):
                rel = pr.relative_to(r).as_posix()
                if rel and rel != '.':
                    return f'{r.name}/{rel}'
                return r.name
    return p.name


def create_snapshot(
    include_projects: bool = True,
    project_ids: Optional[List[str]] = None,
    label: str = 'manual',
    include_template: bool = True,
) -> Dict[str, Any]:
    """Create a consistent, self-contained snapshot of database, custom agents, and project source trees."""
    db.init()
    snap_dir = snapshots_dir()
    now = time.time()
    stamp = datetime.datetime.fromtimestamp(now).strftime('%Y%m%d-%H%M%S')
    safe_label = re.sub(r'[^a-zA-Z0-9_-]', '', label or 'manual')[:24] or 'snap'
    filename = f'rove-snapshot-{stamp}-{safe_label}.tar.gz'
    target_path = snap_dir / filename

    cfg = db.config()
    allowed_roots = cfg.get('allowed_roots', [])
    all_projects = db.query('SELECT * FROM projects')
    if project_ids is not None:
        wanted = set(project_ids)
        selected_projects = [p for p in all_projects if p['id'] in wanted]
    else:
        selected_projects = all_projects

    with tempfile.TemporaryDirectory(prefix='rove-snap-') as tmpdir:
        tmp_path = Path(tmpdir)
        # 1. Consistent online SQLite backup
        db_backup_path = tmp_path / 'state.sqlite3'
        with contextlib.closing(db.connect()) as src_conn, contextlib.closing(sqlite3.connect(db_backup_path)) as dst_conn:
            src_conn.backup(dst_conn)

        project_entries = []
        total_files = 0

        with tarfile.open(target_path, 'w:gz') as tar:
            tar.add(str(db_backup_path), arcname='state.sqlite3')

            agents_file = db.DATA / 'agents.json'
            if agents_file.is_file():
                tar.add(str(agents_file), arcname='agents.json')

            # Include the workstation template itself if requested
            if include_template:
                template_files = _add_directory_to_tar(tar, db.ROOT, 'template/mobile-workstation')
                total_files += template_files

            if include_projects:
                all_proj_paths = {Path(p['path']).resolve() for p in selected_projects if Path(p['path']).exists()}
                for proj in selected_projects:
                    p_path = Path(proj['path'])
                    rel_subpath = _compute_relative_workspace_subpath(proj['path'], allowed_roots)
                    arc_prefix = f"projects/{proj['id']}"
                    fcount = 0
                    if p_path.is_dir():
                        p_res = p_path.resolve()
                        child_proj_paths = {other for other in all_proj_paths if other != p_res and other.is_relative_to(p_res)}
                        fcount = _add_directory_to_tar(tar, p_path, arc_prefix, skip_subpaths=child_proj_paths)
                        total_files += fcount
                    project_entries.append({
                        'id': proj['id'],
                        'name': proj['name'],
                        'original_path': proj['path'],
                        'relative_subpath': rel_subpath,
                        'files_archived': fcount,
                    })

            manifest = {
                'version': 2,
                'created_at': now,
                'created_iso': datetime.datetime.fromtimestamp(now).isoformat(),
                'label': label,
                'node_name': get_cluster_config().get('node_name'),
                'hostname': socket.gethostname(),
                'platform': sys.platform,
                'allowed_roots': allowed_roots,
                'include_projects': include_projects,
                'projects': project_entries,
                'sessions_count': (db.one('SELECT count(*) AS c FROM sessions') or {}).get('c', 0),
                'memories_count': (db.one('SELECT count(*) AS c FROM project_memories') or {}).get('c', 0),
                'total_files_archived': total_files,
            }
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
            info = tarfile.TarInfo(name='manifest.json')
            info.size = len(manifest_bytes)
            info.mtime = int(now)
            tar.addfile(info, io.BytesIO(manifest_bytes))

    os.chmod(target_path, 0o600)
    _prune_old_snapshots()
    size_bytes = target_path.stat().st_size
    return {
        'filename': filename,
        'path': str(target_path),
        'size_bytes': size_bytes,
        'size_formatted': _format_bytes(size_bytes),
        'manifest': manifest,
    }


def _format_bytes(num: int) -> str:
    val = float(max(0, num))
    for unit in ('B', 'KB', 'MB', 'GB'):
        if val < 1024.0 or unit == 'GB':
            return f'{val:.1f} {unit}' if unit != 'B' else f'{int(val)} B'
        val /= 1024.0
    return f'{val:.1f} GB'


def _prune_old_snapshots() -> None:
    max_keep = max(2, int(get_cluster_config().get('max_snapshots') or 8))
    files = sorted(snapshots_dir().glob('rove-snapshot-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[max_keep:]:
        with contextlib.suppress(OSError):
            old.unlink()


def mirror_snapshot_to_secondary(snap_path: Path) -> Optional[Path]:
    """If secondary backup directory is configured and accessible, mirror the snapshot there and prune older snapshots."""
    cfg = get_cluster_config()
    secondary = (cfg.get('pve_secondary_backup_dir', '/mnt/pve/backup/walking-workstation-snapshots') or '').strip()
    if not secondary:
        return None
    if not snap_path.is_file():
        raise FileNotFoundError(f'快照文件不存在：{snap_path}')
    sec_path = Path(secondary)
    sec_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = sec_path / snap_path.name
    temporary = sec_path / (snap_path.name + '.partial-' + secrets.token_hex(4))
    try:
        shutil.copy2(snap_path, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    max_snaps = max(2, int(cfg.get('max_snapshots') or 8))
    sec_files = sorted(sec_path.glob('rove-snapshot-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in sec_files[max_snaps:]:
        old.unlink()
    return target


def list_snapshots() -> List[Dict[str, Any]]:
    items = []
    for f in sorted(snapshots_dir().glob('rove-snapshot-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            st = f.stat()
            manifest = {}
            with contextlib.suppress(Exception):
                with tarfile.open(f, 'r:gz') as tar:
                    mfile = tar.extractfile('manifest.json')
                    if mfile:
                        manifest = json.loads(mfile.read().decode('utf-8'))
            items.append({
                'filename': f.name,
                'size_bytes': st.st_size,
                'size_formatted': _format_bytes(st.st_size),
                'created_at': manifest.get('created_at') or st.st_mtime,
                'label': manifest.get('label') or 'snapshot',
                'node_name': manifest.get('node_name') or '',
                'projects_count': len(manifest.get('projects') or []),
                'sessions_count': manifest.get('sessions_count', 0),
                'total_files': manifest.get('total_files_archived', 0),
            })
        except OSError:
            continue
    return items


def check_projects_health() -> List[Dict[str, Any]]:
    """Check whether any registered project or the workstation template has missing directories/files."""
    results = []
    for p in db.query('SELECT * FROM projects'):
        p_path = Path(p['path'])
        exists = p_path.is_dir()
        non_empty = exists and any(p_path.iterdir()) if exists else False
        results.append({
            'id': p['id'],
            'name': p['name'],
            'path': p['path'],
            'exists': exists,
            'healthy': non_empty,
        })
    return results


def _safe_extract_subpath(tar: tarfile.TarFile, prefix: str, dest_dir: Path, only_missing: bool = False) -> int:
    restored_count = 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    prefix_slash = prefix.rstrip('/') + '/'

    for member in tar.getmembers():
        if not member.name.startswith(prefix_slash):
            continue
        rel_name = member.name[len(prefix_slash):]
        if not rel_name or '..' in Path(rel_name).parts:
            continue
        target = (dest_resolved / rel_name).resolve()
        if not target.is_relative_to(dest_resolved):
            continue
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            continue
        if only_missing and target.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        extracted = tar.extractfile(member)
        if extracted is not None:
            with open(target, 'wb') as out_f:
                shutil.copyfileobj(extracted, out_f)
            with contextlib.suppress(OSError):
                os.chmod(target, member.mode & 0o777 or 0o644)
            restored_count += 1
    return restored_count


def _remap_database_paths(path_map: Dict[str, str]) -> int:
    """Update project and session paths when migrating a snapshot between Mac and PVE Linux."""
    if not path_map:
        return 0
    updated = 0
    with db.transaction() as conn:
        for old_path, new_path in path_map.items():
            if not old_path or not new_path or old_path == new_path:
                continue
            conn.execute('UPDATE projects SET path=? WHERE path=?', (new_path, old_path))
            conn.execute('UPDATE sessions SET cwd=? WHERE cwd=?', (new_path, old_path))
            # Also remap child paths inside the project
            old_prefix = old_path.rstrip('/') + '/'
            rows = conn.execute('SELECT id, cwd FROM sessions WHERE cwd LIKE ?', (old_prefix + '%',)).fetchall()
            for r in rows:
                suffix = r['cwd'][len(old_prefix):]
                conn.execute('UPDATE sessions SET cwd=? WHERE id=?', (f"{new_path.rstrip('/')}/{suffix}", r['id']))
            updated += 1
    return updated


def restore_snapshot(
    snapshot_path: Path,
    restore_db: bool = True,
    restore_projects: bool = True,
    only_missing_files: bool = False,
    target_workspace_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore database, custom agents, and/or project workspaces from a snapshot archive."""
    if not snapshot_path.is_file():
        raise FileNotFoundError(f'快照文件不存在: {snapshot_path}')

    db.init()
    cfg = db.config()
    allowed_roots = [Path(r).expanduser().resolve() for r in cfg.get('allowed_roots', []) if r]
    default_ws_root = (
        Path(target_workspace_root).expanduser().resolve()
        if target_workspace_root
        else (allowed_roots[0] if allowed_roots else (db.DATA / 'workspaces').resolve())
    )
    default_ws_root.mkdir(parents=True, exist_ok=True)
    if str(default_ws_root) not in [str(r) for r in allowed_roots]:
        cfg['allowed_roots'] = [str(default_ws_root)] + cfg.get('allowed_roots', [])
        db.write_private(db.DATA / 'config.json', json.dumps(cfg, ensure_ascii=False, indent=2))
        allowed_roots.insert(0, default_ws_root)

    with tarfile.open(snapshot_path, 'r:gz') as tar:
        manifest = {}
        with contextlib.suppress(Exception):
            mf = tar.extractfile('manifest.json')
            if mf:
                manifest = json.loads(mf.read().decode('utf-8'))

        # 1. Restore SQLite database & agents.json if requested
        if restore_db:
            names = set(tar.getnames())
            if 'state.sqlite3' in names:
                pre_backup = db.DATA / f'state.sqlite3.pre-restore-{int(time.time())}'
                if (db.DATA / 'state.sqlite3').is_file():
                    with contextlib.closing(db.connect()) as src_c, contextlib.closing(sqlite3.connect(pre_backup)) as dst_c:
                        src_c.backup(dst_c)
                    os.chmod(pre_backup, 0o600)
                with tempfile.NamedTemporaryFile(dir=db.DATA, delete=False, suffix='.sqlite3') as tmp_db:
                    tmp_db_path = Path(tmp_db.name)
                    extracted_db = tar.extractfile('state.sqlite3')
                    if extracted_db:
                        shutil.copyfileobj(extracted_db, tmp_db)
                with contextlib.closing(sqlite3.connect(tmp_db_path)) as incoming_conn, contextlib.closing(db.connect()) as live_conn:
                    incoming_conn.backup(live_conn)
                tmp_db_path.unlink(missing_ok=True)
                # Ensure schema migrations are up to date after importing older snapshot
                db.init()

            if 'agents.json' in names:
                extracted_agents = tar.extractfile('agents.json')
                if extracted_agents:
                    raw_agents = extracted_agents.read().decode('utf-8', errors='replace')
                    db.write_private(db.DATA / 'agents.json', raw_agents)

        # 2. Restore projects and compute cross-platform path mapping
        path_map: Dict[str, str] = {}
        restored_files_total = 0
        restored_projects_list = []

        for proj_meta in manifest.get('projects') or []:
            pid = proj_meta.get('id')
            orig_path = proj_meta.get('original_path', '')
            rel_sub = proj_meta.get('relative_subpath') or proj_meta.get('name') or pid
            if not pid or not orig_path:
                continue

            orig_p = Path(orig_path)
            # Check if original path is valid and inside current node's allowed_roots
            can_use_orig = False
            with contextlib.suppress(OSError):
                if any(orig_p.resolve().is_relative_to(r) for r in allowed_roots):
                    can_use_orig = True

            if can_use_orig:
                dest_path = orig_p.resolve()
            else:
                dest_path = (default_ws_root / rel_sub).resolve()
                path_map[orig_path] = str(dest_path)

            if restore_projects:
                count = _safe_extract_subpath(
                    tar,
                    f'projects/{pid}',
                    dest_path,
                    only_missing=only_missing_files,
                )
                restored_files_total += count
                restored_projects_list.append({
                    'id': pid,
                    'name': proj_meta.get('name'),
                    'path': str(dest_path),
                    'restored_files': count,
                })

        # Also check if template itself needs self-healing when only_missing_files is True
        if restore_projects and only_missing_files:
            restored_files_total += _safe_extract_subpath(
                tar,
                'template/mobile-workstation',
                db.ROOT,
                only_missing=True,
            )

        remapped_count = _remap_database_paths(path_map) if path_map else 0

    return {
        'ok': True,
        'snapshot': snapshot_path.name,
        'restore_db': restore_db,
        'restore_projects': restore_projects,
        'only_missing_files': only_missing_files,
        'restored_files_total': restored_files_total,
        'restored_projects': restored_projects_list,
        'remapped_paths_count': remapped_count,
        'path_map': path_map,
    }


def probe_peer_node() -> Dict[str, Any]:
    """Check connectivity to the configured PVE peer node via HTTP or SSH."""
    cfg = get_cluster_config()
    peer_url = validate_peer_url(cfg.get('peer_url'))
    ssh_host = validate_ssh_host(cfg.get('pve_ssh_host'))
    ctid = (cfg.get('pve_ctid') or '').strip()

    if peer_url:
        req = urllib.request.Request(
            f'{peer_url}/api/cluster/ping',
            headers={
                'X-Cluster-Token': cfg.get('cluster_token', ''),
                'Accept': 'application/json',
            },
            method='GET',
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8', errors='replace'))
                return {
                    'reachable': True,
                    'transport': 'http',
                    'target': peer_url,
                    'peer_info': data,
                    'detail': f"已连通远端工作台节点：{data.get('node_name', peer_url)}",
                }
        except Exception as exc:
            http_err = str(exc)
    else:
        http_err = '未配置 Peer HTTP 地址'

    if ssh_host:
        cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', '--', ssh_host]
        if ctid:
            cmd.extend(['pct', 'exec', ctid, '--', 'uname', '-a'])
        else:
            cmd.extend(['uname', '-a'])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            if res.returncode == 0:
                return {
                    'reachable': True,
                    'transport': 'ssh',
                    'target': f'{ssh_host}' + (f' (CT {ctid})' if ctid else ''),
                    'detail': f'SSH 通道就绪：{res.stdout.strip()[:120]}',
                }
            return {
                'reachable': False,
                'transport': 'ssh',
                'target': ssh_host,
                'detail': f'SSH 连接返回码 {res.returncode}: {(res.stderr or res.stdout).strip()[:160]}',
            }
        except Exception as exc:
            return {
                'reachable': False,
                'transport': 'ssh',
                'target': ssh_host,
                'detail': f'SSH 探测未连通：{str(exc)[:160]} ({http_err})',
            }

    return {
        'reachable': False,
        'transport': 'none',
        'target': '',
        'detail': '尚未配置 PVE 节点地址或 SSH 主机',
    }


_push_lock = threading.Lock()


def push_snapshot_to_peer(include_projects: bool = True) -> Dict[str, Any]:
    if not _push_lock.acquire(blocking=False):
        raise RuntimeError('已有快照同步正在进行，请稍后重试')
    try:
        return _push_snapshot_to_peer(include_projects)
    finally:
        _push_lock.release()


def _push_snapshot_to_peer(include_projects: bool = True) -> Dict[str, Any]:
    """Create a fresh snapshot and push it to the PVE standby node via HTTP or SSH."""
    cfg = get_cluster_config()
    snap = create_snapshot(include_projects=include_projects, label='pve-sync')
    snap_path = Path(snap['path'])
    peer_url = validate_peer_url(cfg.get('peer_url'))
    ssh_host = validate_ssh_host(cfg.get('pve_ssh_host'))
    ctid = (cfg.get('pve_ctid') or '').strip()
    remote_data = (cfg.get('remote_data_dir') or '/var/lib/walking-workstation').rstrip('/')

    if peer_url:
        req = urllib.request.Request(
            f'{peer_url}/api/cluster/snapshot/import?restore_db=1&restore_projects=1',
            headers={
                'Content-Type': 'application/gzip',
                'X-Cluster-Token': cfg.get('cluster_token', ''),
                'X-Snapshot-Filename': snap['filename'],
            },
            method='POST',
        )
        try:
            with snap_path.open('rb') as upload:
                req.data = upload
                req.add_header('Content-Length', str(snap_path.stat().st_size))
                with urllib.request.urlopen(req, timeout=180) as resp:
                    remote_res = json.loads(resp.read().decode('utf-8', errors='replace'))
            mirror = remote_res.get('secondary_backup') or {}
            ok = remote_res.get('ok', True) and mirror.get('status') != 'failed'
            detail = f"已通过 HTTP 同步快照至 {peer_url} ({snap['size_formatted']})"
            if not ok:
                detail = f"HTTP 快照已传输，但远端处理未全部完成：{mirror.get('error') or remote_res.get('detail') or '请检查远端状态'}"
            update_cluster_config({
                'last_sync_at': time.time(),
                'last_sync_status': detail,
            })
            return {
                'ok': ok,
                'detail': detail,
                'transport': 'http',
                'snapshot': snap,
                'remote': remote_res,
            }
        except Exception as exc:
            if not ssh_host:
                raise RuntimeError(f'推送快照至 {peer_url} 失败：{exc}') from exc

    if ssh_host:
        if ctid and not str(ctid).isdigit():
            raise ValueError(f'无效的 PVE CTID：{ctid}')
        safe_snap_name = Path(snap['filename']).name
        remote_snap_dir = f'{remote_data}/snapshots'
        remote_snap_file = f'{remote_snap_dir}/{safe_snap_name}'
        secondary_dir = (cfg.get('pve_secondary_backup_dir') or '/mnt/pve/backup/walking-workstation-snapshots').rstrip('/')
        max_snaps = max(2, int(cfg.get('max_snapshots') or 8))
        sec_snap_file = f'{secondary_dir}/{safe_snap_name}' if secondary_dir else ''

        q_snap_dir = shlex.quote(remote_snap_dir)
        q_snap_file = shlex.quote(remote_snap_file)
        q_sec_dir = shlex.quote(secondary_dir) if secondary_dir else ''
        q_sec_file = shlex.quote(sec_snap_file) if sec_snap_file else ''
        q_remote_data = shlex.quote(remote_data)
        q_partial = shlex.quote(remote_snap_file + '.partial')
        q_sec_partial = shlex.quote(sec_snap_file + '.partial')

        # Build remote execution script:
        # 1. Save snapshot to primary remote_snap_dir (Disk 1: Local NVMe SSD)
        # 2. Mirror snapshot to secondary_dir (Disk 2: Independent backup storage pool / CIFS)
        # 3. Automatically prune snapshots beyond max_snaps on both independent volumes
        script_parts = [
            'umask 077',
            f'mkdir -p {q_snap_dir}',
            f'cat > {q_partial}',
            f'mv -f {q_partial} {q_snap_file}',
            f'chmod 600 {q_snap_file}',
            "printf 'ROVE_PRIMARY_OK\n'",
        ]
        if secondary_dir:
            script_parts.append(
                f'(mkdir -p {q_sec_dir} && cp -f {q_snap_file} {q_sec_partial} && chmod 600 {q_sec_partial} && mv -f {q_sec_partial} {q_sec_file})'
            )
        script_parts.append("printf 'ROVE_MIRROR_OK\n'")
        script_parts.append(
            f'(ls -1t {q_snap_dir}/rove-snapshot-*.tar.gz 2>/dev/null | tail -n +{max_snaps + 1} | while IFS= read -r old_snapshot; do rm -f -- "$old_snapshot"; done)'
        )
        if secondary_dir:
            script_parts.append(
                f'([ ! -d {q_sec_dir} ] || (ls -1t {q_sec_dir}/rove-snapshot-*.tar.gz 2>/dev/null | tail -n +{max_snaps + 1} | while IFS= read -r old_snapshot; do rm -f -- "$old_snapshot"; done))'
            )

        remote_cmd = ' && '.join(script_parts)
        if ctid:
            remote_cmd = f"pct exec {int(ctid)} -- sh -c {shlex.quote(remote_cmd)}"

        try:
            with open(snap_path, 'rb') as f_in:
                res = subprocess.run(
                    ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '--', ssh_host, remote_cmd],
                    stdin=f_in, capture_output=True, timeout=180,
                )
            if res.returncode != 0:
                output = res.stdout.decode('utf-8', errors='replace')
                phase = '主盘上传'
                if 'ROVE_PRIMARY_OK' in output:
                    phase = '备盘复制'
                if 'ROVE_MIRROR_OK' in output:
                    phase = '历史快照清理'
                err = res.stderr.decode('utf-8', errors='replace').strip()
                raise RuntimeError(f'{phase}失败：{err or res.returncode}')

            apply_script = (
                "if [ -x /opt/walking-workstation/.venv/bin/python ]; then "
                f"WORKSTATION_DATA={q_remote_data} /opt/walking-workstation/.venv/bin/python "
                f"/opt/walking-workstation/scripts/manage.py snapshot-import {q_snap_file}; "
                "else printf 'ROVE_IMPORT_SKIPPED\n'; fi"
            )
            apply_remote = f"pct exec {int(ctid)} -- sh -c {shlex.quote(apply_script)}" if ctid else apply_script
            applied = subprocess.run(
                ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '--', ssh_host, apply_remote],
                capture_output=True, timeout=120,
            )
            if applied.returncode != 0:
                err = applied.stderr.decode('utf-8', errors='replace').strip()
                raise RuntimeError(f'远端快照导入失败：{err or applied.returncode}')
            import_status = 'skipped' if b'ROVE_IMPORT_SKIPPED' in applied.stdout else 'completed'
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            update_cluster_config({'last_sync_status': f'SSH 同步失败：{exc}'})
            raise RuntimeError(f'通过 SSH 同步至 PVE ({ssh_host}) 失败：{exc}') from exc

        disk_desc = (
            f"[主盘 NVMe: {remote_snap_dir}, 备盘 备份池: {secondary_dir}]"
            if secondary_dir
            else f"[{remote_snap_dir}]"
        )
        sync_label = "双盘同步" if secondary_dir else "主盘同步"
        status_msg = f"已通过 SSH {sync_label}至 {ssh_host}" + (f" (CT {ctid})" if ctid else "") + f" {disk_desc} ({snap['size_formatted']})"
        status_msg += '；远端导入完成' if import_status == 'completed' else '；远端未安装工作台，导入已跳过'
        update_cluster_config({
            'last_sync_at': time.time(),
            'last_sync_status': status_msg,
        })
        return {
            'ok': True,
            'transport': 'ssh',
            'primary_status': 'completed',
            'secondary_status': 'completed' if secondary_dir else 'skipped',
            'import_status': import_status,
            'snapshot': snap,
            'remote_path': remote_snap_file,
            'secondary_path': sec_snap_file if secondary_dir else None,
            'detail': status_msg,
        }

    raise RuntimeError('请先配置 PVE 节点的 HTTP 地址或 SSH 主机名')


def pull_and_restore_from_peer(
    restore_db: bool = True,
    restore_projects: bool = True,
    only_missing_files: bool = False,
) -> Dict[str, Any]:
    """Pull the latest snapshot from the PVE standby node via HTTP or SSH and restore it locally."""
    cfg = get_cluster_config()
    peer_url = validate_peer_url(cfg.get('peer_url'))
    ssh_host = validate_ssh_host(cfg.get('pve_ssh_host'))
    ctid = (cfg.get('pve_ctid') or '').strip()
    remote_data = (cfg.get('remote_data_dir') or '/var/lib/walking-workstation').rstrip('/')

    local_snap = snapshots_dir() / f'rove-snapshot-{datetime.datetime.now().strftime("%Y%m%d-%H%M%S")}-from-pve.tar.gz'

    pulled = False
    if peer_url:
        req = urllib.request.Request(
            f'{peer_url}/api/cluster/snapshot/export?include_projects=1',
            headers={'X-Cluster-Token': cfg.get('cluster_token', '')},
            method='GET',
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp, open(local_snap, 'wb') as out_f:
                shutil.copyfileobj(resp, out_f)
            os.chmod(local_snap, 0o600)
            pulled = True
        except Exception as exc:
            if not ssh_host:
                raise RuntimeError(f'从 PVE 节点 ({peer_url}) 拉取快照失败：{exc}') from exc

    if not pulled and ssh_host:
        if ctid and not str(ctid).isdigit():
            raise ValueError(f'无效的 PVE CTID：{ctid}')
        remote_snap_dir = f'{remote_data}/snapshots'
        secondary_dir = (cfg.get('pve_secondary_backup_dir') or '/mnt/pve/backup/walking-workstation-snapshots').rstrip('/')
        q_snap_dir = shlex.quote(remote_snap_dir)
        q_sec_dir = shlex.quote(secondary_dir) if secondary_dir else ''
        fetch_script = (
            f'p=$(ls -1t {q_snap_dir}/rove-snapshot-*.tar.gz 2>/dev/null | head -n 1); '
            'if [ -n "$p" ] && [ -s "$p" ]; then cat "$p"; '
            f'elif [ -n {q_sec_dir or "''"} ]; then '
            f's=$(ls -1t {q_sec_dir}/rove-snapshot-*.tar.gz 2>/dev/null | head -n 1); '
            'if [ -n "$s" ] && [ -s "$s" ]; then cat "$s"; fi; fi'
        )
        ssh_cmd = (
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '--', ssh_host, 'pct', 'exec', str(int(ctid)), '--', 'sh', '-c', fetch_script]
            if ctid
            else ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '--', ssh_host, fetch_script]
        )
        with open(local_snap, 'wb') as out_f:
            res = subprocess.run(ssh_cmd, stdout=out_f, stderr=subprocess.PIPE, timeout=180)
        if res.returncode != 0 or not local_snap.is_file() or local_snap.stat().st_size < 100:
            local_snap.unlink(missing_ok=True)
            err = res.stderr.decode('utf-8', errors='replace').strip()
            raise RuntimeError(f'从 PVE ({ssh_host}) 拉取快照失败：{err or "远端尚无有效快照文件"}')
        os.chmod(local_snap, 0o600)
        pulled = True

    if not pulled:
        raise RuntimeError('未配置可用的 PVE 远端节点')

    result = restore_snapshot(
        local_snap,
        restore_db=restore_db,
        restore_projects=restore_projects,
        only_missing_files=only_missing_files,
    )
    update_cluster_config({
        'last_sync_at': time.time(),
        'last_sync_status': f'已从 PVE 节点拉取并恢复快照 ({local_snap.name})',
    })
    return result


def get_cluster_status() -> Dict[str, Any]:
    cfg = get_cluster_config()
    snaps = list_snapshots()
    projects_health = check_projects_health()
    missing_projects = [p for p in projects_health if not p['healthy']]
    return {
        'config': {
            **cfg,
            'cluster_token_Preview': (cfg.get('cluster_token') or '')[:6] + '…' if cfg.get('cluster_token') else '',
        },
        'platform': sys.platform,
        'hostname': socket.gethostname(),
        'snapshots': snaps,
        'projects_health': projects_health,
        'missing_projects_count': len(missing_projects),
        'deploy_script_path': str(db.ROOT / 'deploy/pve/install_pve_node.sh'),
    }
