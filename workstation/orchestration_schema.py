"""Structured protocol definitions and strict validation for Rove Multi-Agent Orchestration."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ORCHESTRATION_STRATEGIES = ('off', 'relay', 'parallel', 'auto')
ALLOWED_PLAN_STRATEGIES = ('relay', 'parallel')
ALLOWED_ROLES = (
    'planner',
    'coder_backend',
    'coder_frontend',
    'coder_fast',
    'verifier',
    'reviewer',
)
ALLOWED_CAPABILITIES = {
    'repo_read',
    'repo_write',
    'test_exec',
    'read_only_enforced',
    'approval_bridge',
    'cancel',
    'resume',
    'structured_output',
    'vision',
}

FORBIDDEN_PLAN_STEP_KEYS = {
    'mode',
    'permission_mode',
    'permissions',
    'shell',
    'shell_command',
    'command',
    'exec',
    'dangerously_skip_permissions',
    'sandbox',
}

MAX_PLAN_BYTES = 65536
MAX_IMPL_STEPS = 6
DEFAULT_MAX_FIX_ROUNDS = 2
DEFAULT_MAX_ATTEMPTS = 20
DEFAULT_RUN_TIMEOUT_SECS = 3600
DEFAULT_ATTEMPT_TIMEOUT_SECS = 1200

RUN_TERMINAL_STATES = {'completed', 'failed', 'cancelled'}
RUN_PAUSED_STATES = {'paused', 'waiting_approval', 'waiting_quota', 'needs_attention'}

PLAN_BLOCK_RE = re.compile(
    r'<!--\s*ROVE_PLAN_START\s*-->(.*?)<!--\s*ROVE_PLAN_END\s*-->',
    re.DOTALL,
)
FINDINGS_COUNT_RE = re.compile(r'FINDINGS_COUNT\s*:\s*(\d+)', re.IGNORECASE)
SEVERITY_LINE_RE = re.compile(r'^\s*[-*]?\s*\[?(P[012])\]?\s+(.+)$', re.MULTILINE)


class PlanValidationError(ValueError):
    """Raised when a model-proposed orchestration plan fails protocol validation."""


def normalize_strategy(value: Optional[str]) -> str:
    clean = str(value or 'off').strip().lower()
    if clean in ORCHESTRATION_STRATEGIES:
        return clean
    return 'off'


def default_budget(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    budget = {
        'max_impl_steps': MAX_IMPL_STEPS,
        'max_fix_rounds': DEFAULT_MAX_FIX_ROUNDS,
        'max_attempts': DEFAULT_MAX_ATTEMPTS,
        'max_plan_retries': 1,
        'run_timeout_secs': DEFAULT_RUN_TIMEOUT_SECS,
        'attempt_timeout_secs': DEFAULT_ATTEMPT_TIMEOUT_SECS,
    }
    if isinstance(overrides, dict):
        for key in ('max_impl_steps', 'max_fix_rounds', 'max_attempts', 'max_plan_retries', 'run_timeout_secs', 'attempt_timeout_secs'):
            if key in overrides and overrides[key] is not None:
                try:
                    val = int(overrides[key])
                    if val >= 0:
                        budget[key] = val
                except (TypeError, ValueError):
                    pass
    return budget


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if lines and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    return text


def extract_plan_block(final_output: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Extract and parse the single ROVE_PLAN_START/END block from a Planner final output.

    Returns (parsed_dict, error_reason). Never attempts regex repair on malformed JSON.
    """
    text = final_output or ''
    matches = PLAN_BLOCK_RE.findall(text)
    if not matches:
        return None, '未在 Planner 最终输出中找到 <!-- ROVE_PLAN_START --> 协议块'
    if len(matches) > 1:
        return None, f'Planner 输出包含 {len(matches)} 个计划块，协议要求唯一'

    raw_block = matches[0]
    if len(raw_block.encode('utf-8', errors='replace')) > MAX_PLAN_BYTES:
        return None, f'计划块体积超过 {MAX_PLAN_BYTES} 字节上限'

    cleaned = _strip_json_fences(raw_block)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return None, f'计划 JSON 解析失败: {exc.msg} (line {exc.lineno})'

    if not isinstance(parsed, dict):
        return None, '计划根节点必须为 JSON 对象'
    return parsed, None


def _normalize_scope_path(raw_path: str, project_root: Path) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PlanValidationError('files_scope 包含空路径')
    candidate = raw_path.strip()
    if os.path.isabs(candidate) or candidate.startswith('~'):
        raise PlanValidationError(f'files_scope 禁止绝对路径或家目录路径: {candidate}')
    if '\x00' in candidate:
        raise PlanValidationError('files_scope 包含非法空字符')

    parts = Path(candidate).parts
    if '..' in parts:
        raise PlanValidationError(f'files_scope 禁止父目录越界 (..): {candidate}')

    resolved_root = project_root.resolve()
    joined = project_root / candidate
    # Check symlink boundary for existing path or nearest existing ancestor
    probe = joined
    while not probe.exists() and probe != project_root and probe != probe.parent:
        probe = probe.parent
    try:
        resolved_probe = probe.resolve()
        if not resolved_probe.is_relative_to(resolved_root):
            raise PlanValidationError(f'files_scope 符号链接越界项目根目录: {candidate}')
    except OSError as exc:
        raise PlanValidationError(f'files_scope 路径无法校验: {candidate}') from exc

    norm = os.path.normpath(candidate).replace('\\', '/')
    if norm in ('.', '') or norm.startswith('../'):
        raise PlanValidationError(f'files_scope 路径非法: {candidate}')
    return norm


def _check_dag_acyclic(step_deps: Dict[str, List[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node: str) -> None:
        if node in visiting:
            raise PlanValidationError(f'步骤依赖存在循环: {node}')
        if node in visited:
            return
        visiting.add(node)
        for dep in step_deps.get(node, []):
            dfs(dep)
        visiting.remove(node)
        visited.add(node)

    for sid in step_deps:
        dfs(sid)


def validate_orchestration_plan(
    plan: Dict[str, Any],
    project_path: str,
    max_impl_steps: int = MAX_IMPL_STEPS,
    allowed_strategy: str = 'relay',
) -> Dict[str, Any]:
    """Validate and normalize a parsed orchestration plan dict."""
    if not isinstance(plan, dict):
        raise PlanValidationError('计划必须为 JSON 对象')

    schema_ver = plan.get('schema_version', 1)
    if schema_ver != 1:
        raise PlanValidationError(f'不支持的 schema_version: {schema_ver}')

    strategy = str(plan.get('strategy') or allowed_strategy or 'relay').strip().lower()
    if strategy not in ALLOWED_PLAN_STRATEGIES:
        raise PlanValidationError(f'非法的协同策略 strategy: {strategy}')

    raw_steps = plan.get('steps')
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanValidationError('steps 必须为非空数组')
    if len(raw_steps) > max_impl_steps:
        raise PlanValidationError(f'实现步骤数 {len(raw_steps)} 超过上限 {max_impl_steps}')

    project_root = Path(project_path) if project_path else Path('.')
    seen_ids: set[str] = set()
    step_deps: Dict[str, List[str]] = {}
    normalized_steps: List[Dict[str, Any]] = []

    for idx, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise PlanValidationError(f'第 {idx + 1} 个步骤必须为对象')
        forbidden = FORBIDDEN_PLAN_STEP_KEYS.intersection(raw.keys())
        if forbidden:
            raise PlanValidationError(f'步骤包含禁止的越权字段: {", ".join(sorted(forbidden))}')

        step_id = str(raw.get('id') or '').strip()
        if not step_id or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}', step_id):
            raise PlanValidationError(f'非法步骤 ID: {step_id!r}')
        if step_id in seen_ids:
            raise PlanValidationError(f'重复的步骤 ID: {step_id}')
        seen_ids.add(step_id)

        role = str(raw.get('role') or 'coder_backend').strip()
        if role not in ALLOWED_ROLES or role in ('planner', 'reviewer'):
            raise PlanValidationError(f'步骤 {step_id} 使用了非法实现角色: {role}')

        objective = str(raw.get('objective') or raw.get('prompt') or raw.get('title') or '').strip()
        if not objective:
            raise PlanValidationError(f'步骤 {step_id} 缺少 objective')
        title = str(raw.get('title') or objective[:64]).strip()

        deps_raw = raw.get('depends_on') or []
        if not isinstance(deps_raw, list):
            raise PlanValidationError(f'步骤 {step_id} 的 depends_on 必须为数组')
        deps = [str(d).strip() for d in deps_raw if str(d).strip()]
        step_deps[step_id] = deps

        scope_raw = raw.get('files_scope') or []
        if not isinstance(scope_raw, list):
            raise PlanValidationError(f'步骤 {step_id} 的 files_scope 必须为数组')
        norm_scope = [_normalize_scope_path(p, project_root) for p in scope_raw]

        acc_raw = raw.get('acceptance') or []
        if not isinstance(acc_raw, list):
            raise PlanValidationError(f'步骤 {step_id} 的 acceptance 必须为数组')
        acceptance = [str(a).strip()[:120] for a in acc_raw if str(a).strip()]

        caps_raw = raw.get('required_capabilities') or ['repo_read', 'repo_write']
        if not isinstance(caps_raw, list):
            raise PlanValidationError(f'步骤 {step_id} 的 required_capabilities 必须为数组')
        caps = []
        for cap in caps_raw:
            c_str = str(cap).strip()
            if c_str not in ALLOWED_CAPABILITIES:
                raise PlanValidationError(f'步骤 {step_id} 请求了未知能力: {c_str}')
            caps.append(c_str)

        normalized_steps.append({
            'id': step_id,
            'title': title,
            'role': role,
            'depends_on': deps,
            'files_scope': norm_scope,
            'acceptance': acceptance,
            'required_capabilities': caps or ['repo_read', 'repo_write'],
            'objective': objective,
            'optional': bool(raw.get('optional', False)),
        })

    for sid, deps in step_deps.items():
        for dep in deps:
            if dep == sid:
                raise PlanValidationError(f'步骤 {sid} 不能依赖自身')
            if dep not in seen_ids:
                raise PlanValidationError(f'步骤 {sid} 依赖了不存在的步骤: {dep}')

    _check_dag_acyclic(step_deps)

    return {
        'schema_version': 1,
        'strategy': strategy,
        'require_cross_review': bool(plan.get('require_cross_review', True)),
        'steps': normalized_steps,
    }


def parse_review_result(raw_output: str) -> Dict[str, Any]:
    """Parse structured review findings & machine count.

    Fail-closed: missing FINDINGS_COUNT yields findings_count=None (never coerced to 0).
    """
    text = (raw_output or '').strip()
    matches = FINDINGS_COUNT_RE.findall(text)
    findings_count = int(matches[-1]) if matches else None

    findings: List[Dict[str, str]] = []
    for sev, desc in SEVERITY_LINE_RE.findall(text):
        findings.append({'severity': sev.upper(), 'detail': desc.strip()[:300]})

    p0_p1 = [f for f in findings if f['severity'] in ('P0', 'P1')]
    return {
        'findings_count': findings_count,
        'valid_count': findings_count is not None,
        'findings': findings,
        'blocking_count': len(p0_p1),
        'raw_preview': text[:2000],
    }
