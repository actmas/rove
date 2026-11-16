"""Cross-session persistent memory for projects (claude-mem style).

Captures project conventions, architectural decisions, tooling instructions,
and constraints so they seamlessly persist across mobile sessions without
wasting user typing or token budget.
"""
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from . import store as db

CATEGORIES = {
    'convention': '开发规范',
    'architecture': '架构决策',
    'constraint': '硬性约束',
    'tooling': '构建工具',
    'general': '通用备忘',
}

CATEGORY_PREFIXES = {
    'convention': '【开发规范】',
    'architecture': '【架构决策】',
    'constraint': '【硬性红线】',
    'tooling': '【构建测试】',
    'general': '【项目备忘】',
}


def list_memories(project_id: str) -> List[Dict[str, Any]]:
    """List all memories for a project, pinned first, then by updated timestamp descending."""
    return db.query(
        'SELECT * FROM project_memories WHERE project_id=? ORDER BY pinned DESC, updated DESC',
        (project_id,),
    )


def get_memory(memory_id: str) -> Optional[Dict[str, Any]]:
    return db.one('SELECT * FROM project_memories WHERE id=?', (memory_id,))


def _sync_fts_upsert(mem: Optional[Dict[str, Any]]) -> None:
    if not mem:
        return
    try:
        db.execute('DELETE FROM project_memories_fts WHERE memory_id=?', (mem['id'],))
        db.execute(
            'INSERT INTO project_memories_fts(memory_id, project_id, category, content, why, how_to_apply) '
            'VALUES(?,?,?,?,?,?)',
            (
                mem['id'],
                mem['project_id'],
                mem.get('category') or '',
                mem.get('content') or '',
                mem.get('why') or '',
                mem.get('how_to_apply') or '',
            ),
        )
    except Exception:
        pass


def _sync_fts_delete(memory_id: str) -> None:
    try:
        db.execute('DELETE FROM project_memories_fts WHERE memory_id=?', (memory_id,))
    except Exception:
        pass


def add_memory(
    project_id: str,
    content: str,
    category: str = 'convention',
    source_session_id: Optional[str] = None,
    pinned: bool = False,
    why: str = '',
    how_to_apply: str = '',
) -> Dict[str, Any]:
    clean_content = content.strip()
    if not clean_content:
        raise ValueError('记忆内容不能为空')
    if category not in CATEGORIES:
        category = 'convention'

    # Deduplicate within same project and same content
    existing = db.one(
        'SELECT id FROM project_memories WHERE project_id=? AND content=?',
        (project_id, clean_content),
    )
    now = time.time()
    if existing:
        db.execute(
            'UPDATE project_memories SET category=?, pinned=MAX(pinned, ?), '
            'why=CASE WHEN ?!="" THEN ? ELSE why END, '
            'how_to_apply=CASE WHEN ?!="" THEN ? ELSE how_to_apply END, '
            'updated=? WHERE id=?',
            (category, 1 if pinned else 0, why.strip(), why.strip(), how_to_apply.strip(), how_to_apply.strip(), now, existing['id']),
        )
        mem = get_memory(existing['id'])
        _sync_fts_upsert(mem)
        return mem

    ident = secrets.token_hex(10)
    db.execute(
        'INSERT INTO project_memories(id, project_id, category, content, source_session_id, pinned, why, how_to_apply, created, updated) '
        'VALUES(?,?,?,?,?,?,?,?,?,?)',
        (ident, project_id, category, clean_content, source_session_id, 1 if pinned else 0, why.strip(), how_to_apply.strip(), now, now),
    )
    mem = get_memory(ident)
    _sync_fts_upsert(mem)
    return mem


def update_memory(
    memory_id: str,
    content: Optional[str] = None,
    category: Optional[str] = None,
    pinned: Optional[bool] = None,
    why: Optional[str] = None,
    how_to_apply: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    mem = get_memory(memory_id)
    if not mem:
        return None

    updates = []
    args = []
    now = time.time()

    if content is not None:
        clean_content = content.strip()
        if not clean_content:
            raise ValueError('记忆内容不能为空')
        updates.append('content=?')
        args.append(clean_content)

    if category is not None:
        if category in CATEGORIES:
            updates.append('category=?')
            args.append(category)

    if pinned is not None:
        updates.append('pinned=?')
        args.append(1 if pinned else 0)

    if why is not None:
        updates.append('why=?')
        args.append(why.strip())

    if how_to_apply is not None:
        updates.append('how_to_apply=?')
        args.append(how_to_apply.strip())

    if not updates:
        return mem

    updates.append('updated=?')
    args.append(now)
    args.append(memory_id)

    db.execute(f"UPDATE project_memories SET {', '.join(updates)} WHERE id=?", tuple(args))
    updated_mem = get_memory(memory_id)
    _sync_fts_upsert(updated_mem)
    return updated_mem


def delete_memory(memory_id: str) -> bool:
    count_before = (db.one('SELECT count(*) as c FROM project_memories WHERE id=?', (memory_id,)) or {}).get('c', 0)
    if not count_before:
        return False
    db.execute('DELETE FROM project_memories WHERE id=?', (memory_id,))
    _sync_fts_delete(memory_id)
    return True


def search_memories(project_id: str, query_text: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Search project memories using SQLite FTS5 BM25 ranking with LIKE fallback."""
    q = (query_text or '').strip()
    if not q:
        return list_memories(project_id)[:limit]
    # Ensure index is populated for any pre-existing rows
    try:
        fts_count = (db.one('SELECT count(*) AS c FROM project_memories_fts WHERE project_id=?', (project_id,)) or {}).get('c', 0)
        mem_count = (db.one('SELECT count(*) AS c FROM project_memories WHERE project_id=?', (project_id,)) or {}).get('c', 0)
        if mem_count > 0 and fts_count == 0:
            for row in list_memories(project_id):
                _sync_fts_upsert(row)
        tokens = [t for t in re.findall(r'[\w\u4e00-\u9fff]+', q) if t]
        if tokens:
            fts_query = ' OR '.join(f'"{t}"' for t in tokens[:8])
            rows = db.query(
                'SELECT m.* FROM project_memories_fts f '
                'JOIN project_memories m ON m.id = f.memory_id '
                'WHERE f.project_id=? AND project_memories_fts MATCH ? '
                'ORDER BY m.pinned DESC, bm25(project_memories_fts) ASC, m.updated DESC LIMIT ?',
                (project_id, fts_query, limit),
            )
            if rows:
                return rows
    except Exception:
        pass
    like = f'%{q}%'
    return db.query(
        'SELECT * FROM project_memories WHERE project_id=? '
        'AND (content LIKE ? OR why LIKE ? OR how_to_apply LIKE ?) '
        'ORDER BY pinned DESC, updated DESC LIMIT ?',
        (project_id, like, like, like, limit),
    )


def consolidate_memories(
    project_id: str,
    source_ids: List[str],
    target_content: str,
    category: str = 'convention',
    why: str = '',
    how_to_apply: str = '',
    pinned: bool = False,
) -> Dict[str, Any]:
    """Atomically merge multiple redundant memories into a single consolidated entry."""
    clean = (target_content or '').strip()
    if not clean:
        raise ValueError('合并后的记忆内容不能为空')
    if category not in CATEGORIES:
        category = 'convention'
    now = time.time()
    ident = secrets.token_hex(10)
    with db.transaction() as conn:
        for sid in source_ids:
            conn.execute('DELETE FROM project_memories WHERE id=? AND project_id=?', (sid, project_id))
            try:
                conn.execute('DELETE FROM project_memories_fts WHERE memory_id=?', (sid,))
            except Exception:
                pass
        conn.execute(
            'INSERT INTO project_memories(id, project_id, category, content, source_session_id, pinned, why, how_to_apply, created, updated) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (ident, project_id, category, clean, None, 1 if pinned else 0, why.strip(), how_to_apply.strip(), now, now),
        )
    mem = get_memory(ident)
    _sync_fts_upsert(mem)
    return mem


def build_memory_context(project_id: str, max_items: int = 8, max_chars: int = 900) -> str:
    """Build a concise, token-efficient Markdown snippet to inject into initial LLM prompt."""
    memories = list_memories(project_id)
    if not memories:
        return ''

    # Prioritize pinned first, then recent ones, up to max_items
    selected = memories[:max_items]

    lines = ['[项目持久上下文与全局约定（跨会话保留，请严格遵从）]']
    used_chars = len(lines[0])
    for m in selected:
        cat_tag = CATEGORY_PREFIXES.get(m['category'], '【项目约定】')
        pin_mark = ' 📌' if m.get('pinned') else ''
        content = (m.get('content') or '').strip()
        if len(content) > 160:
            content = content[:160] + '…'
        line = f'- {cat_tag}{pin_mark} {content}'
        if used_chars + len(line) > max_chars and len(lines) > 1:
            break
        lines.append(line)
        used_chars += len(line)

    return '\n'.join(lines)


# Patterns that indicate explicit rules or conventions given by the user (must start at line boundary with explicit colon)
EXPLICIT_PATTERNS = [
    (r'(?:^|\n)\s*(?:以后|后续|请)?(?:记住|牢记|约定|规范|规则)[:：]\s*([^\n\r]+)', 'convention'),
    (r'(?:^|\n)\s*(?:架构决策|设计决定|技术选型)[:：]\s*([^\n\r]+)', 'architecture'),
    (r'(?:^|\n)\s*(?:严禁|禁止|切勿|红线|硬性约束)[:：]\s*([^\n\r]+)', 'constraint'),
    (r'(?:^|\n)\s*(?:构建|测试|运行|打包)(?:命令|方式|规范)[:：]\s*([^\n\r]+)', 'tooling'),
    (r'(?:^|\n)\s*(?:Remember|Convention|Rule)[:：]\s*([^\n\r]+)', 'convention'),
    (r'(?:^|\n)\s*(?:Do not|Never|Constraint)[:：]\s*([^\n\r]+)', 'constraint'),
]


def extract_explicit_memories(text: str) -> List[Dict[str, str]]:
    """Scan text for explicit user instructions meant to be remembered across sessions."""
    results = []
    if not text:
        return results

    for pattern, cat in EXPLICIT_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            candidate = match.strip().rstrip('。；;')
            if len(candidate) >= 4 and len(candidate) <= 240:
                results.append({'content': candidate, 'category': cat})
    return results


def auto_record_from_job(session: Dict[str, Any], job: Dict[str, Any], project: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Inspect completed job prompt and outcomes to extract and persist memories."""
    prompt = job.get('prompt', '')
    candidates = extract_explicit_memories(prompt)

    saved = []
    for cand in candidates:
        try:
            item = add_memory(
                project_id=project['id'],
                content=cand['content'],
                category=cand['category'],
                source_session_id=session['id'],
                pinned=False,
            )
            saved.append(item)
        except Exception:
            pass
    return saved
