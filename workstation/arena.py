"""Dual-Engine Arena: run the same prompt on two engines in isolated Git worktrees, compare diffs, and adopt the winner."""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Dict, List

from . import store as db
from . import worktree


def start_arena_run(
    project: Dict[str, Any],
    prompt: str,
    engine_a: str,
    engine_b: str,
    model_a: str = '',
    model_b: str = '',
) -> Dict[str, Any]:
    """Create two worktree-isolated sessions and enqueue the same prompt on two engines."""
    clean_prompt = (prompt or '').strip()
    if not clean_prompt:
        raise ValueError('对比任务提示词不能为空')
    if not worktree.is_git_repository(project['path']) or not worktree.has_valid_head(project['path']):
        raise RuntimeError('双引擎对比模式需要项目为有效 Git 仓库且已有初始提交')

    now = time.time()
    arena_id = 'arena_' + secrets.token_hex(6)
    sess_a_id = secrets.token_hex(12)
    sess_b_id = secrets.token_hex(12)
    job_a_id = secrets.token_hex(12)
    job_b_id = secrets.token_hex(12)

    title_prefix = clean_prompt.replace('\n', ' ')[:36]
    with db.transaction() as conn:
        for sid, eng, mod, tag in (
            (sess_a_id, engine_a, model_a, 'A'),
            (sess_b_id, engine_b, model_b, 'B'),
        ):
            conn.execute(
                'INSERT INTO sessions(id,project_id,title,engine,model,effort,mode,fast,cwd,created,updated,thread_kind) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (sid, project['id'], f'[对比 {tag} · {eng}] {title_prefix}', eng, mod or None, 'medium', 'auto', 0, project['path'], now, now, 'arena'),
            )

    sess_a = db.one('SELECT * FROM sessions WHERE id=?', (sess_a_id,))
    sess_b = db.one('SELECT * FROM sessions WHERE id=?', (sess_b_id,))
    worktree.create_session_worktree(project, sess_a)
    worktree.create_session_worktree(project, sess_b)

    with db.transaction() as conn:
        for sid, jid, eng, mod in (
            (sess_a_id, job_a_id, engine_a, model_a),
            (sess_b_id, job_b_id, engine_b, model_b),
        ):
            conn.execute(
                'INSERT INTO jobs(id,session_id,request_id,prompt,status,started,model,effort,mode,fast,engine) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (jid, sid, f'{arena_id}-{sid[:6]}', clean_prompt, 'queued', now, mod or None, 'medium', 'auto', 0, eng),
            )
            conn.execute(
                'INSERT INTO events(session_id,job_id,kind,payload,created) VALUES(?,?,?,?,?)',
                (sid, jid, 'user', json.dumps({'text': clean_prompt, 'arena_id': arena_id}, ensure_ascii=False), now),
            )
        conn.execute(
            'INSERT INTO arena_runs(id,project_id,prompt,session_a_id,session_b_id,engine_a,engine_b,status,created,updated) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (arena_id, project['id'], clean_prompt, sess_a_id, sess_b_id, engine_a, engine_b, 'running', now, now),
        )

    return {
        'id': arena_id,
        'project_id': project['id'],
        'prompt': clean_prompt,
        'session_a_id': sess_a_id,
        'session_b_id': sess_b_id,
        'job_a_id': job_a_id,
        'job_b_id': job_b_id,
        'engine_a': engine_a,
        'engine_b': engine_b,
        'status': 'running',
    }


def _summarize_candidate(project: Dict[str, Any], session_id: str, engine: str) -> Dict[str, Any]:
    from pathlib import Path
    import subprocess

    sess = db.one('SELECT * FROM sessions WHERE id=?', (session_id,))
    job = db.one('SELECT * FROM jobs WHERE session_id=? ORDER BY started DESC LIMIT 1', (session_id,))
    diff_info = {'branch': (sess or {}).get('git_branch') or '', 'additions': 0, 'deletions': 0, 'diff_preview': ''}
    if sess and sess.get('use_worktree') and sess.get('worktree_path'):
        wt_path = str(sess['worktree_path'])
        try:
            committed_diff = worktree.get_worktree_diff(project, sess) or ''
            uncommitted_diff = subprocess.run(
                ['git', '-C', wt_path, 'diff', 'HEAD'],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout or ''
            combined = (committed_diff + '\n' + uncommitted_diff).strip()
            adds = sum(1 for ln in combined.splitlines() if ln.startswith('+') and not ln.startswith('+++'))
            dels = sum(1 for ln in combined.splitlines() if ln.startswith('-') and not ln.startswith('---'))
            untracked = subprocess.run(
                ['git', '-C', wt_path, 'ls-files', '--others', '--exclude-standard'],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout or ''
            for rel in untracked.splitlines():
                rel = rel.strip()
                if not rel:
                    continue
                p = Path(wt_path) / rel
                if p.is_file():
                    adds += max(1, len(p.read_text(encoding='utf-8', errors='ignore').splitlines()))
            diff_info = {
                'branch': sess.get('git_branch') or '',
                'additions': adds,
                'deletions': dels,
                'diff_preview': combined[:20000],
            }
        except Exception:
            pass
    return {
        'session_id': session_id,
        'engine': engine,
        'title': (sess or {}).get('title') or session_id,
        'job_status': (job or {}).get('status') or 'unknown',
        'error': (job or {}).get('error') or '',
        'diff': diff_info,
    }


def get_arena_status(arena_id: str) -> Dict[str, Any]:
    row = db.one('SELECT * FROM arena_runs WHERE id=?', (arena_id,))
    if not row:
        raise ValueError('未找到对比任务记录')
    proj = db.one('SELECT * FROM projects WHERE id=?', (row['project_id'],))
    if not proj:
        raise ValueError('关联项目不存在')
    cand_a = _summarize_candidate(proj, row['session_a_id'], row['engine_a'])
    cand_b = _summarize_candidate(proj, row['session_b_id'], row['engine_b'])
    done_states = {'completed', 'failed', 'stopped'}
    if row['status'] == 'running' and cand_a['job_status'] in done_states and cand_b['job_status'] in done_states:
        db.execute("UPDATE arena_runs SET status='ready', updated=? WHERE id=?", (time.time(), arena_id))
        row['status'] = 'ready'
    return {**row, 'candidate_a': cand_a, 'candidate_b': cand_b}


def adopt_arena_winner(arena_id: str, winner_session_id: str) -> Dict[str, Any]:
    row = db.one('SELECT * FROM arena_runs WHERE id=?', (arena_id,))
    if not row:
        raise ValueError('未找到对比任务记录')
    if winner_session_id not in (row['session_a_id'], row['session_b_id']):
        raise ValueError('指定的胜出会话不属于该对比任务')
    proj = db.one('SELECT * FROM projects WHERE id=?', (row['project_id'],))
    if not proj:
        raise ValueError('关联项目不存在')

    loser_id = row['session_b_id'] if winner_session_id == row['session_a_id'] else row['session_a_id']
    winner_sess = db.one('SELECT * FROM sessions WHERE id=?', (winner_session_id,))
    loser_sess = db.one('SELECT * FROM sessions WHERE id=?', (loser_id,))

    merge_res = worktree.merge_session_worktree(
        proj, winner_sess, commit_message=f"feat(arena): adopt {winner_sess.get('engine')} solution for {row['prompt'][:50]}"
    )
    if loser_sess:
        try:
            worktree.cleanup_session_worktree(proj, loser_sess, force=True)
        except Exception:
            pass
    db.execute(
        "UPDATE arena_runs SET status='adopted', winner_session_id=?, updated=? WHERE id=?",
        (winner_session_id, time.time(), arena_id),
    )
    return {'ok': True, 'winner_session_id': winner_session_id, 'merge': merge_res}
