# AGENTS.md

Automated 24/7 ticket sniper, seat-drop watcher, PDF availability reporter, and KTM Wallet checkout bot for **KTMB Shuttle Tebrau** (`Woodlands CIQ ↔ JB Sentral`).

## Repository Map

- `ktmb_sniper.py` — Main Playwright sniper, split-pax state machine, Telegram bot listener/menu, and KTM Wallet auto-checkout.
- `pdf_scraper.py` — Live availability PDF report generator (`handle_scrape_command`, `parse_scrape_command`, shared `format_ktmb_date` and `wait_for_trips_loaded`).
- `setup_lxc.sh` — Debian/Ubuntu LXC installer (`Xvfb`, `noVNC`, `.venv`, Playwright Chromium, `systemd` units).
- `systemd/ktmb-display.service` — Virtual X11 display (`:99`) + `fluxbox` + `x11vnc` + `noVNC` (port `6080`).
- `systemd/ktmb-sniper.service` — Runs `ktmb_sniper.py` with `DISPLAY=:99` and memory caps (`MemoryHigh`/`MemoryMax`).
- `.env.example` — Environment variable template (`.env` is gitignored).
- `docs/` — Detailed specs: [`docs/telegram-commands.md`](docs/telegram-commands.md) and [`docs/operations.md`](docs/operations.md).

## Commands

```bash
# Syntax / import check
python3 -m py_compile ktmb_sniper.py pdf_scraper.py

# Run locally (uses .env)
.venv/bin/python ktmb_sniper.py --mode auto_weekend

# Systemd service management (on LXC)
systemctl restart ktmb-sniper.service
journalctl -u ktmb-sniper.service -f
```

## Guardrails

- **Never commit secrets or runtime state:** `.env`, `browser_profile/`, `bookings_state.json`, `custom_snipes.json`, `blockout_dates.json`, `watch_list.json`, `reports/`, and `screenshots/` are gitignored.
- **Preserve KTMB DOM & AJAX flows:** `ktmb_sniper.py` and `pdf_scraper.py` rely on `/ShuttleTrip` `ChangeDate()` / `.depart-trips tr` / `#popupModal.show` selectors and `bookings_state.json` schemas—do not alter them without explicit verification.
- **Keep `pdf_scraper.py` standalone:** `pdf_scraper.py` must not import `ktmb_sniper.py` so its CLI entrypoint remains runnable independently.
- **Headed browser on LXC:** `HEADLESS=false` with `DISPLAY=:99` is intentional to preserve Google reCAPTCHA trust scores.
