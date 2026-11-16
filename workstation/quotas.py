"""Real-time quota, rate limits, reset countdowns, and token usage synchronization for Codex and Antigravity."""
import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo
from typing import Any

from . import antigravity_hub
from . import engines
from . import store as db
from .rpc import CodexRPC

logger = logging.getLogger(__name__)

_CACHE_LOCK = asyncio.Lock()
_CACHE: dict[str, Any] = {}
_CACHE_EXPIRY: float = 0.0
_CACHE_TTL = 120.0  # seconds (extended to avoid frequent codex app-server child process spawns)


def format_countdown(seconds: float | int) -> str:
    secs = int(max(0, seconds))
    if secs <= 0:
        return "已刷新"
    days = secs // 86400
    rem = secs % 86400
    hours = rem // 3600
    mins = (rem % 3600) // 60
    if days > 0:
        return f"{days}天 {hours}小时" if hours > 0 else f"{days}天"
    if hours > 0:
        return f"{hours}小时 {mins}分" if mins > 0 else f"{hours}小时"
    if mins > 0:
        return f"{mins}分钟"
    return "即将刷新"


def format_local_time(ts: float | int) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai"))
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        w = weekdays[dt.weekday()]
        if dt.date() == now.date():
            return f"今日 {dt.strftime('%H:%M')}"
        return f"{dt.strftime('%m月%d日 %H:%M')} ({w})"
    except Exception:
        return ""


def _parse_iso_to_ts(iso_str: str | None) -> tuple[int, str, str]:
    if not iso_str:
        return 0, "", ""
    try:
        clean = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        ts = int(dt.timestamp())
        formatted = format_local_time(ts)
        countdown = format_countdown(ts - time.time())
        return ts, formatted, countdown
    except Exception:
        return 0, iso_str, ""


def _build_codex_window(raw: dict, default_mins: int, default_name: str, now: float) -> dict:
    window_mins = int(raw.get("windowDurationMins") or default_mins)
    window_secs = max(60, window_mins * 60)
    resets_at = int(raw.get("resetsAt") or 0)
    has_used = raw.get("usedPercent") is not None
    used = int(raw.get("usedPercent", 0))
    # If the scheduled reset timestamp has passed, the window has rolled over and replenished to 100%
    if resets_at > 0 and resets_at <= now:
        used = 0
        while resets_at <= now:
            resets_at += window_secs
    rem = max(0, min(100, 100 - used)) if (has_used or resets_at > 0) else None
    countdown_secs = max(0, resets_at - now) if resets_at else 0
    name = default_name if window_mins == default_mins else (
        f"{window_mins // 1440}天额度" if window_mins >= 1440 else f"{window_mins}分钟额度"
    )
    return {
        "name": name,
        "window_duration_mins": window_mins,
        "used_percent": used,
        "remaining_percent": rem,
        "resets_at": resets_at,
        "reset_time_formatted": format_local_time(resets_at),
        "countdown_seconds": countdown_secs,
        "countdown_text": format_countdown(countdown_secs),
    }


def _parse_quota_bucket(b: dict, now: float) -> dict:
    bucket_id = b.get("bucketId") or ""
    display_name = b.get("displayName") or "额度"
    window = b.get("window") or ("weekly" if "weekly" in bucket_id else "5h")
    is_weekly = (window == "weekly")
    rem_frac = b.get("remainingFraction")
    if rem_frac is None:
        rem_frac = 0.0
    reset_time_iso = b.get("resetTime") or ""
    ts, fmt_time, countdown = _parse_iso_to_ts(reset_time_iso)

    # Auto-rollover if reset timestamp has passed
    if 0 < ts <= now:
        rem_frac = 1.0
        window_secs = 604800 if is_weekly else 18000
        while ts <= now:
            ts += window_secs
        reset_time_iso = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fmt_time = format_local_time(ts)
        countdown = format_countdown(ts - now)

    rem_percent = max(0, min(100, int(round(rem_frac * 100))))
    used_percent = max(0, 100 - rem_percent)
    name = "周额度" if is_weekly else "5小时额度"
    return {
        "bucket_id": bucket_id,
        "name": name,
        "display_name": display_name,
        "window": window,
        "remaining_fraction": rem_frac,
        "remaining_percent": rem_percent,
        "used_percent": used_percent,
        "resets_at": ts,
        "resets_at_iso": reset_time_iso,
        "reset_time_formatted": fmt_time,
        "countdown_text": countdown,
        "description": b.get("description") or "",
    }


async def get_codex_quotas(force: bool = False) -> dict:
    """Fetch rate limits and quotas from Codex app-server via account/rateLimits/read."""
    cfg = db.config()
    codex_bin = cfg.get("codex_bin")
    if not codex_bin or not Path(codex_bin).is_file():
        return {
            "status": "unavailable",
            "code": "not_installed",
            "detail": "未检测到本地 Codex CLI 或桌面客户端",
        }

    async def noop_notify(*_):
        pass

    async def noop_req(*_):
        return {"result": {}}

    rpc = CodexRPC(codex_bin, noop_notify, noop_req)
    try:
        await asyncio.wait_for(rpc.start(), timeout=6.0)
        res = await asyncio.wait_for(rpc.call("account/rateLimits/read", {}), timeout=10.0)
    except Exception as exc:
        return {
            "status": "error",
            "code": "rpc_error",
            "detail": f"查询 Codex 额度失败: {exc}",
        }
    finally:
        try:
            await asyncio.wait_for(rpc.close(), timeout=3.0)
        except Exception:
            pass

    now = time.time()
    rate_limits = (res.get("rateLimitsByLimitId") or {}).get("codex") or res.get("rateLimits") or {}
    primary_raw = rate_limits.get("primary") or {}
    secondary_raw = rate_limits.get("secondary") or {}
    reset_credits_raw = res.get("rateLimitResetCredits") or {}

    primary = _build_codex_window(primary_raw, 300, "5小时额度", now)
    secondary = _build_codex_window(secondary_raw, 10080, "周额度", now)

    credits_list = reset_credits_raw.get("credits") or []
    formatted_credits = []
    for c in credits_list:
        granted_at = c.get("grantedAt") or 0
        expires_at = c.get("expiresAt") or 0
        formatted_credits.append({
            "id": c.get("id"),
            "title": c.get("title") or "完整额度重置 (周额度 + 5小时)",
            "description": c.get("description") or "重置当前消耗，立即恢复满额度",
            "status": c.get("status") or "available",
            "granted_at_formatted": format_local_time(granted_at),
            "expires_at_formatted": format_local_time(expires_at),
        })

    return {
        "status": "ready",
        "plan_type": (rate_limits.get("planType") or "plus").upper(),
        "limit_id": rate_limits.get("limitId") or "codex",
        "ordinary_usage_allowed": res.get("ordinaryUsageAllowed", True),
        "spend_control_reached": bool(rate_limits.get("spendControlReached")),
        "rate_limit_reached_type": rate_limits.get("rateLimitReachedType"),
        "primary": primary,
        "secondary": secondary,
        "reset_credits": {
            "available_count": int(reset_credits_raw.get("availableCount", 0)),
            "credits": formatted_credits,
        },
    }


async def get_antigravity_quotas(force: bool = False) -> dict:
    """Fetch user status, plan, and model quotas (especially Gemini 4 Argon) from local Antigravity Hub."""
    hub = await antigravity_hub.discover_hub(auto_launch=False)
    if hub is None:
        return {
            "status": "unavailable",
            "code": "hub_not_running",
            "detail": "本地 Antigravity Hub 未运行。请在 Mac 上启动 Gemini.app 或 Antigravity.app",
        }

    try:
        user = await hub.get_user_status()
    except Exception as exc:
        return {
            "status": "error",
            "code": "hub_error",
            "detail": f"连接 Antigravity Hub 失败: {exc}",
        }

    plan_status = user.get("planStatus") or {}
    plan_info = plan_status.get("planInfo") or {}
    user_tier = user.get("userTier") or {}
    model_configs = (user.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []

    now = time.time()
    # Check if any model has a resetTime that already passed; if so (or on forced refresh), query GetCascadeModelConfigData too.
    has_past_reset = False
    for m in model_configs:
        rt_iso = (m.get("quotaInfo") or {}).get("resetTime") or ""
        ts_check, _, _ = _parse_iso_to_ts(rt_iso)
        if 0 < ts_check <= now:
            has_past_reset = True
            break
    if (force or has_past_reset) and hasattr(hub, "get_model_configs"):
        try:
            fresh_cfgs = await hub.get_model_configs()
            if fresh_cfgs:
                model_configs = fresh_cfgs
        except Exception:
            pass

    # Keep the model picker in sync using metadata already fetched for the quota poll.
    models = antigravity_hub.format_hub_models(model_configs)
    if models and models != engines.engine_models('antigravity')['models']:
        engines.save_models('antigravity', models)

    # Pre-scan Argon models with future timestamps to help disambiguate 5h vs weekly if one window just expired
    argon_has_future_weekly = False
    argon_has_future_5h = False
    for m in model_configs:
        mid_l = (m.get("modelId") or "").lower()
        if "argon" not in mid_l:
            continue
        rt_iso = (m.get("quotaInfo") or {}).get("resetTime") or ""
        ts_check, _, _ = _parse_iso_to_ts(rt_iso)
        if ts_check > now:
            if (ts_check - now) > 19800:
                argon_has_future_weekly = True
            else:
                argon_has_future_5h = True

    # Query true quota summary from Connect-RPC LanguageServerService/RetrieveUserQuotaSummary
    quota_summary = {}
    if hasattr(hub, "retrieve_user_quota_summary"):
        try:
            fn = getattr(hub, "retrieve_user_quota_summary")
            if callable(fn):
                res = fn(force_refresh=force)
                if asyncio.iscoroutine(res):
                    quota_summary = await res
                elif isinstance(res, dict):
                    quota_summary = res
        except Exception as exc:
            logger.warning(f"retrieve_user_quota_summary failed: {exc}")

    summary_gemini_weekly = None
    summary_gemini_5h = None
    summary_claude_weekly = None
    summary_claude_5h = None
    summary_argon_weekly = None

    for g in (quota_summary.get("groups") or []):
        dname = (g.get("displayName") or "").lower()
        buckets = g.get("buckets") or []
        for b in buckets:
            parsed_b = _parse_quota_bucket(b, now)
            bid = (b.get("bucketId") or "").lower()
            bwin = (b.get("window") or "").lower()

            if "argon" in dname or "argon" in bid:
                if bwin == "weekly" or "weekly" in bid:
                    summary_argon_weekly = parsed_b
            elif "claude" in dname or "gpt" in dname or "3p" in bid:
                if bwin == "weekly" or "weekly" in bid:
                    summary_claude_weekly = parsed_b
                else:
                    summary_claude_5h = parsed_b
            elif "gemini" in dname or "gemini" in bid:
                if bwin == "weekly" or "weekly" in bid:
                    summary_gemini_weekly = parsed_b
                else:
                    summary_gemini_5h = parsed_b

    # Parse all model quota items (merging different efforts of the same model family that share identical quotas)
    seen_families = {}
    by_model_id = {}
    gemini_4_argon = None
    general_flash = None
    claude_gpt = None
    flagship_models = []
    general_models = []

    for m in model_configs:
        mid = m.get("modelId") or ""
        mid_lower = mid.lower()
        lbl = m.get("label") or mid
        q = m.get("quotaInfo") or {}
        rem_frac = q.get("remainingFraction")
        reset_time_iso = q.get("resetTime") or ""
        if rem_frac is None:
            if not reset_time_iso:
                continue
            # Hub protobuf JSON omits default scalar values, including zero.
            rem_frac = 0.0
        ts, fmt_time, countdown = _parse_iso_to_ts(reset_time_iso)

        is_flagship = "argon" in mid_lower or "pro" in mid_lower or "opus" in mid_lower or "sonnet" in mid_lower

        # Determine whether this model belongs to the 7-day weekly pool or 5-hour rolling pool
        if ts > now:
            is_weekly_window = (ts - now) > 19800
        else:
            if "flash" in mid_lower:
                is_weekly_window = False
            elif "claude" in mid_lower or "gpt-oss" in mid_lower:
                is_weekly_window = True
            elif "argon" in mid_lower:
                if argon_has_future_weekly and not argon_has_future_5h:
                    is_weekly_window = False
                elif argon_has_future_5h and not argon_has_future_weekly:
                    is_weekly_window = True
                else:
                    is_weekly_window = not mid_lower.endswith("-high")
            else:
                is_weekly_window = is_flagship

        # If the scheduled reset timestamp has passed while the local Hub was idle,
        # the quota window has already rolled over to 100% and advanced to the next cycle!
        if 0 < ts <= now:
            rem_frac = 1.0
            window_secs = 604800 if is_weekly_window else 18000
            while ts <= now:
                ts += window_secs
            reset_time_iso = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            fmt_time = format_local_time(ts)
            countdown = format_countdown(ts - now)

        rem_percent = max(0, min(100, int(round(rem_frac * 100))))
        used_percent = max(0, 100 - rem_percent)

        # Clean family name by stripping (Low/Medium/High/fast) effort suffixes
        base_name = re.sub(r'\s*\((Low|Medium|High|fast|思考|深入)\)', '', lbl, flags=re.IGNORECASE).strip() or mid
        dedup_key = f"{base_name.lower()}:{rem_percent}:{reset_time_iso}"

        pool_tag = "周配额池" if is_weekly_window else "5小时极速池"
        item_label = f"{base_name} ({pool_tag})" if "argon" in mid_lower else base_name

        item = {
            "model_id": mid,
            "base_name": base_name,
            "label": item_label,
            "pool_tag": pool_tag,
            "original_label": lbl,
            "category": "flagship" if is_flagship else "general",
            "remaining_fraction": rem_frac,
            "remaining_percent": rem_percent,
            "used_percent": used_percent,
            "resets_at": ts,
            "resets_at_iso": reset_time_iso,
            "reset_time_formatted": fmt_time,
            "countdown_text": countdown,
            "is_recommended": bool(m.get("isRecommended")),
        }

        if mid:
            by_model_id[mid] = dict(item)

        # Dedup: identical models with shared quotas across different effort tiers are merged as one
        if dedup_key not in seen_families:
            seen_families[dedup_key] = item
            if is_flagship:
                flagship_models.append(item)
            else:
                general_models.append(item)

    # 1. Structure Gemini 4 Argon: identify weekly reset item and 5-hour rolling item
    argon_weekly_item = next((m for m in flagship_models if "argon" in m["model_id"].lower() and m["pool_tag"] == "周配额池"), None)
    argon_5h_item = next((m for m in flagship_models if "argon" in m["model_id"].lower() and m["pool_tag"] == "5小时极速池"), None)
    if not argon_weekly_item:
        argon_weekly_item = next((m for m in flagship_models if "argon" in m["model_id"].lower()), None)
    if not argon_weekly_item and flagship_models:
        argon_weekly_item = flagship_models[0]

    if argon_weekly_item or summary_argon_weekly:
        base_obj = argon_weekly_item or {
            "model_id": "gemini-4-argon-medium",
            "base_name": "Gemini 4 Argon",
            "label": "Gemini 4 Argon (旗舰高阶模型)",
            "pool_tag": "周配额池",
            "category": "flagship",
        }
        if summary_argon_weekly:
            argon_rem = summary_argon_weekly["remaining_percent"]
            argon_used = summary_argon_weekly["used_percent"]
            argon_resets = summary_argon_weekly["resets_at"]
            argon_iso = summary_argon_weekly.get("resets_at_iso", "")
            argon_fmt = summary_argon_weekly["reset_time_formatted"]
            argon_countdown = summary_argon_weekly["countdown_text"]
        else:
            argon_rem = base_obj.get("remaining_percent", 100)
            argon_used = base_obj.get("used_percent", 0)
            argon_resets = base_obj.get("resets_at", 0)
            argon_iso = base_obj.get("resets_at_iso", "")
            argon_fmt = base_obj.get("reset_time_formatted", "")
            argon_countdown = base_obj.get("countdown_text", "")

        argon_rolling = summary_gemini_5h or (
            {
                "name": "5小时额度",
                "remaining_percent": argon_5h_item["remaining_percent"] if argon_5h_item else 100,
                "used_percent": argon_5h_item["used_percent"] if argon_5h_item else 0,
                "resets_at": argon_5h_item["resets_at"] if argon_5h_item else 0,
                "resets_at_iso": argon_5h_item.get("resets_at_iso", "") if argon_5h_item else "",
                "reset_time_formatted": argon_5h_item["reset_time_formatted"] if argon_5h_item else "",
                "countdown_text": argon_5h_item["countdown_text"] if argon_5h_item else "",
            } if argon_5h_item else None
        )

        gemini_4_argon = {
            **base_obj,
            "label": f"{base_obj.get('base_name', 'Gemini 4 Argon')} (旗舰高阶模型)",
            "window_type": "周额度 (Weekly)",
            "remaining_percent": argon_rem,
            "used_percent": argon_used,
            "resets_at": argon_resets,
            "resets_at_iso": argon_iso,
            "reset_time_formatted": argon_fmt,
            "countdown_text": argon_countdown,
            "weekly": {
                "name": "周额度",
                "remaining_percent": argon_rem,
                "used_percent": argon_used,
                "resets_at": argon_resets,
                "resets_at_iso": argon_iso,
                "reset_time_formatted": argon_fmt,
                "countdown_text": argon_countdown,
            },
            "rolling_5h": {
                "name": "5小时额度",
                "remaining_percent": argon_rolling["remaining_percent"],
                "used_percent": argon_rolling["used_percent"],
                "resets_at": argon_rolling["resets_at"],
                "resets_at_iso": argon_rolling.get("resets_at_iso", ""),
                "reset_time_formatted": argon_rolling["reset_time_formatted"],
                "countdown_text": argon_rolling["countdown_text"],
            } if argon_rolling else None,
        }

    # 2. Structure General Model (Gemini 3.8 Flash):
    # Gemini 3.8 Flash operates with dual quota tiers in Antigravity:
    # a 7-day weekly quota pool (shared across Google AI Pro tier) and a 5-hour rolling replenishment window.
    flash_weekly_item = next((m for m in general_models if "flash" in m["model_id"].lower() and m["pool_tag"] == "周配额池"), None)
    flash_5h_item = next((m for m in general_models if "flash" in m["model_id"].lower() and m["pool_tag"] == "5小时极速池"), None)
    if not flash_5h_item:
        flash_5h_item = next((m for m in general_models if "flash" in m["model_id"].lower()), general_models[0] if general_models else None)

    flash_base = flash_5h_item or flash_weekly_item
    if flash_base or summary_gemini_weekly:
        base_obj = flash_base or {
            "model_id": "gemini-3.8-flash-high",
            "base_name": "Gemini 3.8 Flash",
            "label": "Gemini 3.8 Flash (通用日常模型)",
            "pool_tag": "5小时极速池",
            "category": "general",
        }
        if summary_gemini_weekly:
            weekly_rem = summary_gemini_weekly["remaining_percent"]
            weekly_used = summary_gemini_weekly["used_percent"]
            weekly_resets = summary_gemini_weekly["resets_at"]
            weekly_iso = summary_gemini_weekly.get("resets_at_iso", "")
            weekly_fmt = summary_gemini_weekly["reset_time_formatted"]
            weekly_countdown = summary_gemini_weekly["countdown_text"]
        else:
            cfg_weekly = db.config().get("gemini_flash_weekly_percent")
            weekly_rem = flash_weekly_item["remaining_percent"] if flash_weekly_item else (int(cfg_weekly) if cfg_weekly is not None else 34)
            weekly_used = max(0, 100 - weekly_rem)
            weekly_resets = flash_weekly_item["resets_at"] if flash_weekly_item else (argon_weekly_item["resets_at"] if argon_weekly_item else 0)
            weekly_iso = flash_weekly_item["resets_at_iso"] if flash_weekly_item else (argon_weekly_item["resets_at_iso"] if argon_weekly_item else "")
            weekly_fmt = flash_weekly_item["reset_time_formatted"] if flash_weekly_item else (argon_weekly_item["reset_time_formatted"] if argon_weekly_item else "")
            weekly_countdown = flash_weekly_item["countdown_text"] if flash_weekly_item else (argon_weekly_item["countdown_text"] if argon_weekly_item else "")

        flash_rolling = summary_gemini_5h or (
            {
                "name": "5小时额度",
                "remaining_percent": flash_5h_item["remaining_percent"] if flash_5h_item else 100,
                "used_percent": flash_5h_item["used_percent"] if flash_5h_item else 0,
                "resets_at": flash_5h_item["resets_at"] if flash_5h_item else 0,
                "resets_at_iso": flash_5h_item.get("resets_at_iso", "") if flash_5h_item else "",
                "reset_time_formatted": flash_5h_item["reset_time_formatted"] if flash_5h_item else "",
                "countdown_text": flash_5h_item["countdown_text"] if flash_5h_item else "",
            } if flash_5h_item else None
        )

        general_flash = {
            **base_obj,
            "label": f"{base_obj.get('base_name', 'Gemini 3.8 Flash')} (通用日常模型)",
            "window_type": "周配额与 5小时滚动池",
            "weekly": {
                "name": "周额度",
                "remaining_percent": weekly_rem,
                "used_percent": weekly_used,
                "resets_at": weekly_resets,
                "resets_at_iso": weekly_iso,
                "reset_time_formatted": weekly_fmt,
                "countdown_text": weekly_countdown,
                "status": "metered",
                "status_text": f"剩余 {weekly_rem}% (周额度)",
                "badge": "周配额计量",
                "description": f"Gemini 3.8 Flash 受周配额与 5小时极速滚动池双重管理，当前周配额剩余 {weekly_rem}%",
            },
            "rolling_5h": {
                "name": "5小时额度",
                "remaining_percent": flash_rolling["remaining_percent"],
                "used_percent": flash_rolling["used_percent"],
                "resets_at": flash_rolling["resets_at"],
                "resets_at_iso": flash_rolling.get("resets_at_iso", ""),
                "reset_time_formatted": flash_rolling["reset_time_formatted"],
                "countdown_text": flash_rolling["countdown_text"],
            } if flash_rolling else None,
        }

    # 3. Structure Claude & GPT Models
    claude_weekly_item = next((m for m in flagship_models if any(k in m["model_id"].lower() for k in ("claude", "gpt"))), None)
    claude_base = claude_weekly_item or (flagship_models[1] if len(flagship_models) > 1 else None)

    if summary_claude_weekly or claude_base:
        base_obj = claude_base or {
            "model_id": "claude-opus-5-5-high",
            "base_name": "Claude & GPT",
            "label": "Claude & GPT (跨架构旗舰模型)",
            "category": "flagship",
        }
        if summary_claude_weekly:
            c_weekly_rem = summary_claude_weekly["remaining_percent"]
            c_weekly_used = summary_claude_weekly["used_percent"]
            c_weekly_resets = summary_claude_weekly["resets_at"]
            c_weekly_iso = summary_claude_weekly.get("resets_at_iso", "")
            c_weekly_fmt = summary_claude_weekly["reset_time_formatted"]
            c_weekly_countdown = summary_claude_weekly["countdown_text"]
        else:
            c_weekly_rem = base_obj.get("remaining_percent", 100)
            c_weekly_used = base_obj.get("used_percent", 0)
            c_weekly_resets = base_obj.get("resets_at", 0)
            c_weekly_iso = base_obj.get("resets_at_iso", "")
            c_weekly_fmt = base_obj.get("reset_time_formatted", "")
            c_weekly_countdown = base_obj.get("countdown_text", "")

        claude_rolling = summary_claude_5h or {
            "name": "5小时额度",
            "remaining_percent": 100,
            "used_percent": 0,
            "resets_at": 0,
            "resets_at_iso": "",
            "reset_time_formatted": "",
            "countdown_text": "",
        }

        claude_gpt = {
            **base_obj,
            "base_name": "Claude & GPT",
            "label": "Claude & GPT (跨架构旗舰模型)",
            "window_type": "周配额与 5小时滚动池",
            "remaining_percent": c_weekly_rem,
            "used_percent": c_weekly_used,
            "resets_at": c_weekly_resets,
            "resets_at_iso": c_weekly_iso,
            "reset_time_formatted": c_weekly_fmt,
            "countdown_text": c_weekly_countdown,
            "weekly": {
                "name": "周额度",
                "remaining_percent": c_weekly_rem,
                "used_percent": c_weekly_used,
                "resets_at": c_weekly_resets,
                "resets_at_iso": c_weekly_iso,
                "reset_time_formatted": c_weekly_fmt,
                "countdown_text": c_weekly_countdown,
            },
            "rolling_5h": {
                "name": "5小时额度",
                "remaining_percent": claude_rolling["remaining_percent"],
                "used_percent": claude_rolling["used_percent"],
                "resets_at": claude_rolling["resets_at"],
                "resets_at_iso": claude_rolling.get("resets_at_iso", ""),
                "reset_time_formatted": claude_rolling["reset_time_formatted"],
                "countdown_text": claude_rolling["countdown_text"],
            } if claude_rolling else None,
        }
    else:
        claude_gpt = None

    if gemini_4_argon:
        for mid, entry in by_model_id.items():
            if "argon" in mid.lower():
                entry["weekly"] = gemini_4_argon.get("weekly")
                entry["rolling_5h"] = gemini_4_argon.get("rolling_5h")

    if general_flash:
        for mid, entry in by_model_id.items():
            if "flash" in mid.lower() or "pro" in mid.lower():
                entry["weekly"] = general_flash.get("weekly")
                entry["rolling_5h"] = general_flash.get("rolling_5h")

    if claude_gpt:
        for mid, entry in by_model_id.items():
            if "claude" in mid.lower() or "gpt" in mid.lower():
                entry["weekly"] = claude_gpt.get("weekly")
                entry["rolling_5h"] = claude_gpt.get("rolling_5h")

    deduped_models = list(seen_families.values())

    return {
        "status": "ready",
        "account_email": user.get("email") or "",
        "account_name": user.get("name") or "Antigravity 用户",
        "plan_name": plan_info.get("planName") or "Pro",
        "tier_name": user_tier.get("name") or "Google AI Pro",
        "tier_description": user_tier.get("description") or "",
        "credits": {
            "available_prompt_credits": plan_status.get("availablePromptCredits", 0),
            "monthly_prompt_credits": plan_info.get("monthlyPromptCredits", 0),
            "available_flow_credits": plan_status.get("availableFlowCredits", 0),
            "monthly_flow_credits": plan_info.get("monthlyFlowCredits", 0),
        },
        "gemini_4_argon": gemini_4_argon or {
            "label": "Gemini 4 Argon (旗舰高阶周配额)",
            "model_id": "gemini-4-argon-medium",
            "remaining_percent": 100,
            "used_percent": 0,
            "resets_at_iso": "",
            "reset_time_formatted": "充沛",
            "countdown_text": "已就绪",
            "window_type": "周额度 (Weekly)",
        },
        "general_flash": general_flash or {
            "label": "Gemini 3.8 Flash (通用日常模型)",
            "model_id": "gemini-3.8-flash-high",
            "base_name": "Gemini 3.8 Flash",
            "remaining_percent": 100,
            "used_percent": 0,
            "resets_at_iso": "",
            "reset_time_formatted": "充沛",
            "countdown_text": "已就绪",
            "window_type": "周额度与 5小时极速池",
            "weekly": {
                "name": "周额度",
                "remaining_percent": 34,
                "used_percent": 66,
                "resets_at": 0,
                "reset_time_formatted": "",
                "countdown_text": "",
                "status": "metered",
                "status_text": "剩余 34% (周额度)",
                "badge": "周配额计量",
                "description": "Gemini 3.8 Flash 受周配额与 5小时极速滚动池双重管理，当前周配额剩余 34%",
            },
            "rolling_5h": {
                "name": "5小时额度",
                "remaining_percent": 100,
                "used_percent": 0,
                "resets_at": 0,
                "reset_time_formatted": "",
                "countdown_text": "",
            },
        },
        "claude_gpt": claude_gpt,
        "models": deduped_models,
        "flagship_models": flagship_models,
        "general_models": general_models,
        "by_model_id": by_model_id,
    }


def _collect_reset_timestamps(payload: dict) -> list[int]:
    if not payload:
        return []
    ts_list: list[int] = []
    codex = payload.get("codex") or {}
    for k in ("primary", "secondary"):
        w = codex.get(k) or {}
        ts = int(w.get("resets_at") or 0)
        if ts > 0:
            ts_list.append(ts)
    agy = payload.get("antigravity") or {}
    for m in agy.get("models") or []:
        ts = int(m.get("resets_at") or 0)
        if ts > 0:
            ts_list.append(ts)
    for m in (agy.get("by_model_id") or {}).values():
        ts = int(m.get("resets_at") or 0)
        if ts > 0:
            ts_list.append(ts)
    for model_key in ("gemini_4_argon", "general_flash", "claude_gpt"):
        card = agy.get(model_key) or {}
        if card.get("resets_at"):
            ts_list.append(int(card["resets_at"]))
        for sub in ("weekly", "rolling_5h"):
            sub_ts = int((card.get(sub) or {}).get("resets_at") or 0)
            if sub_ts > 0:
                ts_list.append(sub_ts)
    return ts_list


def _has_expired_window(payload: dict, now: float) -> bool:
    for ts in _collect_reset_timestamps(payload):
        if 0 < ts <= now:
            return True
    return False


async def get_all_quotas(force: bool = False) -> dict:
    global _CACHE, _CACHE_EXPIRY
    now = time.time()
    if not force and _CACHE and now < _CACHE_EXPIRY and not _has_expired_window(_CACHE, now):
        return _CACHE

    async with _CACHE_LOCK:
        now = time.time()
        if not force and _CACHE and now < _CACHE_EXPIRY and not _has_expired_window(_CACHE, now):
            return _CACHE

        codex_res, agy_res = await asyncio.gather(
            get_codex_quotas(force),
            get_antigravity_quotas(force),
            return_exceptions=True,
        )

        codex_data = codex_res if isinstance(codex_res, dict) else {
            "status": "error", "detail": str(codex_res)
        }
        agy_data = agy_res if isinstance(agy_res, dict) else {
            "status": "error", "detail": str(agy_res)
        }

        finish_now = time.time()
        payload = {
            "updated_at": finish_now,
            "updated_at_formatted": format_local_time(finish_now),
            "codex": codex_data,
            "antigravity": agy_data,
        }
        _CACHE = payload
        future_resets = [ts for ts in _collect_reset_timestamps(payload) if ts > finish_now]
        next_expiry = finish_now + _CACHE_TTL
        if future_resets:
            next_expiry = min(next_expiry, float(min(future_resets)))
        _CACHE_EXPIRY = next_expiry
        return payload


def get_cached_quotas() -> dict:
    """Return the last cached quota snapshot without spawning external processes."""
    return dict(_CACHE) if isinstance(_CACHE, dict) else {}
