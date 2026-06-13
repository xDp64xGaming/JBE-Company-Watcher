# torn_api.py
import time
import aiohttp
import asyncio

TORN_BASE = "https://api.torn.com"

REQUEST_GAP = 1.2  # polite per-key spacing
_last_call_by_key: dict[str, float] = {}

async def _respect_rate_limit(api_key: str):
    prev = _last_call_by_key.get(api_key, 0.0)
    now = time.time()
    if now - prev < REQUEST_GAP:
        await asyncio.sleep(REQUEST_GAP - (now - prev))

async def _get(session: aiohttp.ClientSession, url: str, params: dict):
    await _respect_rate_limit(params.get("key", ""))
    async with session.get(url, params=params, timeout=30) as r:
        data = await r.json(content_type=None)
    _last_call_by_key[params.get("key", "")] = time.time()
    if isinstance(data, dict) and "error" in data:
        code = data["error"].get("code")
        msg = data["error"].get("error")
        raise RuntimeError(f"Torn error {code}: {msg}")
    return data

async def fetch_all_company(session: aiohttp.ClientSession, company_id: int, api_key: str) -> dict:
    """
    Pulls employees, detailed, profile, stock, news (serial for safety).
    """
    results: dict[str, dict] = {}
    base = f"{TORN_BASE}/company/{company_id}"
    for sel in ["employees", "detailed", "profile", "stock", "news"]:
        results[sel] = await _get(session, base, {"key": api_key, "comment": "TornAPI", "selections": sel})
    return results

async def fetch_user_bundle(session: aiohttp.ClientSession, user_id: int, api_key: str) -> dict:
    """
    Get enough user data to infer addiction if exposed. We try multiple selections
    because addiction exposure varies by endpoint/permissions.
    """
    url = f"{TORN_BASE}/user/{user_id}"
    # Try to keep calls minimal; one call with multiple selections if allowed.
    # If Torn disallows compound selections here, you can split them.
    return await _get(session, url, {"key": api_key, "comment": "TornAPI", "selections": "profile,personalstats"})
