# BSE IPO Monitor → Telegram

Watches multiple ongoing BSE IPO pages. Telegrams you on any change. Auto-drops each IPO after its listing date — so the list stays clean by itself.

## Your workflow once it's set up

- **New IPO opens** → add one line to `ipos.txt`, commit. You get a "now tracking" Telegram.
- **Anything changes on the page** (status, subscription, anchor list, basis, listing details) → you get a Telegram with the diff.
- **Listing day passes** → IPO auto-drops, you get a final "stopped tracking" Telegram. No cleanup needed.
- **Want to stop early** (IPO withdrawn) → delete the line from `ipos.txt`, commit.

## ipos.txt format

```
YYYY-MM-DD | URL | Name
```

Example:
```
2026-07-15 | https://www.bseindia.com/markets/publicissues/displayipo?id=4654&...&IPONo=7799&... | IC Electricals
2026-07-22 | https://www.bseindia.com/markets/publicissues/displayipo?id=4660&...&IPONo=7812&... | Navya Bakes
```

- `YYYY-MM-DD` is the **expected listing date** (IST). The script keeps monitoring until this date + 1 day (so it catches listing-day status flips and the listing price posting).
- `Name` is optional — shows up in alerts so you know which IPO changed at a glance.
- Lines starting with `#` are ignored.

If BSE postpones an IPO, you'll get an alert about the page change → update the date in `ipos.txt` and commit. Done.

## One-time setup (~10 min)

### 1. Telegram

Reuse your existing bot token + chat ID from the SEBI DRHP monitor. Or create a new bot via **@BotFather** → `/newbot`, then DM the bot once and grab your `chat.id` from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

### 2. GitHub repo

1. Create a new **public** GitHub repo (keeps Actions minutes free).
2. Upload everything in this folder to it.
3. **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. **Settings → Actions → General → Workflow permissions** → "Read and write permissions" → Save.

### 3. First run

**Actions → BSE IPO Monitor → Run workflow**. You'll get a "Now tracking..." Telegram for each IPO in `ipos.txt`. From then on it runs every 15 min automatically.

## Tweaks

- **Frequency** — edit cron in `.github/workflows/monitor.yml`. `*/5 * * * *` = every 5 min, `0 */1 * * *` = hourly.
- **Suppress subscription tick noise** — open `bse_monitor.py`, change `IGNORE_SUBSCRIPTION_ONLY_CHANGES = False` → `True`. Then you only get pinged on real events (anchor list, status change, basis published, listing details), not the 15-min subscription updates.
- **Buffer days after listing** — `EXPIRY_BUFFER_DAYS = 1` near the top of `bse_monitor.py`. Bump to 2 or 3 if you want to keep watching post-listing for a bit.

## Running on your VPS instead of GitHub Actions

```bash
pip install -r requirements.txt
playwright install chromium
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python bse_monitor.py
```

Cron line for every 15 min:
```
*/15 * * * * cd /path/to/bse-monitor && /usr/bin/python3 bse_monitor.py >> monitor.log 2>&1
```

## Files

- `bse_monitor.py` — scraper, diff, Telegram, auto-expiry
- `ipos.txt` — the only file you edit day-to-day
- `state.json` — auto-generated, stores last-seen content per IPO
- `requirements.txt` — Python deps
- `.github/workflows/monitor.yml` — schedule + runner
