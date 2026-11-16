"""Model Group urgency routing ("use expiring quota first") and quota-limit fallback suggestions.

Inspired by makecindy/cindy's model-groups.md:
- Computes urgency score = remaining_percent / max(0.5, countdown_hours) so
  subscriptions with high remaining quota expiring soonest are consumed first.
- Avoids 5h-almost-full windows far from reset.
- Respects image attachment requirements (skips text-only models when images exist).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _quota_urgency_for_engine(engine: str, model_id: str, quotas_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return {'available': bool, 'urgency': float, 'remaining': float|None, 'reason': str}."""
    if not quotas_snapshot or not isinstance(quotas_snapshot, dict):
        return {'available': True, 'urgency': 0.0, 'remaining': None, 'reason': 'quota_unknown'}

    if engine == 'codex':
        codex = quotas_snapshot.get('codex') or {}
        primary = codex.get('primary') or {}
        secondary = codex.get('secondary') or {}
        p_rem = primary.get('remaining_percent')
        s_rem = secondary.get('remaining_percent')
        if (p_rem is not None and p_rem <= 1) or (s_rem is not None and s_rem <= 1):
            return {'available': False, 'urgency': -1.0, 'remaining': 0.0, 'reason': 'codex_quota_exhausted'}
        # Score by whichever window is tighter / expiring sooner
        scores = []
        for w in (primary, secondary):
            rem = w.get('remaining_percent')
            secs = float(w.get('countdown_seconds') or 0)
            if rem is not None and secs > 0:
                hours = max(0.5, secs / 3600.0)
                scores.append(float(rem) / hours)
        urgency = max(scores) if scores else 1.0
        return {'available': True, 'urgency': round(urgency, 3), 'remaining': p_rem, 'reason': 'codex_urgency'}

    if engine == 'antigravity':
        agy = quotas_snapshot.get('antigravity') or {}
        buckets = agy.get('buckets') or []
        best_urgency = None
        any_exhausted = False
        for b in buckets:
            if not isinstance(b, dict):
                continue
            rem = b.get('remaining_percent')
            secs = float(b.get('countdown_seconds') or 0)
            if rem is not None and rem <= 1:
                any_exhausted = True
                continue
            if rem is not None:
                hours = max(0.5, secs / 3600.0)
                score = float(rem) / hours
                if best_urgency is None or score > best_urgency:
                    best_urgency = score
        if best_urgency is None and any_exhausted:
            return {'available': False, 'urgency': -1.0, 'remaining': 0.0, 'reason': 'antigravity_quota_exhausted'}
        return {'available': True, 'urgency': round(best_urgency or 1.0, 3), 'remaining': None, 'reason': 'antigravity_urgency'}

    # Non-subscription or API agents: lower urgency than expiring subscriptions
    return {'available': True, 'urgency': 0.1, 'remaining': None, 'reason': 'api_or_cli'}


def rank_candidates_by_urgency(
    candidates: List[Dict[str, Any]],
    quotas_snapshot: Optional[Dict[str, Any]] = None,
    has_images: bool = False,
) -> List[Dict[str, Any]]:
    """Rank model candidates by expiring-quota urgency while respecting image capability."""
    pool = list(candidates or [])
    if has_images:
        vision_only = [c for c in pool if c.get('supportsImage', True)]
        if vision_only:
            pool = vision_only

    scored: List[Dict[str, Any]] = []
    for idx, cand in enumerate(pool):
        engine = str(cand.get('engine') or '')
        model_id = str(cand.get('model') or '')
        q_info = _quota_urgency_for_engine(engine, model_id, quotas_snapshot)
        scored.append({
            **cand,
            'available': q_info['available'],
            'urgency': q_info['urgency'],
            'remaining_percent': q_info['remaining'],
            'reason': q_info['reason'],
            '_order': idx,
        })

    # Available candidates first, highest urgency first, preserve original order on ties
    scored.sort(key=lambda c: (0 if c['available'] else 1, -c['urgency'], c['_order']))
    for item in scored:
        item.pop('_order', None)
    return scored


def suggest_fallback_on_quota_limit(
    current_engine: str,
    candidates: List[Dict[str, Any]],
    quotas_snapshot: Optional[Dict[str, Any]] = None,
    has_images: bool = False,
) -> Optional[Dict[str, Any]]:
    """Return the best alternative engine/model when current_engine hits a quota limit."""
    others = [c for c in (candidates or []) if c.get('engine') != current_engine]
    if not others:
        return None
    ranked = rank_candidates_by_urgency(others, quotas_snapshot=quotas_snapshot, has_images=has_images)
    for item in ranked:
        if item.get('available'):
            return item
    return None
