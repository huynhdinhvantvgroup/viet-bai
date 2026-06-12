"""Google AdSense OAuth2 + Reporting API service."""
import httpx
from datetime import datetime, timezone, timedelta

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
ADSENSE_API = "https://adsense.googleapis.com/v2"
SCOPES = "https://www.googleapis.com/auth/adsense.readonly"


def get_auth_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"


async def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        return resp.json()


async def get_adsense_account(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ADSENSE_API}/accounts",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        accounts = data.get("accounts", [])
        if accounts:
            return accounts[0]
        return {}


async def get_report(access_token: str, account_id: str, start_date: str, end_date: str) -> list:
    """Get earnings report grouped by date + domain.
    start_date/end_date format: YYYY-MM-DD
    Returns list of dicts: [{date, domain, earnings, clicks, impressions, pageviews}]
    """
    params = [
        ("dateRange", "CUSTOM"),
        ("startDate.year", start_date.split("-")[0]),
        ("startDate.month", str(int(start_date.split("-")[1]))),
        ("startDate.day", str(int(start_date.split("-")[2]))),
        ("endDate.year", end_date.split("-")[0]),
        ("endDate.month", str(int(end_date.split("-")[1]))),
        ("endDate.day", str(int(end_date.split("-")[2]))),
        ("dimensions", "DATE"),
        ("dimensions", "DOMAIN_NAME"),
        ("metrics", "ESTIMATED_EARNINGS"),
        ("metrics", "CLICKS"),
        ("metrics", "IMPRESSIONS"),
        ("metrics", "PAGE_VIEWS"),
        ("reportingTimeZone", "ACCOUNT_TIME_ZONE"),
    ]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ADSENSE_API}/{account_id}/reports:generate",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()

    rows = data.get("rows", [])
    headers_list = [h.get("name", "") for h in data.get("headers", [])]
    result = []
    total_earnings = 0
    for row in rows:
        cells = row.get("cells", [])
        entry = {}
        for i, h in enumerate(headers_list):
            val = cells[i].get("value", "") if i < len(cells) else ""
            if h == "DATE":
                entry["date"] = val
            elif h == "DOMAIN_NAME":
                entry["domain"] = val
            elif h == "ESTIMATED_EARNINGS":
                entry["earnings"] = float(val) if val else 0
                total_earnings += entry["earnings"]
            elif h == "CLICKS":
                entry["clicks"] = int(val) if val else 0
            elif h == "IMPRESSIONS":
                entry["impressions"] = int(val) if val else 0
            elif h == "PAGE_VIEWS":
                entry["pageviews"] = int(val) if val else 0
        result.append(entry)

    return result, total_earnings
