"""Git Worktree isolation manager for parallel AI agent tasks (inspired by worktrunk).

Enables multiple sessions to modify code and run tests simultaneously on the same
repository without colliding on the main working tree.
"""
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional

from . import store as db


def _run_git(args: List[str], cwd: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git'] + args,
        cwd=cwd,
        capture_output=True,
        text=text,
        check=check,
        timeout=30,
    )


def is_git_repository(project_path: str) -> bool:
    try:
        res = _run_git(['rev-parse', '--is-inside-work-tree'], cwd=project_path, check=False)
        return res.returncode == 0 and res.stdout.strip() == 'true'
    except Exception:
        return False


def has_valid_head(project_path: str) -> bool:
    try:
        res = _run_git(['rev-parse', '--verify', 'HEAD'], cwd=project_path, check=False)
        return res.returncode == 0
    except Exception:
        return False


def ensure_git_exclude(project_path: str):
    """Ensure .worktrees is hidden from git status without touching tracked .gitignore."""
    try:
        git_dir = _run_git(['rev-parse', '--git-dir'], cwd=project_path, check=False).stdout.strip()
        if not git_dir:
            return
        exclude_file = (Path(project_path) / git_dir / 'info' / 'exclude').resolve()
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        current = exclude_file.read_text(errors='ignore') if exclude_file.is_file() else ''
        if '.worktrees' not in current:
            with open(exclude_file, 'a', encoding='utf-8') as f:
                f.write('\n# Rove transient worktrees\n.worktrees/\n')
    except Exception:
        pass


def get_default_branch(project_path: str) -> str:
    try:
        res = _run_git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=project_path, check=False)
        branch = res.stdout.strip()
        if branch and branch != 'HEAD':
            return branch
    except Exception:
        pass
    return 'main'


def create_session_worktree(project: Dict[str, Any], session: Dict[str, Any]) -> Dict[str, Any]:
    project_path = str(Path(project['path']).resolve())
    if not is_git_repository(project_path):
        raise RuntimeError('该项目不是 Git 仓库，无法创建独立工作树')
    if not has_valid_head(project_path):
        raise RuntimeError('该仓库尚未创建任何提交（HEAD 无效），请先在主工作区提交初始 commit 后再启用工作树')

    session_id = session['id']
    short_id = session_id[:8]
    branch_name = f'rove/{short_id}'
    worktree_dir = (Path(project_path) / '.worktrees' / f'rove-{short_id}').resolve()

    ensure_git_exclude(project_path)
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    # If worktree already exists, return existing
    if worktree_dir.is_dir() and (worktree_dir / '.git').exists():
        db.execute(
            'UPDATE sessions SET use_worktree=1, worktree_path=?, git_branch=?, cwd=? WHERE id=?',
            (str(worktree_dir), branch_name, str(worktree_dir), session_id),
        )
        return {
            'path': str(worktree_dir),
            'branch': branch_name,
            'is_worktree': True,
        }

    # Check if branch already exists
    branch_exists = _run_git(['rev-parse', '--verify', f'refs/heads/{branch_name}'], cwd=project_path, check=False).returncode == 0

    if branch_exists:
        # Checkout existing branch into new worktree
        _run_git(['worktree', 'add', str(worktree_dir), branch_name], cwd=project_path)
    else:
        # Create new branch from HEAD
        _run_git(['worktree', 'add', '-b', branch_name, str(worktree_dir), 'HEAD'], cwd=project_path)

    db.execute(
        'UPDATE sessions SET use_worktree=1, worktree_path=?, git_branch=?, cwd=? WHERE id=?',
        (str(worktree_dir), branch_name, str(worktree_dir), session_id),
    )

    return {
        'path': str(worktree_dir),
        'branch': branch_name,
        'is_worktree': True,
    }


def get_session_worktree(project: Dict[str, Any], session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    wt_path = session.get('worktree_path')
    if not wt_path or not session.get('use_worktree'):
        return None
    path_obj = Path(wt_path)
    if not path_obj.is_dir():
        return None

    # Check uncommitted changes in worktree
    uncommitted = 0
    try:
        status_res = _run_git(['status', '--porcelain'], cwd=str(path_obj), check=False)
        if status_res.returncode == 0:
            lines = [l for l in status_res.stdout.splitlines() if l.strip()]
            uncommitted = len(lines)
    except Exception:
        pass

    branch = session.get('git_branch') or f"rove/{session['id'][:8]}"

    # Check commit diff against main
    commits_ahead = 0
    try:
        rev_res = _run_git(['rev-list', '--count', f'HEAD..{branch}'], cwd=str(Path(project['path'])), check=False)
        if rev_res.returncode == 0 and rev_res.stdout.strip().isdigit():
            commits_ahead = int(rev_res.stdout.strip())
    except Exception:
        pass

    return {
        'path': str(path_obj),
        'branch': branch,
        'uncommitted_files': uncommitted,
        'commits_ahead': commits_ahead,
        'is_worktree': True,
    }


def remove_session_worktree(project: Dict[str, Any], session: Dict[str, Any], force: bool = True) -> bool:
    wt_path = session.get('worktree_path')
    project_path = str(Path(project['path']).resolve())
    session_id = session['id']

    if wt_path and Path(wt_path).exists():
        cmd = ['worktree', 'remove']
        if force:
            cmd.append('--force')
        cmd.append(str(wt_path))
        _run_git(cmd, cwd=project_path, check=False)
        _run_git(['worktree', 'prune'], cwd=project_path, check=False)

    db.execute(
        'UPDATE sessions SET use_worktree=0, worktree_path=NULL, cwd=NULL WHERE id=?',
        (session_id,),
    )
    return True


def merge_session_worktree(
    project: Dict[str, Any],
    session: Dict[str, Any],
    commit_message: Optional[str] = None,
) -> Dict[str, Any]:
    project_path = str(Path(project['path']).resolve())
    wt_info = get_session_worktree(project, session)
    if not wt_info:
        raise RuntimeError('该会话没有挂载活跃的独立工作树')

    wt_path = wt_info['path']
    branch = wt_info['branch']

    # 1. Commit any uncommitted work inside worktree first
    status = _run_git(['status', '--porcelain'], cwd=wt_path, check=False)
    if status.stdout.strip():
        _run_git(['add', '-A'], cwd=wt_path)
        msg = commit_message or f"Auto-checkpoint from Rove worktree ({session.get('title') or branch})"
        _run_git(
            ['-c', 'user.name=Rove Workstation', '-c', 'user.email=rove@local', 'commit', '-m', msg],
            cwd=wt_path,
        )

    # 2. Check diff against main project
    diff_res = _run_git(['diff', f'HEAD...{branch}'], cwd=project_path, check=False)
    if not diff_res.stdout.strip():
        return {
            'status': 'no_changes',
            'detail': '工作树与主分支之间没有需要合并的代码差异',
        }

    # Ensure main project working directory is clean or safely stash uncommitted changes before squash merge
    main_status = _run_git(['status', '--porcelain', '--untracked-files=no'], cwd=project_path, check=False)
    if main_status.stdout.strip():
        dirty_files = [line[3:].strip() for line in main_status.stdout.splitlines() if line.strip()]
        raise RuntimeError(
            f'主项目工作区存在未提交的本地修改（{", ".join(dirty_files[:3])}），请先在主目录提交或暂存后再合并工作树'
        )

    # 3. Perform squash merge into current HEAD of main project
    merge_res = _run_git(['merge', '--squash', branch], cwd=project_path, check=False)
    if merge_res.returncode != 0:
        # Check if there are conflict markers
        conflict_status = _run_git(['status', '--porcelain'], cwd=project_path, check=False)
        conflicts = [line[3:].strip() for line in conflict_status.stdout.splitlines() if line.startswith(('UU', 'AA', 'UD', 'DU'))]
        return {
            'status': 'conflict',
            'detail': f'合并存在冲突，请检查文件：{", ".join(conflicts[:5])}',
            'conflict_files': conflicts,
        }

    # 4. Commit the squash merged changes on main project
    final_msg = commit_message or f"Merge parallel work from {branch}: {session.get('title') or 'task'}"
    _run_git(
        ['-c', 'user.name=Rove Workstation', '-c', 'user.email=rove@local', 'commit', '-m', final_msg],
        cwd=project_path,
    )

    return {
        'status': 'merged',
        'branch': branch,
        'detail': f'已成功将工作树 {branch} 的改动合并至主分支',
    }


def get_worktree_diff(project: Dict[str, Any], session: Dict[str, Any]) -> str:
    project_path = str(Path(project['path']).resolve())
    wt_info = get_session_worktree(project, session)
    if not wt_info:
        return ''
    branch = wt_info['branch']
    try:
        # Compare branch with main project HEAD
        res = _run_git(['diff', f'HEAD...{branch}'], cwd=project_path, check=False)
        return res.stdout
    except Exception:
        return ''


def list_project_worktrees(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    project_path = str(Path(project['path']).resolve())
    if not is_git_repository(project_path):
        return []

    res = _run_git(['worktree', 'list', '--porcelain'], cwd=project_path, check=False)
    if res.returncode != 0:
        return []

    trees = []
    current = {}
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            if current:
                trees.append(current)
                current = {}
            continue
        if line.startswith('worktree '):
            current['path'] = line[9:].strip()
        elif line.startswith('HEAD '):
            current['head'] = line[5:].strip()
        elif line.startswith('branch '):
            current['branch'] = line[7:].strip()
        elif line == 'bare':
            current['bare'] = True
        elif line == 'detached':
            current['detached'] = True
    if current:
        trees.append(current)

    # Attach related session info if matched
    sessions = db.query('SELECT id, title, worktree_path, updated FROM sessions WHERE project_id=?', (project['id'],))
    session_by_path = {s['worktree_path']: s for s in sessions if s.get('worktree_path')}

    results = []
    main_path = str(Path(project['path']).resolve())
    for t in trees:
        is_main = str(Path(t.get('path', '')).resolve()) == main_path
        matched_session = session_by_path.get(t.get('path'))
        results.append({
            'path': t.get('path'),
            'branch': t.get('branch', '').replace('refs/heads/', ''),
            'is_main': is_main,
            'session_id': matched_session['id'] if matched_session else None,
            'session_title': matched_session['title'] if matched_session else None,
        })
    return results
