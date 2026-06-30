"""
BSE IPO Monitor → Telegram

Tracks multiple ongoing IPOs from BSE. Each IPO has a listing date attached;
once that date passes (plus a 1-day buffer to catch listing-day updates), the
IPO is auto-dropped and you get a final "stopped tracking" Telegram.

To add a new IPO: append a line to ipos.txt. To remove one early: delete the
line. Everything else (state cleanup, alerts) is automatic.
"""
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from difflib import unified_diff
from pathlib import Path

import requests
from playwright.async_api import async_playwright

IPOS_FILE = "ipos.txt"
STATE_FILE = "state.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY_BUFFER_DAYS = 1   # keep tracking N days after listing date

# True = suppress alerts that are ONLY live subscription-number ticks.
# False = alert on every change including subs.
IGNORE_SUBSCRIPTION_ONLY_CHANGES = False


# ---------- Telegram ----------

def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram creds missing. Message would have been:\n" + message)
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(message) > 4000:
        message = message[:4000] + "\n\n... (truncated)"
    try:
        r = requests.post(
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
    Path(STATE_FILE).write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------- IPO list parsing ----------

def today_ist():
    return datetime.now(IST).date()


def parse_ipos_file() -> list[dict]:
    """
    Format per line:
        YYYY-MM-DD | URL | optional name
    """
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
        out.append({"url": url, "listing_date": listing_date, "name": name})
    return out


def is_active(ipo: dict) -> bool:
    cutoff = ipo["listing_date"] + timedelta(days=EXPIRY_BUFFER_DAYS)
    return today_ist() <= cutoff


# ---------- Content normalization & diff ----------

def normalize(text: str) -> str:
    text = re.sub(
        r"\d{1,2}[-/ ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/ ]\d{4}\s+\d{1,2}:\d{2}(:\d{2})?",
        "<TS>", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}(:\d{2})?", "<TS>", text)
    text = re.sub(r"as on\s+\d{1,2}:\d{2}(:\d{2})?", "as on <TS>", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_subscription_only_change(diff_lines: list[str]) -> bool:
    kw = ("subscription", "subscribed", "times", "qib", "nii", "retail",
          "employee", "shareholders", "non-institutional")
    for line in diff_lines:
        body = line[1:].lower().strip()
        if not body:
            continue
        if any(k in body for k in kw):
            continue
        if re.match(r"^[\d.,xX\s]+$", body):
            continue
        return False
    return True


def build_change_msg(ipo: dict, old: str, new: str) -> tuple[str, list[str]]:
    old_lines = [l for l in old.splitlines() if l.strip()]
    new_lines = [l for l in new.splitlines() if l.strip()]
    diff = list(unified_diff(old_lines, new_lines, lineterm="", n=0))
    changes = [l for l in diff if l.startswith(("+", "-"))
               and not l.startswith(("+++", "---"))]
    preview = "\n".join(changes[:25])
    if len(changes) > 25:
        preview += f"\n... and {len(changes) - 25} more lines"
    days_to_listing = (ipo["listing_date"] - today_ist()).days
    listing_info = (
        f"Listing: {ipo['listing_date'].isoformat()} "
        f"({'today' if days_to_listing == 0 else f'in {days_to_listing}d' if days_to_listing > 0 else f'{-days_to_listing}d ago'})"
    )
    msg = (
        f"🔔 <b>{ipo['name']}</b> — BSE page changed\n"
        f"{listing_info}\n\n"
        f"<a href=\"{ipo['url']}\">Open IPO page</a>\n\n"
        f"<b>Diff:</b>\n<pre>{preview or '(content reshuffled)'}</pre>"
    )
    return msg, changes


# ---------- Page fetch ----------

async def fetch_rendered_text(url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
        )
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3500)
        try:
            text = await page.locator("body").inner_text()
        except Exception:
            text = await page.content()
        await browser.close()
        return text


# ---------- Per-IPO check ----------

async def check_one(ipo: dict, state: dict) -> None:
    url = ipo["url"]
    print(f"→ {ipo['name']}  (listing {ipo['listing_date']})")
    try:
        raw = await fetch_rendered_text(url)
    except Exception as e:
        print(f"  ✗ fetch failed: {e}")
        return

    norm = normalize(raw)
    new_hash = hashlib.sha256(norm.encode()).hexdigest()
    entry = state.get(url, {})
    old_hash = entry.get("hash")
    old_content = entry.get("content", "")

    if old_hash is None:
        state[url] = {
            "name": ipo["name"],
            "listing_date": ipo["listing_date"].isoformat(),
            "hash": new_hash,
            "content": norm,
            "last_checked": datetime.now(IST).isoformat(),
        }
        send_telegram(
            f"📡 <b>Now tracking {ipo['name']}</b>\n"
            f"Listing: {ipo['listing_date'].isoformat()}\n\n"
            f"<a href=\"{url}\">Open IPO page</a>\n\n"
            f"Baseline captured. You'll be alerted on changes until "
            f"{(ipo['listing_date'] + timedelta(days=EXPIRY_BUFFER_DAYS)).isoformat()}."
        )
        print("  ✓ baseline captured")
        return

    if old_hash == new_hash:
        state[url]["last_checked"] = datetime.now(IST).isoformat()
        print("  · no change")
        return

    msg, changes = build_change_msg(ipo, old_content, norm)
    if IGNORE_SUBSCRIPTION_ONLY_CHANGES and is_subscription_only_change(changes):
        print("  · subscription-only change, suppressed")
    else:
        send_telegram(msg)
        print(f"  ✓ change detected ({len(changes)} lines), alerted")

    state[url].update({
        "name": ipo["name"],
        "listing_date": ipo["listing_date"].isoformat(),
        "hash": new_hash,
        "content": norm,
        "last_checked": datetime.now(IST).isoformat(),
    })


# ---------- Expiry / cleanup ----------

def handle_expired_and_removed(all_ipos: list[dict], state: dict) -> None:
    """
    Three cases for URLs currently in state:
      1. URL in ipos.txt and active → keep
      2. URL in ipos.txt but past listing+buffer → notify, drop from state
      3. URL no longer in ipos.txt (user removed manually) → drop silently
    """
    file_urls = {ipo["url"]: ipo for ipo in all_ipos}
    to_drop = []

    for url, entry in list(state.items()):
        if url not in file_urls:
            print(f"⌫  Removed by user: {entry.get('name', url)}")
            to_drop.append(url)
            continue
        ipo = file_urls[url]
        if not is_active(ipo):
            send_telegram(
                f"✅ <b>Stopped tracking {ipo['name']}</b>\n"
                f"Listing date {ipo['listing_date'].isoformat()} has passed.\n\n"
                f"To restart tracking, re-add it to ipos.txt with a new date."
            )
            print(f"✓ Auto-expired: {ipo['name']}")
            to_drop.append(url)

    for u in to_drop:
        state.pop(u, None)


# ---------- Main ----------

async def main() -> None:
    all_ipos = parse_ipos_file()
    state = load_state()

    # First: handle anything that just expired or was removed
    handle_expired_and_removed(all_ipos, state)

    # Then: check only active IPOs
    active = [i for i in all_ipos if is_active(i)]
    if not active:
        print("No active IPOs to monitor.")
        save_state(state)
        return

    print(f"Monitoring {len(active)} active IPO(s).")
    for ipo in active:
        try:
            await check_one(ipo, state)
        except Exception as e:
            print(f"  ✗ unexpected error on {ipo['name']}: {e}")

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
