# LXC Architecture, Memory & Polling Operations

## Why This Architecture Works on Linux LXC

Running a pure `headless=True` browser on `shuttleonline.ktmb.com.my` lowers Google reCAPTCHA trust scores and triggers image challenges when reserving seats.

To solve this inside a headless Linux LXC container, this repo uses a lightweight **Virtual Display + Web VNC stack (`Xvfb` + `x11vnc` + `noVNC`)**:

1. **Real Headed Browser (`HEADLESS=false`):** Chromium runs inside virtual display `:99`, indistinguishable from a real desktop browser.
2. **Phone / Browser Access via NoVNC (`http://<lxc-ip>:6080/vnc.html`):** If KTMB prompts a reCAPTCHA challenge, you get an instant **Telegram alert with a screenshot and clickable NoVNC link**.
3. **1+1 Split-Pax Sniping (`SPLIT_PAX_SNIPE=true`):**
   - For sold-out peak trains, single cancelled seats (`1` seat) drop much more frequently than `2` seats at once.
   - When `PAX_COUNT=2` and `SPLIT_PAX_SNIPE=true`, the bot searches with `1 Pax` so `Select` activates when **1** seat drops. It books **Passenger 1** first, saves progress in `bookings_state.json`, and loops back to snipe the second ticket for **Passenger 2** on the **exact same train**.
4. **Fast In-Place AJAX Refresh (~300ms):** Uses KTMB's internal `ChangeDate()` / `/ShuttleTrip/Trip` endpoint instead of full page reloads to catch 10-minute cart timeouts and 72-hour refund cancellations without triggering `HTTP 503` rate limits.

---

## Memory & Polling Settings

Chromium grows when one page is polled for days, so `systemd/ktmb-sniper.service` keeps itself small:

- **Hourly recycle** — every `RECYCLE_MINUTES` (default `60`) the bot exits between sweeps and `systemd` restarts it within seconds. It never does this around `PEAK_WINDOW`, during a `/buy` or `/scrap` job, or when run manually outside `systemd`.
- **Memory cap** — `ktmb-sniper.service` sets `MemoryHigh=1200M` / `MemoryMax=1500M`, so a leak restarts the bot instead of filling swap. Raise them if a normal checkout gets killed.
- **Polling by time of day** — the pause between sweeps is `PEAK_POLL_INTERVAL_SECONDS` (`2s`) in `PEAK_WINDOW` (`08:25-08:45`), `NIGHT_POLL_INTERVAL_SECONDS` (`60s`) in `NIGHT_WINDOW` (`00:00-07:00`), and `POLL_INTERVAL_SECONDS` (`15s`) otherwise (container local time).

After changing a systemd unit file:

```bash
cp systemd/ktmb-sniper.service /etc/systemd/system/ && systemctl daemon-reload && systemctl restart ktmb-sniper
```
