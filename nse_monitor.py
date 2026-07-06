"""
NSE IPO Monitor → Telegram (v1)

Watches individual NSE IPOs and alerts on changes. Mirrors the BSE monitor
but uses NSE's three API endpoints:

  - /api/ipo-detail?symbol=X&series=Y      → issue info, dates, price band
  - /api/ipo-bid-details?symbol=X&series=Y → category-wise subscription
  - /api/ipo-chart-demand?symbol=X&exchange=NSE → demand curve

NSE requires session cookies (unlike BSE) — the script first visits the
homepage to establish a session, then calls the APIs with those cookies.
"""
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from curl_cffi import requests as http_client
    USE_IMPERSONATE = True
except ImportError:
    import requests as http_client
    USE_IMPERSONATE = False

import requests as tg_requests


IPOS_FILE = "nse_ipos.txt"
STATE_FILE = "nse_state.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY_BUFFER_DAYS = 1

# Suppress alerts that are ONLY demand-curve ticks (BSE-style noisy)
IGNORE_SUBSCRIPTION_ONLY_CHANGES = True

NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
    "X-Requested-With": "XMLHttpRequest",
    "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

API_ENDPOINTS = {
    "detail": "https://www.nseindia.com/api/ipo-detail?symbol={symbol}&series={series}",
    "bid":    "https://www.nseindia.com/api/ipo-bid-details?symbol={symbol}&series={series}",
    "demand": "https://www.nseindia.com/api/ipo-chart-demand?symbol={symbol}&exchange=NSE",
}

# NSE stuffs a server timestamp into every response — filter it out
NOISE_KEYS = {"asOnDate", "as_on_date", "timeStamp", "timestamp",
              "requestTime", "processed_on", "processedOn", "generatedOn",
              "DT_TM", "DT_TMC", "responseTime"}


# ---------- Telegram ----------

def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram creds missing. Message would have been:\n" + message)
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(message) > 4000:
        message = message[:4000] + "\n\n... (truncated)"
    try:
        r = tg_requests.post(api, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=20)
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"Telegram exception: {e}")


# ---------- State ----------

def load_state() -> dict:
    p = Path(STATE_FILE)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    Path(STATE_FILE).write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str))


# ---------- IPO list parsing ----------

def today_ist():
    return datetime.now(IST).date()


def parse_symbol_and_series(url: str) -> tuple[str, str] | tuple[None, None]:
    """Extract symbol + series from an NSE issue-information URL."""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    symbol = params.get("symbol", [""])[0].strip().upper()
    series = params.get("series", ["EQ"])[0].strip().upper()  # default EQ
    if not symbol:
        return None, None
    return symbol, series


def parse_ipos_file() -> list[dict]:
    """Format per line: YYYY-MM-DD | URL | optional name"""
    path = Path(IPOS_FILE)
    if not path.exists():
        print(f"Create {IPOS_FILE} (one IPO per line: 'YYYY-MM-DD | URL | name').")
        sys.exit(1)

    out = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            print(f"⚠ Skipping malformed line: {raw}")
            continue
        date_str, url = parts[0], parts[1]
        name = parts[2] if len(parts) >= 3 and parts[2] else url
        try:
            listing_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            print(f"⚠ Bad date in line, skipping: {raw}")
            continue
        symbol, series = parse_symbol_and_series(url)
        if not symbol:
            print(f"⚠ Couldn't extract symbol from URL, skipping: {url}")
            continue
        out.append({
            "url": url,
            "listing_date": listing_date,
            "name": name,
            "symbol": symbol,
            "series": series,
        })
    return out


def is_active(ipo: dict) -> bool:
    cutoff = ipo["listing_date"] + timedelta(days=EXPIRY_BUFFER_DAYS)
    return today_ist() <= cutoff


# ---------- NSE session + API calls ----------

def _make_session():
    """Create a session that handles cookies + Chrome TLS impersonation."""
    if USE_IMPERSONATE:
        return http_client.Session(impersonate="chrome124")
    return http_client.Session()


def fetch_nse_data(symbol: str, series: str) -> dict:
    """
    Fetch all 3 endpoints for one IPO. Establishes session cookies via a
    warmup GET to the NSE homepage first (required — API returns 401
    without cookies).
    """
    combined = {}
    session = _make_session()

    # Warmup: NSE issues session cookies on first HTML page load
    try:
        session.get("https://www.nseindia.com/",
                    headers={**NSE_HEADERS, "Accept": "text/html,*/*"},
                    timeout=30)
        # Additional warmup on the specific IPO page path
        session.get("https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
                    headers={**NSE_HEADERS, "Accept": "text/html,*/*"},
                    timeout=30)
    except Exception as e:
        print(f"  · warmup: {e}")

    for key, template in API_ENDPOINTS.items():
        url = template.format(symbol=symbol, series=series)
        try:
            r = session.get(url, headers=NSE_HEADERS, timeout=30)
            r.raise_for_status()
            combined[key] = r.json()
            # Log a hint of what we got
            top = list(combined[key].keys())[:6] if isinstance(combined[key], dict) else "list"
            print(f"  · {key}: {top}")
        except Exception as e:
            combined[key] = {"_error": str(e)[:200]}
            print(f"  ✗ {key}: {e}")

    return combined


def all_endpoints_failed(data: dict) -> bool:
    return all(isinstance(v, dict) and "_error" in v for v in data.values())


# ---------- Canonicalize ----------

def _looks_like_timestamp(v) -> bool:
    if not isinstance(v, str) or not v:
        return False
    return bool(
        re.match(r"^\d{1,2}[-/]\d{1,2}[-/]\d{4}\s+\d{1,2}:\d{2}", v) or
        re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", v) or
        re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*day", v)
    )


def _flatten(obj, prefix=""):
    """Yield (path, value) pairs from nested dict/list. Scalars only."""
    if isinstance(obj, dict):
        for k in sorted(obj.keys(), key=str):
            key_path = f"{prefix}.{k}" if prefix else str(k)
            yield from _flatten(obj[k], key_path)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _flatten(item, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def canonicalize(data: dict) -> dict:
    """
    Flatten each endpoint's response into dotted paths and drop noise
    (timestamp fields, error placeholders).
    """
    out = {}
    for endpoint_name, response in data.items():
        if not isinstance(response, (dict, list)):
            continue
        if isinstance(response, dict) and "_error" in response:
            continue
        for path, value in _flatten(response, prefix=endpoint_name):
            # Drop known noise keys anywhere in the path
            segments = re.split(r"[.\[]", path)
            last_segment = segments[-1].rstrip("]")
            if last_segment in NOISE_KEYS:
                continue
            if _looks_like_timestamp(value):
                continue
            out[path] = value if value is not None else ""
    return out


# ---------- Diff & summarize ----------

def build_diff(old: dict, new: dict) -> tuple[str, list[str]]:
    lines = []
    changed = []
    all_keys = sorted(set(old) | set(new))
    for k in all_keys:
        if k not in old:
            lines.append(f"+ {k}: {new[k]}")
            changed.append(k)
        elif k not in new:
            lines.append(f"- {k}: {old[k]}")
            changed.append(k)
        elif str(old[k]) != str(new[k]):
            lines.append(f"~ {k}: {old[k]} → {new[k]}")
            changed.append(k)
    return "\n".join(lines), changed


def summarize(canonical: dict) -> str:
    """Show the most interesting non-noise scalar fields at the top."""
    # Try common NSE field patterns first
    priority_keywords = [
        "symbol", "companyname", "issuername", "issue_name",
        "priceband", "price_band", "issueprice",
        "issuesize", "issue_size", "totalissuesize",
        "issueperiod", "issueopendate", "issuestartdate",
        "issueclosedate", "closingdate",
        "lotsize", "lot_size", "marketlot",
        "status", "issuestatus",
        "listingdate", "listing_date",
        "subscription", "subs",
    ]
    parts = []
    seen_keys = set()
    for kw in priority_keywords:
        for key, val in canonical.items():
            if kw in key.lower() and key not in seen_keys:
                if isinstance(val, (str, int, float, bool)) and str(val).strip():
                    parts.append(f"{key}: {val}")
                    seen_keys.add(key)
                    if len(parts) >= 15:
                        break
        if len(parts) >= 15:
            break

    # Fallback: if no priority matches, show first 15 non-empty scalars
    if not parts:
        for key, val in list(canonical.items())[:15]:
            if isinstance(val, (str, int, float, bool)) and str(val).strip():
                parts.append(f"{key}: {val}")

    return "\n".join(parts) if parts else "(no recognizable fields)"


def is_subscription_only(changed_fields: list[str]) -> bool:
    """True iff every changed field looks like subscription/demand noise."""
    if not changed_fields:
        return False
    keywords = ("bid.", "demand.", "subscription", "subs", "cumulative",
                "totalbid", "total_bid", "sharesqty", "shares_qty",
                "quantity", "qty", "times", "oversub", "ratio")
    for f in changed_fields:
        f_l = f.lower()
        if not any(k in f_l for k in keywords):
            return False
    return True


# ---------- Per-IPO check ----------

def check_one(ipo: dict, state: dict) -> None:
    url = ipo["url"]
    print(f"→ {ipo['name']} (NSE symbol={ipo['symbol']}, series={ipo['series']}, listing {ipo['listing_date']})")
    raw = fetch_nse_data(ipo["symbol"], ipo["series"])

    if all_endpoints_failed(raw):
        print("  ✗ all API calls failed; skipping")
        return

    data = canonicalize(raw)
    if not data:
        print("  ✗ canonicalized data is empty; skipping (state untouched)")
        return

    canonical_json = json.dumps(data, sort_keys=True, default=str)
    new_hash = hashlib.sha256(canonical_json.encode()).hexdigest()

    entry = state.get(url, {})
    old_hash = entry.get("hash")
    old_data = entry.get("data", {})

    if old_hash is None:
        summary = summarize(data)
        state[url] = {
            "name": ipo["name"],
            "symbol": ipo["symbol"],
            "series": ipo["series"],
            "listing_date": ipo["listing_date"].isoformat(),
            "hash": new_hash,
            "data": data,
            "last_checked": datetime.now(IST).isoformat(),
        }
        send_telegram(
            f"📡 <b>Now tracking {ipo['name']} (NSE)</b>\n"
            f"Symbol: {ipo['symbol']} | Expected listing: {ipo['listing_date'].isoformat()}\n\n"
            f"<a href=\"{url}\">Open NSE page</a>\n\n"
            f"<b>Current state:</b>\n<pre>{summary}</pre>"
        )
        print("  ✓ baseline captured")
        return

    if old_hash == new_hash:
        state[url]["last_checked"] = datetime.now(IST).isoformat()
        print("  · no change")
        return

    diff_text, changed_fields = build_diff(old_data, data)

    if IGNORE_SUBSCRIPTION_ONLY_CHANGES and is_subscription_only(changed_fields):
        print(f"  · subscription-only change ({len(changed_fields)} fields), suppressed")
    else:
        preview_lines = diff_text.splitlines()
        preview = "\n".join(preview_lines[:30])
        if len(preview_lines) > 30:
            preview += f"\n... and {len(preview_lines) - 30} more"
        days_to_listing = (ipo["listing_date"] - today_ist()).days
        listing_str = (
            "today" if days_to_listing == 0
            else f"in {days_to_listing}d" if days_to_listing > 0
            else f"{-days_to_listing}d ago"
        )
        send_telegram(
            f"🔔 <b>{ipo['name']} (NSE)</b> — data changed\n"
            f"Symbol: {ipo['symbol']} | Listing: {ipo['listing_date'].isoformat()} ({listing_str})\n\n"
            f"<a href=\"{url}\">Open NSE page</a>\n\n"
            f"<b>Changed ({len(changed_fields)}):</b>\n<pre>{preview}</pre>"
        )
        print(f"  ✓ change detected ({len(changed_fields)} fields), alerted")

    state[url].update({
        "name": ipo["name"],
        "symbol": ipo["symbol"],
        "series": ipo["series"],
        "listing_date": ipo["listing_date"].isoformat(),
        "hash": new_hash,
        "data": data,
        "last_checked": datetime.now(IST).isoformat(),
    })


# ---------- Expiry / cleanup ----------

def handle_expired_and_removed(all_ipos: list[dict], state: dict) -> None:
    file_urls = {ipo["url"]: ipo for ipo in all_ipos}
    to_drop = []
    for url, entry in list(state.items()):
        if url not in file_urls:
            print(f"⌫ Removed by user: {entry.get('name', url)}")
            to_drop.append(url)
            continue
        ipo = file_urls[url]
        if not is_active(ipo):
            send_telegram(
                f"✅ <b>Stopped tracking {ipo['name']} (NSE)</b>\n"
                f"Listing date {ipo['listing_date'].isoformat()} has passed."
            )
            print(f"✓ Auto-expired: {ipo['name']}")
            to_drop.append(url)
    for u in to_drop:
        state.pop(u, None)


# ---------- Main ----------

def main() -> None:
    all_ipos = parse_ipos_file()
    state = load_state()

    handle_expired_and_removed(all_ipos, state)

    active = [i for i in all_ipos if is_active(i)]
    if not active:
        print("No active NSE IPOs to monitor.")
        save_state(state)
        return

    print(f"Monitoring {len(active)} active NSE IPO(s) "
          f"(impersonate={'on' if USE_IMPERSONATE else 'off'}).")
    for ipo in active:
        try:
            check_one(ipo, state)
        except Exception as e:
            print(f"  ✗ unexpected error on {ipo['name']}: {e}")

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
