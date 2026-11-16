"""Layered tool-loop guard with secret redaction and auxiliary review hook.

Inspired by makecindy/cindy's ToolLoopGuard & ToolLoopMonitor:
- Distinguishes legitimate paced polling (>=30s gap, .log tailing, human approval wait)
  from genuine infinite loops (identical repeats, ABAB ping-pong, ABCD rotation).
- Redacts credentials/tokens BEFORE truncating evidence strings so truncated prefixes
  never leak raw secrets.
- Supports asynchronous auxiliary-model review (CONTINUE / STOP) before hard-killing.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import re
import time
from typing import Any, Callable, Deque, Dict, List, Optional

DEFAULT_CONSECUTIVE_LIMIT = 4
DEFAULT_WINDOW_SIZE = 12
DEFAULT_WINDOW_DISTINCT_LIMIT = 2
DEFAULT_ROTATION_WINDOW_SIZE = 16
DEFAULT_ROTATION_DISTINCT_LIMIT = 4
DEFAULT_PACED_INTERVAL_MS = 30_000
DEFAULT_FINAL_CONSECUTIVE_LIMIT = 30
EVIDENCE_SIZE = 12
EVIDENCE_MAX_CHARS = 400

_LOG_TAIL_RE = re.compile(r'(?:^|\s)(?:tail|Get-Content)\b[^\n]*\.log(?:\s|$)', re.IGNORECASE)

_SECRET_PATTERNS = (
    (re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}'), r'\1[REDACTED]'),
    (re.compile(r'(?i)((?:api[_-]?key|token|secret|password|passwd|auth)\s*[:=]\s*["\']?)[^\s"\']{6,}'), r'\1[REDACTED]'),
    (re.compile(r'(https?://[^/\s:@]+:)[^@\s]+(@)'), r'\1[REDACTED]\2'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)'), '[REDACTED_PRIVATE_KEY]'),
    (re.compile(r'\b(?:sk|pk|ghp|gho|xoxb|xoxp|AIza)[A-Za-z0-9_\-]{16,}\b'), '[REDACTED_TOKEN]'),
    (re.compile(r'\b[A-Za-z0-9+/=_-]{36,}\b'), '[REDACTED_LONG_SECRET]'),
)


def redact_sensitive_text(text: str) -> str:
    """Redact credentials/tokens from text BEFORE truncating."""
    if not text:
        return ''
    out = str(text)
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def format_redacted_evidence(evidence: List[Dict[str, Any]], max_chars: int = EVIDENCE_MAX_CHARS) -> List[Dict[str, Any]]:
    """Redact first, then truncate each evidence entry."""
    formatted = []
    for item in evidence[-EVIDENCE_SIZE:]:
        inp = redact_sensitive_text(str(item.get('input') or ''))[:max_chars]
        out = redact_sensitive_text(str(item.get('output') or ''))[:max_chars]
        formatted.append({
            'tool': str(item.get('tool') or ''),
            'input': inp,
            'output': out,
        })
    return formatted


def _stable_hash(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = str(value)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _is_log_tail_command(tool_name: str, tool_input: Any) -> bool:
    if tool_name.lower() not in ('commandexecution', 'bash', 'shell', 'exec_command'):
        return False
    cmd = ''
    if isinstance(tool_input, dict):
        cmd = str(tool_input.get('command') or tool_input.get('cmd') or '')
    else:
        cmd = str(tool_input or '')
    if not cmd or any(ch in cmd for ch in ('>', '|', '&&', ';')):
        return False
    return bool(_LOG_TAIL_RE.search(cmd))


class ToolLoopGuard:
    """Tracks tool calls in a turn and detects stuck/looping agent behavior."""

    def __init__(
        self,
        consecutive_limit: int = DEFAULT_CONSECUTIVE_LIMIT,
        window_size: int = DEFAULT_WINDOW_SIZE,
        window_distinct_limit: int = DEFAULT_WINDOW_DISTINCT_LIMIT,
        rotation_window_size: int = DEFAULT_ROTATION_WINDOW_SIZE,
        rotation_distinct_limit: int = DEFAULT_ROTATION_DISTINCT_LIMIT,
        paced_interval_ms: int = DEFAULT_PACED_INTERVAL_MS,
        final_consecutive_limit: int = DEFAULT_FINAL_CONSECUTIVE_LIMIT,
    ) -> None:
        self.consecutive_limit = consecutive_limit
        self.window_size = window_size
        self.window_distinct_limit = window_distinct_limit
        self.rotation_window_size = rotation_window_size
        self.rotation_distinct_limit = rotation_distinct_limit
        self.paced_interval_ms = paced_interval_ms
        self.final_consecutive_limit = final_consecutive_limit

        self._last_exact_fp: Optional[str] = None
        self._consecutive_exact = 0
        self._rapid_exact = 0
        self._short_window: Deque[str] = deque(maxlen=window_size)
        self._rotation_window: Deque[str] = deque(maxlen=rotation_window_size)
        self._evidence: Deque[Dict[str, Any]] = deque(maxlen=EVIDENCE_SIZE)

        self._in_flight_starts: Dict[str, float] = {}
        self._last_call_start_ms: Optional[float] = None
        self._last_result_ms: Optional[float] = None
        self._waiting_for_human = False
        self._grace_remaining = 0
        self._reviews_used = 0
        self._max_reviews_per_turn = 3

    def set_waiting_for_human(self, waiting: bool) -> None:
        """Pause loop escalation while waiting for human approval/input."""
        self._waiting_for_human = bool(waiting)

    def grant_grace(self, count: int = 20) -> None:
        self._grace_remaining = max(self._grace_remaining, count)

    def on_tool_use(self, call_id: str, tool_name: str, tool_input: Any, now_ms: Optional[float] = None) -> None:
        ts = now_ms if now_ms is not None else time.monotonic() * 1000.0
        self._in_flight_starts[call_id] = ts

    def on_tool_result(
        self,
        call_id: str,
        tool_name: str,
        tool_input: Any,
        output: Any,
        now_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Record a completed tool call and return verdict {'status': 'ok'|'suspected'|'stop', ...}."""
        ts = now_ms if now_ms is not None else time.monotonic() * 1000.0
        start_ms = self._in_flight_starts.pop(call_id, ts)

        # Record evidence (raw; redacted before leaving process)
        self._evidence.append({
            'tool': tool_name,
            'input': tool_input,
            'output': output,
        })

        if self._waiting_for_human or _is_log_tail_command(tool_name, tool_input):
            self._last_call_start_ms = start_ms
            self._last_result_ms = ts
            return {'status': 'ok'}

        # Paced polling exemption (>=30s between calls or results)
        is_paced = False
        if self._last_call_start_ms is not None and (start_ms - self._last_call_start_ms) >= self.paced_interval_ms:
            is_paced = True
        if self._last_result_ms is not None and (ts - self._last_result_ms) >= self.paced_interval_ms:
            is_paced = True

        self._last_call_start_ms = start_ms
        self._last_result_ms = ts

        in_fp = f'{tool_name}:{_stable_hash(tool_input)}'
        exact_fp = f'{in_fp}:{_stable_hash(output)}'

        if exact_fp == self._last_exact_fp:
            self._consecutive_exact += 1
            if not is_paced:
                self._rapid_exact += 1
        else:
            self._last_exact_fp = exact_fp
            self._consecutive_exact = 1
            self._rapid_exact = 0 if is_paced else 1

        if self._rapid_exact >= self.final_consecutive_limit:
            return {
                'status': 'stop',
                'final': True,
                'reason': 'rapid_identical_loop',
                'count': self._rapid_exact,
                'tool': tool_name,
            }

        if is_paced:
            return {'status': 'ok'}

        self._short_window.append(in_fp)
        self._rotation_window.append(in_fp)

        if self._grace_remaining > 0:
            self._grace_remaining -= 1
            return {'status': 'ok'}

        reason = None
        count = 0
        if self._consecutive_exact >= self.consecutive_limit:
            reason = 'consecutive_identical'
            count = self._consecutive_exact
        elif len(self._short_window) == self.window_size and len(set(self._short_window)) <= self.window_distinct_limit:
            reason = 'short_window_pingpong'
            count = len(self._short_window)
        elif len(self._rotation_window) == self.rotation_window_size and len(set(self._rotation_window)) <= self.rotation_distinct_limit:
            reason = 'rotation_loop'
            count = len(self._rotation_window)

        if reason:
            return {
                'status': 'suspected',
                'final': False,
                'reason': reason,
                'count': count,
                'tool': tool_name,
                'can_review': self._reviews_used < self._max_reviews_per_turn,
                'evidence': format_redacted_evidence(list(self._evidence)),
            }
        return {'status': 'ok'}

    def mark_review_used(self) -> None:
        self._reviews_used += 1


def review_suspected_loop(
    verdict: Dict[str, Any],
    user_goal: str,
    assistant_explanation: str = '',
    reviewer_fn: Optional[Callable[[str], str]] = None,
) -> str:
    """Ask an auxiliary reviewer whether a suspected loop should CONTINUE or STOP.

    Fail-safe: returns 'CONTINUE' on missing reviewer, error, or ambiguous output.
    """
    if reviewer_fn is None:
        return 'CONTINUE'
    evidence = verdict.get('evidence') or []
    safe_goal = redact_sensitive_text(user_goal)[:1200]
    safe_expl = redact_sensitive_text(assistant_explanation)[:800]
    prompt = (
        'You are a loop-detection reviewer. Decide whether the coding agent is stuck in a '
        'pointless loop or making legitimate progress.\n'
        'Reply with ONLY one word: CONTINUE or STOP.\n\n'
        f'User Goal:\n{safe_goal}\n\n'
        f'Recent Assistant Statement:\n{safe_expl or "(none)"}\n\n'
        f'Trigger: {verdict.get("reason")} (count={verdict.get("count")})\n'
        f'Recent 12 Tool Calls (redacted):\n{json.dumps(evidence, ensure_ascii=False, indent=2)}'
    )
    try:
        ans = (reviewer_fn(prompt) or '').strip().upper()
    except Exception:
        return 'CONTINUE'
    if 'STOP' in ans and 'CONTINUE' not in ans:
        return 'STOP'
    return 'CONTINUE'
