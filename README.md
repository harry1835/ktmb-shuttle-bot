# KTMB Shuttle Tebrau Automated Sniper & Checkout Bot (`shuttleonline.ktmb.com.my`)

Automated 24/7 ticket sniper and checkout bot for **KTMB Shuttle Tebrau** (`Woodlands CIQ ↔ JB Sentral`), purpose-built for deployment on a **Linux LXC Container** (Proxmox / Debian / Ubuntu) or macOS.

---

## Why This Architecture Works on Linux LXC

Running a pure `headless=True` browser on `shuttleonline.ktmb.com.my` lowers your Google reCAPTCHA trust score and triggers image challenges when reserving seats.

To solve this inside a headless Linux LXC container, this repo uses a lightweight **Virtual Display + Web VNC stack (`Xvfb` + `x11vnc` + `noVNC`)**:
1. **Real Headed Browser (`HEADLESS=false`):** Chromium runs inside virtual display `:99`, completely indistinguishable from a real desktop browser.
2. **Phone / Browser Access via NoVNC (`http://<lxc-ip>:6080/vnc.html`):** If KTMB ever prompts a reCAPTCHA challenge or you want to watch the bot live, you get an instant **Telegram alert with a screenshot and clickable NoVNC link** so you can tap the checkbox directly from your phone.
3. **1+1 Split-Pax Sniping (`SPLIT_PAX_SNIPE=true`):**
   - For sold-out peak trains (Saturday ~1:00 PM `12:30`/`13:45` and Sunday ~8:00 PM `20:15`/`19:00`), single cancelled seats (`1` seat) drop much more frequently than `2` seats at once.
   - When `PAX_COUNT=2` and `SPLIT_PAX_SNIPE=true`, the bot searches with `1 Pax` so the `Select` button activates the moment **1** seat drops. It books **Passenger 1** first, saves progress in `bookings_state.json`, and immediately loops back to snipe the second ticket for **Passenger 2** on the **exact same train**!
4. **Fast In-Place AJAX Refresh (~300ms):** Uses KTMB's internal `ChangeDate()` / `/ShuttleTrip/Trip` endpoint instead of full page reloads to catch 10-minute cart timeouts and 72-hour refund cancellations within seconds without triggering `HTTP 503` rate limits.

---

## Quick Start: Deploying on a Linux LXC Container

### 1. Clone Repo into Your LXC Container
SSH or `pct enter` into your Debian/Ubuntu LXC container as `root`:
```bash
git clone <your-repo-url> /opt/ktmb-shuttle-bot
cd /opt/ktmb-shuttle-bot
chmod +x setup_lxc.sh
./setup_lxc.sh
```

### 2. Configure `.env`
Edit `/opt/ktmb-shuttle-bot/.env`:
```bash
nano /opt/ktmb-shuttle-bot/.env
```
Key settings to fill in:
- `KTMB_EMAIL` & `KTMB_PASSWORD`: Your KITS login.
- `PAX_COUNT=2` & `SPLIT_PAX_SNIPE=true`: Snipes 2 tickets per leg (even if seats drop 1 by 1).
- `PASSENGER_1_MATCH` & `PASSENGER_2_MATCH`: Names (or partial names) matching the 2 saved passengers in your KITS Favorite Passenger list.
- `PAYMENT_METHOD=KTM_WALLET` & `KTM_WALLET_PIN`: For automated checkout without bank SMS OTPs.
- `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Sends instant alerts & screenshots to your phone.
- `VNC_PUBLIC_URL=http://<your-lxc-ip>:6080/vnc.html`

### 3. Start the Services
```bash
# Start virtual X11 display + NoVNC web viewer on port 6080
systemctl start ktmb-display.service

# Start the KTMB Sniper bot
systemctl enable --now ktmb-sniper.service

# View live logs
journalctl -u ktmb-sniper.service -f
```

---

## ⚡ Telegram Commands

### 1. Instant Ticket Buying (`/buy`)
You can trigger an immediate booking right from Telegram without waiting for the background loop:
```
/buy YYYYMMDD HH:MM [optional-route]
```
**Examples:**
* `/buy 20270221 22:45` — Immediately books 2 tickets on 21 Feb 2027 at 22:45 (`JB Sentral ➔ Woodlands CIQ`).
* `/buy 20270207 20:15 tebrau-woodlands` — Immediately books 2 tickets for 7 Feb 2027 at 20:15.
* `/buy 20261114 12:30 woodlands-tebrau` — Immediately books 2 tickets for 14 Nov 2026 at 12:30 (`Woodlands CIQ ➔ JB Sentral`).

The bot will:
1. Navigate directly to that train slot.
2. Confirm `>= 2` seats are available.
3. Automatically solve reCAPTCHA via Groq Whisper.
4. Select both saved passengers (`HAU KWAN SHIAN` and `Lai Si Ying`).
5. Pay via **KTM Wallet**.
6. Send you an instant purchase confirmation screenshot in Telegram!

---

## 📊 Telegram PDF Scraping Commands

You can send commands directly into your Telegram channel or direct bot chat at any time, and the bot will scrape live availability and upload an A4 PDF report showing **only the timeslots that currently have available seats**:

1. **`/scrap-saturday-woodlands-tebrau`**
   - Scrapes all released upcoming Saturdays for `Woodlands CIQ ➔ JB Sentral`.
   - Sends a formatted PDF with all available timeslots & seat counts.

2. **`/scrap-sunday-tebrau-woodlands`**
   - Scrapes all released upcoming Sundays for `JB Sentral ➔ Woodlands CIQ`.
   - Sends a formatted PDF with all available timeslots & seat counts.

3. **`/scrap-day/YYYYMMDD-departure-arrival`**
   - Scrapes a single specific date and route. Examples:
     - `/scrap-day/20261114-woodlands-tebrau`
     - `/scrap-day/20261115-tebrau-woodlands`
     - `/scrap-day/20270207-jb-woodlands`

---

## Manual CLI Usage

You can also run [`ktmb_sniper.py`](ktmb_sniper.py) directly for specific dates or modes:

```bash
# Automatically snipe both Saturday (~1pm) and Sunday (~8pm) for the upcoming weekend
.venv/bin/python ktmb_sniper.py --mode auto_weekend

# Target a specific weekend N weeks ahead (e.g. 2 weeks ahead)
.venv/bin/python ktmb_sniper.py --mode auto_weekend --weeks-ahead 2

# Target explicit dates
.venv/bin/python ktmb_sniper.py --mode auto_weekend --sat-date "14 Nov 2026" --sun-date "15 Nov 2026"

# Snipe only Saturday (Woodlands CIQ -> JB Sentral)
.venv/bin/python ktmb_sniper.py --mode single_sat --sat-date "14 Nov 2026"

# Snipe only Sunday (JB Sentral -> Woodlands CIQ)
.venv/bin/python ktmb_sniper.py --mode single_sun --sun-date "15 Nov 2026"
```

---

## Memory & Polling Settings

Chromium grows when one page is polled for days, so the service keeps itself small:

- **Hourly recycle** — every `RECYCLE_MINUTES` (default 60) the bot exits between sweeps and systemd
  restarts it within seconds. It never does this around `PEAK_WINDOW`, during a `/buy` or `/scrap`
  job, or when run manually outside systemd.
- **Memory cap** — `ktmb-sniper.service` sets `MemoryHigh` / `MemoryMax`, so a leak restarts the bot
  instead of filling swap. Raise them if a normal checkout gets killed.
- **Polling by time of day** — the pause between sweeps is `PEAK_POLL_INTERVAL_SECONDS` (2s) in
  `PEAK_WINDOW` (08:25-08:45), `NIGHT_POLL_INTERVAL_SECONDS` (60s) in `NIGHT_WINDOW` (00:00-07:00),
  and `POLL_INTERVAL_SECONDS` (15s) otherwise. Times are the container's local time.

After changing the unit file: `cp systemd/ktmb-sniper.service /etc/systemd/system/ && systemctl daemon-reload && systemctl restart ktmb-sniper`.

---

## Repository Structure

- `ktmb_sniper.py` — Main Playwright sniper, split-pax state machine, and KTM Wallet auto-checkout script.
- `.env.example` — Environment configuration template.
- `setup_lxc.sh` — One-command installer for Debian/Ubuntu LXC containers (`Xvfb`, `noVNC`, `Playwright`, `systemd`).
- `systemd/ktmb-display.service` — Background service running `Xvfb :99`, `fluxbox`, `x11vnc`, and `noVNC` on port `6080`.
- `systemd/ktmb-sniper.service` — Background service running `ktmb_sniper.py` with auto-restart.
