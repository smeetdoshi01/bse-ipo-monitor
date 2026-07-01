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

# True = suppress alerts that are ONLY subscription/demand-curve ticks
# (BSE updates these every ~15 min during open period — noisy). Any change
# to actual IPO details (price band, dates, notices, anchor, status, etc.)
# always alerts regardless.
IGNORE_SUBSCRIPTION_ONLY_CHANGES = True

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
        # Log the structure for diagnosis
        if data:
            if isinstance(data, dict):
                summary_bits = []
                for k, v in data.items():
                    if isinstance(v, list):
                        summary_bits.append(f"{k}[{len(v)}]")
                    elif isinstance(v, dict):
                        summary_bits.append(f"{k}{{...}}")
                    else:
                        summary_bits.append(k)
                print(f"  · {key}: {', '.join(summary_bits)}")
            elif isinstance(data, list):
                print(f"  · {key}: list[{len(data)}]")
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


import re as _re

# Labels BSE returns that are pure noise — server timestamps, placeholders,
# duplicate identifiers etc. Stripping these prevents false-positive alerts.
NOISE_LABELS = {
    "DT_TM", "DT_TMC", "INSERT_DTTM", "AS_ON", "ISFormat",
    "DY1", "DY2", "DY3", "DY4", "DY5", "DY6",
    "IPO_Market_Timings", "Cut_off_time_for_UPI_Mandate_Confirmation",
}

# Value patterns that indicate a timestamp — skip these even if the label
# isn't in NOISE_LABELS. Catches BSE adding new timestamp fields we don't know.
_TS_PATTERNS = [
    _re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}(:\d{2})?"),        # 7/1/2026 12:16:56
    _re.compile(r"^\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*day,\s+\w+\s+\d"),        # Wednesday, July 01, ...
]


def _looks_like_timestamp(value: str) -> bool:
    if not value:
        return False
    return any(p.match(value) for p in _TS_PATTERNS)


def canonicalize(data: dict) -> dict:
    """
    Transform BSE's raw API response into a stable, diff-friendly form.

    BSE returns:
      - details.TableRows: [{Label: "Price Band", Value: "39-42"}, ...]
      - subscription.IPONO_0: master details (mostly dupes of details)
      - subscription.IPONO_1: dynamic-column junk with UUIDs (skip)
      - subscription.IPONO_2: demand curve [{Price, Quantity, ...}]
      - subscription.IPONO_3: duplicate of IPONO_2 (skip)
      - subscription.IPONO_4: empty

    We collapse to:
      { "details": {Label: Value, ...}, "demand": {price: quantity, ...} }

    Noise labels (server timestamps like DT_TM/DT_TMC, dynamic placeholder
    columns DY1..DY6) are filtered out so they don't cause false diffs.
    """
    out: dict = {"details": {}, "demand": {}}

    # Parse detail rows: Label -> Value
    details = data.get("details")
    if isinstance(details, dict):
        for r in details.get("TableRows", []) or []:
            if not isinstance(r, dict):
                continue
            label = str(r.get("Label", "")).strip()
            value = r.get("Value")
            if not label or value in (None, "", "-"):
                continue
            if label in NOISE_LABELS:
                continue
            value_str = str(value).strip()
            if _looks_like_timestamp(value_str):
                continue
            out["details"][label] = value_str

    # Parse demand curve: price -> quantity
    sub = data.get("subscription")
    if isinstance(sub, dict):
        for r in sub.get("IPONO_2", []) or []:
            if not isinstance(r, dict):
                continue
            price = str(r.get("Price", "")).strip()
            qty = str(r.get("Quantity", "")).strip()
            if price and qty:
                out["demand"][price] = qty
        # Also capture status if present
        status = sub.get("status")
        if status:
            out["api_status"] = str(status)

    return out


def summarize(canonical: dict) -> str:
    """Human-readable snapshot from the canonicalized form."""
    d = canonical.get("details", {})
    if not d:
        return "(no details captured)"

    priority_labels = [
        "ScripName", "Symbol", "Security Type",
        "Issue Period", "Price Band", "Issue Size – No. of Shares",
        "Market Lot", "Minimum Bid Quantity", "Face Value",
        "IPO Categories", "UPI Categories",
        # Important event fields — surface these in the summary if present
        "Exchange Notices", "Notes", "Remarks", "Anchor Details",
        "Addendum", "Corrigendum", "Public Notices",
    ]
    parts = []
    for label in priority_labels:
        if label in d:
            parts.append(f"{label}: {d[label]}")

    # Demand / subscription summary
    demand = canonical.get("demand", {})
    if demand:
        # Highest price = cutoff price (usually upper end of band)
        try:
            cutoff = max(demand.keys(), key=lambda p: float(p))
            qty = demand[cutoff]
            parts.append(f"\nDemand at cutoff ₹{cutoff}: {int(qty):,} shares")
            # Compute subscription ratio if issue size is known
            iss_raw = d.get("Issue Size – No. of Shares", "").replace(",", "")
            if iss_raw.isdigit() and int(iss_raw) > 0:
                ratio = int(qty) / int(iss_raw)
                parts.append(f"Subscription: {ratio:.2f}x")
        except (ValueError, KeyError):
            pass

    return "\n".join(parts) if parts else "(no recognizable fields)"


def is_subscription_only(changed_fields: list[str]) -> bool:
    """
    True iff every changed field is a demand-curve tick (`demand.*`).
    Any change to a details field always alerts.
    """
    if not changed_fields:
        return False
    return all(f.startswith("demand.") for f in changed_fields)


# ---------- Per-IPO check ----------

def check_one(ipo: dict, state: dict) -> None:
    url = ipo["url"]
    print(f"→ {ipo['name']}  (IPO_NO={ipo['ipo_no']}, listing {ipo['listing_date']})")
    raw = fetch_ipo_data(ipo["ipo_no"])

    if all_endpoints_failed(raw):
        print("  ✗ all API calls failed; skipping")
        return

    # Collapse BSE's messy shape into a stable form before hashing/diffing
    data = canonicalize(raw)

    if not data.get("details"):
        print("  ✗ canonicalized data has no details; likely API returned junk. Skipping.")
        return

    canonical_json = json.dumps(data, sort_keys=True, default=str)
    new_hash = hashlib.sha256(canonical_json.encode()).hexdigest()

    entry = state.get(url, {})
    old_hash = entry.get("hash")
    old_data = entry.get("data", {})

    # If old data is in raw format (from a previous version), canonicalize
    # it so the diff makes sense.
    if isinstance(old_data, dict) and ("details" in old_data and isinstance(old_data.get("details"), dict) and "TableRows" in old_data["details"]):
        old_data = canonicalize(old_data)
        old_hash = None  # force alert this run so summary gets sent

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
