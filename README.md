# KTMB Shuttle Tebrau Automated Sniper & Checkout Bot (`shuttleonline.ktmb.com.my`)

Automated 24/7 ticket sniper, seat-drop watcher, PDF availability reporter, and KTM Wallet checkout bot for **KTMB Shuttle Tebrau** (`Woodlands CIQ ↔ JB Sentral`), built for **Linux LXC** (Proxmox / Debian / Ubuntu) or macOS.

---

## Quick Start (Linux LXC Container)

### 1. Clone & Run Setup
SSH or `pct enter` into your Debian/Ubuntu LXC container as `root`:

```bash
git clone <your-repo-url> /opt/ktmb-shuttle-bot
cd /opt/ktmb-shuttle-bot
chmod +x setup_lxc.sh
./setup_lxc.sh
```

### 2. Configure `.env`
Edit `/opt/ktmb-shuttle-bot/.env` (created from `.env.example`):

- `KTMB_EMAIL` & `KTMB_PASSWORD`: Your KITS login.
- `PAX_COUNT=2` & `SPLIT_PAX_SNIPE`: Set `SPLIT_PAX_SNIPE=true` to snipe 2 tickets even if seats drop 1 by 1.
- `PASSENGER_1_MATCH` & `PASSENGER_2_MATCH`: Names (or partial names) matching saved passengers in your KITS Favorite Passenger list.
- `PAYMENT_METHOD=KTM_WALLET` & `KTM_WALLET_PIN`: For automated checkout without bank SMS OTPs.
- `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Sends instant alerts & screenshots to your phone.
- `VNC_PUBLIC_URL=http://<your-lxc-ip>:6080/vnc.html`: Optional NoVNC link for manual reCAPTCHA fallback.

### 3. Start the Services

```bash
systemctl start ktmb-display.service
systemctl enable --now ktmb-sniper.service
journalctl -u ktmb-sniper.service -f
```

---

## Manual CLI Usage

```bash
# Automatically snipe Saturday (~1pm) and Sunday (~8pm) for the upcoming weekend
.venv/bin/python ktmb_sniper.py --mode auto_weekend

# Target a specific weekend N weeks ahead
.venv/bin/python ktmb_sniper.py --mode auto_weekend --weeks-ahead 2

# Target explicit dates
.venv/bin/python ktmb_sniper.py --mode auto_weekend --sat-date "14 Nov 2026" --sun-date "15 Nov 2026"

# Snipe only Saturday or Sunday
.venv/bin/python ktmb_sniper.py --mode single_sat --sat-date "14 Nov 2026"
.venv/bin/python ktmb_sniper.py --mode single_sun --sun-date "15 Nov 2026"
```

---

## Documentation

- [`AGENTS.md`](AGENTS.md) — Agent & contributor guide, guardrails, and quick commands.
- [`docs/telegram-commands.md`](docs/telegram-commands.md) — Interactive `/help` menu, `/snipe`, `/buy`, `/watch`, `/block`, and `/scrap-*` PDF reports.
- [`docs/operations.md`](docs/operations.md) — LXC `Xvfb` + `noVNC` architecture, `systemd` memory caps, and polling windows.

---

## Repository Structure

- `ktmb_sniper.py` — Main Playwright sniper, split-pax state machine, Telegram bot, and KTM Wallet auto-checkout.
- `pdf_scraper.py` — Live availability PDF report generator and shared KTMB helper utilities.
- `.env.example` — Environment configuration template.
- `setup_lxc.sh` — One-command installer for Debian/Ubuntu LXC containers (`Xvfb`, `noVNC`, `Playwright`, `systemd`).
- `systemd/ktmb-display.service` — Background service running `Xvfb :99`, `fluxbox`, `x11vnc`, and `noVNC` on port `6080`.
- `systemd/ktmb-sniper.service` — Background service running `ktmb_sniper.py` with auto-restart.
