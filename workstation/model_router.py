"""Task-aware & quota-adaptive auto selection for engine, model, and thinking effort.

Capabilities:
1. Classifies task complexity ('simple' / 'standard' / 'complex') from prompt text, mode, and attachments.
2. Reads real Codex (5h + 7d weekly) and Antigravity (Argon / Flash / Claude&GPT 7d + 5h pools) quotas.
3. Prioritizes 7-day weekly quotas that are resetting soon with healthy remaining balance ("use expiring quota first").
4. Dynamically scales model tier and thinking effort up when 7-day quota is about to expire unused,
   and scales down (or switches engines/pools) when quota is scarce to avoid mid-task rate limits.
5. Preserves rank_candidates_by_urgency() and suggest_fallback_on_quota_limit() for backwards compatibility.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple


_EFFORT_ORDER = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')

_COMPLEX_KEYWORDS = (
    '架构', '重构', '全面', '审计', '深度', '疑难', '死锁', '并发', '竞态',
    '迁移', '安全漏洞', '性能优化', '系统设计', '从零', '根因', '底层', '全栈',
    '核心模块', '事务', '分布式', '排查', '分析所有', '检查所有', '完整实现',
    'architect', 'refactor', 'audit', 'concurrency', 'race condition', 'deadlock',
    'migration', 'security', 'vulnerability', 'root cause', 'performance',
    'benchmark', 'end-to-end', 'deep dive', 'redesign', 'system design',
)

_HIGH_COMPLEX_SIGNALS = (
    '架构', '重构', '审计', '死锁', '并发', '竞态', '安全漏洞', '系统设计',
    'architect', 'audit', 'concurrency', 'deadlock', 'security', 'vulnerability',
    'redesign', 'root cause',
)

_SIMPLE_KEYWORDS = (
    '什么意思', '怎么用', '解释', '翻译', '总结', '简述', '查看', '看一下',
    '查一下', '列出来', '改个名', '改名', '拼写', '注释', '格式化', '你好', '在吗',
    'explain', 'what is', 'how to', 'translate', 'summarize', 'typo', 'rename',
    'format', 'comment', 'list', 'check', 'status', 'hello', 'hi',
)

_STANDARD_CODE_KEYWORDS = (
    '修复', '修改', '实现', '添加', '新增', '编写', '开发', '补充', '测试', '单元测试',
    '接口', '组件', '页面', '报错', '异常', '校验', '调试', '适配', '支持', '接入', '集成',
    'fix', 'implement', 'add', 'create', 'update', 'write', 'test', 'debug',
    'endpoint', 'component', 'bug', 'error', 'issue', 'feature', 'support',
)

_EFFORT_ZH = {
    'low': '极速 (Low)',
    'medium': '均衡 (Medium)',
    'high': '深入 (High)',
    'xhigh': '高强 (X-High)',
    'max': '极限 (Max)',
    'ultra': '超算 (Ultra)',
}


def _effective_text_length(text: str) -> int:
    """Estimate semantic weight by weighting CJK characters higher than ASCII."""
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return len(text) + int(cjk * 1.2)


def classify_task(
    prompt: str = '',
    mode: str = 'standard',
    has_images: bool = False,
) -> Dict[str, Any]:
    """Classify task complexity and baseline desired tier / effort."""
    text = (prompt or '').strip()
    lower = text.lower()
    char_len = len(text)
    eff_len = _effective_text_length(text)
    lines = text.count('\n') + (1 if text else 0)
    mentions = len(re.findall(r'(?:^|\s)@[A-Za-z0-9_./-]+', text))
    has_code_block = '```' in text
    multi_steps = len(re.findall(r'(?m)^\s*(?:\d+\.|[-*])\s+', text)) >= 3

    complex_hits = sum(1 for kw in _COMPLEX_KEYWORDS if kw in lower)
    deep_hits = sum(1 for kw in _HIGH_COMPLEX_SIGNALS if kw in lower)
    simple_hits = sum(1 for kw in _SIMPLE_KEYWORDS if kw in lower)
    code_hits = sum(1 for kw in _STANDARD_CODE_KEYWORDS if kw in lower)

    is_plan = (mode == 'plan')
    if (
        complex_hits >= 1
        or eff_len >= 280
        or lines >= 6
        or mentions >= 3
        or multi_steps
        or (is_plan and char_len >= 25)
    ):
        want_xhigh = deep_hits >= 2 or (complex_hits >= 2 and eff_len >= 320) or (is_plan and complex_hits >= 1)
        return {
            'complexity': 'complex',
            'desired_tier': 'flagship',
            'base_effort': 'xhigh' if want_xhigh else 'high',
            'reason_label': '只读规划/架构分析' if is_plan else '复杂开发/重构排查',
        }

    if (
        not is_plan
        and not has_images
        and not has_code_block
        and code_hits == 0
        and eff_len <= 110
        and lines <= 2
        and mentions <= 1
        and (simple_hits >= 1 or eff_len <= 28)
    ):
        return {
            'complexity': 'simple',
            'desired_tier': 'fast',
            'base_effort': 'low',
            'reason_label': '轻量问答/快速修改',
        }

    return {
        'complexity': 'standard',
        'desired_tier': 'balanced',
        'base_effort': 'medium',
        'reason_label': '图文分析与常规开发' if has_images else '常规编码与排错',
    }


def _classify_model_tier(engine: str, model_spec: Dict[str, Any]) -> str:
    mid = str(model_spec.get('id') or model_spec.get('model') or '').lower()
    # Check flagship families before general/fast tokens (and use token boundaries so 'gemini' != 'mini')
    if any(tok in mid for tok in ('argon', 'astra', 'opus', 'gpt-5.4')) or re.search(r'(?:^|[-_.])(o1|o3)(?:$|[-_.])', mid):
        return 'flagship'
    if 'hydrogen' in mid:
        # Hydrogen (Low) handles trivial/quick prompts; Hydrogen (Medium/High) serves everyday standard coding
        if mid.endswith('-low') or model_spec.get('defaultEffort') == 'low':
            return 'fast'
        return 'balanced'
    if any(tok in mid for tok in ('flash', 'luna')) or re.search(r'(?:^|[-_.])(mini|nano|haiku|light)(?:$|[-_.])', mid):
        return 'fast'
    if any(tok in mid for tok in ('sonnet', 'sol', 'terra', 'gpt-oss')) or re.search(r'(?:^|[-_.])pro(?:$|[-_.])', mid):
        return 'balanced'
    if model_spec.get('defaultEffort') in ('high', 'xhigh', 'max', 'ultra'):
        return 'flagship'
    if model_spec.get('defaultEffort') == 'low':
        return 'fast'
    return 'balanced'


def _window_countdown(win: Optional[Dict[str, Any]], now: float) -> float:
    if not isinstance(win, dict):
        return 0.0
    if win.get('countdown_seconds') is not None:
        return max(0.0, float(win.get('countdown_seconds') or 0.0))
    resets_at = float(win.get('resets_at') or 0.0)
    if resets_at > 0:
        return max(0.0, resets_at - now)
    return 0.0


def _pick_agy_pool(agy: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """Select the most specific quota record for an Antigravity model id."""
    mid = (model_id or '').strip()
    mid_l = mid.lower()
    by_mid = agy.get('by_model_id') or {}
    fallback_card: Optional[Dict[str, Any]] = None
    if 'argon' in mid_l and isinstance(agy.get('gemini_4_argon'), dict):
        fallback_card = agy['gemini_4_argon']
    elif ('claude' in mid_l or 'gpt' in mid_l) and isinstance(agy.get('claude_gpt'), dict):
        fallback_card = agy['claude_gpt']
    elif any(k in mid_l for k in ('hydrogen', 'flash', 'pro')) and isinstance(agy.get('general_flash'), dict):
        fallback_card = agy['general_flash']

    if mid and isinstance(by_mid.get(mid), dict):
        entry = dict(by_mid[mid])
        if fallback_card:
            if not entry.get('weekly') and fallback_card.get('weekly'):
                entry['weekly'] = fallback_card['weekly']
            if not entry.get('rolling_5h') and fallback_card.get('rolling_5h'):
                entry['rolling_5h'] = fallback_card['rolling_5h']
        return entry
    if fallback_card:
        return fallback_card
    for item in (agy.get('models') or []):
        if isinstance(item, dict) and item.get('model_id') == mid:
            return item
    return agy.get('general_flash') or agy.get('gemini_4_argon') or agy.get('claude_gpt') or {}


def _format_hours_brief(secs: float) -> str:
    if secs <= 0:
        return '即将刷新'
    hours = secs / 3600.0
    if hours < 1.0:
        return f'{max(1, int(round(secs / 60.0)))}分钟后刷新'
    if hours < 48.0:
        return f'{round(hours, 1):g}小时后刷新'
    days = hours / 24.0
    return f'{round(days, 1):g}天后刷新'


def evaluate_quota_profile(
    engine: str,
    model_id: str,
    quotas_snapshot: Optional[Dict[str, Any]],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Extract normalized 7-day weekly & 5h rolling quota metrics and compute budget state."""
    cur_now = float(now if now is not None else time.time())
    if not quotas_snapshot or not isinstance(quotas_snapshot, dict):
        return {
            'engine_ready': True,
            'available': True,
            'weekly_remaining': None,
            'weekly_countdown_secs': 0.0,
            'rolling_remaining': None,
            'rolling_countdown_secs': 0.0,
            'weekly_surplus': 0.0,
            'urgency': 0.5,
            'quota_score': 0.0,
            'budget_state': 'unknown',
            'quota_summary': '额度未知（按任务复杂度智能选择）',
            'reason': 'quota_unknown',
        }

    weekly_rem: Optional[float] = None
    weekly_secs: float = 0.0
    rolling_rem: Optional[float] = None
    rolling_secs: float = 0.0

    if engine == 'codex':
        codex = quotas_snapshot.get('codex') or {}
        if codex.get('status') in ('unavailable', 'error'):
            return {
                'engine_ready': False,
                'available': False,
                'weekly_remaining': None,
                'weekly_countdown_secs': 0.0,
                'rolling_remaining': None,
                'rolling_countdown_secs': 0.0,
                'weekly_surplus': 0.0,
                'urgency': -1.0,
                'quota_score': -100.0,
                'budget_state': 'exhausted',
                'quota_summary': 'Codex 额度暂不可用',
                'reason': 'codex_unavailable',
            }
        primary = codex.get('primary') or {}
        secondary = codex.get('secondary') or {}
        if primary.get('remaining_percent') is not None:
            rolling_rem = float(primary['remaining_percent'])
            rolling_secs = _window_countdown(primary, cur_now)
        if secondary.get('remaining_percent') is not None:
            weekly_rem = float(secondary['remaining_percent'])
            weekly_secs = _window_countdown(secondary, cur_now)

    elif engine == 'antigravity':
        agy = quotas_snapshot.get('antigravity') or {}
        if agy.get('status') in ('unavailable', 'error'):
            return {
                'engine_ready': False,
                'available': False,
                'weekly_remaining': None,
                'weekly_countdown_secs': 0.0,
                'rolling_remaining': None,
                'rolling_countdown_secs': 0.0,
                'weekly_surplus': 0.0,
                'urgency': -1.0,
                'quota_score': -100.0,
                'budget_state': 'exhausted',
                'quota_summary': 'Antigravity 额度暂不可用',
                'reason': 'antigravity_unavailable',
            }
        pool = _pick_agy_pool(agy, model_id)
        if pool:
            w = pool.get('weekly')
            r5 = pool.get('rolling_5h')
            if isinstance(w, dict) and w.get('remaining_percent') is not None:
                weekly_rem = float(w['remaining_percent'])
                weekly_secs = _window_countdown(w, cur_now)
            elif pool.get('remaining_percent') is not None:
                # Fallback if card stores weekly at top level
                p_secs = _window_countdown(pool, cur_now)
                if '5小时' in str(pool.get('pool_tag') or ''):
                    rolling_rem = float(pool['remaining_percent'])
                    rolling_secs = p_secs
                else:
                    weekly_rem = float(pool['remaining_percent'])
                    weekly_secs = p_secs
            if isinstance(r5, dict) and r5.get('remaining_percent') is not None:
                rolling_rem = float(r5['remaining_percent'])
                rolling_secs = _window_countdown(r5, cur_now)

        # Compatibility with synthetic 'buckets' schema used in tests
        if weekly_rem is None and rolling_rem is None:
            buckets = agy.get('buckets') or []
            best_b_score = -1.0
            for b in buckets:
                if not isinstance(b, dict) or b.get('remaining_percent') is None:
                    continue
                rem = float(b['remaining_percent'])
                secs = _window_countdown(b, cur_now)
                if secs > 19800 or 'weekly' in str(b.get('window', '')).lower():
                    weekly_rem = rem
                    weekly_secs = secs
                else:
                    rolling_rem = rem
                    rolling_secs = secs
                sc = rem / max(0.5, secs / 3600.0) if secs > 0 else rem
                if sc > best_b_score:
                    best_b_score = sc
    else:
        return {
            'engine_ready': True,
            'available': True,
            'weekly_remaining': None,
            'weekly_countdown_secs': 0.0,
            'rolling_remaining': None,
            'rolling_countdown_secs': 0.0,
            'weekly_surplus': 0.0,
            'urgency': 0.1,
            'quota_score': 0.0,
            'budget_state': 'normal',
            'quota_summary': '第三方/API 引擎（按任务需求匹配）',
            'reason': 'api_or_cli',
        }

    if (weekly_rem is not None and weekly_rem <= 1) or (rolling_rem is not None and rolling_rem <= 1):
        return {
            'engine_ready': True,
            'available': False,
            'weekly_remaining': weekly_rem,
            'weekly_countdown_secs': weekly_secs,
            'rolling_remaining': rolling_rem,
            'rolling_countdown_secs': rolling_secs,
            'weekly_surplus': -50.0,
            'urgency': -1.0,
            'quota_score': -100.0,
            'budget_state': 'exhausted',
            'quota_summary': '该模型池额度已耗尽，自动避让',
            'reason': f'{engine}_quota_exhausted',
        }

    # Compute 7-day surplus and expiring urgency ("7天马上刷新的优先使用")
    surplus = 0.0
    quota_score = 0.0
    urgency_scores: List[float] = []
    rolling_healthy = rolling_rem is None or rolling_rem >= 22

    if weekly_rem is not None:
        if weekly_secs > 0:
            w_hours = max(1.0, weekly_secs / 3600.0)
            urgency_scores.append(weekly_rem / w_hours)
            expected_rem = 100.0 * min(1.0, w_hours / 168.0)
            surplus = weekly_rem - expected_rem

            if weekly_rem < 10:
                # Nearly empty weekly tank: avoid blowing the final sliver unless no alternative
                quota_score -= 15.0
            else:
                if surplus > 0:
                    quota_score += min(30.0, surplus * 0.45)
                if w_hours <= 48.0 and weekly_rem >= 12:
                    imminent = ((48.0 - w_hours) / 48.0) * min(15.0, weekly_rem * 0.25)
                    quota_score += imminent
                if surplus < -15.0 and w_hours > 24.0:
                    quota_score += max(-25.0, (surplus + 15.0) * 0.5)
        else:
            if weekly_rem >= 60:
                quota_score += 4.0
            elif weekly_rem < 20:
                quota_score -= 12.0

    if rolling_rem is not None:
        if rolling_secs > 0:
            r_hours = max(0.5, rolling_secs / 3600.0)
            urgency_scores.append(rolling_rem / r_hours)
        if rolling_rem < 12 and (rolling_secs == 0 or rolling_secs > 1200):
            quota_score -= 25.0
        elif rolling_rem < 25 and (rolling_secs == 0 or rolling_secs > 1800):
            quota_score -= 10.0
        elif rolling_rem >= 65 and 0 < rolling_secs <= 5400:
            quota_score += 6.0

    # Determine overall budget_state
    w_hours_eff = (weekly_secs / 3600.0) if weekly_secs > 0 else 84.0
    if not rolling_healthy and (rolling_secs == 0 or rolling_secs > 1800):
        budget_state = 'scarce'
    elif weekly_rem is not None and weekly_rem < 22 and w_hours_eff > 24.0:
        budget_state = 'scarce'
    elif (
        weekly_rem is not None
        and weekly_secs > 0
        and rolling_healthy
        and (
            (w_hours_eff <= 42.0 and weekly_rem >= 20 and surplus >= 15.0)
            or (w_hours_eff <= 24.0 and weekly_rem >= 15)
        )
    ):
        budget_state = 'expiring_abundant'
    elif (weekly_rem is None or weekly_rem >= 55) and (rolling_rem is None or rolling_rem >= 45) and surplus >= -5.0:
        budget_state = 'abundant'
    else:
        budget_state = 'normal'

    parts = []
    if weekly_rem is not None:
        reset_hint = _format_hours_brief(weekly_secs) if weekly_secs > 0 else '周配额'
        parts.append(f'7天额度剩{int(round(weekly_rem))}%（{reset_hint}）')
    if rolling_rem is not None and (weekly_rem is None or rolling_rem < 40):
        parts.append(f'5h额度剩{int(round(rolling_rem))}%')
    quota_summary = ' · '.join(parts) if parts else '额度充裕'

    urgency = max(urgency_scores) if urgency_scores else 1.0
    return {
        'engine_ready': True,
        'available': True,
        'weekly_remaining': weekly_rem,
        'weekly_countdown_secs': weekly_secs,
        'rolling_remaining': rolling_rem,
        'rolling_countdown_secs': rolling_secs,
        'weekly_surplus': round(surplus, 2),
        'urgency': round(urgency, 3),
        'quota_score': round(quota_score, 3),
        'budget_state': budget_state,
        'quota_summary': quota_summary,
        'reason': f'{engine}_{budget_state}',
    }


def _quota_urgency_for_engine(engine: str, model_id: str, quotas_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return {'available': bool, 'urgency': float, 'remaining': float|None, 'reason': str}."""
    if not quotas_snapshot or not isinstance(quotas_snapshot, dict):
        return {'available': True, 'urgency': 0.0, 'remaining': None, 'reason': 'quota_unknown'}
    prof = evaluate_quota_profile(engine, model_id, quotas_snapshot)
    rem = prof['rolling_remaining'] if prof['rolling_remaining'] is not None else prof['weekly_remaining']
    return {
        'available': prof['available'] and prof['engine_ready'],
        'urgency': prof['urgency'],
        'remaining': rem,
        'reason': prof['reason'],
    }


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
        model_id = str(cand.get('model') or cand.get('id') or '')
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


def _adjust_tier_and_effort(
    task_info: Dict[str, Any],
    budget_state: str,
    supported_efforts: List[str],
    pinned_effort: Optional[str] = None,
) -> Tuple[str, str]:
    """Compute effective (desired_tier, chosen_effort) given task + quota budget state."""
    desired_tier = task_info['desired_tier']
    base_effort = task_info['base_effort']
    complexity = task_info['complexity']

    if complexity == 'simple':
        # Never waste heavy flagship thinking on simple/quick prompts
        desired_tier = 'fast'
        target_effort = 'low'
    elif budget_state == 'expiring_abundant':
        # 7-day quota about to expire unused: upgrade standard -> flagship, bump effort +1 tier
        if desired_tier == 'standard':
            desired_tier = 'flagship'
        effort_map = {'low': 'medium', 'medium': 'high', 'high': 'xhigh', 'xhigh': 'xhigh'}
        target_effort = effort_map.get(base_effort, base_effort)
    elif budget_state == 'scarce':
        # Running tight: scale down model tier & conserve thinking tokens
        if desired_tier == 'flagship':
            desired_tier = 'balanced'
        elif desired_tier == 'standard':
            desired_tier = 'fast'
        effort_map = {'xhigh': 'medium', 'high': 'medium', 'medium': 'low', 'low': 'low'}
        target_effort = effort_map.get(base_effort, 'low')
    else:
        target_effort = base_effort

    effs = list(supported_efforts or ['low', 'medium', 'high'])
    if pinned_effort and pinned_effort != 'auto':
        if pinned_effort in effs:
            return desired_tier, pinned_effort
        target_effort = pinned_effort

    if target_effort in effs:
        return desired_tier, target_effort

    # Snap to nearest supported effort (preferring closest rank without overshooting 'max'/'ultra' unless asked)
    target_idx = _EFFORT_ORDER.index(target_effort) if target_effort in _EFFORT_ORDER else 1
    best_eff = min(
        effs,
        key=lambda e: (abs((_EFFORT_ORDER.index(e) if e in _EFFORT_ORDER else 1) - target_idx), e),
    )
    return desired_tier, best_eff


def _tier_fit_score(model_tier: str, desired_tier: str) -> float:
    if model_tier == desired_tier:
        return 35.0
    pairs = {
        ('flagship', 'balanced'), ('balanced', 'flagship'),
        ('balanced', 'fast'), ('fast', 'balanced'),
    }
    if (model_tier, desired_tier) in pairs:
        return 18.0
    return 0.0


def auto_select_model_and_effort(
    prompt: str = '',
    mode: str = 'standard',
    has_images: bool = False,
    requested_engine: str = 'auto',
    requested_model: Optional[str] = 'auto',
    requested_effort: Optional[str] = 'auto',
    requested_fast: bool = False,
    quotas_snapshot: Optional[Dict[str, Any]] = None,
    catalog: Optional[Dict[str, Any]] = None,
    installed_engines: Optional[List[str]] = None,
    preferred_engine: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Select optimal concrete (engine, model, effort, fast) based on task & dynamic 7-day quota."""
    from . import agents, engines

    cat = catalog if isinstance(catalog, dict) else engines.catalog()
    task_info = classify_task(prompt=prompt, mode=mode, has_images=has_images)

    auto_engine = (requested_engine == 'auto' or not requested_engine)
    auto_model = (requested_model == 'auto' or not requested_model)
    auto_effort = (requested_effort == 'auto' or not requested_effort)

    if auto_engine:
        if installed_engines is not None:
            candidate_engines = [e for e in installed_engines if e != 'auto']
        else:
            candidate_engines = [e for e in ('codex', 'antigravity') if agents.is_agent_installed(e)]
            if not candidate_engines:
                candidate_engines = ['codex', 'antigravity']
    else:
        candidate_engines = [requested_engine]

    scored_choices: List[Tuple[float, Dict[str, Any]]] = []
    for eng in candidate_engines:
        models_list = ((cat.get(eng) or {}).get('models')) or engines.engine_models(eng).get('models') or []
        if not models_list:
            continue

        if not auto_model:
            matched = [m for m in models_list if m.get('id') == requested_model]
            if matched:
                pool = matched
            else:
                pool = [{'id': requested_model, 'name': requested_model, 'efforts': ['low', 'medium', 'high'], 'supportsImage': True}]
        else:
            pool = list(models_list)
            if has_images:
                vision_pool = [m for m in pool if m.get('supportsImage', True)]
                if vision_pool:
                    pool = vision_pool

        for idx, m_spec in enumerate(pool):
            mid = str(m_spec.get('id') or '')
            q_prof = evaluate_quota_profile(eng, mid, quotas_snapshot, now=now)
            efforts = m_spec.get('efforts') or ['low', 'medium', 'high']
            pinned_eff = None if auto_effort else requested_effort
            effective_tier, chosen_effort = _adjust_tier_and_effort(
                task_info, q_prof['budget_state'], efforts, pinned_effort=pinned_eff
            )
            model_tier = _classify_model_tier(eng, m_spec)

            mid_l = mid.lower()
            score = 0.0
            if not q_prof['engine_ready']:
                score -= 500.0
            if not q_prof['available']:
                score -= 250.0

            score += _tier_fit_score(model_tier, effective_tier)
            score += q_prof['quota_score']

            # Conserve Codex quota during cross-engine auto routing unless Codex 7-day quota is expiring soon
            if auto_engine and len(candidate_engines) > 1 and eng == 'codex' and q_prof['budget_state'] != 'expiring_abundant':
                score -= 14.0

            # Task-matched family preferences:
            # - Simple & standard tasks prefer fast 5-hour Hydrogen pool to preserve 7-day Argon/Codex
            # - Simple tasks strongly avoid wasting flagship models
            # - Complex tasks prefer Argon (unless another 7-day flagship pool is expiring soon)
            if task_info['complexity'] == 'simple':
                if 'hydrogen' in mid_l:
                    score += 10.0
                elif model_tier == 'flagship':
                    score -= 15.0
            elif task_info['complexity'] == 'standard':
                if 'hydrogen' in mid_l and q_prof['budget_state'] != 'expiring_abundant':
                    score += 8.0
            elif task_info['complexity'] == 'complex':
                if 'argon' in mid_l and q_prof['budget_state'] != 'scarce':
                    score += 5.0

            # In catalogs with effort-suffixed models (e.g. gemini-4-argon-high/medium/low), align variant to chosen effort
            if m_spec.get('defaultEffort') == chosen_effort:
                score += 8.0
            elif chosen_effort == 'xhigh' and m_spec.get('defaultEffort') == 'high':
                score += 6.0

            if preferred_engine and eng == preferred_engine and (not auto_engine or eng != 'codex' or q_prof['budget_state'] == 'expiring_abundant'):
                score += 2.0
            if m_spec.get('isDefault'):
                score += 1.5
            # Tiny deterministic preference for earlier catalog items on complete tie
            score -= idx * 0.01

            chosen_fast = bool(requested_fast and m_spec.get('supportsFast'))
            if auto_effort and q_prof['budget_state'] == 'scarce':
                chosen_fast = False

            scored_choices.append((score, {
                'engine': eng,
                'model': mid,
                'model_name': m_spec.get('name') or mid,
                'effort': chosen_effort,
                'effort_label': _EFFORT_ZH.get(chosen_effort, chosen_effort),
                'fast': chosen_fast,
                'model_tier': model_tier,
                'effective_tier': effective_tier,
                'task_complexity': task_info['complexity'],
                'task_label': task_info['reason_label'],
                'budget_state': q_prof['budget_state'],
                'quota_summary': q_prof['quota_summary'],
                'weekly_remaining': q_prof['weekly_remaining'],
                'weekly_countdown_secs': q_prof['weekly_countdown_secs'],
                'rolling_remaining': q_prof['rolling_remaining'],
                'weekly_surplus': q_prof['weekly_surplus'],
                'score': round(score, 2),
            }))

    if not scored_choices:
        fallback_eng = candidate_engines[0] if candidate_engines else 'codex'
        fb_model, fb_effort = engines.resolve_model(
            fallback_eng,
            None if auto_model else requested_model,
            None if auto_effort else requested_effort,
            allow_auto=False,
        )
        fb_effort = fb_effort or 'medium'
        return {
            'engine': fallback_eng,
            'model': fb_model,
            'model_name': fb_model or fallback_eng,
            'effort': fb_effort,
            'effort_label': _EFFORT_ZH.get(fb_effort, fb_effort),
            'fast': False,
            'auto_routed': bool(auto_engine or auto_model or auto_effort),
            'task_complexity': task_info['complexity'],
            'task_label': task_info['reason_label'],
            'budget_state': 'unknown',
            'quota_summary': '使用默认配置',
            'weekly_remaining': None,
            'weekly_countdown_secs': 0.0,
            'rolling_remaining': None,
            'reason': f"{task_info['reason_label']} → 选用 {fb_model or fallback_eng} · {_EFFORT_ZH.get(fb_effort, fb_effort)}",
            'score': 0.0,
        }

    scored_choices.sort(key=lambda pair: pair[0], reverse=True)
    best = scored_choices[0][1]
    eng_label = {'codex': 'Codex', 'antigravity': 'Antigravity'}.get(best['engine'], best['engine'])

    if best['budget_state'] == 'expiring_abundant':
        action_hint = f"{best['quota_summary']}，即将刷新优先消费"
    elif best['budget_state'] == 'scarce':
        action_hint = f"{best['quota_summary']}，动态节流保护余量"
    else:
        action_hint = best['quota_summary']

    best['auto_routed'] = bool(auto_engine or auto_model or auto_effort)
    best['reason'] = (
        f"{best['task_label']} · {action_hint} → "
        f"{eng_label} · {best['model_name']} · {best['effort_label']}"
    )
    return best
