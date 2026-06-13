# tasks.py
import aiohttp
import asyncio
from typing import Optional
import os
import math

from db import *
from torn_api import fetch_all_company, fetch_user_bundle

INACTIVITY_CHECK_SECONDS = int(os.getenv("INACTIVITY_CHECK_SECONDS", "21600"))  # 6h
ADDICTION_CHECK_SECONDS  = int(os.getenv("ADDICTION_CHECK_SECONDS", "86400"))   # 24h
ADDICTION_COOLDOWN_DAYS = 1  # default cooldown
# Threshold levels in hours → increasing severity


def parse_hours_from_relative(rel: str | None) -> Optional[float]:
    if not rel:
        return None
    s = rel.strip().lower()
    if s == "online":
        return 0.0
    parts = s.split()
    if len(parts) < 2:
        return None
    try:
        val = float(parts[0])
    except Exception:
        return None
    unit = parts[1]
    if "minute" in unit:
        return val / 60.0
    if "hour" in unit:
        return val
    if "day" in unit:
        return val * 24.0
    return None

async def process_company_once(company_id: int) -> dict:
    """
    Fetch → persist → return a summary.
    """
    company = await get_company(company_id)
    if not company:
        raise RuntimeError(f"Company {company_id} not found in DB.")

    async with aiohttp.ClientSession() as session:
        bundle = await fetch_all_company(session, company_id, company["api_key"])

    # PROFILE
    company_obj = (bundle.get("profile") or {}).get("company") or {}
    if company_obj:
        await set_company_name(company_id, company_obj.get("name"))
        await update_company_profile(company_id, company_obj)

    # DETAILED
    detailed_obj = (bundle.get("detailed") or {}).get("company_detailed") or {}
    if detailed_obj:
        await update_company_detailed(company_id, detailed_obj)

    # STOCK
    stock_obj = (bundle.get("stock") or {}).get("company_stock") or {}
    await update_company_stock(company_id, stock_obj)

    # EMPLOYEES
    employees_obj = (bundle.get("employees") or {}).get("company_employees") or {}
    if employees_obj:
        await upsert_employees(company_id, employees_obj)

    # NEWS
    news_obj = (bundle.get("news") or {})
    if news_obj:
        await upsert_news(company_id, news_obj)

    trains = detailed_obj.get("trains_available")
    funds = detailed_obj.get("company_funds")
    hired = company_obj.get("employees_hired")
    cap = company_obj.get("employees_capacity")
        # After current tables have been updated:
    await replace_company_stock_items(company_id, stock_obj)          # current stock rows
    await insert_company_stock_snapshot(company_id, stock_obj)        # stock history
    await insert_company_metrics_snapshot(company_id, company_obj, detailed_obj)  # metrics history
    if employees_obj:
        await insert_employee_effectiveness_snapshots(company_id, employees_obj)  # employee EE + LA history

    return {
        "name": company_obj.get("name"),
        "trains": trains,
        "funds": funds,
        "hired": hired,
        "capacity": cap,
        "new_news_count": len(news_obj),
        "employees_count": len(employees_obj),
    }


async def collect_inactivity_buckets(company_id: int):
    """
    Returns dict: level -> list of (employee_id, name, relative, hours, label)
    Levels: 1=ping, 2=warning, 3=2nd last warning
    """
    th = await get_thresholds(company_id)
    p, w, l = th["inactivity_ping_hours"], th["inactivity_warn_hours"], th["inactivity_lastwarn_hours"]
    rows = await get_employees_by_company(company_id)
    buckets = {1: [], 2: [], 3: []}
    for e in rows:
        hrs = parse_hours_from_relative(e.get("last_action_relative"))
        if hrs is None:
            continue
        level_hit = 0
        label = ""
        if hrs >= p: level_hit, label = 1, "Ping"
        if hrs >= w: level_hit, label = 2, "Warning"
        if hrs >= l: level_hit, label = 3, "2nd Last Warning"
        if level_hit:
            buckets[level_hit].append((e["employee_id"], e["name"], e.get("last_action_relative"), hrs, label))
    return buckets
# Reset inactivity level to 0 for anyone now under ping threshold or Online
async def reset_inactivity_for_active(company_id: int):
    th = await get_thresholds(company_id)
    ping_h = th["inactivity_ping_hours"]
    rows = await get_employees_by_company(company_id)
    for e in rows:
        hrs = parse_hours_from_relative(e.get("last_action_relative"))
        if hrs is None or hrs < ping_h:
            state = await get_alert_state(company_id, e["employee_id"])
            if state.get("inactivity_level", 0) != 0:
                await set_inactivity_level(company_id, e["employee_id"], 0)


def _format_inactivity_lines(items: list[tuple]) -> list[str]:
    lines = []
    for _, name, rel, hrs, _ in items:
        hrs_int = math.floor(hrs)
        lines.append(f"• **{name}** — {rel} (~{hrs_int}h)")
    return lines

async def run_inactivity_alerts(bot, company_id: int):
    company = await get_company(company_id)
    if not company:
        return
    channel_id = company.get("alert_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    buckets = await collect_inactivity_buckets(company_id)
    # Send only if the employee hasn't already received this level (or higher)
    for level in [1, 2, 3]:
        to_alert = []
        for emp_id, name, rel, hrs, label in buckets[level]:
            state = await get_alert_state(company_id, emp_id)
            if state.get("inactivity_level", 0) < level:
                to_alert.append((emp_id, name, rel, hrs, label))
        if not to_alert:
            continue
        title = {1: "Inactivity Alert — 18h (Ping)",
                 2: "Inactivity Warning — 24h",
                 3: "Inactivity 2nd Last Warning — 48h"}[level]
        pretty = []
        for emp_id, name, rel, hrs, _ in to_alert:
            link = await get_member_link_by_torn_id(emp_id)
            mention = f"<@{link['discord_id']}>" if link else f"**{name}**"
            pretty.append(f"• {mention} — {rel}")
        await channel.send(f"**{title}**\n" + "\n".join(pretty))


async def infer_addiction_value(user_payload: dict) -> Optional[float]:
    """
    Try a few likely spots. If not available, return None.
    """
    if not isinstance(user_payload, dict):
        return None
    # Common places:
    # - user.personalstats.drugaddiction (some docs use lowercase)
    # - user.personalstats.drugAddiction (camelCase)
    # - user.addiction / user.profile.addiction (if ever exposed)
    ps = user_payload.get("personalstats") or user_payload.get("personalStats") or {}
    for k in ("drugaddiction", "drugAddiction", "addiction"):
        if k in ps:
            try:
                return float(ps[k])
            except Exception:
                pass
    # Some payloads might include a top-level or profile field
    for k in ("addiction",):
        if k in user_payload:
            try:
                return float(user_payload[k])
            except Exception:
                pass
    prof = user_payload.get("profile") or {}
    if "addiction" in prof:
        try:
            return float(prof["addiction"])
        except Exception:
            pass
    return None

async def run_addiction_check(bot, company_id: int):
    """
    Daily addiction scan based on cached employees.effectiveness.addiction (abs) → employees.addiction.
    - Alerts when addiction > 0 and not yet flagged.
    - Resets flag when addiction returns to 0.
    """
    company = await get_company(company_id)
    if not company:
        return

    # Respect per-company toggle
    th = await get_thresholds(company_id)
    if not th.get("addiction_alert", 1):
        return
    min_thr = int(th.get("addiction_threshold", 1))

    channel_id = company.get("alert_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    # Pull current cache
    rows = await get_employees_by_company(company_id)
    if not rows:
        return

    name_of = {int(r["employee_id"]): (r.get("name") or str(r["employee_id"])) for r in rows}
    addiction_of = {int(r["employee_id"]): float(r.get("addiction") or 0.0) for r in rows}
    emp_ids = list(addiction_of.keys())
    
    cooldown_secs = ADDICTION_COOLDOWN_DAYS * 86400
    now = int(time.time())

    to_alert_ids, to_reset_ids = [], []
    for uid, val in addiction_of.items():
        val_int = int(round(val or 0))
        st = await get_alert_state(company_id, uid)

        if val_int >= min_thr:
            should_alert = False
            if not st.get("addiction_flag"):
                should_alert = True
            else:
                last_val = st.get("addiction_last_value")
                last_ts  = st.get("addiction_last_ts")
                # Re-alert if increased by ≥1, or cooldown elapsed
                if last_val is not None and val_int > int(last_val):
                    should_alert = True
                elif last_ts is not None and (now - int(last_ts)) >= cooldown_secs:
                    should_alert = True

            if should_alert:
                to_alert_ids.append(uid)
        else:
            # below threshold → clear flag so future rises will re-trigger
            if st.get("addiction_flag"):
                to_reset_ids.append(uid)
    if to_alert_ids:
        pretty = []
        for uid in to_alert_ids:
            name = name_of.get(uid) or str(uid)
            val = addiction_of.get(uid) or 0.0
            link = await get_member_link_by_torn_id(uid)
            mention = f"<@{link['discord_id']}>" if link else f"**{name}**"
            val_int = int(round(val or 0))
            pretty.append(f"• {mention} — Addiction: {val_int}")
            # Update state
            await set_addiction_flag(company_id, uid, True)
            #await set_addiction_last(company_id, uid, val_int, now)
        title = f"Addiction Alert — Threshold {min_thr}"
        await channel.send(f"**{title}**\n" + "\n".join(pretty))
    for uid in to_reset_ids:
        await set_addiction_flag(company_id, uid, False, last_value=None, last_ts=None)


    
    




