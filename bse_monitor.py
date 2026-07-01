"""
BSE IPO Monitor → Telegram (v2, API-based)

Uses BSE's own JSON API endpoints (the ones its own frontend calls) instead
of scraping the HTML page:
  - api.bseindia.com/BseIndiaAPI/api/ipo_details_ng/w?stripono=<IPO_NO>
  - api.bseindia.com/BseIndiaAPI/api/GetMkt_ISSUE_BBS_IPO/w?IPO_NO=<IPO_NO>

Much faster and more reliable than Playwright — no Akamai, no session games.
"""
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Prefer curl_cffi for Chrome TLS impersonation (bypasses BSE's TLS-fingerprint
# checks). Fall back to plain requests if not installed.
try:
    from curl_cffi import requests as http_client
    USE_IMPERSONATE = True
except ImportError:
    import requests as http_client
    USE_IMPERSONATE = False

# For Telegram — always plain requests, no impersonation needed
import requests as tg_requests


IPOS_FILE = "ipos.txt"
STATE_FILE = "state.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY_BUFFER_DAYS = 1

# Set to True to skip alerts that are ONLY subscription-number changes.
# During an open IPO these tick every 15 min and get noisy.
IGNORE_SUBSCRIPTION_ONLY_CHANGES = False

BSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
    "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

API_ENDPOINTS = {
    "details":      "https://api.bseindia.com/BseIndiaAPI/api/ipo_details_ng/w?stripono={ipo_no}",
    "subscription": "https://api.bseindia.com/BseIndiaAPI/api/GetMkt_ISSUE_BBS_IPO/w?IPO_NO={ipo_no}",
}


# ---------- Telegram ----------

def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram creds missing. Message would have been:\n" + message)
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(message) > 4000:
        message = message[:4000] + "\n\n... (truncated)"
    try:
        r = tg_requests.post(
            api,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
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
        ipo_no = parse_ipo_no_from_url(url)
        if not ipo_no:
            print(f"⚠ Couldn't extract IPONo from URL, skipping: {url}")
            continue
        out.append({
            "url": url,
            "listing_date": listing_date,
            "name": name,
            "ipo_no": ipo_no,
        })
    return out


def parse_ipo_no_from_url(url: str) -> str | None:
    """Extract IPONo=xxxx or IPO_NO=xxxx from a BSE URL."""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    # BSE uses various casings across pages
    for key in ("IPONo", "IPO_NO", "IPONO", "ipono", "stripono"):
        if key in params and params[key]:
            return params[key][0]
    return None


def is_active(ipo: dict) -> bool:
    cutoff = ipo["listing_date"] + timedelta(days=EXPIRY_BUFFER_DAYS)
    return today_ist() <= cutoff


# ---------- BSE API fetching ----------

def http_get(url: str) -> dict | None:
    """GET a BSE API endpoint. Returns parsed JSON or None on failure."""
    try:
        kwargs = {"headers": BSE_HEADERS, "timeout": 30}
        if USE_IMPERSONATE:
            kwargs["impersonate"] = "chrome124"
        r = http_client.get(url, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  · GET {url} → {e}")
        return None


def fetch_ipo_data(ipo_no: str) -> dict:
    """Fetch all API endpoints for one IPO, return combined dict."""
    combined = {}
    for key, template in API_ENDPOINTS.items():
        url = template.format(ipo_no=ipo_no)
        data = http_get(url)
        combined[key] = data if data is not None else {"_error": "fetch failed"}
    return combined


def all_endpoints_failed(data: dict) -> bool:
    return all(isinstance(v, dict) and "_error" in v for v in data.values())


# ---------- Diff & summarize ----------

def flatten(obj, prefix: str = "") -> list[str]:
    """Flatten nested dict/list to 'dotted.path: value' strings."""
    out = []
    if isinstance(obj, dict):
        for k in sorted(obj.keys()):
            out.extend(flatten(obj[k], f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            out.extend(flatten(item, f"{prefix}[{i}]"))
    else:
        val = str(obj) if obj is not None else ""
        out.append(f"{prefix}\t{val}")
    return out


def build_diff(old: dict, new: dict) -> tuple[str, list[str]]:
    """Return (human-readable diff, list of changed field names)."""
    def to_map(d):
        m = {}
        for line in flatten(d):
            if "\t" in line:
                k, v = line.split("\t", 1)
                m[k] = v
        return m

    old_map = to_map(old)
    new_map = to_map(new)
    lines = []
    changed_fields = []
    for k in sorted(set(old_map) | set(new_map)):
        if k not in old_map:
            lines.append(f"+ {k}: {new_map[k]}")
            changed_fields.append(k)
        elif k not in new_map:
            lines.append(f"- {k}: {old_map[k]}")
            changed_fields.append(k)
        elif old_map[k] != new_map[k]:
            lines.append(f"~ {k}: {old_map[k]} → {new_map[k]}")
            changed_fields.append(k)
    return "\n".join(lines), changed_fields


def summarize(data: dict) -> str:
    """Human-readable snapshot of the current IPO state."""
    details = data.get("details") or {}
    rows = details.get("Table") if isinstance(details, dict) else None
    if not rows or not isinstance(rows[0], dict):
        return "(no details data)"
    row = rows[0]
    # Show whichever of these fields exist in the response
    fields_of_interest = [
        ("Company", ["Company_Name", "COMPANY_NAME", "co_name", "CoName"]),
        ("Type",    ["ISS_TYPE", "IssueType", "type"]),
        ("Status",  ["STATUS", "status", "IPO_STATUS"]),
        ("Open",    ["ISS_OPEN_DT", "IssueOpenDate", "iss_open_dt", "OPEN_DT"]),
        ("Close",   ["ISS_CLOSE_DT", "IssueCloseDate", "iss_close_dt", "CLOSE_DT"]),
        ("Price",   ["PRICE_BAND", "price_band", "IPO_PRICE"]),
        ("Size",    ["ISS_SIZE", "iss_size", "IssueSize"]),
        ("Lot",     ["LOT_SIZE", "lot_size", "MarketLot", "LotSize"]),
        ("Listing", ["LISTING_DT", "ListingDate", "listing_dt", "LIST_DT"]),
    ]
    parts = []
    for label, keys in fields_of_interest:
        for k in keys:
            if k in row and row[k] not in (None, "", "-"):
                parts.append(f"{label}: {row[k]}")
                break
    return "\n".join(parts) if parts else "(details present but no known fields)"


def is_subscription_only(changed_fields: list[str]) -> bool:
    """True if every changed field is a subscription-related field."""
    if not changed_fields:
        return False
    kw = ("subscription", "subs", "sub_", "nii", "qib", "retail", "employee",
          "shareholders", "times", "oversub", "bid_qty", "bidcnt", "bidqty",
          "GetMkt_ISSUE_BBS_IPO", "bbs", "BID")
    for f in changed_fields:
        f_l = f.lower()
        if not any(k.lower() in f_l for k in kw):
            return False
    return True


# ---------- Per-IPO check ----------

def check_one(ipo: dict, state: dict) -> None:
    url = ipo["url"]
    print(f"→ {ipo['name']}  (IPO_NO={ipo['ipo_no']}, listing {ipo['listing_date']})")
    data = fetch_ipo_data(ipo["ipo_no"])

    if all_endpoints_failed(data):
        print("  ✗ all API calls failed; skipping")
        return

    canonical = json.dumps(data, sort_keys=True, default=str)
    new_hash = hashlib.sha256(canonical.encode()).hexdigest()

    entry = state.get(url, {})
    old_hash = entry.get("hash")
    old_data = entry.get("data", {})

    if old_hash is None:
        summary = summarize(data)
        state[url] = {
            "name": ipo["name"],
            "listing_date": ipo["listing_date"].isoformat(),
            "hash": new_hash,
            "data": data,
            "last_checked": datetime.now(IST).isoformat(),
        }
        send_telegram(
            f"📡 <b>Now tracking {ipo['name']}</b>\n"
            f"Expected listing: {ipo['listing_date'].isoformat()}\n\n"
            f"<a href=\"{url}\">Open IPO page</a>\n\n"
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
        # Trim diff to fit Telegram
        preview_lines = diff_text.splitlines()
        preview = "\n".join(preview_lines[:30])
        if len(preview_lines) > 30:
            preview += f"\n... and {len(preview_lines) - 30} more"
        days_to_listing = (ipo["listing_date"] - today_ist()).days
        listing_str = (
            f"today" if days_to_listing == 0
            else f"in {days_to_listing}d" if days_to_listing > 0
            else f"{-days_to_listing}d ago"
        )
        send_telegram(
            f"🔔 <b>{ipo['name']}</b> — BSE data changed\n"
            f"Listing: {ipo['listing_date'].isoformat()} ({listing_str})\n\n"
            f"<a href=\"{url}\">Open IPO page</a>\n\n"
            f"<b>Changed fields ({len(changed_fields)}):</b>\n<pre>{preview}</pre>"
        )
        print(f"  ✓ change detected ({len(changed_fields)} fields), alerted")

    state[url].update({
        "name": ipo["name"],
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
                f"✅ <b>Stopped tracking {ipo['name']}</b>\n"
                f"Listing date {ipo['listing_date'].isoformat()} has passed.\n\n"
                f"Re-add it to ipos.txt with a new date to restart tracking."
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
        print("No active IPOs to monitor.")
        save_state(state)
        return

    print(f"Monitoring {len(active)} active IPO(s) via BSE API "
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
