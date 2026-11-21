# Telegram Commands & Interactive Menu

Send **`/help`** (or **`/menu`**) in your Telegram channel or bot chat to open the interactive inline button menu:

- **Add Snipe Schedule**: Pick Date (e.g. This Friday) ➔ Pick Direction ➔ Choose Wanted Slot(s) (`7pm`, `8pm`, `9pm`, morning/afternoon hours, or **Load Exact Train Times** from KTMB). Added directly into the live background sniping loop, picks the earliest available selected slot, and automatically stops after snatching 1 slot on that day.
- **Watch Seat Drops** (`/watch` / `/unwatch`): Alert-only seat-drop monitor for sold-out dates (even dates already booked). Sends an instant Telegram alert with available train times, seat counts, and a direct KTMB link — without auto-buying or triggering checkout.
  - Examples: `/watch 20261226 woodlands-tebrau`, `/watch 20261226 12:30,13:45 woodlands-tebrau`, `/watch sunday 7pm,8pm,9pm tebrau-woodlands 2seats`.
- **My Schedule & Status**: View active custom snipes, watchlist, blockout dates, and recorded weekend bookings.
- **Blockout Dates** (`/block` / `/unblock`): Toggle upcoming Saturdays/Sundays to skip, or block custom dates/ranges (e.g. `/block 20261226`, `/block 20261225-20261228`, `/block next-weekend`, `/unblock all`). Saved in `blockout_dates.json` (and optionally via `BLOCKOUT_DATES` in `.env`).
- **Scrap PDF Report**: One-tap PDF reports for Fridays, Saturdays, or Sundays in either direction.
- **Remove Custom Snipe**: Cancel any scheduled custom snipe.
- **Update Bot** (`/update` / `/restart`): Pulls the latest `origin/main` from GitHub and restarts `ktmb-sniper.service` automatically, confirming once back online.

---

## Custom One-Off Snipe (`/snipe`)

```text
/snipe <date|weekday> <slots_or_range> [route]
```

**Examples:**
- `/snipe friday 7pm,8pm,9pm woodlands-tebrau` — Snipes this Friday only for `7pm`, `8pm`, or `9pm` departures (`Woodlands CIQ ➔ JB Sentral`), picking the earliest available and stopping once booked on that day.
- `/snipe friday 20:15 woodlands-tebrau` — Snipes only the exact `20:15` train.
- `/snipe 20261120 19:00-22:00 woodlands-tebrau` — Snipes any train in the `19:00–22:00` range.
- `/snipe 20261122 19:00-22:00 tebrau-woodlands` — Snipes `JB Sentral ➔ Woodlands CIQ`.

---

## Instant Ticket Buying (`/buy`)

Trigger an immediate booking from Telegram without waiting for the background loop:

```text
/buy YYYYMMDD HH:MM [optional-route]
```

**Examples:**
- `/buy 20270221 22:45` — Immediately books `PAX_COUNT` tickets on 21 Feb 2027 at 22:45 (`JB Sentral ➔ Woodlands CIQ`).
- `/buy 20270207 20:15 tebrau-woodlands` — Immediately books tickets for 7 Feb 2027 at 20:15.
- `/buy 20261114 12:30 woodlands-tebrau` — Immediately books tickets for 14 Nov 2026 at 12:30 (`Woodlands CIQ ➔ JB Sentral`).

The bot will:
1. Navigate directly to that train slot.
2. Confirm `>= PAX_COUNT` seats are available.
3. Automatically solve reCAPTCHA via Groq Whisper (if `GROQ_API_KEY` is set) or alert via NoVNC.
4. Select your saved passengers (`PASSENGER_1_MATCH`, `PASSENGER_2_MATCH`).
5. Pay via **KTM Wallet** (or hold and alert if `PAYMENT_METHOD=MANUAL_ALERT`).
6. Send a purchase confirmation screenshot in Telegram.

---

## PDF Scraping Commands (`/scrap-*`)

Generates and uploads an A4 PDF report showing **only the timeslots that currently have available seats**:

- `/scrap-friday-woodlands-tebrau` (or `/scrap-friday-tebrau-woodlands`)
- `/scrap-saturday-woodlands-tebrau` (or `/scrap-saturday-tebrau-woodlands`)
- `/scrap-sunday-tebrau-woodlands` (or `/scrap-sunday-woodlands-tebrau`)
- `/scrap-day/YYYYMMDD-departure-arrival` (e.g. `/scrap-day/20261114-woodlands-tebrau`, `/scrap-day/20270207-jb-woodlands`)
