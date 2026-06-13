# main.py
import os
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime
import re

from db import * #init_db, upsert_company, get_companies, get_company, get_employees_by_company
from tasks import * #company_loop, process_company_once, run_inactivity_alerts, run_addiction_check, INACTIVITY_CHECK_SECONDS, ADDICTION_CHECK_SECONDS

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "900"))
VERIFY_CHECK_SECONDS = int(os.getenv("VERIFY_CHECK_SECONDS", "300"))


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)



company_tasks: dict[int, asyncio.Task] = {}
inactivity_timers: dict[int, float] = {}
addiction_timers: dict[int, float] = {}


from zoneinfo import ZoneInfo
DETROIT = ZoneInfo("America/Detroit")
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "15"))  # 2pm local by default
MAX_STOCK_ITEMS = int(os.getenv("DAILY_REPORT_MAX_STOCK", "15"))
MAX_NEWS_ITEMS  = int(os.getenv("DAILY_REPORT_MAX_NEWS", "10"))


async def start_company_task(company_id: int):
    if company_id in company_tasks and not company_tasks[company_id].done():
        return
    # Core data refresh loop (news, employees, etc.)
    async def loop():
        await bot.wait_until_ready()
        last_inact = 0.0
        last_addic = 0.0
        while not bot.is_closed():
            try:
                await process_company_once(company_id)
                stock_alerts = await check_stock_rules_for_company(company_id)

                if stock_alerts:
                    company = await get_company(company_id)
                    channel_id = company.get("alert_channel_id") if company else None
                    channel = bot.get_channel(int(channel_id)) if channel_id else None

                    if channel:
                        await channel.send(
                            "**📦 Stock Alert**\n" + "\n".join(stock_alerts[:20])
                        )
                now = asyncio.get_event_loop().time()

                # Inactivity every 6h
                if now - last_inact >= INACTIVITY_CHECK_SECONDS:
                    await run_inactivity_alerts(bot, company_id)
                    last_inact = now

                # Addiction every 24h
                if now - last_addic >= ADDICTION_CHECK_SECONDS:
                    await run_addiction_check(bot, company_id)
                    last_addic = now

            except Exception as e:
                print(f"[company {company_id}] error: {e}")

            await asyncio.sleep(POLL_SECONDS)

    company_tasks[company_id] = asyncio.create_task(loop())

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    companies = await get_companies()
    
    for c in companies:
        await start_company_task(c["company_id"])
    asyncio.create_task(daily_reports_loop())

        


    # Start one verifier loop per guild
    for g in bot.guilds:
        asyncio.create_task(verifier_loop_for_guild(g))

def is_training_news(text: str) -> bool:
    text = (text or "").lower()

    training_phrases = [
        "trained",
        "train",
        "trains",
        "was trained",
        "received a train",
        "used a train",
    ]

    return any(p in text for p in training_phrases)


def clean_news_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"<a[^>]*>([^<]+)</a>", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def make_news_lines(rows: list[dict], limit: int = 10) -> list[str]:
    lines = []

    for n in rows[:limit]:
        ts = n.get("timestamp")
        if ts is None:
            continue

        text = clean_news_text(n.get("news_text") or n.get("news") or "")

        if not text:
            continue

        lines.append(f"• {text}\n<t:{int(ts)}:R>")

    if len(rows) > limit:
        lines.append(f"… and {len(rows) - limit} more")

    return lines

TRAINING_RE = re.compile(
    r'(?P<name>.+?) has been trained(?: by the director)?',
    re.IGNORECASE
)

def is_training_news(text: str) -> bool:
    return bool(TRAINING_RE.search(clean_news_text(text)))

def parse_training_name(text: str) -> str | None:
    text = clean_news_text(text)
    m = TRAINING_RE.search(text)
    if not m:
        return None
    return m.group("name").strip()

def clean_news_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"<a[^>]*>([^<]+)</a>", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()

@bot.event
async def on_guild_join(guild):
    asyncio.create_task(verifier_loop_for_guild(guild))

@bot.event
async def on_guild_available(guild):
    # optional: ensure loop exists if the bot reconnected
    asyncio.create_task(verifier_loop_for_guild(guild))

import re
from db import *

NAME_ID_BRACKET = re.compile(r"\[(\d{4,10})\]")  # matches [123456]

async def apply_company_role_if_mapped(member: discord.Member, company_id: int):
    await ensure_only_mapped_company_role(member, company_id, remove_others=True)
    """role_id = await get_company_role_id(company_id)
    if not role_id:
        return False
    role = member.guild.get_role(role_id)
    if not role:
        return False
    # add if not present
    if role not in member.roles:
        try:
            await member.add_roles(role, reason="Mapped company role")
        except discord.Forbidden:
            pass
    return True"""

async def set_nick_safe(member: discord.Member, nick: str):
    try:
        if member.guild.me.guild_permissions.manage_nicknames:
            await member.edit(nick=nick, reason="Auto-verify: Torn format")
    except discord.Forbidden:
        pass

async def try_link_by_torn_id(member: discord.Member, torn_id: int):
    emp = await get_employee_by_id(torn_id)
    if not emp:
        return False
    torn_name = emp.get("name") or f"User {torn_id}"
    cname = emp.get("name") or f"User {torn_id}"
    cid = emp["company_id"]
    pos = emp.get("position")
    await upsert_member_link(member.id, torn_id, torn_name, emp["company_id"], verified=True)
    await set_nick_safe(member, f"{torn_name} [{torn_id}]")
    #await apply_company_role_if_mapped(member, emp["company_id"])
    await ensure_only_mapped_company_role(member, emp["company_id"], remove_others=True)
    await ensure_position_role(member, cid, pos, remove_other_position_roles=True)
    return True

async def try_link_by_name(member: discord.Member, torn_name: str):
    emp = await get_employee_by_name_ci(torn_name)
    if not emp:
        return False
    tid = int(emp["employee_id"])
    cid = emp["company_id"]
    pos = emp.get("position")
    await upsert_member_link(member.id, tid, emp["name"], emp["company_id"], verified=True)
    await set_nick_safe(member, f"{emp['name']} [{tid}]")
    #await apply_company_role_if_mapped(member, emp["company_id"])
    await ensure_only_mapped_company_role(member, emp["company_id"], remove_others=True)
    await ensure_position_role(member, cid, pos, remove_other_position_roles=True)
    print(f"Linked {member} to {emp['name']} [{tid}]")
    return True

@bot.event
async def on_member_join(member: discord.Member):
    # 1) If they already match "[Torn_ID]" in display name → verify via ID.
    m = NAME_ID_BRACKET.search(member.display_name)
    if m:
        tid = int(m.group(1))
        ok = await try_link_by_torn_id(member, tid)
        if ok:
            return

    # 2) If their username matches an employee name (case-insensitive) → link by name.
    base_name = member.name
    if await try_link_by_name(member, base_name):
        return

    # 3) If nothing matched, store a placeholder row to make future mentions possible.
    await upsert_member_link(member.id, None, None, None, verified=False)

async def weekly_digest_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        # Run every hour and check: Sunday after 14:00 local? (simple approach)
        now = datetime.now()
        if now.weekday() == 6 and now.hour >= 14 and now.minute < 5:
            # TODO: build a summary per company (trains used, funds change, top inactivity/addiction)
            # send to each company's report_channel_id if set
            pass
        await asyncio.sleep(3600)



async def verifier_loop_for_guild(guild: discord.Guild):
    """Runs forever, checking links + roles every VERIFY_CHECK_SECONDS."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await guild.chunk()
            members = list(guild.members)
            print(f"[verifier] sweeping guild {guild.name} ({len(members)} members)")

            for member in members:
                if member.bot:
                    continue

                link = await get_member_link_by_discord(member.id)

                # Existing verified link → ensure correct role
                if link and link.get("torn_id") and link.get("company_id"):
                    tid = int(link["torn_id"])
                    emp = await get_employee_by_id(tid)
                    if emp:
                        desired = f"{emp.get('name') or 'Unknown'} [{tid}]"
                        if (member.nick or member.display_name) != desired:
                            await set_nick_safe(member, desired)
                        await ensure_only_mapped_company_role(member, emp["company_id"], remove_others=True)
                        await ensure_position_role(member, emp["company_id"], emp.get("position"), remove_other_position_roles=True)
                        continue  # skip to next

                # Try matching by name → ID → placeholder
                if await try_link_by_name(member, member.name):
                    continue

                m = NAME_ID_BRACKET.search(member.display_name or "")
                if m and await try_link_by_torn_id(member, int(m.group(1))):
                    continue

                if not link:
                    await upsert_member_link(member.id, None, None, None, verified=False)

                await asyncio.sleep(0.05)

        except Exception as e:
            print(f"[verifier_loop {guild.id}] error: {e}")

        await asyncio.sleep(VERIFY_CHECK_SECONDS)



async def get_all_mapped_company_roles(guild: discord.Guild) -> dict[int, discord.Role]:
    """
    Return {company_id: discord.Role} for all company->role mappings that exist in this guild.
    """
    from db import get_companies  # reuse tracked companies as the source of company IDs you care about
    roles = {}
    for cid, rid in await get_all_company_role_maps():
        role = guild.get_role(rid)
        if role:
            roles[cid] = role
    return roles
    # If you store mappings separate from tracked companies, make a DB getter that returns all rows of company_role_map.
    # Example alternative:
    # from db import get_all_company_role_maps
    # for company_id, role_id in await get_all_company_role_maps(): ...
    """try:
        # Prefer retrieving from the mapping table:
        from db import get_company_role_id
        companies = await get_companies()
        for c in companies:
            cid = c["company_id"]
            rid = await get_company_role_id(cid)
            if not rid:
                continue
            role = guild.get_role(rid)
            if role:
                roles[cid] = role
    except Exception:
        pass
    return roles"""

async def ensure_only_mapped_company_role(member: discord.Member, company_id: int, *, remove_others: bool = True):
    """
    Add the mapped role for company_id; remove mapped roles of other companies if remove_others=True.
    """
    mapped = await get_all_mapped_company_roles(member.guild)
    # Add correct role if present
    target_role = mapped.get(company_id)
    to_add = []
    if target_role and target_role not in member.roles:
        to_add.append(target_role)

    to_remove = []
    if remove_others:
        for cid, role in mapped.items():
            if cid != company_id and role in member.roles:
                to_remove.append(role)

    # Apply diffs
    try:
        if to_add:
            await member.add_roles(*to_add, reason="Company role mapping sync")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Company role mapping cleanup")
    except discord.Forbidden:
        pass

async def ensure_position_role(member: discord.Member, company_id: int, position: str | None, *, remove_other_position_roles: bool = False):
    """
    Grant mapped role for this position (within the same company).
    Optionally remove other mapped position roles for that same company.
    """
    if not position:
        return

    # Map for this company
    maps = await get_all_position_role_maps(company_id)
    if not maps:
        return

    pos_to_role = {p: member.guild.get_role(rid) for p, rid in maps}
    target = pos_to_role.get(position)
    to_add, to_remove = [], []

    if target and target not in member.roles:
        to_add.append(target)

    if remove_other_position_roles:
        for p, role in pos_to_role.items():
            if role and role in member.roles and p != position:
                to_remove.append(role)

    try:
        if to_add:
            await member.add_roles(*to_add, reason="Position role mapping sync")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Position role mapping cleanup")
    except discord.Forbidden:
        pass


async def build_company_report_embeds(company_id: int) -> list[discord.Embed] | None:
    from db import (
        get_company, get_current_stock, get_recent_metrics,
        get_unseen_news
    )
    comp = await get_company(company_id)
    if not comp:
        return None

    # cached metrics
    metrics = await get_recent_metrics(company_id, limit=2)
    latest = metrics[0] if metrics else {}
    prev   = metrics[1] if len(metrics) > 1 else {}

    name   = comp.get("name") or f"Company {company_id}"
    funds  = latest.get("company_funds")
    bank   = latest.get("company_bank")
    trains = latest.get("trains_available")
    val    = latest.get("value")
    pop, eff, env = latest.get("popularity"), latest.get("efficiency"), latest.get("environment")
    hired = latest.get("employees_hired")
    cap   = latest.get("employees_capacity")

    def fmt(n):
        return f"{n:,}" if isinstance(n, (int, float)) and n is not None else "—"

    # delta helpers
    def d(cur, old):
        if cur is None or old is None:
            return ""
        diff = int(cur) - int(old)
        return f" ({'+' if diff>=0 else ''}{diff:,})" if diff else ""

    e1 = discord.Embed(
        title=f"{name} — Daily Snapshot",
        description=f"`{company_id}`",
        colour=discord.Colour.blurple()
    )
    e1.add_field(name="Funds / Bank", value=f"${fmt(funds)}{d(funds, prev.get('company_funds'))}  |  ${fmt(bank)}{d(bank, prev.get('company_bank'))}", inline=False)
    e1.add_field(name="Value / Trains", value=f"${fmt(val)}  |  {fmt(trains)}", inline=True)
    e1.add_field(name="P/E/E", value=f"{fmt(pop)}/{fmt(eff)}/{fmt(env)}", inline=True)
    e1.add_field(name="Staffing", value=f"{fmt(hired)}/{fmt(cap)}", inline=True)

    # Stock (cached current)
    stock = await get_current_stock(company_id)
    if stock:
        e2 = discord.Embed(title=f"{name} — Stock (top {min(len(stock), MAX_STOCK_ITEMS)})", colour=discord.Colour.dark_teal())
        for r in stock[:MAX_STOCK_ITEMS]:
            e2.add_field(
                name=r["item_name"],
                value=(
                    f"Price: {fmt(r.get('price'))}  |  In: {fmt(r.get('in_stock'))}  |  On-order: {fmt(r.get('on_order'))}\n"
                    f"Sold: {fmt(r.get('sold_amount'))}  (${fmt(r.get('sold_worth'))})"
                ),
                inline=False
            )
    else:
        e2 = discord.Embed(title=f"{name} — Stock", description="No stock cached yet.", colour=discord.Colour.dark_teal())

        # News — last 7 days, separated into training and other news
    from db import get_recent_news

    seven_days_ago = int(time.time()) - 7 * 86400
    recent_news = await get_recent_news(company_id, seven_days_ago)

    train_counts = {}
    other_lines = []

    for n in recent_news:
        ts = n.get("timestamp")
        text = clean_news_text(n.get("news_text") or "")

        if not text or ts is None:
            continue

        trainee = parse_training_name(text)

        if trainee:
            train_counts[trainee] = train_counts.get(trainee, 0) + 1
        else:
            other_lines.append(f"• {text}\n<t:{int(ts)}:R>")

    e3 = discord.Embed(
        title=f"{name} — Trains Given (last 7 days)",
        colour=discord.Colour.green()
    )

    if train_counts:
        lines = [
            f"• **{trainee}** has been trained **{count}** time{'s' if count != 1 else ''} by the director."
            for trainee, count in sorted(train_counts.items(), key=lambda x: (-x[1], x[0].lower()))
        ]
        e3.add_field(name="\u200b", value="\n".join(lines[:MAX_NEWS_ITEMS]), inline=False)

        if len(lines) > MAX_NEWS_ITEMS:
            e3.set_footer(text=f"{len(lines) - MAX_NEWS_ITEMS} more trainees not shown.")
    else:
        e3.description = "No train news in the last 7 days."

    e4 = discord.Embed(
        title=f"{name} — Other Company News (last 7 days)",
        colour=discord.Colour.dark_gold()
    )

    if other_lines:
        e4.add_field(name="\u200b", value="\n\n".join(other_lines[:MAX_NEWS_ITEMS]), inline=False)

        if len(other_lines) > MAX_NEWS_ITEMS:
            e4.set_footer(text=f"{len(other_lines) - MAX_NEWS_ITEMS} more news items not shown.")
    else:
        e4.description = "No other company news in the last 7 days."

    return [e1, e2, e3, e4]



async def post_company_daily_report(bot: commands.Bot, company_id: int, *, mark_news_seen: bool = True) -> bool:
    from db import get_company, mark_news_seen as db_mark_news_seen, get_unseen_news, set_last_report_date
    comp = await get_company(company_id)
    if not comp or not comp.get("report_channel_id"):
        return False
    channel = bot.get_channel(int(comp["report_channel_id"]))
    if not channel:
        return False

    embeds = await build_company_report_embeds(company_id)
    if not embeds:
        return False

    # Post (Discord max 10 embeds per message; we’re using 3)
    await channel.send(embeds=embeds)

    # Mark news seen & record the report date
    if mark_news_seen:
        unseen = await get_unseen_news(company_id)
        if unseen:
            keys = [n["news_id"] if "news_id" in n else n.get("id") for n in unseen]
            # your mark function probably expects ids; adapt if needed
            await db_mark_news_seen(company_id, keys)
    today = datetime.now(DETROIT).strftime("%Y-%m-%d")
    await set_last_report_date(company_id, today)
    return True

async def daily_reports_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now_local = datetime.now(DETROIT)
            if now_local.hour >= DAILY_REPORT_HOUR:
                from db import get_companies, get_last_report_date
                today = now_local.strftime("%Y-%m-%d")
                companies = await get_companies()
                for c in companies:
                    cid = int(c["company_id"])
                    last = await get_last_report_date(cid)
                    if last != today:
                        ok = await post_company_daily_report(bot, cid)
                        if ok:
                            print(f"[daily-report] posted for company {cid}")
                        else:
                            print(f"[daily-report] skipped/failed for company {cid}")
        except Exception as e:
            print(f"[daily-report] error: {e}")

        # check every 15 minutes
        await asyncio.sleep(900)


# -------------------------
# Slash Commands
# -------------------------

@bot.tree.command(name="add_company", description="Track a company: saves API key and optional channels.")
@app_commands.describe(
    company_id="Torn company ID",
    api_key="API key to use for this company",
    alert_channel="Channel for alerts (inactivity/news/addiction)",
    report_channel="Channel for periodic summaries (future)"
)
async def add_company(
    interaction: discord.Interaction,
    company_id: int,
    api_key: str,
    alert_channel: discord.TextChannel | None = None,
    report_channel: discord.TextChannel | None = None
):
    from db import get_company
    await upsert_company(
        company_id=company_id,
        api_key=api_key,
        name=None,
        alert_channel_id=alert_channel.id if alert_channel else None,
        report_channel_id=report_channel.id if report_channel else None
    )
    await start_company_task(company_id)
    await interaction.response.send_message(
        f"✅ Tracking company `{company_id}`. Refresh={POLL_SECONDS}s, inactivity every {INACTIVITY_CHECK_SECONDS}s, addiction every {ADDICTION_CHECK_SECONDS}s.",
        ephemeral=True
    )

@bot.tree.command(name="set_channels", description="Update alert/report channels for a tracked company.")
@app_commands.describe(
    company_id="Torn company ID",
    alert_channel="Channel for alerts (inactivity/news/addiction)",
    report_channel="Channel for periodic summaries"
)
async def set_channels(
    interaction: discord.Interaction,
    company_id: int,
    alert_channel: discord.TextChannel | None = None,
    report_channel: discord.TextChannel | None = None
):
    from db import get_company
    company = await get_company(company_id)
    if not company:
        await interaction.response.send_message("Company not found. Add it with /add_company.", ephemeral=True)
        return

    await upsert_company(
        company_id=company_id,
        api_key=company["api_key"],
        name=company.get("name"),
        alert_channel_id=alert_channel.id if alert_channel else company.get("alert_channel_id"),
        report_channel_id=report_channel.id if report_channel else company.get("report_channel_id")
    )
    await interaction.response.send_message("✅ Channels updated.", ephemeral=True)

@bot.tree.command(name="list_companies", description="List tracked companies.")
async def list_companies(interaction: discord.Interaction):
    companies = await get_companies()
    if not companies:
        await interaction.response.send_message("No companies are being tracked.", ephemeral=True)
        return
    desc = []
    for c in companies:
        line = f"🏢 **{c.get('name') or 'Unknown'}** (`{c['company_id']}`) — Alerts: {c.get('alert_channel_id') or '—'}"
        desc.append(line)
    await interaction.response.send_message("\n".join(desc), ephemeral=True)

@bot.tree.command(name="refresh_now", description="Force a refresh for a company and show a short summary.")
@app_commands.describe(company_id="Torn company ID")
async def refresh_now(interaction: discord.Interaction, company_id: int):
    await interaction.response.defer(ephemeral=True)
    try:
        summary = await process_company_once(company_id)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)
        return

    msg = (
        f"**{summary.get('name') or 'Unknown'}** (`{company_id}`)\n"
        f"• Employees: {summary['employees_count']}\n"
        f"• Trains: {summary.get('trains')}\n"
        f"• Funds: {summary.get('funds')}\n"
        f"• Staffed: {summary.get('hired')}/{summary.get('capacity')}\n"
        f"• News entries this pull: {summary.get('new_news_count')}"
    )
    await interaction.followup.send(msg, ephemeral=True)

@bot.tree.command(name="get_employees", description="Show cached employees for a company (first 20).")
@app_commands.describe(company_id="Torn company ID")
async def get_employees_cmd(interaction: discord.Interaction, company_id: int):
    rows = await get_employees_by_company(company_id, limit=20)
    if not rows:
        await interaction.response.send_message("No employees cached yet.", ephemeral=True)
        return
    e = discord.Embed(title=f"Company {company_id} — Employees (first 20)", colour=discord.Color.blue())
    for r in rows:
        ee = r.get("eff_total")
        pos = r.get("position") or "—"
        la = r.get("last_action_relative") or "—"
        addic = r.get("addiction")
        addic_str = f" | Addiction: {addic}" if addic is not None else ""
        e.add_field(name=r["name"], value=f"{pos} | EE: {ee} | Last action: {la}{addic_str}", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="map_company_role", description="Link a Torn company to a Discord role for auto-assignment.")
@app_commands.describe(company_id="Torn company ID", role="Role to assign to employees of this company")
@app_commands.checks.has_permissions(manage_roles=True)
async def map_company_role(interaction: discord.Interaction, company_id: int, role: discord.Role):
    await set_company_role_map(company_id, role.id)
    await interaction.response.send_message(
        f"✅ Mapped company `{company_id}` → role **{role.name}**. Syncing now…", ephemeral=True
    )

    # Immediately resync roles for all members
    guild = interaction.guild
    if guild:
        asyncio.create_task(immediate_verify_sweep(guild))


async def immediate_verify_sweep(guild: discord.Guild):
    """One-shot verification sweep used after mapping updates."""
    try:
        await guild.chunk()
    except Exception:
        pass
    members = list(guild.members)
    print(f"[verifier] immediate sweep for {guild.name} ({len(members)} members)")
    for member in members:
        if member.bot:
            continue
        link = await get_member_link_by_discord(member.id)
        if link and link.get("torn_id") and link.get("company_id"):
            tid = int(link["torn_id"])
            emp = await get_employee_by_id(tid)
            if emp:
                await set_nick_safe(member, f"{emp['name']} [{tid}]")
                await ensure_only_mapped_company_role(member, emp["company_id"], remove_others=True)
                continue
        if await try_link_by_name(member, member.name):
            continue
        m = NAME_ID_BRACKET.search(member.display_name or "")
        if m and await try_link_by_torn_id(member, int(m.group(1))):
            continue
        if not link:
            await upsert_member_link(member.id, None, None, None, verified=False)
        await asyncio.sleep(0.05)


@bot.tree.command(name="verify_user", description="Force-verify a Discord member against a Torn ID.")
@app_commands.describe(member="Discord member to verify", torn_id="Torn user ID")
@app_commands.checks.has_permissions(manage_nicknames=True, manage_roles=True)
async def verify_user(interaction: discord.Interaction, member: discord.Member, torn_id: int):
    await interaction.response.defer(ephemeral=True)
    ok = await try_link_by_torn_id(member, torn_id)
    if not ok:
        await interaction.followup.send("Could not find that Torn ID in cached employees. Make sure the company trackers have pulled.", ephemeral=True)
        return
    await interaction.followup.send(f"✅ Linked {member.mention} to `{torn_id}` and updated nickname/roles.", ephemeral=True)

@bot.tree.command(name="verify_name", description="Force-verify a Discord member against a Torn name.")
@app_commands.describe(member="Discord member to verify", torn_name="Exact Torn name (case-insensitive)")
@app_commands.checks.has_permissions(manage_nicknames=True, manage_roles=True)
async def verify_name(interaction: discord.Interaction, member: discord.Member, torn_name: str):
    await interaction.response.defer(ephemeral=True)
    ok = await try_link_by_name(member, torn_name)
    if not ok:
        await interaction.followup.send("Name not found in cached employees. Verify spelling or wait for company refresh.", ephemeral=True)
        return
    await interaction.followup.send(f"✅ Linked {member.mention} to **{torn_name}** and updated nickname/roles.", ephemeral=True)

#from db import get_member_link_by_discord

@bot.tree.command(name="whois", description="Show the Torn link for a member.")
@app_commands.describe(member="Discord member")
async def whois(interaction: discord.Interaction, member: discord.Member):
    link = await get_member_link_by_discord(member.id)
    if not link or not link.get("torn_id"):
        await interaction.response.send_message("No link found.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"{member.mention} → **{link.get('torn_name') or 'Unknown'}** [{link['torn_id']}] (company {link.get('company_id') or '—'}) — {'verified' if link.get('verified') else 'unverified'}",
        ephemeral=True
    )

@bot.tree.command(name="resync_member", description="Re-apply nickname and company role from the DB link.")
@app_commands.describe(member="Discord member to resync")
@app_commands.checks.has_permissions(manage_nicknames=True, manage_roles=True)
async def resync_member(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    link = await get_member_link_by_discord(member.id)
    if not link or not link.get("torn_id"):
        await interaction.followup.send("No link saved yet for this member.", ephemeral=True)
        return
    tid = int(link["torn_id"])
    emp = await get_employee_by_id(tid)
    if not emp:
        await interaction.followup.send("Linked Torn ID not found in current employees cache.", ephemeral=True)
        return
    await set_nick_safe(member, f"{emp['name']} [{tid}]")
    await apply_company_role_if_mapped(member, emp["company_id"])
    await interaction.followup.send("✅ Resynced nickname and role.", ephemeral=True)


from discord.app_commands import checks
@bot.tree.command(name="sync_commands", description="Force re-sync of slash commands (owner only).")
@checks.has_permissions(manage_roles=True)
async def sync_commands_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if interaction.guild and interaction.guild_id:
            synced = await bot.tree.sync(guild=discord.Object(id=interaction.guild_id))
            await interaction.followup.send(f"Synced {len(synced)} commands to this guild.", ephemeral=True)
        else:
            synced = await bot.tree.sync()
            await interaction.followup.send(f"Globally synced {len(synced)} commands.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Sync error: `{e}`", ephemeral=True)

@bot.tree.command(name="debug_commands", description="List currently registered slash commands (this guild).")
async def debug_commands(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cmds = await bot.tree.fetch_commands(guild=interaction.guild)
    if not cmds:
        await interaction.followup.send("No commands registered in this guild (yet). Try /sync_commands.", ephemeral=True)
        return
    lines = [f"• `{c.name}` – {c.description or '(no desc)'}" for c in cmds]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="resync_all", description="Run an immediate server-wide verify/role sync.")
@app_commands.checks.has_permissions(manage_roles=True, manage_nicknames=True)
async def verify_sweep_now(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await immediate_verify_sweep(interaction.guild)
    await interaction.followup.send("✅ Verification sweep completed for this server.", ephemeral=True)

@bot.tree.command(name="set_thresholds", description="Set inactivity/addiction alert thresholds for a company.")
@app_commands.describe(
    company_id="Torn company ID",
    ping_hours="First ping threshold (hours), default 18",
    warn_hours="Warning threshold (hours), default 24",
    lastwarn_hours="Second-last warning threshold (hours), default 48",
    addiction_alert="Enable addiction alerts? (true/false)",
    addiction_threshold="Minimum addiction penalty to alert (whole number, default 1)"
)
async def set_thresholds_cmd(
    interaction: discord.Interaction,
    company_id: int,
    ping_hours: float | None = None,
    warn_hours: float | None = None,
    lastwarn_hours: float | None = None,
    addiction_alert: bool | None = None,
    addiction_threshold: int | None = None
):
    from db import get_company, set_thresholds
    if not await get_company(company_id):
        await interaction.response.send_message("Company not found. Add it with /add_company first.", ephemeral=True)
        return
    await set_thresholds(company_id, ping_hours, warn_hours, lastwarn_hours, addiction_alert, addiction_threshold)
    await interaction.response.send_message("✅ Thresholds updated.", ephemeral=True)



@bot.tree.command(name="company_status", description="Show quick status for a tracked company.")
@app_commands.describe(company_id="Torn company ID")
async def company_status(interaction: discord.Interaction, company_id: int):
    from db import get_company
    await interaction.response.defer(ephemeral=True)
    comp = await get_company(company_id)
    if not comp:
        await interaction.followup.send("Company not found.", ephemeral=True)
        return

    # Reuse your quick summary call
    try:
        summary = await process_company_once(company_id)  # live refresh; or make a cached variant if you prefer
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)
        return

    th = await get_thresholds(company_id)
    msg = (
        f"**{summary.get('name') or 'Unknown'}** (`{company_id}`)\n"
        f"• Employees: {summary.get('employees_count')}\n"
        f"• Trains: {summary.get('trains')}  • Funds: {summary.get('funds')}\n"
        f"• Staffed: {summary.get('hired')}/{summary.get('capacity')}\n"
        f"• Inactivity: {th['inactivity_ping_hours']}/{th['inactivity_warn_hours']}/{th['inactivity_lastwarn_hours']}h\n"
        f"• Addiction alerts: {'On' if th['addiction_alert'] else 'Off'}"
    )
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="list_mappings", description="Show company and position role mappings.")
@app_commands.checks.has_permissions(manage_roles=True, manage_nicknames=True)
async def list_mappings(interaction: discord.Interaction, company_id: int | None = None):
    await interaction.response.defer(ephemeral=True)
    lines = []
    if company_id:
        comp = await get_company(company_id)
        if not comp:
            await interaction.followup.send("Company not found.", ephemeral=True); return
        rid = await get_company_role_id(company_id)
        if rid:
            role = interaction.guild.get_role(rid); lines.append(f"Company role: <@&{rid}>" if role else f"Company role ID: {rid}")
        maps = await get_all_position_role_maps(company_id)
        if maps:
            lines.append("Position roles:")
            for p, rid in maps:
                role = interaction.guild.get_role(rid)
                lines.append(f"• {p} → <@&{rid}>" if role else f"• {p} → (missing role {rid})")
        else:
            lines.append("No position role mappings.")
    else:
        # all companies
        pairs = await get_all_company_role_maps()
        if pairs:
            lines.append("Company roles:")
            for cid, rid in pairs:
                role = interaction.guild.get_role(rid)
                lines.append(f"• {cid} → <@&{rid}>" if role else f"• {cid} → (missing role {rid})")
        else:
            lines.append("No company role mappings.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="stock", description="Show current company stock (cached).")
@app_commands.describe(company_id="Torn company ID")
async def stock_cmd(interaction: discord.Interaction, company_id: int):
    await interaction.response.defer(ephemeral=True)
    from db import get_company, get_current_stock
    comp = await get_company(company_id)
    if not comp:
        await interaction.followup.send("Company not found.", ephemeral=True); return
    rows = await get_current_stock(company_id)
    if not rows:
        await interaction.followup.send("No stock cached yet.", ephemeral=True); return

    # Keep the embed readable (first 15 items)
    e = discord.Embed(title=f"Company {company_id} — Stock (cached)", colour=discord.Colour.blue())
    for r in rows[:15]:
        e.add_field(
            name=r["item_name"],
            value=f"Price: {r.get('price'):,} | In-stock: {r.get('in_stock')} | On-order: {r.get('on_order')}\n"
                  f"Sold: {r.get('sold_amount')} (${r.get('sold_worth'):,})",
            inline=False
        )
    if len(rows) > 15:
        e.set_footer(text=f"{len(rows)} items total. Showing first 15.")
    await interaction.followup.send(embed=e, ephemeral=True)

@bot.tree.command(name="company_metrics", description="Show recent company metrics (cached history).")
@app_commands.describe(company_id="Torn company ID", points="How many recent points to include (default 10)")
async def company_metrics_cmd(interaction: discord.Interaction, company_id: int, points: int = 10):
    await interaction.response.defer(ephemeral=True)
    from db import get_company, get_recent_metrics
    comp = await get_company(company_id)
    if not comp:
        await interaction.followup.send("Company not found.", ephemeral=True); return

    hist = await get_recent_metrics(company_id, limit=max(1, min(points, 50)))
    if not hist:
        await interaction.followup.send("No metrics history yet. Wait for a snapshot cycle.", ephemeral=True); return

    latest = hist[0]
    lines = [
        f"Funds: {latest.get('company_funds'):,} | Bank: {latest.get('company_bank'):,}",
        f"Trains: {latest.get('trains_available')} | Value: {latest.get('value'):,}",
        f"Popularity/Efficiency/Environment: {latest.get('popularity')}/{latest.get('efficiency')}/{latest.get('environment')}",
        f"Staffed: {latest.get('employees_hired')}/{latest.get('employees_capacity')}",
        f"Daily income/customers: {latest.get('daily_income'):,}/{latest.get('daily_customers')}",
        f"Weekly income/customers: {latest.get('weekly_income'):,}/{latest.get('weekly_customers')}",
    ]
    await interaction.followup.send("**Latest metrics**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="employee_effectiveness", description="Show an employee's effectiveness breakdown (cached).")
@app_commands.describe(employee="Exact Torn name or '[ID]'")
async def employee_effectiveness_cmd(interaction: discord.Interaction, employee: str):
    await interaction.response.defer(ephemeral=True)
    from db import get_employee_by_name_ci, get_employee_by_id, get_employee_eff_history
    import re
    m = re.search(r"\[(\d{4,10})\]", employee)
    row = None
    if m:
        row = await get_employee_by_id(int(m.group(1)))
    if not row:
        row = await get_employee_by_name_ci(employee)
    if not row:
        await interaction.followup.send("Employee not found in cache.", ephemeral=True); return

    # Current snapshot from employees table
    name = row.get("name") or "Unknown"
    tid = int(row["employee_id"])
    eff = {
        "Working stats": row.get("eff_working_stats"),
        "Settled in": row.get("eff_settled_in"),
        "Merits": row.get("eff_merits"),
        "Director education": row.get("eff_director_education"),
        "Inactivity": row.get("eff_inactivity"),
        "Addiction (penalty)": row.get("addiction"), 
        "Total": row.get("eff_total"),
    }
    txt = "\n".join([f"• {k}: {v}" for k, v in eff.items()])
    msg = f"**{name} [{tid}]**\n{txt}"

    # Include last few historical points
    hist = await get_employee_eff_history(tid, limit=5)
    if hist:
        msg += "\n\n**Recent history (most recent first)**"
        for h in hist[:5]:
            msg += f"\n<t:{h['ts']}:R> — total {h['total']}, ws {h['working_stats']}, si {h['settled_in']}"

    await interaction.followup.send(msg, ephemeral=True)
@bot.tree.command(name="export_employees", description="Export current employees to CSV.")
@app_commands.describe(company_id="Torn company ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def export_employees_cmd(interaction: discord.Interaction, company_id: int):
    await interaction.response.defer(ephemeral=True)
    from db import get_employees_by_company
    import csv, time, os
    rows = await get_employees_by_company(company_id)
    if not rows:
        await interaction.followup.send("No employees cached.", ephemeral=True); return
    path = f"/mnt/data/employees_{company_id}_{int(time.time())}.csv"
    keys = ["employee_id","name","position","days_in_company","wage","manual_labor","intelligence","endurance",
            "eff_working_stats","eff_settled_in","eff_merits","eff_director_education","eff_inactivity","eff_total",
            "last_action_status","last_action_relative","status_state","status_desc","status_color","status_until","addiction"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})
    await interaction.followup.send(f"Here you go: [Download CSV]({path})", ephemeral=True)

@bot.tree.command(
    name="employee_workstats",
    description="Show employees' manual labor, intelligence, endurance and their total. Scope to one company or all."
)
@app_commands.describe(
    company_id="Torn company ID (omit for all companies)",
    limit="How many to show (default 25, max 100)",
    export_csv="Also attach a CSV file (true/false)?"
)
async def employee_workstats_cmd(
    interaction: discord.Interaction,
    company_id: int | None = None,
    limit: int = 25,
    export_csv: bool = False
):
    await interaction.response.defer(ephemeral=True)

    # clamp
    limit = max(1, min(limit, 100))

    from db import get_company, get_employee_work_stats
    if company_id is not None:
        comp = await get_company(company_id)
        if not comp:
            await interaction.followup.send("Company not found.", ephemeral=True)
            return

    rows = await get_employee_work_stats(company_id=company_id, limit=limit)
    if not rows:
        await interaction.followup.send("No employees found for that scope.", ephemeral=True)
        return

    # Build a readable embed
    scope_title = f"Company {company_id}" if company_id is not None else "All Companies"
    e = discord.Embed(
        title=f"{scope_title} — Top {len(rows)} by Total Work Stats",
        colour=discord.Colour.blue()
    )

    # Put compact lines into the embed (Discord has length limits; keep it tight)
    def fmt(n): 
        return f"{int(n):,}"
    lines = []
    for r in rows:
        name = r.get("name") or str(r["employee_id"])
        tid = int(r["employee_id"])
        ml, it, en, tot = r["manual_labor"], r["intelligence"], r["endurance"], r["total_ws"]
        if company_id is None:
            lines.append(f"• **{name} [{tid}]** — {fmt(ml)}/{fmt(it)}/{fmt(en)} = **{fmt(tot)}** (c:{r['company_id']})")
        else:
            lines.append(f"• **{name} [{tid}]** — {fmt(ml)}/{fmt(it)}/{fmt(en)} = **{fmt(tot)}**")

    # Discord embeds cap field length; chunk if needed
    chunk = []
    total_chars = 0
    for line in lines:
        if total_chars + len(line) > 950:  # keep headroom
            e.add_field(name="\u200b", value="\n".join(chunk), inline=False)
            chunk, total_chars = [], 0
        chunk.append(line); total_chars += len(line) + 1
    if chunk:
        e.add_field(name="\u200b", value="\n".join(chunk), inline=False)

    if not export_csv:
        await interaction.followup.send(embed=e, ephemeral=True)
        return

    # Also export a CSV to download
    import csv, time
    path = f"/mnt/data/workstats_{company_id if company_id is not None else 'all'}_{int(time.time())}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if company_id is None:
            w.writerow(["employee_id", "name", "company_id", "manual_labor", "intelligence", "endurance", "total_ws"])
            for r in rows:
                w.writerow([r["employee_id"], r.get("name") or "", r["company_id"], r["manual_labor"], r["intelligence"], r["endurance"], r["total_ws"]])
        else:
            w.writerow(["employee_id", "name", "manual_labor", "intelligence", "endurance", "total_ws"])
            for r in rows:
                w.writerow([r["employee_id"], r.get("name") or "", r["manual_labor"], r["intelligence"], r["endurance"], r["total_ws"]])

    await interaction.followup.send(embed=e, content=f"[Download CSV]({path})", ephemeral=True)

@bot.tree.command(
    name="employee_effectiveness_rank",
    description="Rank employees by effectiveness (company or all)."
)
@app_commands.describe(
    company_id="Torn company ID (omit for all companies)",
    sort_by="Which metric to sort: total, working, settled, merits, director, inactivity",
    limit="How many to show (default 25, max 100)"
)
async def employee_effectiveness_rank_cmd(
    interaction: discord.Interaction,
    company_id: int | None = None,
    sort_by: str = "total",
    limit: int = 25
):
    await interaction.response.defer(ephemeral=True)
    limit = max(1, min(limit, 100))
    sort_by = (sort_by or "total").lower().strip()
    if sort_by not in {"total","working","settled","merits","director","inactivity"}:
        await interaction.followup.send("Invalid sort_by. Use: total, working, settled, merits, director, inactivity.", ephemeral=True)
        return

    from db import get_company, get_employee_effectiveness_rank
    if company_id is not None:
        comp = await get_company(company_id)
        if not comp:
            await interaction.followup.send("Company not found.", ephemeral=True); return

    rows = await get_employee_effectiveness_rank(company_id=company_id, limit=limit, sort_by=sort_by)
    if not rows:
        await interaction.followup.send("No employees found for that scope.", ephemeral=True); return

    scope = f"Company {company_id}" if company_id is not None else "All Companies"
    title = f"{scope} — Top {len(rows)} by {sort_by.capitalize()}"
    e = discord.Embed(title=title, colour=discord.Colour.blue())

    def line(r):
        return (f"• **{r.get('name') or r['employee_id']} [{r['employee_id']}]** "
                f"— WS:{r.get('eff_working_stats') or 0} "
                f"SI:{r.get('eff_settled_in') or 0} "
                f"M:{r.get('eff_merits') or 0} "
                f"D:{r.get('eff_director_education') or 0} "
                f"IA:{r.get('eff_inactivity') or 0} "
                f"= **{r.get('eff_total') or 0}**"
                + ("" if company_id is not None else f" (c:{r['company_id']})"))

    chunk, chars = [], 0
    for r in rows:
        ln = line(r)
        if chars + len(ln) > 950:
            e.add_field(name="\u200b", value="\n".join(chunk), inline=False)
            chunk, chars = [], 0
        chunk.append(ln); chars += len(ln)+1
    if chunk:
        e.add_field(name="\u200b", value="\n".join(chunk), inline=False)

    await interaction.followup.send(embed=e, ephemeral=True)

import re
TRAIN_RX = re.compile(r'profiles\.php\?XID=(\d+).*?>([^<]+)</a> has been trained by the director', re.I)
TRAIN_FALLBACK_RX = re.compile(r'(.+?) has been trained by the director', re.I)

def _parse_training_news_row(row: dict) -> tuple[int|None, str|None, int]:
    """
    Returns (employee_id_or_None, name_or_None, timestamp)
    """
    text = row.get("news_text") or row.get("news") or ""
    ts = int(row.get("timestamp") or 0)
    m = TRAIN_RX.search(text)
    if m:
        return int(m.group(1)), m.group(2).strip(), ts
    m2 = TRAIN_FALLBACK_RX.search(re.sub(r"<.*?>","",text))  # strip tags then fallback
    if m2:
        return None, m2.group(1).strip(), ts
    return None, None, ts

@bot.tree.command(name="training_report", description="Show condensed train history for a company.")
@app_commands.describe(
    company_id="Torn company ID",
    days="How many days to look back, default 7"
)
async def training_report_cmd(interaction: discord.Interaction, company_id: int, days: int = 7):
    await interaction.response.defer(ephemeral=True)

    from db import get_company, get_recent_news

    comp = await get_company(company_id)
    if not comp:
        await interaction.followup.send("Company not found.", ephemeral=True)
        return

    cutoff = int(time.time()) - max(1, days) * 86400
    rows = await get_recent_news(company_id, cutoff)

    train_counts = {}

    for n in rows:
        text = n.get("news_text") or ""
        trainee = parse_training_name(text)
        if trainee:
            train_counts[trainee] = train_counts.get(trainee, 0) + 1

    if not train_counts:
        await interaction.followup.send(f"No train records found in the last {days} days.", ephemeral=True)
        return

    lines = [
        f"• **{trainee}** — {count} train{'s' if count != 1 else ''}"
        for trainee, count in sorted(train_counts.items(), key=lambda x: (-x[1], x[0].lower()))
    ]

    e = discord.Embed(
        title=f"Training Report — Company {company_id} — Last {days} Days",
        description="\n".join(lines[:40]),
        colour=discord.Colour.green()
    )

    if len(lines) > 40:
        e.set_footer(text=f"{len(lines) - 40} more not shown.")

    await interaction.followup.send(embed=e, ephemeral=True)
@bot.tree.command(name="db_migrate", description="Run database migrations now (adds missing columns).")
@discord.app_commands.checks.has_permissions(manage_guild=True)
async def db_migrate_cmd(interaction: discord.Interaction):
    from db import init_db
    await interaction.response.defer(ephemeral=True)
    try:
        await init_db()
        await interaction.followup.send("✅ Database migrations completed.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Migration error: `{e}`", ephemeral=True)

@bot.tree.command(name="debug_addiction_cache", description="Show cached addiction values (top 15) for a company.")
@app_commands.describe(company_id="Torn company ID")
async def debug_addiction_cache(interaction: discord.Interaction, company_id: int):
    await interaction.response.defer(ephemeral=True)
    from db import get_employees_by_company
    rows = await get_employees_by_company(company_id, limit=9999)
    if not rows:
        await interaction.followup.send("No employees cached.", ephemeral=True); return
    nz = [r for r in rows if (r.get("addiction") or 0.0) > 0.0]
    z  = [r for r in rows if not (r.get("addiction") or 0.0)]
    lines = []
    for r in (nz[:15] or z[:15]):
        lines.append(f"• {r.get('name') or r['employee_id']} [{r['employee_id']}] — {int(round(r.get('addiction') or 0))}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="debug_addiction_alerts", description="Show who meets addiction threshold and their flag state.")
@app_commands.describe(company_id="Torn company ID")
async def debug_addiction_alerts(interaction: discord.Interaction, company_id: int):
    await interaction.response.defer(ephemeral=True)
    from db import get_employees_by_company, get_thresholds, get_alert_state
    rows = await get_employees_by_company(company_id, limit=9999)
    if not rows:
        await interaction.followup.send("No employees cached.", ephemeral=True); return
    th = await get_thresholds(company_id)
    thr = int(th.get("addiction_threshold", 1))
    lines = [f"Threshold ≥ {thr}"]
    for r in rows:
        uid = int(r["employee_id"])
        val = int(round(r.get("addiction") or 0))
        st = await get_alert_state(company_id, uid)
        flag = int(st.get("addiction_flag") or 0)
        mark = "ALERT" if (val >= thr and not flag) else ("FLAGGED" if flag else "OK")
        lines.append(f"• {r.get('name') or uid} [{uid}] — val={val}, state={mark}")
        if len("\n".join(lines)) > 1800:
            break
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(
    name="reset_addiction_flags",
    description="Reset addiction alert flags (per company, per member, or across all companies)."
)
@app_commands.describe(
    company_id="Torn company ID (omit if using all_companies=true)",
    member="Discord member to reset (optional). If omitted, resets everyone in the chosen scope.",
    all_companies="If true, applies to all tracked companies (company_id is ignored)."
)
@app_commands.checks.has_permissions(manage_guild=True)
async def reset_addiction_flags_cmd(
    interaction: discord.Interaction,
    company_id: int | None = None,
    member: discord.Member | None = None,
    all_companies: bool = False
):
    """Clears addiction flags so users can alert again."""
    from db import (
        get_company, get_companies, get_employees_by_company,
        get_member_link_by_discord, set_addiction_flag
    )

    await interaction.response.defer(ephemeral=True)

    # Determine scope
    companies: list[int] = []
    if all_companies:
        comps = await get_companies()
        if not comps:
            await interaction.followup.send("No tracked companies.", ephemeral=True)
            return
        companies = [int(c["company_id"]) for c in comps]
    else:
        if company_id is None:
            await interaction.followup.send("Please provide company_id or set all_companies=true.", ephemeral=True)
            return
        comp = await get_company(company_id)
        if not comp:
            await interaction.followup.send("Company not found.", ephemeral=True)
            return
        companies = [int(company_id)]

    total_resets = 0

    if member:
        # Reset this member in the chosen scope (one or all companies)
        link = await get_member_link_by_discord(member.id)
        if not link or not link.get("torn_id"):
            await interaction.followup.send("That member isn’t linked to a Torn ID.", ephemeral=True)
            return
        tid = int(link["torn_id"])
        for cid in companies:
            await set_addiction_flag(cid, tid, False)
            total_resets += 1
    else:
        # Reset everyone in each company
        for cid in companies:
            rows = await get_employees_by_company(cid)
            for r in rows:
                await set_addiction_flag(cid, int(r["employee_id"]), False)
                total_resets += 1

    scope_txt = "all companies" if all_companies else f"company `{companies[0]}`"
    who_txt = f"for {member.mention}" if member else "for all members"
    await interaction.followup.send(
        f"✅ Cleared addiction flags {who_txt} in {scope_txt}. "
        f"(total updates: {total_resets})",
        ephemeral=True
    )

@bot.tree.command(name="company_report_now", description="Post the daily company report now.")
@app_commands.describe(company_id="Torn company ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def company_report_now(interaction: discord.Interaction, company_id: int):
    await interaction.response.defer(ephemeral=True)

    await process_company_once(company_id)

    ok = await post_company_daily_report(bot, company_id, mark_news_seen=False)

    if not ok:
        await interaction.followup.send("Could not post report (missing channel or data).", ephemeral=True)
        return

    await interaction.followup.send("✅ Report posted.", ephemeral=True)

from discord import app_commands
import discord
from discord import app_commands
#from db import get_tracker_by_id, get_company_stock, set_tracker_stock_rule


@bot.tree.command(name="stock_rule_set", description="Set a company stock rule.")
@app_commands.describe(
    company_id="Torn company ID",
    item_name="Exact stock item name",
    low="Alert if in_stock is below this",
    high="Alert if in_stock is above this"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def stock_rule_set_cmd(interaction: discord.Interaction, company_id: int, item_name: str, low: int | None = None, high: int | None = None):
    await interaction.response.defer(ephemeral=True)

    if not await get_company(company_id):
        await interaction.followup.send("Company not found.", ephemeral=True)
        return

    if low is None and high is None:
        await interaction.followup.send("Provide at least `low` or `high`.", ephemeral=True)
        return

    await set_tracker_stock_rule(company_id, item_name, low, high)

    await interaction.followup.send(
        f"✅ Stock rule set for **{item_name}** on company `{company_id}`.",
        ephemeral=True
    )


@stock_rule_set_cmd.autocomplete("item_name")
async def stock_item_autocomplete(interaction: discord.Interaction, current: str):
    try:
        company_id = getattr(interaction.namespace, "company_id", None)
        if not company_id:
            return []

        rows = await get_current_stock(int(company_id))
        names = [r["item_name"] for r in rows if r.get("item_name")]

        cur = (current or "").casefold()
        if cur:
            names = [n for n in names if cur in n.casefold()]

        return [app_commands.Choice(name=n, value=n) for n in sorted(set(names))[:25]]
    except Exception as e:
        print(f"[autocomplete stock_item] error: {e}")
        return []


async def check_stock_rules_for_company(company_id: int) -> list[str]:
    rules = await list_company_stock_rules(company_id)
    stock = await get_current_stock(company_id)

    if not rules or not stock:
        return []

    stock_by_name = {r["item_name"]: r for r in stock}
    alerts = []

    for item_name, rule in rules.items():
        row = stock_by_name.get(item_name)

        if not row:
            alerts.append(f"⚠️ **{item_name}** — item not found in current stock.")
            continue

        in_stock = int(row.get("in_stock") or 0)
        low = rule.get("low")
        high = rule.get("high")

        if low is not None and in_stock < int(low):
            alerts.append(f"🔻 **{item_name}** low stock: `{in_stock}` / threshold `{low}`")

        if high is not None and in_stock > int(high):
            alerts.append(f"🔺 **{item_name}** high stock: `{in_stock}` / threshold `{high}`")

    return alerts

@bot.tree.command(name="stock_rule_remove", description="Remove a per-tracker stock rule.")
@app_commands.describe(company_id="Company ID", item_name="Exact stock item name")
@app_commands.checks.has_permissions(manage_guild=True)
async def stock_rule_remove_cmd(interaction: discord.Interaction, company_id: int, item_name: str):
    await interaction.response.defer(ephemeral=True)
    if not await get_company(company_id):
        await interaction.followup.send("Company not found.", ephemeral=True)
        return
    ok = await delete_company_stock_rule(company_id, item_name)
    if not ok:
        await interaction.followup.send("Rule not found.", ephemeral=True)
        return

    await interaction.followup.send(f"✅ Removed stock rule for **{item_name}** on company #{company_id}.", ephemeral=True)

from discord import app_commands

@stock_rule_remove_cmd.autocomplete("item_name")
async def stock_rule_remove_item_autocomplete(interaction: discord.Interaction, current: str):
    try:
        company_id = getattr(interaction.namespace, "company_id", None)
        if not company_id:
            return []

        # get rules for this company
        settings = await get_company_settings(int(company_id))
        rules = settings.get("stock_rules") or {}

        names = list(rules.keys())

        cur = (current or "").casefold()
        if cur:
            names = [n for n in names if cur in n.casefold()]

        names = sorted(names)[:25]
        return [app_commands.Choice(name=n, value=n) for n in names]

    except Exception as e:
        print(f"[autocomplete stock_rule_remove] error: {e}")
        return []


@bot.tree.command(name="stock_rules", description="List per-tracker stock rules.")
@app_commands.describe(company_id="Company ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def stock_rules_list_cmd(interaction: discord.Interaction, company_id: int):
    await interaction.response.defer(ephemeral=True)
    if not await get_company(company_id):
        await interaction.followup.send("Company not found.", ephemeral=True)
        return
    rules = await list_company_stock_rules(company_id)
    if not rules:
        await interaction.followup.send("No stock rules configured for this company.", ephemeral=True)
        return
    lines = [f"Stock rules for company #{company_id}:"]
    for name, rule in rules.items():
        lo = rule.get("low")
        hi = rule.get("high")
        lines.append(f"• **{name}** — low:{lo if lo is not None else '—'} high:{hi if hi is not None else '—'}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(DISCORD_TOKEN)
