#!/usr/bin/env python3
"""
KTMB Shuttle Tebrau Automated Sniper & Checkout Bot (Linux LXC & macOS Ready)
Portal: https://shuttleonline.ktmb.com.my/Home/Shuttle

Key Features:
1. Scans ALL released weekends up to the last released date on KTMB's calendar
   (auto-detects when a future Sunday/Saturday returns 'No trip available' / 1 row
   and wraps back to the nearest upcoming weekend).
2. Zero-Login Fast AJAX Polling:
   - Uses KTMB's internal `ChangeDate()` AJAX endpoint (~300ms per date) to cycle
     through every Sunday (or Saturday) without reloading the page and without
     logging in first (preventing KTMB's "Not allow multiple login" lock!).
3. On-Demand Cross-Domain SSO Login & Checkout:
   - Only logs in when a seat (>=1) is actually found on your preferred train,
     locks the seat in cart, selects your saved KITS Favorite Passenger (`PASSENGER_1_MATCH`
     then `PASSENGER_2_MATCH`), and triggers KTM Wallet checkout + Telegram screenshot alerts.
4. Persistent State (`bookings_state.json`):
   - Tracks `booked_pax` and `locked_train_time` per date (e.g. `SUN_15 Nov 2026`)
     so once a Sunday has 2/2 tickets booked, it automatically skips that Sunday
     on future scans!
"""

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

from pdf_scraper import handle_scrape_command, parse_scrape_command

BASE_URL = "https://shuttleonline.ktmb.com.my"
SHUTTLE_URL = f"{BASE_URL}/Home/Shuttle"
LOGIN_URL = f"{BASE_URL}/Account/Login"

ROOT_DIR = Path(__file__).parent.resolve()
USER_DATA_DIR = ROOT_DIR / "browser_profile"
STATE_FILE = ROOT_DIR / "bookings_state.json"
CUSTOM_SNIPES_FILE = ROOT_DIR / "custom_snipes.json"
UPDATE_NOTIFY_FILE = ROOT_DIR / ".update_notify.json"
SCREENSHOT_DIR = ROOT_DIR / "screenshots"

# Memory hygiene: Chromium + the Playwright driver grow steadily when one page is polled
# for days, so the process exits at a safe point every RECYCLE_MINUTES and systemd
# (Restart=always) starts a fresh one. The persistent browser profile keeps the session.
_PROCESS_START = time.monotonic()
# Held by /buy, /scrap, and /update jobs: allows one extra job at a time and blocks a recycle mid-job.
_SIDE_JOB_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_CUSTOM_SNIPES_LOCK = threading.Lock()
_RESTART_EVENT = threading.Event()


class RecycleRequested(Exception):
    """Raised between sweeps when the process should exit so systemd restarts it fresh."""


def _parse_window(spec: str) -> tuple[int, int] | None:
    """Parse 'HH:MM-HH:MM' into (start_minute, end_minute) of the day; None if blank/invalid."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", spec or "")
    if not m:
        return None
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    return (h1 % 24) * 60 + m1, (h2 % 24) * 60 + m2


def _in_window(window: tuple[int, int] | None, now: datetime.datetime, lead_minutes: int = 0) -> bool:
    """True if `now` falls inside the window (windows may wrap past midnight)."""
    if not window:
        return False
    start, end = window
    start = (start - lead_minutes) % 1440
    minute = now.hour * 60 + now.minute
    return start <= minute < end if start <= end else (minute >= start or minute < end)


def _peak_window() -> tuple[int, int] | None:
    return _parse_window(os.getenv("PEAK_WINDOW", "08:25-08:45"))


def current_poll_interval(base_interval: float, now: datetime.datetime | None = None) -> float:
    """
    Pause between sweeps for the current time of day:
      - PEAK_WINDOW  (daily ticket release): PEAK_POLL_INTERVAL_SECONDS
      - NIGHT_WINDOW (few cancellations)   : NIGHT_POLL_INTERVAL_SECONDS
      - otherwise                          : `base_interval` (POLL_INTERVAL_SECONDS / --interval)
    """
    now = now or datetime.datetime.now()
    if _in_window(_peak_window(), now):
        return float(os.getenv("PEAK_POLL_INTERVAL_SECONDS", "2"))
    if _in_window(_parse_window(os.getenv("NIGHT_WINDOW", "00:00-07:00")), now):
        return float(os.getenv("NIGHT_POLL_INTERVAL_SECONDS", "60"))
    return base_interval


def sleep_or_wake(seconds: float) -> bool:
    """Sleep up to `seconds`, returning True early if a restart was requested."""
    return _RESTART_EVENT.wait(timeout=max(0.0, seconds))


def maybe_recycle(page: Page):
    """Between sweeps: exit cleanly if /update requested restart or process is older than RECYCLE_MINUTES."""
    if _RESTART_EVENT.is_set():
        print("[*] Restart requested via Telegram /update. Closing browser cleanly and exiting...")
        try:
            page.context.close()
        except Exception:
            pass
        raise RecycleRequested()

    recycle_minutes = float(os.getenv("RECYCLE_MINUTES", "60"))
    # Only under systemd (which sets INVOCATION_ID): a manual run has nothing to restart it.
    if not os.getenv("INVOCATION_ID"):
        return
    if recycle_minutes <= 0 or time.monotonic() - _PROCESS_START < recycle_minutes * 60:
        return
    # Never restart just before or during the release window.
    if _in_window(_peak_window(), datetime.datetime.now(), lead_minutes=5):
        return
    # A /buy or /scrap job is running: try again after the next sweep. On success the lock
    # stays held until the process exits, so no new job can start a browser meanwhile.
    if not _SIDE_JOB_LOCK.acquire(blocking=False):
        return
    print(f"[*] Process is older than {recycle_minutes:g} min. Exiting so systemd restarts it with fresh memory...")
    try:
        page.context.close()
    except Exception:
        pass
    raise RecycleRequested()


def _git_cmd(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"safe.directory={ROOT_DIR}", *args],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def trigger_bot_restart():
    """Signal main loop to close browser and exit cleanly, with a fallback restart if mid-sweep."""
    _RESTART_EVENT.set()
    time.sleep(3.5)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.getenv("INVOCATION_ID"):
        try:
            subprocess.Popen(
                ["systemctl", "restart", "ktmb-sniper.service"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2.0)
        except Exception:
            pass
        os._exit(0)
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)


def perform_bot_update(chat_id: str | None = None, force_restart: bool = False):
    """Pull latest commits from origin/main (and install requirements if changed), then restart."""
    with _SIDE_JOB_LOCK:
        try:
            old_res = _git_cmd(["rev-parse", "--short", "HEAD"])
            old_sha = old_res.stdout.strip() if old_res.returncode == 0 else "unknown"

            if not force_restart:
                fetch_res = _git_cmd(["fetch", "origin", "main"], timeout=30)
                if fetch_res.returncode != 0:
                    err = (fetch_res.stderr or fetch_res.stdout).strip()
                    send_telegram(f"❌ Update failed during git fetch:\n{err}", chat_id=chat_id)
                    return

                pending_res = _git_cmd(["log", "--oneline", "HEAD..origin/main"])
                pending_commits = pending_res.stdout.strip() if pending_res.returncode == 0 else ""

                if not pending_commits:
                    head_msg_res = _git_cmd(["log", "-1", "--oneline"])
                    head_line = head_msg_res.stdout.strip() if head_msg_res.returncode == 0 else old_sha
                    send_telegram(
                        f"✅ Already up to date on main.\n• Current: {head_line}",
                        reply_markup={
                            "inline_keyboard": [
                                [
                                    {"text": "♻️ Restart Bot Anyway", "callback_data": "bot_restart:now"},
                                    {"text": "🏠 Main Menu", "callback_data": "menu:main"},
                                ]
                            ]
                        },
                        chat_id=chat_id,
                    )
                    return

                pull_res = _git_cmd(["pull", "--ff-only", "origin", "main"], timeout=30)
                if pull_res.returncode != 0:
                    err = (pull_res.stderr or pull_res.stdout).strip()
                    send_telegram(f"❌ git pull failed (not restarting):\n{err}", chat_id=chat_id)
                    return

                new_res = _git_cmd(["rev-parse", "--short", "HEAD"])
                new_sha = new_res.stdout.strip() if new_res.returncode == 0 else old_sha

                diff_res = _git_cmd(["diff", "--name-only", old_sha, new_sha])
                if diff_res.returncode == 0 and "requirements.txt" in (diff_res.stdout or "").splitlines():
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-r", str(ROOT_DIR / "requirements.txt")],
                        cwd=str(ROOT_DIR),
                        capture_output=True,
                        text=True,
                        timeout=120,
                        check=False,
                    )

                try:
                    _atomic_write_json(
                        UPDATE_NOTIFY_FILE,
                        {
                            "chat_id": (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip(),
                            "sha": new_sha,
                            "old_sha": old_sha,
                        },
                    )
                except Exception:
                    pass

                commit_lines = "\n".join(f"• {line}" for line in pending_commits.splitlines()[:8])
                send_telegram(
                    f"🚀 Updated {old_sha} ➔ {new_sha}!\n{commit_lines}\n\n♻️ Restarting bot now...",
                    chat_id=chat_id,
                )
            else:
                try:
                    _atomic_write_json(
                        UPDATE_NOTIFY_FILE,
                        {
                            "chat_id": (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip(),
                            "sha": old_sha,
                            "old_sha": old_sha,
                            "restarted_only": True,
                        },
                    )
                except Exception:
                    pass
                send_telegram(f"♻️ Restarting bot ({old_sha}) now...", chat_id=chat_id)

            trigger_bot_restart()
        except Exception as e:
            print(f"[!] Update failed: {e}")
            send_telegram(f"❌ Update failed: {e}", chat_id=chat_id)


def run_side_job(fn, *args):
    """Run a Telegram-triggered browser job (/buy, /scrap) one at a time."""
    target_chat = args[1] if len(args) > 1 and isinstance(args[1], str) else None
    with _SIDE_JOB_LOCK:
        try:
            res = fn(*args)
            if fn is handle_scrape_command and res is False:
                send_telegram(
                    "❌ Unrecognized /scrap command format.\n"
                    "Try /help to pick a report from the menu, or send e.g.:\n"
                    "• /scrap-friday-woodlands-tebrau\n"
                    "• /scrap-day/20261120-woodlands-tebrau",
                    chat_id=target_chat,
                )
        except Exception as e:
            print(f"[!] {fn.__name__} failed: {e}")
            send_telegram(f"❌ {fn.__name__} failed: {e}", chat_id=target_chat)


# Pending confirmation prompts keyed by request_id -> {"event": threading.Event, "approved": bool | None}
_PENDING_CONFIRMATIONS: dict[str, dict] = {}
_PENDING_CONFIRMATIONS_LOCK = threading.Lock()
# Per-message interactive slot-picker draft state keyed by (chat_id, message_id)
_MENU_DRAFTS: dict[tuple[str, int], dict] = {}
_MENU_DRAFTS_LOCK = threading.Lock()


def send_telegram(
    message: str,
    photo_path: Path | None = None,
    reply_markup: dict | None = None,
    chat_id: str | None = None,
    parse_mode: str | None = None,
) -> int | None:
    """Send a Telegram alert (and optional screenshot / inline keyboard) if TELEGRAM_BOT_TOKEN is configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not target_chat:
        return None

    try:
        if photo_path and photo_path.exists():
            cmd = [
                "curl",
                "-sS",
                "--max-time",
                "30",
                "-X",
                "POST",
                f"https://api.telegram.org/bot{token}/sendPhoto",
                "-F",
                f"chat_id={target_chat}",
                "-F",
                f"photo=@{photo_path}",
                "-F",
                f"caption={message}",
            ]
            if parse_mode:
                cmd.extend(["-F", f"parse_mode={parse_mode}"])
            if reply_markup is not None:
                cmd.extend(["-F", f"reply_markup={json.dumps(reply_markup)}"])
            res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=35)
            try:
                return json.loads(res.stdout).get("result", {}).get("message_id")
            except Exception:
                return None
        else:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload: dict[str, str] = {"chat_id": target_chat, "text": message}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup is not None:
                payload["reply_markup"] = json.dumps(reply_markup)
            data = urllib.parse.urlencode(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            resp = urllib.request.urlopen(req, timeout=10)
            res_json = json.loads(resp.read().decode("utf-8"))
            return res_json.get("result", {}).get("message_id")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        print(f"[!] Telegram notification HTTP {e.code}: {body or e}")
        return None
    except Exception as e:
        print(f"[!] Telegram notification error: {e}")
        return None


def answer_callback_query(callback_query_id: str, text: str = "", show_alert: bool = False):
    """Acknowledge a Telegram inline button tap so the client stops showing a spinner."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not callback_query_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
        payload: dict[str, str] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = "true"
        data = urllib.parse.urlencode(payload).encode("utf-8")
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        print(f"[!] Telegram answerCallbackQuery error: {e}")


def edit_telegram_message(
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
) -> bool:
    """Edit an existing Telegram message (clears inline keyboard if reply_markup is None)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not chat_id or not message_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        markup = reply_markup if reply_markup is not None else {"inline_keyboard": []}
        payload: dict[str, str] = {
            "chat_id": chat_id,
            "message_id": str(message_id),
            "text": text,
            "reply_markup": json.dumps(markup),
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(payload).encode("utf-8")
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        return True
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if "message is not modified" in body.lower():
            return True
        print(f"[!] Telegram editMessageText HTTP {e.code}: {body or e}")
        return False
    except Exception as e:
        print(f"[!] Telegram editMessageText error: {e}")
        return False


def hours_until_train(
    target_date: str,
    train_time: str,
    now: datetime.datetime | None = None,
) -> float | None:
    """Return hours from `now` until `target_date` + `train_time` (e.g. '15 Nov 2026', '20:15')."""
    try:
        train_dt = datetime.datetime.strptime(f"{target_date.strip()} {train_time.strip()}", "%d %b %Y %H:%M")
    except ValueError:
        return None
    now = now or datetime.datetime.now()
    return (train_dt - now).total_seconds() / 3600.0


def ask_user_confirm_buy(
    target_date: str,
    train_time: str,
    origin: str,
    destination: str,
    seats_avail: int,
    hours_left: float,
    timeout_sec: int = 300,
) -> bool:
    """
    Send a Telegram prompt with Buy / Skip inline buttons when a train is within
    CONFIRM_WITHIN_HOURS of departure. Returns True only if the user taps Buy.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[!] Train is within confirmation window, but Telegram is not configured. Skipping auto-buy.")
        return False

    req_id = f"{int(time.time() * 1000)}"
    evt = threading.Event()
    entry = {"event": evt, "approved": None}
    with _PENDING_CONFIRMATIONS_LOCK:
        _PENDING_CONFIRMATIONS[req_id] = entry

    time_desc = (
        f"{hours_left:.1f}h" if hours_left >= 1 else f"{max(0, int(hours_left * 60))}m"
    )
    prompt_text = (
        f"⚠️ [Confirm Purchase - Within {time_desc}]\n"
        f"Seat found for {target_date} ({train_time})\n"
        f"Route: {origin} ➔ {destination}\n"
        f"Available seats: {seats_avail}\n\n"
        f"Departure is in {time_desc}. Do you want to buy this ticket?"
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Buy Now", "callback_data": f"confirm_buy:{req_id}:yes"},
                {"text": "❌ Skip", "callback_data": f"confirm_buy:{req_id}:no"},
            ]
        ]
    }

    msg_id = send_telegram(prompt_text, reply_markup=reply_markup)
    print(f"[*] Sent 36h confirmation prompt to Telegram (req={req_id}, timeout={timeout_sec}s)...")

    responded = evt.wait(timeout=timeout_sec)
    with _PENDING_CONFIRMATIONS_LOCK:
        result = _PENDING_CONFIRMATIONS.pop(req_id, entry)

    if not responded or result["approved"] is None:
        print(f"[*] Confirmation timed out after {timeout_sec}s for {target_date} {train_time}. Skipping.")
        if msg_id:
            try:
                dt = datetime.datetime.strptime(target_date.strip(), "%d %b %Y")
                ymd = dt.strftime("%Y%m%d")
                buy_hint = f"Use /buy {ymd} {train_time} if you still want it."
            except ValueError:
                buy_hint = ""
            edit_telegram_message(
                chat_id,
                msg_id,
                f"{prompt_text}\n\n⏱️ Timed out after {timeout_sec // 60}m — skipped. {buy_hint}".strip(),
            )
        return False

    return bool(result["approved"])


def notify(title: str, message: str, page: Page | None = None, sound: bool = True):
    """Unified notification across Linux LXC (Telegram + logs) and macOS (native notification)."""
    full_msg = f"[{title}] {message}"
    print(f"\n🔔 {full_msg}")

    photo_path = None
    if page is not None:
        try:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            photo_path = SCREENSHOT_DIR / f"alert_{ts}.png"
            page.screenshot(path=str(photo_path))

            # Auto-clean older screenshots: keep only the most recent 20 screenshots
            try:
                all_shots = sorted(SCREENSHOT_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime)
                if len(all_shots) > 20:
                    for old_file in all_shots[:-20]:
                        old_file.unlink(missing_ok=True)
            except Exception:
                pass
        except Exception:
            photo_path = None

    send_telegram(full_msg, photo_path)

    if platform.system() == "Darwin":
        try:
            script = f'display notification "{message}" with title "{title}"'
            if sound:
                script += ' sound name "Glass"'
            subprocess.run(["osascript", "-e", script], check=False)
        except Exception:
            pass


def _atomic_write_json(path: Path, data):
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2))
    os.replace(tmp_path, path)


def load_state() -> dict:
    with _STATE_LOCK:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                return {}
        return {}


def save_state(state: dict):
    with _STATE_LOCK:
        _atomic_write_json(STATE_FILE, state)


def load_custom_snipes() -> list[dict]:
    with _CUSTOM_SNIPES_LOCK:
        if CUSTOM_SNIPES_FILE.exists():
            try:
                data = json.loads(CUSTOM_SNIPES_FILE.read_text())
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []


def save_custom_snipes(rules: list[dict]):
    with _CUSTOM_SNIPES_LOCK:
        _atomic_write_json(CUSTOM_SNIPES_FILE, rules)


def has_completed_booking_on_date(ktmb_date: str) -> bool:
    """True if a custom snipe (or direct /buy on that custom date) already completed on `ktmb_date`."""
    for rule in load_custom_snipes():
        if rule.get("date") == ktmb_date:
            target = int(rule.get("target_pax", 1))
            booked = int(rule.get("booked_pax", 0))
            if rule.get("status") == "completed" or booked >= target:
                return True
    state = load_state()
    custom_entry = state.get(f"CUSTOM_{ktmb_date}")
    if isinstance(custom_entry, dict):
        total_pax = int(os.getenv("PAX_COUNT", "2"))
        if int(custom_entry.get("booked_pax", 0)) >= total_pax:
            return True
    return False


def _normalize_slots(
    slots: list[str] | None,
    start_time: str = "",
    end_time: str = "",
) -> tuple[list[str], str, str]:
    """Validate and normalize slot specs (e.g. ['19:00-19:59', '20:15']) and return (slots, min_start, max_end)."""
    raw_slots = [s.strip() for s in (slots or []) if s and s.strip()]
    if not raw_slots:
        if start_time and end_time:
            raw_slots = [f"{start_time}-{end_time}"]
        else:
            raise ValueError("No time slots specified.")

    normalized: list[str] = []
    seen: set[str] = set()
    min_start: tuple[int, str] | None = None
    max_end: tuple[int, str] | None = None

    for item in raw_slots:
        if "-" in item:
            s_raw, e_raw = item.split("-", 1)
            s_min = _hhmm_to_minutes(s_raw)
            e_min = _hhmm_to_minutes(e_raw)
            if s_min is None or e_min is None or s_min > e_min:
                raise ValueError(f"Invalid time slot range: {item}")
            s_fmt = f"{s_min // 60:02d}:{s_min % 60:02d}"
            e_fmt = f"{e_min // 60:02d}:{e_min % 60:02d}"
            spec = f"{s_fmt}-{e_fmt}"
        else:
            t_min = _hhmm_to_minutes(item)
            if t_min is None:
                raise ValueError(f"Invalid time slot: {item}")
            s_min = e_min = t_min
            s_fmt = e_fmt = f"{t_min // 60:02d}:{t_min % 60:02d}"
            spec = s_fmt

        if spec not in seen:
            seen.add(spec)
            normalized.append(spec)
        if min_start is None or s_min < min_start[0]:
            min_start = (s_min, s_fmt)
        if max_end is None or e_min > max_end[0]:
            max_end = (e_min, e_fmt)

    assert min_start is not None and max_end is not None
    return normalized, min_start[1], max_end[1]


def _hour_label(hour: int) -> str:
    if hour == 0:
        return "12am"
    if hour < 12:
        return f"{hour}am"
    if hour == 12:
        return "12pm"
    return f"{hour - 12}pm"


def format_slots_summary(rule_or_slots) -> str:
    """Format rule['slots'] (or a rule dict) nicely as e.g. '7pm, 8pm' or '20:15, 21:30'."""
    if isinstance(rule_or_slots, dict):
        slots = rule_or_slots.get("slots") or []
        if not slots and rule_or_slots.get("start_time") and rule_or_slots.get("end_time"):
            slots = [f"{rule_or_slots['start_time']}-{rule_or_slots['end_time']}"]
    else:
        slots = list(rule_or_slots or [])

    parts: list[str] = []
    for spec in slots:
        if "-" in spec:
            s, e = spec.split("-", 1)
            if s.endswith(":00") and e.endswith(":59") and s[:2] == e[:2]:
                parts.append(_hour_label(int(s[:2])))
            else:
                parts.append(f"{s}–{e}")
        else:
            parts.append(spec)
    return ", ".join(parts) if parts else "-"


def add_custom_snipe(
    ymd: str,
    origin: str,
    destination: str,
    start_time: str = "",
    end_time: str = "",
    target_pax: int | None = None,
    slots: list[str] | None = None,
) -> dict:
    """
    Add or narrow a custom one-off snipe rule and persist to custom_snipes.json.
    If an unbooked active rule already exists for the same (ymd, origin, destination),
    it updates that rule's slots in place so narrowing a schedule replaces the old window.
    """
    dt = datetime.datetime.strptime(ymd, "%Y%m%d").date()
    ktmb_date = format_ktmb_date(dt)
    display_date = dt.strftime("%a, %d %b %Y")
    norm_slots, norm_start, norm_end = _normalize_slots(slots, start_time, end_time)

    if dt < datetime.date.today():
        raise ValueError(f"Date {display_date} is already in the past.")
    if has_completed_booking_on_date(ktmb_date):
        raise ValueError(f"A ticket is already booked for {display_date}. Remove/clear it first if you want another.")

    pax_goal = target_pax if target_pax is not None else int(os.getenv("PAX_COUNT", "2"))
    with _CUSTOM_SNIPES_LOCK:
        rules: list[dict] = []
        if CUSTOM_SNIPES_FILE.exists():
            try:
                loaded = json.loads(CUSTOM_SNIPES_FILE.read_text())
                if isinstance(loaded, list):
                    rules = loaded
            except Exception:
                rules = []

        for existing in rules:
            if (
                existing.get("status") == "active"
                and existing.get("ymd") == ymd
                and existing.get("origin") == origin
                and existing.get("destination") == destination
            ):
                if int(existing.get("booked_pax", 0)) == 0:
                    existing["slots"] = norm_slots
                    existing["start_time"] = norm_start
                    existing["end_time"] = norm_end
                    existing["target_pax"] = pax_goal
                    _atomic_write_json(CUSTOM_SNIPES_FILE, rules)
                return existing

        rule = {
            "id": str(int(time.time() * 1000)),
            "ymd": ymd,
            "date": ktmb_date,
            "display_date": display_date,
            "origin": origin,
            "destination": destination,
            "start_time": norm_start,
            "end_time": norm_end,
            "slots": norm_slots,
            "target_pax": pax_goal,
            "booked_pax": 0,
            "locked_train_time": None,
            "skipped_times": [],
            "status": "active",
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        rules.append(rule)
        _atomic_write_json(CUSTOM_SNIPES_FILE, rules)
        return rule


def remove_custom_snipe(rule_id: str) -> dict | None:
    """Remove a custom snipe rule by id. Returns the removed rule or None."""
    with _CUSTOM_SNIPES_LOCK:
        if not CUSTOM_SNIPES_FILE.exists():
            return None
        try:
            rules = json.loads(CUSTOM_SNIPES_FILE.read_text())
        except Exception:
            return None
        if not isinstance(rules, list):
            return None
        removed = None
        kept = []
        for r in rules:
            if str(r.get("id")) == str(rule_id) and removed is None:
                removed = r
            else:
                kept.append(r)
        if removed is not None:
            _atomic_write_json(CUSTOM_SNIPES_FILE, kept)
        return removed


def _hhmm_to_minutes(hhmm: str) -> int | None:
    cleaned = hhmm.replace(":", "").strip()
    if len(cleaned) != 4 or not cleaned.isdigit():
        return None
    h, m = int(cleaned[:2]), int(cleaned[2:])
    if 0 <= h < 24 and 0 <= m < 60:
        return h * 60 + m
    return None


def _parse_time_token(tok: str) -> str | None:
    """Parse '19:00', '1900', '7pm', '7:30pm', '10am' into 'HH:MM'."""
    t = tok.strip().lower()
    m12 = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm)", t)
    if m12:
        h = int(m12.group(1))
        mins = int(m12.group(2) or "0")
        ampm = m12.group(3)
        if not (1 <= h <= 12 and 0 <= mins < 60):
            return None
        if ampm == "am":
            h = 0 if h == 12 else h
        else:
            h = 12 if h == 12 else h + 12
        return f"{h:02d}:{mins:02d}"

    m24 = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m24:
        h, mins = int(m24.group(1)), int(m24.group(2))
        if 0 <= h < 24 and 0 <= mins < 60:
            return f"{h:02d}:{mins:02d}"
        return None

    if re.fullmatch(r"\d{4}", t):
        h, mins = int(t[:2]), int(t[2:])
        if 0 <= h < 24 and 0 <= mins < 60:
            return f"{h:02d}:{mins:02d}"
    return None


def _parse_slot_expression(expr: str) -> list[str] | None:
    """
    Parse comma-separated time expressions like:
      '7pm'              -> ['19:00-19:59']
      '7pm,8pm,9pm'      -> ['19:00-19:59', '20:00-20:59', '21:00-21:59']
      '20:15,21:30'      -> ['20:15', '21:30']
      '19:00-22:00'      -> ['19:00-22:00']
      '7pm-10pm'         -> ['19:00-22:00']
    """
    items = [p.strip().lower() for p in expr.split(",") if p.strip()]
    if not items:
        return None
    slots: list[str] = []
    for item in items:
        if "-" in item:
            s_raw, e_raw = item.split("-", 1)
            s_t = _parse_time_token(s_raw)
            e_t = _parse_time_token(e_raw)
            if not s_t or not e_t or _hhmm_to_minutes(s_t) > _hhmm_to_minutes(e_t):
                return None
            slots.append(f"{s_t}-{e_t}")
        else:
            t_fmt = _parse_time_token(item)
            if not t_fmt:
                return None
            # Bare hour like '7pm' or '8am' covers that whole hour (e.g. 19:00-19:59)
            if re.fullmatch(r"\d{1,2}\s*(am|pm)", item):
                slots.append(f"{t_fmt[:2]}:00-{t_fmt[:2]}:59")
            else:
                slots.append(t_fmt)
    return slots


_SNIPE_WEEKDAY_MAP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _resolve_snipe_date(token: str, today: datetime.date | None = None) -> datetime.date | None:
    """Resolve 'YYYYMMDD', 'today', 'tomorrow', 'friday', 'this-friday', 'next-friday' into a date."""
    today = today or datetime.date.today()
    t = token.strip().lower()
    if re.fullmatch(r"\d{8}", t):
        try:
            return datetime.datetime.strptime(t, "%Y%m%d").date()
        except ValueError:
            return None
    if t == "today":
        return today
    if t == "tomorrow":
        return today + datetime.timedelta(days=1)

    next_week = False
    if t.startswith("next-") or t.startswith("next_"):
        next_week = True
        t = t[5:]
    elif t.startswith("this-") or t.startswith("this_"):
        t = t[5:]

    if t in _SNIPE_WEEKDAY_MAP:
        wd = _SNIPE_WEEKDAY_MAP[t]
        days_ahead = (wd - today.weekday()) % 7
        if next_week:
            days_ahead = days_ahead + 7 if days_ahead == 0 else days_ahead + 7
        return today + datetime.timedelta(days=days_ahead)
    return None


def parse_snipe_command(text: str, today: datetime.date | None = None) -> dict | None:
    """
    Parse `/snipe <date|weekday> <slots_or_range> [route]` into
    {ymd, date, display_date, origin, destination, start_time, end_time, slots}.
    Examples:
      /snipe friday 7pm,8pm,9pm woodlands-tebrau
      /snipe friday 20:15 woodlands-tebrau
      /snipe 20261120 19:00-22:00 woodlands-tebrau
    """
    cleaned = text.strip()
    parts = cleaned.split()
    if not parts:
        return None
    args = parts[1:] if parts[0].startswith("/") else parts
    if len(args) < 2:
        return None

    dt = _resolve_snipe_date(args[0], today=today)
    if dt is None:
        return None

    slots = _parse_slot_expression(args[1])
    if not slots:
        return None
    try:
        norm_slots, start_t, end_t = _normalize_slots(slots)
    except ValueError:
        return None

    if len(args) >= 3:
        route_tokens = [t for t in re.split(r"[-_/]+", args[2].lower()) if t]
        origin = None
        destination = None
        for tok in route_tokens:
            if tok in ("woodland", "woodlands", "sg", "singapore", "ciq"):
                station = "WOODLANDS CIQ"
            elif tok in ("tebrau", "jb", "johor", "sentral"):
                station = "JB SENTRAL"
            else:
                continue
            if origin is None:
                origin = station
            elif destination is None and station != origin:
                destination = station
        if origin is None:
            origin = "JB SENTRAL" if dt.weekday() == 6 else "WOODLANDS CIQ"
        if destination is None:
            destination = "JB SENTRAL" if origin == "WOODLANDS CIQ" else "WOODLANDS CIQ"
    else:
        if dt.weekday() == 6:
            origin, destination = "JB SENTRAL", "WOODLANDS CIQ"
        else:
            origin, destination = "WOODLANDS CIQ", "JB SENTRAL"

    return {
        "ymd": dt.strftime("%Y%m%d"),
        "date": format_ktmb_date(dt),
        "display_date": dt.strftime("%a, %d %b %Y"),
        "origin": origin,
        "destination": destination,
        "start_time": start_t,
        "end_time": end_t,
        "slots": norm_slots,
    }


def get_times_in_range(page: Page, section_selector: str, start_time: str, end_time: str) -> list[str]:
    """Return sorted 'HH:MM' departure times visible on the page within [start_time, end_time]."""
    start_min = _hhmm_to_minutes(start_time)
    end_min = _hhmm_to_minutes(end_time)
    if start_min is None or end_min is None:
        return []

    try:
        hms = page.eval_on_selector_all(
            f"{section_selector} tbody tr[data-hourminute]",
            "els => els.map(e => e.getAttribute('data-hourminute'))",
        )
    except Exception:
        return []

    matched: list[tuple[int, str]] = []
    seen: set[str] = set()
    for hm in hms or []:
        if not hm:
            continue
        mins = _hhmm_to_minutes(str(hm))
        if mins is not None and start_min <= mins <= end_min:
            fmt = f"{mins // 60:02d}:{mins % 60:02d}"
            if fmt not in seen:
                seen.add(fmt)
                matched.append((mins, fmt))
    matched.sort(key=lambda item: item[0])
    return [fmt for _, fmt in matched]


def get_times_for_rule(page: Page, section_selector: str, rule: dict) -> list[str]:
    """Return sorted 'HH:MM' departure times visible on the page matching `rule['slots']` (or start/end)."""
    slots = rule.get("slots")
    if not slots:
        return get_times_in_range(page, section_selector, rule["start_time"], rule["end_time"])

    try:
        hms = page.eval_on_selector_all(
            f"{section_selector} tbody tr[data-hourminute]",
            "els => els.map(e => e.getAttribute('data-hourminute'))",
        )
    except Exception:
        return []

    available_mins: list[tuple[int, str]] = []
    seen_avail: set[str] = set()
    for hm in hms or []:
        if not hm:
            continue
        mins = _hhmm_to_minutes(str(hm))
        if mins is not None:
            fmt = f"{mins // 60:02d}:{mins % 60:02d}"
            if fmt not in seen_avail:
                seen_avail.add(fmt)
                available_mins.append((mins, fmt))

    matched: list[tuple[int, str]] = []
    seen_matched: set[str] = set()
    for slot in slots:
        if "-" in slot:
            s_part, e_part = slot.split("-", 1)
            s_min = _hhmm_to_minutes(s_part)
            e_min = _hhmm_to_minutes(e_part)
            if s_min is None or e_min is None:
                continue
            for mins, fmt in available_mins:
                if s_min <= mins <= e_min and fmt not in seen_matched:
                    seen_matched.add(fmt)
                    matched.append((mins, fmt))
        else:
            exact_min = _hhmm_to_minutes(slot)
            if exact_min is None:
                continue
            fmt = f"{exact_min // 60:02d}:{exact_min % 60:02d}"
            if fmt in seen_avail and fmt not in seen_matched:
                seen_matched.add(fmt)
                matched.append((exact_min, fmt))

    matched.sort(key=lambda item: item[0])
    return [fmt for _, fmt in matched]


def fetch_day_timetable(ymd: str, origin: str, destination: str) -> list[dict]:
    """Fetch live train departure times and seat counts for one date & route using headless Chromium."""
    dt = datetime.datetime.strptime(ymd, "%Y%m%d").date()
    ktmb_date = format_ktmb_date(dt)
    trains: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            submit_search(page, origin, destination, ktmb_date, "", pax=1)
            rows = page.locator(".DepartSection tbody tr[data-hourminute]")
            count = rows.count()
            for idx in range(count):
                row = rows.nth(idx)
                hm = (row.get_attribute("data-hourminute") or "").strip()
                mins = _hhmm_to_minutes(hm)
                if mins is None:
                    continue
                time_fmt = f"{mins // 60:02d}:{mins % 60:02d}"
                seats_text = row.locator("td").nth(4).inner_text().strip()
                seats_match = re.search(r"(\d+)", seats_text)
                seats = int(seats_match.group(1)) if seats_match else 0
                trains.append({"time": time_fmt, "hm": f"{mins // 60:02d}{mins % 60:02d}", "seats": seats})
        finally:
            browser.close()
    trains.sort(key=lambda x: x["hm"])
    return trains


def format_ktmb_date(d: datetime.date) -> str:
    """Format date as 'D MMM YYYY' without leading zero on day (e.g. '15 Nov 2026' or '7 Feb 2027')."""
    return f"{d.day} {d.strftime('%b %Y')}"


def get_upcoming_weekday_dates(weekday: int, max_weeks: int = 26) -> list[str]:
    """
    Generate upcoming dates for a specific weekday (5 = Saturday, 6 = Sunday)
    up to `max_weeks` ahead.
    """
    today = datetime.date.today()
    days_ahead = (weekday - today.weekday()) % 7
    if days_ahead == 0 and datetime.datetime.now().hour >= 20:
        days_ahead = 7
    first_date = today + datetime.timedelta(days=days_ahead)
    return [format_ktmb_date(first_date + datetime.timedelta(days=w * 7)) for w in range(max_weeks)]


def wait_for_trips_loaded(page: Page, timeout_ms: int = 15_000):
    """Wait until KTMB's RenderTrip AJAX finishes loading train rows and hides the loading modal."""
    try:
        page.wait_for_function(
            """() => {
                const rows = document.querySelectorAll('.depart-trips tr');
                const modalVisible = document.querySelector('#popupModal.show');
                return rows.length > 0 && !modalVisible;
            }""",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(250)


def ensure_logged_in(page: Page, email: str, password: str):
    """
    Handle KTMB's cross-domain SSO (`online.ktmb.com.my/Account/Login?q=...`)
    when redirected from `.btn-login` on `/ShuttleTrip`.
    """
    if "/Account/Login" not in page.url:
        return

    print(f"[*] On KTMB SSO Login page ({page.url[:60]}...)...")
    if email and password and email != "your_email@example.com":
        print(f"[*] Submitting KITS login for {email}...")
        page.fill("#Email", email)
        page.fill("#Password", password)
        page.click("#LoginButton")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1500)

        err_modal = page.locator("#validationSummaryModalBody")
        if err_modal.count() > 0 and err_modal.is_visible():
            err_text = err_modal.inner_text().strip()
            if err_text:
                notify("KTMB Login Error", f"KITS rejected login: {err_text}", page=page)
                sys.exit(1)
    else:
        vnc_url = os.getenv("VNC_PUBLIC_URL", "")
        notify(
            "KTMB Bot Login Needed",
            f"Please log in to your KITS account in the browser. {vnc_url}",
            page=page,
        )

    try:
        page.wait_for_url("**/ShuttleTrip**", timeout=120_000)
        wait_for_trips_loaded(page)
        print("[+] SSO Login successful! Returned to /ShuttleTrip with active session!")
    except PlaywrightTimeoutError:
        notify("KTMB Login Failed", f"Login timed out at URL: {page.url}. Check credentials in .env.", page=page)
        sys.exit(1)


def submit_search(
    page: Page,
    origin: str,
    destination: str,
    onward_date: str,
    return_date: str = "",
    pax: int = 1,
):
    """Fill the KTMB Shuttle search form and navigate to /ShuttleTrip (no login required to poll seats!)."""
    print(f"[*] Initializing search session on {SHUTTLE_URL}...")
    page.goto(SHUTTLE_URL, wait_until="domcontentloaded")
    page.wait_for_selector("#FromStationId", state="visible", timeout=15_000)
    page.wait_for_timeout(400)

    current_from = page.input_value("#FromStationId").strip().upper()
    if current_from != origin.upper():
        print(f"[*] Setting direction: {origin} -> {destination}")
        page.evaluate("SwapFromToTerminal()")
        page.wait_for_timeout(200)

    print(
        f"[*] Submitting search: {origin} -> {destination} | Date: {onward_date} | Pax: {pax}"
    )
    page.select_option("#PassengerCount", str(pax))
    page.evaluate(
        """([onward, ret]) => {
            const onInput = document.getElementById("OnwardDate");
            const retInput = document.getElementById("ReturnDate");
            if (onInput) onInput.value = onward;
            if (retInput) retInput.value = ret;
            if (window.jQuery && $('#theForm').length) {
                $('#theForm').submit();
            } else {
                document.getElementById('btnSubmit').click();
            }
        }""",
        [onward_date, return_date],
    )

    page.wait_for_url("**/ShuttleTrip**", timeout=30_000)
    wait_for_trips_loaded(page)


def switch_date_in_place(page: Page, target_date: str, return_date: str = "") -> bool:
    """
    Fast AJAX switch to `target_date` on `/ShuttleTrip` using KTMB's `ChangeDate()`.
    Returns `True` if train timetable rows exist for `target_date`, or `False` if
    we have reached the end of KTMB's released booking calendar!
    """
    page.evaluate(
        """([onwardDate, returnDate]) => {
            const searchData = $(id_formSearchData).val();
            ChangeDate(searchData, onwardDate, false, 1);
            if (returnDate && returnDate.length > 0) {
                setTimeout(() => {
                    ChangeDate(searchData, returnDate, true, 2);
                }, 250);
            }
        }""",
        [target_date, return_date],
    )
    try:
        page.wait_for_function(
            f"() => {{ const el = document.querySelector('.depart-date-input'); return el && el.innerText.includes('{target_date}'); }}",
            timeout=12_000,
        )
    except Exception:
        pass
    page.wait_for_timeout(300)
    wait_for_trips_loaded(page)

    # Check if trains are actually released on this date (released dates have > 3 train rows with data-hourminute)
    train_rows = page.locator(".DepartSection tbody tr[data-hourminute]").count()
    return train_rows > 0


def find_matching_train_button(
    page: Page,
    section_selector: str,
    preferred_times: list[str],
    min_seats_required: int = 1,
    date_label: str = "",
):
    """
    Scan `.DepartSection` or `.ReturnSection` for the first available train matching `preferred_times`.
    Works whether logged in (`.btn-seat-layout`) OR not logged in yet (`.btn-login`)!
    Returns `(button_locator, time_str, seats_avail, is_login_btn)`.
    """
    status_parts = []
    found_result = (None, None, 0, False)

    for time_str in preferred_times:
        hm = time_str.replace(":", "").strip()
        row_selector = f"{section_selector} tbody tr[data-hourminute='{hm}']"
        row = page.locator(row_selector)
        if row.count() == 0:
            status_parts.append(f"{time_str}:N/A")
            continue

        row_el = row.first
        seats_text = row_el.locator("td").nth(4).inner_text().strip()
        seats_match = re.search(r"(\d+)", seats_text)
        seats_avail = int(seats_match.group(1)) if seats_match else 0
        status_parts.append(f"{time_str}:{seats_avail}")

        if seats_avail >= min_seats_required and found_result[0] is None:
            train_name = row_el.locator("td").nth(0).inner_text().strip()
            select_btn = row_el.locator(".btn-seat-layout:not(.disabled)")
            login_btn = row_el.locator(".btn-login")
            if select_btn.count() > 0:
                print(f"    [+] {date_label} -> FOUND {train_name} at {time_str} ({seats_avail} seats!)")
                found_result = (select_btn.first, time_str, seats_avail, False)
            elif login_btn.count() > 0:
                print(f"    [+] {date_label} -> FOUND {train_name} at {time_str} ({seats_avail} seats!)")
                found_result = (login_btn.first, time_str, seats_avail, True)

    summary = " | ".join(status_parts)
    print(f"    [{date_label:<12}] {summary}")
    return found_result


def get_vnc_url() -> str:
    vnc_url = os.getenv("VNC_PUBLIC_URL", "").strip()
    if not vnc_url or "your-lxc-ip" in vnc_url:
        return "http://100.108.209.57:6080/vnc.html"
    return vnc_url


def transcribe_audio_groq(audio_url: str, groq_api_key: str) -> str:
    """Download reCAPTCHA audio challenge MP3 and transcribe digits using Groq Whisper."""
    print(f"[*] Downloading reCAPTCHA audio challenge from Google...")
    req = urllib.request.Request(audio_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    audio_data = urllib.request.urlopen(req, timeout=15).read()

    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body = []
    body.append(f"--{boundary}\r\n".encode())
    body.append(b'Content-Disposition: form-data; name="file"; filename="audio.mp3"\r\n')
    body.append(b"Content-Type: audio/mp3\r\n\r\n")
    body.append(audio_data)
    body.append(b"\r\n")
    body.append(f"--{boundary}\r\n".encode())
    body.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
    body.append(b"whisper-large-v3-turbo\r\n")
    body.append(f"--{boundary}\r\n".encode())
    body.append(b'Content-Disposition: form-data; name="response_format"\r\n\r\n')
    body.append(b"json\r\n")
    body.append(f"--{boundary}--\r\n".encode())

    payload = b"".join(body)
    groq_req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=payload,
        headers={
            "Authorization": f"Bearer {groq_api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    resp = urllib.request.urlopen(groq_req, timeout=20)
    res_json = json.loads(resp.read().decode("utf-8"))
    transcribed_text = res_json.get("text", "").strip()
    print(f"[+] Groq Whisper transcribed audio: '{transcribed_text}'")
    return transcribed_text


def try_solve_audio_recaptcha(page: Page, groq_api_key: str) -> bool:
    """Attempts to auto-solve reCAPTCHA v2 using audio challenge + Groq Whisper."""
    if not groq_api_key:
        return False

    try:
        bframe = page.frame_locator("iframe[title*='recaptcha challenge']").first
        audio_btn = bframe.locator("#recaptcha-audio-button")
        if audio_btn.count() == 0 or not audio_btn.is_visible():
            return False

        print("[*] reCAPTCHA challenge detected! Switching to Audio challenge for Groq Whisper...")
        audio_btn.click()
        page.wait_for_timeout(1500)

        # Check if Google blocked audio ("Your computer or network may be sending automated queries")
        doscaptcha = bframe.locator(".rc-doscaptcha-header")
        if doscaptcha.count() > 0 and doscaptcha.is_visible():
            print("[!] Google blocked audio challenge on this IP (doscaptcha). Falling back to NoVNC.")
            return False

        # Find audio download link or source
        audio_source = bframe.locator("#audio-source, a.rc-audiochallenge-tdownload-link")
        if audio_source.count() == 0:
            return False

        audio_url = audio_source.first.get_attribute("src") or audio_source.first.get_attribute("href")
        if not audio_url:
            return False

        text = transcribe_audio_groq(audio_url, groq_api_key)
        if not text:
            return False

        input_box = bframe.locator("#audio-response")
        input_box.fill(text)
        page.wait_for_timeout(300)

        verify_btn = bframe.locator("#recaptcha-verify-button")
        verify_btn.click()
        print("[+] Clicked verify on audio challenge! Waiting for KTMB reservation...")
        page.wait_for_timeout(2000)
        return True
    except Exception as e:
        print(f"[!] Audio solver note: {e}")
        return False


def reserve_and_wait_passenger_page(page: Page, timeout_sec: int = 180) -> bool:
    """
    After clicking Select on a train, handles waiting for reCAPTCHA (auto-solving via Groq Whisper
    if GROQ_API_KEY is configured, or sending Telegram alert with NoVNC link if visual challenge appears),
    clicking .btn-passenger, and waiting for the page to navigate to /BookShuttle.
    """
    start_time = time.time()
    vnc_alert_sent = False
    groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
    print("[*] Waiting for seat reservation / reCAPTCHA / navigation to /BookShuttle...")

    while time.time() - start_time < timeout_sec:
        if "/BookShuttle" in page.url:
            print("[+] Successfully navigated to /BookShuttle!")
            return True

        # Check for reCAPTCHA modal
        modal = page.locator("#recaptcha-modal")
        if modal.count() > 0 and modal.is_visible():
            # 1. Try clicking the reCAPTCHA checkbox automatically
            try:
                frame = page.frame_locator("iframe[title='reCAPTCHA']").first
                checkbox = frame.locator("#recaptcha-anchor")
                if checkbox.count() > 0 and checkbox.is_visible():
                    checkbox.click()
                    page.wait_for_timeout(1200)
            except Exception:
                pass

            # 2. If challenge appears and Groq API key is set, try audio auto-solve!
            if groq_api_key:
                solved = try_solve_audio_recaptcha(page, groq_api_key)
                if solved:
                    page.wait_for_timeout(1500)
                    if "/BookShuttle" in page.url:
                        return True

            # 3. Fallback alert to Telegram with NoVNC link
            if not vnc_alert_sent:
                vnc_url = get_vnc_url()
                notify(
                    "reCAPTCHA Challenge",
                    f"Train found! Please solve the reCAPTCHA in NoVNC to lock the seat: {vnc_url}",
                    page=page,
                )
                vnc_alert_sent = True

        # Check for .btn-passenger
        proceed_btn = page.locator(".btn-passenger")
        if proceed_btn.count() > 0 and proceed_btn.is_visible():
            try:
                proceed_btn.click()
            except Exception:
                pass

        page.wait_for_timeout(1000)

    notify("Reservation Timeout", "Timed out waiting for reCAPTCHA to be solved or /BookShuttle to load.", page=page)
    return False


def _first_visible(page: Page, selectors: list[str]):
    """Return (locator, selector) for the first visible match among `selectors`, else (None, None)."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 10)):
                item = loc.nth(i)
                if item.is_visible():
                    return item, sel
        except Exception:
            continue
    return None, None


def _log_clickables(page: Page) -> None:
    """Print visible buttons/links/images so a stuck payment page can be diagnosed from the log."""
    try:
        items = page.evaluate(
            """() => [...document.querySelectorAll('button,a,input[type=button],input[type=submit],img,label')]
                .filter(e => e.offsetParent !== null)
                .map(e => e.tagName + ':' + (e.innerText || e.value || e.alt || e.getAttribute('src') || '').trim().slice(0, 60))
                .slice(0, 60)"""
        )
        print(f"[*] Visible clickables on page: {items}")
    except Exception:
        pass


WALLET_SELECTORS = [
    "img[alt*='wallet' i]",
    "img[src*='wallet' i]",
    "[class*='wallet' i]:visible",
    "[id*='wallet' i]:visible",
    "text=/^\\s*KTM Wallet\\s*$/i",
    # Fallback: the first image after the "Payment Options" heading is the KTM Wallet card.
    "xpath=(//*[normalize-space(text())='Payment Options']/following::img)[1]",
]

PAY_BUTTON_SELECTORS = [
    "button:text-matches('^\\\\s*(pay now|pay|bayar|confirm|submit|proceed|continue)\\\\s*$', 'i')",
    "a:text-matches('^\\\\s*(pay now|pay|bayar|confirm|submit|proceed|continue)\\\\s*$', 'i')",
    "input[type='submit'][value*='Pay' i], input[type='button'][value*='Pay' i]",
    "input[type='submit'][value*='Confirm' i], input[type='button'][value*='Confirm' i]",
    "#btnPay, #btnConfirm",
]

PIN_SELECTORS = [
    "input[type='password']",
    "input[id*='pin' i]",
    "input[name*='pin' i]",
    "input[placeholder*='pin' i]",
]


def _checkout_ktm_wallet(page: Page, wallet_pin: str, date_label: str, train_time: str) -> bool:
    """On the Payment page: pick the KTM Wallet card, accept T&C, enter PIN, press Pay/Confirm."""
    try:
        page.wait_for_selector("text=/Payment Options/i", timeout=20_000)
    except Exception as e:
        print(f"[*] 'Payment Options' not seen yet: {e}")
    page.wait_for_timeout(800)

    # The wallet cards sit at the bottom of the page.
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
    except Exception:
        pass

    wallet, used = _first_visible(page, WALLET_SELECTORS)
    if wallet is None:
        print("[!] Could not find the KTM Wallet card on the payment page.")
        _log_clickables(page)
        return False

    print(f"[*] Clicking KTM Wallet card (matched {used!r})...")
    try:
        wallet.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        wallet.click(timeout=5000)
    except Exception:
        wallet.click(force=True)
    page.wait_for_timeout(1500)
    notify(
        "KTM Wallet selected",
        f"Clicked KTM Wallet for {date_label} ({train_time}). Continuing checkout...",
        page=page,
    )

    # Walk through whatever follows (T&C tick, PIN prompt, Pay / Confirm buttons).
    pin_filled = False
    pay_clicked = False
    clicked_keys: set[tuple[str, bool]] = set()
    for step in range(1, 7):
        page.wait_for_timeout(800)

        # T&C / consent checkboxes
        try:
            boxes = page.locator("input[type='checkbox']")
            for i in range(boxes.count()):
                cb = boxes.nth(i)
                if cb.is_visible() and not cb.is_checked():
                    cb.check(force=True)
                    print("[+] Ticked a checkbox on payment page")
        except Exception:
            pass

        # PIN prompt
        if wallet_pin and not pin_filled:
            pin_input, _ = _first_visible(page, PIN_SELECTORS)
            if pin_input is not None:
                print("[*] Entering KTM Wallet PIN...")
                pin_input.click()
                pin_input.fill(wallet_pin)
                pin_filled = True
                page.wait_for_timeout(300)

        # Pay / Confirm button
        btn, used = _first_visible(page, PAY_BUTTON_SELECTORS)
        if btn is None:
            if pay_clicked:
                break
            print(f"[*] Step {step}: no Pay/Confirm button visible yet")
            _log_clickables(page)
            continue
        try:
            label = (btn.inner_text() or btn.get_attribute("value") or "").strip().lower()
        except Exception:
            label = ""
        # Never press the same button twice in the same state (avoids paying twice);
        # a second press is only allowed after a new PIN prompt was filled or a different button shows up.
        click_key = (label, pin_filled)
        if click_key in clicked_keys or len(clicked_keys) >= 3:
            print(f"[*] Step {step}: '{label}' already pressed, waiting for the result instead of pressing again")
            break
        clicked_keys.add(click_key)
        print(f"[*] Step {step}: clicking '{label}' ({used!r})...")
        try:
            btn.click(timeout=5000)
        except Exception:
            btn.click(force=True)
        pay_clicked = True
        page.wait_for_timeout(2500)

    if pay_clicked:
        page.wait_for_timeout(2000)
        notify(
            "🎉 KTMB Payment Submitted",
            f"KTM Wallet payment submitted for {date_label} ({train_time}). Check the screenshot / your email to confirm.",
            page=page,
        )
        return True
    return False


def automate_passenger_and_checkout(
    page: Page,
    payment_method: str,
    wallet_pin: str,
    passenger_matches: list[str] | None = None,
    date_label: str = "",
    train_time: str = "",
) -> bool:
    """
    Complete `/BookShuttle` passenger selection and KTM Wallet payment.
    """
    if "/BookShuttle" not in page.url:
        try:
            page.wait_for_url("**/BookShuttle**", timeout=15_000)
        except PlaywrightTimeoutError:
            return False

    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)

    # 1. Fill Passenger 1: Check "I am travelling"
    try:
        travelling_cb = page.locator("input[type='checkbox']").first
        if travelling_cb.count() > 0 and not travelling_cb.is_checked():
            print("[*] Checking 'I am travelling' for Passenger 1...")
            travelling_cb.check(force=True)
            page.wait_for_timeout(800)

        # Fallback click text if not checked
        if travelling_cb.count() > 0 and not travelling_cb.is_checked():
            page.locator("text='I am travelling'").first.click(force=True)
            page.wait_for_timeout(800)
    except Exception as e:
        print(f"[*] 'I am travelling' checkbox note: {e}")

    # 2. Fill the OTHER passengers from their "Select Friends and Family" dropdown.
    #    Passenger 1 is the logged-in account holder ("I am travelling"), so the first
    #    dropdown is left untouched. Dropdown 2, 3, ... get the configured names in order,
    #    never reusing one (KTMB rejects "Duplicated passport number").
    try:
        names = [n for n in (passenger_matches or []) if n]
        used_labels: set[str] = set()
        dropdown_no = 0
        all_selects = page.locator("select").all()
        for sel in all_selects:
            try:
                opts_text = [t.strip() for t in sel.locator("option").all_inner_texts()]
                if not any("friends" in o.lower() or "family" in o.lower() for o in opts_text):
                    continue
                dropdown_no += 1
                if dropdown_no == 1:
                    print("[*] Passenger 1 dropdown left as-is (using 'I am travelling')")
                    continue
                print(f"[*] Found passenger {dropdown_no} dropdown with options: {opts_text}")

                def _pick(label_filter) -> int | None:
                    for idx, opt_label in enumerate(opts_text):
                        if idx == 0 or not opt_label or opt_label in used_labels:
                            continue
                        if label_filter(opt_label):
                            return idx
                    return None

                chosen = None
                for target_name in names:
                    chosen = _pick(lambda o, t=target_name.lower(): t in o.lower())
                    if chosen is not None:
                        break
                # Only guess a saved passenger when no names are configured at all;
                # a wrong person on a ticket is worse than leaving it blank.
                if chosen is None and not names:
                    chosen = _pick(lambda o: True)

                if chosen is not None:
                    sel.select_option(index=chosen)
                    sel.dispatch_event("change")
                    used_labels.add(opts_text[chosen])
                    print(f"[+] Passenger {dropdown_no}: selected '{opts_text[chosen]}'")
                else:
                    print(f"[!] Passenger {dropdown_no}: no matching saved passenger for {names}")
                page.wait_for_timeout(600)
            except Exception as ex:
                print(f"[*] Friends & Family dropdown item error: {ex}")
    except Exception as e:
        print(f"[*] Friends and Family selection note: {e}")

    # Make sure "I am travelling" is still ticked for Passenger 1.
    try:
        travelling_cb = page.locator("input[type='checkbox']").first
        if travelling_cb.count() > 0 and not travelling_cb.is_checked():
            print("[*] Re-checking 'I am travelling' for Passenger 1...")
            travelling_cb.check(force=True)
            page.wait_for_timeout(800)
    except Exception as e:
        print(f"[*] 'I am travelling' re-check note: {e}")

    # 3. Ensure "Ticket type *" (Adult / Dewasa) is selected for all passengers
    try:
        all_selects = page.locator("select").all()
        for sel in all_selects:
            try:
                opts_text = [t.strip() for t in sel.locator("option").all_inner_texts()]
                if any("adult" in o.lower() or "dewasa" in o.lower() for o in opts_text):
                    for idx, opt_label in enumerate(opts_text):
                        if "adult" in opt_label.lower() or "dewasa" in opt_label.lower():
                            sel.select_option(index=idx)
                            sel.dispatch_event("change")
                            print(f"[+] Selected Ticket Type '{opt_label}'")
                            break
                    page.wait_for_timeout(300)
            except Exception as ex:
                print(f"[*] Ticket type item error: {ex}")
    except Exception as e:
        print(f"[*] Ticket type selection note: {e}")

    # 4. Check any Terms and Conditions checkboxes
    try:
        tnc_boxes = page.locator(
            "input[type='checkbox'][id*='Term'], input[type='checkbox'][name*='Term'], input[type='checkbox'][id*='Agree']"
        )
        for i in range(tnc_boxes.count()):
            box = tnc_boxes.nth(i)
            if box.is_visible() and not box.is_checked():
                box.check(force=True)
    except Exception:
        pass

    page.wait_for_timeout(800)

    # Send screenshot with filled passenger details!
    notify(
        "KTMB Passenger Details Filled",
        f"Locked {date_label} ({train_time}) in cart for 10 mins! Passenger details filled for {passenger_matches}. Proceeding to payment...",
        page=page,
    )

    # 5. Click the yellow "PROCEED TO PAYMENT" button
    print("[*] Clicking yellow 'PROCEED TO PAYMENT' button...")
    try:
        proceed_btn = page.locator(
            "button:has-text('PROCEED TO PAYMENT'), a:has-text('PROCEED TO PAYMENT'), .btn:has-text('PROCEED TO PAYMENT'), input[value*='PROCEED TO PAYMENT'], button:has-text('PROCEED'), a:has-text('PROCEED'), #btnProceed, #btnSubmit"
        ).first
        if proceed_btn.count() > 0 and proceed_btn.is_visible():
            proceed_btn.click()
            print("[+] Clicked PROCEED TO PAYMENT!")
    except Exception as e:
        print(f"[!] Error clicking PROCEED TO PAYMENT: {e}")

    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)

    # 6. KTM Wallet checkout on the payment page
    if payment_method.upper() == "KTM_WALLET":
        print("[*] Proceeding with automated KTM Wallet checkout...")
        try:
            if _checkout_ktm_wallet(page, wallet_pin, date_label, train_time):
                return True
        except Exception as e:
            print(f"[!] Partial auto-checkout note: {e}")

    vnc_url = get_vnc_url()
    notify(
        "Complete Checkout",
        f"Seat for {date_label} ({train_time}) is held in cart for 10 mins. Finish checkout at {vnc_url}",
        page=page,
    )
    try:
        page.wait_for_url("**/Payment/**|**/Summary**", timeout=600_000)
        return True
    except PlaywrightTimeoutError:
        return False


def scan_custom_snipes(
    page: Page,
    total_pax: int,
    split_pax: bool,
    payment_method: str,
    wallet_pin: str,
    pax_names: list[str],
    email: str = "",
    password: str = "",
) -> bool:
    """
    Scan all active one-off custom snipes (e.g. Friday 19:00-22:00) and book the earliest
    available train in each rule's window. Never buys more than one slot on the same day.
    Returns True if at least one active custom snipe was scanned.
    """
    today = datetime.date.today()
    today_ymd = today.strftime("%Y%m%d")

    rules = load_custom_snipes()
    changed = False
    active_rules: list[dict] = []

    for rule in rules:
        if rule.get("status") != "active":
            continue
        if str(rule.get("ymd", "")) < today_ymd:
            rule["status"] = "expired"
            changed = True
            continue
        target_date = rule.get("date", "")
        rule_target_pax = int(rule.get("target_pax", total_pax))
        if int(rule.get("booked_pax", 0)) >= rule_target_pax or has_completed_booking_on_date(target_date):
            rule["status"] = "completed"
            changed = True
            continue
        active_rules.append(rule)

    if changed:
        save_custom_snipes(rules)

    if not active_rules:
        return False

    # Group active custom snipes by (origin, destination) to minimize page reloads
    by_route: dict[tuple[str, str], list[dict]] = {}
    for rule in sorted(active_rules, key=lambda r: str(r.get("ymd", ""))):
        key = (rule["origin"], rule["destination"])
        by_route.setdefault(key, []).append(rule)

    for (origin, destination), route_rules in by_route.items():
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        print(
            f"\n==================== [Custom Snipe @ {now_str}] Scanning {origin} -> {destination} ({len(route_rules)} date(s)) ===================="
        )
        first_rule = route_rules[0]
        first_target = int(first_rule.get("target_pax", total_pax))
        first_remaining = max(1, first_target - int(first_rule.get("booked_pax", 0)))
        first_search_pax = 1 if split_pax else first_remaining
        submit_search(page, origin, destination, first_rule["date"], "", pax=first_search_pax)

        for idx, rule in enumerate(route_rules):
            target_date = rule["date"]
            rule_id = rule["id"]
            rule_target_pax = int(rule.get("target_pax", total_pax))
            booked_so_far = int(rule.get("booked_pax", 0))
            locked_time = rule.get("locked_train_time")

            if booked_so_far >= rule_target_pax or has_completed_booking_on_date(target_date):
                continue

            remaining_pax = max(1, rule_target_pax - booked_so_far)
            search_pax = 1 if split_pax else remaining_pax

            # If search_pax differs from first_search_pax, re-submit search with exact pax
            if idx > 0 and search_pax != first_search_pax:
                submit_search(page, origin, destination, target_date, "", pax=search_pax)
                first_search_pax = search_pax
            elif idx > 0:
                is_released = switch_date_in_place(page, target_date, "")
                if not is_released:
                    print(f"    [{target_date:<12}] Not released yet on KTMB calendar.")
                    continue
            else:
                if page.locator(".DepartSection tbody tr[data-hourminute]").count() == 0:
                    print(f"    [{target_date:<12}] Not released yet on KTMB calendar.")
                    continue

            skipped_times = set(rule.get("skipped_times", []))
            if locked_time:
                candidate_times = [locked_time]
            else:
                candidate_times = get_times_for_rule(page, ".DepartSection", rule)
            times_to_check = [
                t
                for t in candidate_times
                if t not in skipped_times
                and (hours_until_train(target_date, t) is None or hours_until_train(target_date, t) > 0)
            ]
            if not times_to_check:
                print(
                    f"    [{target_date:<12}] No upcoming trains matching slots [{format_slots_summary(rule)}]"
                )
                continue

            btn, chosen_time, seats_avail, is_login_btn = find_matching_train_button(
                page,
                ".DepartSection",
                times_to_check,
                min_seats_required=search_pax,
                date_label=target_date,
            )

            if btn:
                if is_login_btn:
                    print(f"[!] Custom snipe seat found on {target_date} {chosen_time} ({seats_avail} seats)! Logging in via SSO...")
                    btn.click()
                    page.wait_for_url("**/Account/Login**", timeout=30_000)
                    ensure_logged_in(page, email, password)
                    switch_date_in_place(page, target_date, "")
                    btn, chosen_time, seats_avail, _ = find_matching_train_button(
                        page,
                        ".DepartSection",
                        [chosen_time],
                        min_seats_required=search_pax,
                        date_label=target_date,
                    )

                if btn:
                    print(f"[!] Custom snipe clicking Select on {target_date} {chosen_time} ({seats_avail} available)...")
                    btn.click()
                    nav_ok = reserve_and_wait_passenger_page(page, timeout_sec=180)
                    if not nav_ok:
                        print("[!] Did not reach /BookShuttle in time. Continuing scan...")
                        break

                    if search_pax >= 2:
                        current_pax_match = [p for p in pax_names if p]
                    else:
                        current_pax_match = [pax_names[booked_so_far]] if booked_so_far < len(pax_names) else None

                    ok = automate_passenger_and_checkout(
                        page,
                        payment_method,
                        wallet_pin,
                        current_pax_match,
                        date_label=target_date,
                        train_time=chosen_time,
                    )
                    if ok:
                        booked_so_far += search_pax
                        latest_rules = load_custom_snipes()
                        for r in latest_rules:
                            if str(r.get("id")) == str(rule_id):
                                r["booked_pax"] = booked_so_far
                                r["locked_train_time"] = chosen_time
                                if booked_so_far >= rule_target_pax:
                                    r["status"] = "completed"
                                break
                        save_custom_snipes(latest_rules)

                        if booked_so_far >= rule_target_pax:
                            state = load_state()
                            state[f"CUSTOM_{target_date}"] = {
                                "booked_pax": booked_so_far,
                                "locked_train_time": chosen_time,
                                "route": f"{origin} -> {destination}",
                            }
                            save_state(state)

                        status_note = (
                            "Day complete — will not buy another ticket on this date!"
                            if booked_so_far >= rule_target_pax
                            else f"Locked train {chosen_time} — waiting for remaining {rule_target_pax - booked_so_far} pax."
                        )
                        send_telegram(
                            f"🎯 Custom Snipe Booked ({booked_so_far}/{rule_target_pax} pax)!\n"
                            f"• Date: {rule.get('display_date', target_date)}\n"
                            f"• Train: {chosen_time}\n"
                            f"• Route: {origin} ➔ {destination}\n"
                            f"• {status_note}"
                        )
                    break

            time.sleep(0.4)

    return True


def scan_and_snipe_all_dates(
    page: Page,
    leg_prefix: str,
    origin: str,
    destination: str,
    candidate_dates: list[str],
    preferred_times: list[str],
    total_pax: int,
    split_pax: bool,
    poll_interval: float,
    payment_method: str,
    wallet_pin: str,
    pax_names: list[str],
    email: str = "",
    password: str = "",
    single_sweep: bool = False,
    is_manual_date: bool = False,
    max_weeks: int = 36,
) -> bool:
    """
    Continuously scans through `candidate_dates` (all upcoming Sundays or Saturdays
    until the website's last released date) and books up to `total_pax` tickets on each date!
    Returns True if a sweep was executed, False if there were no pending dates.
    """
    search_pax = 1 if split_pax else total_pax
    cycle = 1

    while True:
        if not is_manual_date:
            weekday = 5 if "SAT" in leg_prefix else 6
            active_dates = get_upcoming_weekday_dates(weekday, max_weeks=max_weeks)
        else:
            active_dates = candidate_dates

        state = load_state()
        # Filter out dates that already have total_pax booked (or completed via custom snipe)
        pending_dates = [
            d
            for d in active_dates
            if int(state.get(f"{leg_prefix}_{d}", {}).get("booked_pax", 0)) < total_pax
            and not has_completed_booking_on_date(d)
        ]
        if not pending_dates:
            print(f"[+] All target dates for {leg_prefix} have {total_pax}/{total_pax} tickets booked!")
            return False

        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        print(
            f"\n==================== [Cycle #{cycle} @ {now_str}] Scanning {origin} -> {destination} ===================="
        )

        # Initialize search on the first pending date
        submit_search(page, origin, destination, pending_dates[0], "", pax=search_pax)

        for idx, target_date in enumerate(pending_dates):
            leg_key = f"{leg_prefix}_{target_date}"
            state = load_state()
            leg_state = state.get(leg_key, {"booked_pax": 0, "locked_train_time": None})
            booked_so_far = int(leg_state.get("booked_pax", 0))
            locked_time = leg_state.get("locked_train_time")

            if booked_so_far >= total_pax or has_completed_booking_on_date(target_date):
                continue

            skipped_times = set(leg_state.get("skipped_times", []))
            times_to_check = [
                t for t in ([locked_time] if locked_time else preferred_times) if t not in skipped_times
            ]
            if not times_to_check:
                continue

            if idx > 0:
                is_released = switch_date_in_place(page, target_date, "")
                if not is_released:
                    print(f"    [{target_date:<12}] Reached end of KTMB released calendar. Wrapping back to start!")
                    break

            btn, chosen_time, seats_avail, is_login_btn = find_matching_train_button(
                page,
                ".DepartSection",
                times_to_check,
                min_seats_required=search_pax,
                date_label=target_date,
            )

            if btn:
                confirm_hours = float(os.getenv("CONFIRM_WITHIN_HOURS", "36"))
                hours_left = hours_until_train(target_date, chosen_time)
                if confirm_hours > 0 and hours_left is not None and hours_left <= confirm_hours:
                    confirm_timeout = int(os.getenv("CONFIRM_TIMEOUT_SECONDS", "300"))
                    approved = ask_user_confirm_buy(
                        target_date=target_date,
                        train_time=chosen_time,
                        origin=origin,
                        destination=destination,
                        seats_avail=seats_avail,
                        hours_left=hours_left,
                        timeout_sec=confirm_timeout,
                    )
                    if not approved:
                        skipped = list(leg_state.get("skipped_times", []))
                        if chosen_time not in skipped:
                            skipped.append(chosen_time)
                        leg_state["skipped_times"] = skipped
                        state[leg_key] = leg_state
                        save_state(state)
                        print(f"[*] Skipped {target_date} {chosen_time} (within {confirm_hours:g}h).")
                        continue

                    # Re-verify seat availability after waiting for user confirmation
                    switch_date_in_place(page, target_date, "")
                    btn, chosen_time, seats_avail, is_login_btn = find_matching_train_button(
                        page,
                        ".DepartSection",
                        [chosen_time],
                        min_seats_required=search_pax,
                        date_label=target_date,
                    )
                    if not btn:
                        send_telegram(
                            f"⚠️ Seat for {target_date} ({chosen_time}) is no longer available (taken while waiting for confirmation)."
                        )
                        continue

                if is_login_btn:
                    print(f"[!] Seat found on {target_date} {chosen_time} ({seats_avail} seats)! Logging in via SSO...")
                    btn.click()
                    page.wait_for_url("**/Account/Login**", timeout=30_000)
                    ensure_logged_in(page, email, password)
                    # Make sure we are viewing `target_date` after SSO redirect
                    switch_date_in_place(page, target_date, "")
                    btn, chosen_time, seats_avail, _ = find_matching_train_button(
                        page,
                        ".DepartSection",
                        [chosen_time],
                        min_seats_required=search_pax,
                        date_label=target_date,
                    )

                if btn:
                    print(f"[!] Clicking Select on {target_date} {chosen_time} ({seats_avail} available)...")
                    btn.click()
                    nav_ok = reserve_and_wait_passenger_page(page, timeout_sec=180)
                    if not nav_ok:
                        print("[!] Did not reach /BookShuttle in time. Continuing scan...")
                        break

                    if search_pax >= 2:
                        current_pax_match = [p for p in pax_names if p]
                    else:
                        current_pax_match = [pax_names[booked_so_far]] if booked_so_far < len(pax_names) else None
                    ok = automate_passenger_and_checkout(
                        page,
                        payment_method,
                        wallet_pin,
                        current_pax_match,
                        date_label=target_date,
                        train_time=chosen_time,
                    )
                    if ok:
                        booked_so_far += search_pax
                        leg_state["booked_pax"] = booked_so_far
                        leg_state["locked_train_time"] = chosen_time
                        state[leg_key] = leg_state
                        save_state(state)
                        print(
                            f"[+] Booked {booked_so_far}/{total_pax} for {target_date} at {chosen_time}! "
                            f"{'Week complete - will not buy another slot for this date!' if booked_so_far >= total_pax else ''}"
                        )
                    # Re-initialize search session on next loop after a checkout attempt
                    break

            time.sleep(0.4)

        cycle += 1
        maybe_recycle(page)
        pause = current_poll_interval(poll_interval)
        print(f"[*] Finished full calendar sweep. Waiting {pause:g}s before next sweep...")
        if sleep_or_wake(pause):
            maybe_recycle(page)
        if single_sweep:
            return True


def register_telegram_bot_commands(token: str):
    """Registers slash commands with Telegram so they appear in the autocomplete popup."""
    try:
        commands = [
            {"command": "help", "description": "Open interactive bot menu"},
            {"command": "menu", "description": "Open interactive bot menu"},
            {"command": "snipe", "description": "Add snipe: /snipe friday 7pm,8pm,9pm woodlands-tebrau"},
            {"command": "status", "description": "Show snipe schedule & booking state"},
            {"command": "buy", "description": "Instant buy: /buy YYYYMMDD HH:MM [route]"},
            {"command": "scrap_friday_woodlands_tebrau", "description": "PDF report: Fridays (Woodlands -> JB)"},
            {"command": "scrap_saturday_woodlands_tebrau", "description": "PDF report: Saturdays (Woodlands -> JB)"},
            {"command": "scrap_sunday_tebrau_woodlands", "description": "PDF report: Sundays (JB -> Woodlands)"},
            {"command": "update", "description": "Pull latest code from GitHub & restart bot"},
        ]
        data = urllib.parse.urlencode({"commands": json.dumps(commands)}).encode("utf-8")
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/setMyCommands", data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[*] Note registering bot commands: {e}")


def parse_buy_command(text: str):
    """Parses /buy commands into (ktmb_date, display_date, time_fmt, dep, arr)."""
    cleaned = text.strip()
    match = re.search(r"(\d{8})[-_\s]+(\d{2}:?\d{2})(?:[-_\s]+([a-zA-Z]+)[-_]([a-zA-Z]+))?", cleaned)
    if not match:
        return None
    raw_date, raw_time, raw_dep, raw_arr = match.group(1), match.group(2).replace(":", ""), match.group(3), match.group(4)
    time_fmt = f"{raw_time[:2]}:{raw_time[2:]}"
    y, m, d = int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8])
    dt = datetime.date(y, m, d)
    ktmb_date = f"{dt.day} {dt.strftime('%b %Y')}"
    display_date = dt.strftime("%a, %d %b %Y")

    if raw_dep and raw_arr:
        dep = "JB SENTRAL" if any(k in raw_dep.lower() for k in ["tebrau", "jb", "johor"]) else "WOODLANDS CIQ"
        arr = "WOODLANDS CIQ" if dep == "JB SENTRAL" else "JB SENTRAL"
    else:
        if dt.weekday() == 6 or int(raw_time[:2]) >= 17:
            dep, arr = "JB SENTRAL", "WOODLANDS CIQ"
        else:
            dep, arr = "WOODLANDS CIQ", "JB SENTRAL"

    return ktmb_date, display_date, time_fmt, dep, arr


def execute_direct_buy(text: str, chat_id: str | None = None):
    """Executes an instant on-demand ticket purchase triggered from Telegram."""
    parsed = parse_buy_command(text)
    if not parsed:
        send_telegram(
            "❌ Invalid /buy format.\n\n"
            "Format:\n/buy YYYYMMDD HH:MM [route]\n\n"
            "Examples:\n"
            "• /buy 20270221 22:45\n"
            "• /buy 20270207 20:15 tebrau-woodlands\n"
            "• /buy 20261120 19:15 woodlands-tebrau",
            chat_id=chat_id,
        )
        return

    ktmb_date, display_date, time_fmt, origin, destination = parsed
    email = os.getenv("KTMB_EMAIL", "")
    password = os.getenv("KTMB_PASSWORD", "")
    total_pax = int(os.getenv("PAX_COUNT", "2"))
    payment_method = os.getenv("PAYMENT_METHOD", "KTM_WALLET")
    wallet_pin = os.getenv("KTM_WALLET_PIN", "")
    pax_names = [
        os.getenv("PASSENGER_1_MATCH", "").strip(),
        os.getenv("PASSENGER_2_MATCH", "").strip(),
    ]
    pax_names = [p for p in pax_names if p]
    headless = os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes")

    send_telegram(
        f"⚡ Processing Instant Buy Request:\n"
        f"• Date: {display_date}\n"
        f"• Train Time: {time_fmt}\n"
        f"• Route: {origin} ➔ {destination}\n"
        f"• Passengers: {total_pax} Pax ({pax_names})\n\n"
        f"Checking availability & booking via KTM Wallet now...",
        chat_id=chat_id,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})

            submit_search(page, origin, destination, ktmb_date, "", pax=total_pax)

            btn, chosen_time, seats_avail, is_login_btn = find_matching_train_button(
                page, ".DepartSection", [time_fmt], min_seats_required=total_pax, date_label=ktmb_date
            )

            if not btn:
                send_telegram(
                    f"❌ Instant Buy Unavailable:\n"
                    f"{display_date} at {time_fmt} has fewer than {total_pax} seats available right now.\n"
                    f"The 24/7 background sniper will continue monitoring for drops!",
                    chat_id=chat_id,
                )
                return

            if is_login_btn:
                print(f"[!] Logging in via SSO for direct buy...")
                btn.click()
                page.wait_for_url("**/Account/Login**", timeout=30_000)
                ensure_logged_in(page, email, password)
                btn, chosen_time, seats_avail, _ = find_matching_train_button(
                    page, ".DepartSection", [time_fmt], min_seats_required=total_pax, date_label=ktmb_date
                )

            if btn:
                print(f"[!] Clicking Select for direct buy: {display_date} {chosen_time} ({seats_avail} seats)...")
                btn.click()
                nav_ok = reserve_and_wait_passenger_page(page, timeout_sec=180)
                if not nav_ok:
                    send_telegram(
                        f"❌ Timed out waiting for reCAPTCHA/reservation on {display_date} at {time_fmt}.",
                        chat_id=chat_id,
                    )
                    return

                ok = automate_passenger_and_checkout(
                    page, payment_method, wallet_pin, pax_names, date_label=ktmb_date, train_time=chosen_time
                )
                if ok:
                    state = load_state()
                    dt_obj = datetime.datetime.strptime(ktmb_date, "%d %b %Y").date()
                    wd_prefix = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][dt_obj.weekday()]
                    if wd_prefix in ("SAT", "SUN"):
                        leg_key = f"{wd_prefix}_{ktmb_date}"
                    else:
                        leg_key = f"CUSTOM_{ktmb_date}"
                    state[leg_key] = {"booked_pax": total_pax, "locked_train_time": chosen_time}
                    save_state(state)

                    # Also mark any active custom snipe on this date complete (don't buy twice same day)
                    rules = load_custom_snipes()
                    updated_rules = False
                    for r in rules:
                        if r.get("date") == ktmb_date and r.get("status") == "active":
                            r["booked_pax"] = int(r.get("target_pax", total_pax))
                            r["locked_train_time"] = chosen_time
                            r["status"] = "completed"
                            updated_rules = True
                    if updated_rules:
                        save_custom_snipes(rules)

                    notify(
                        "🎉 Instant Purchase Confirmed!",
                        f"Successfully booked {total_pax} tickets for {display_date} at {chosen_time} ({origin} ➔ {destination}) via KTM Wallet!",
                        page=page,
                    )
        finally:
            browser.close()


def _next_date_for_weekday(weekday: int, offset_weeks: int = 0, today: datetime.date | None = None) -> datetime.date:
    today = today or datetime.date.today()
    days_ahead = (weekday - today.weekday()) % 7
    return today + datetime.timedelta(days=days_ahead + offset_weeks * 7)


def _dir_label(dir_code: str) -> tuple[str, str, str]:
    if dir_code == "j2w":
        return "JB SENTRAL", "WOODLANDS CIQ", "JB Sentral ➔ Woodlands CIQ"
    return "WOODLANDS CIQ", "JB SENTRAL", "Woodlands CIQ ➔ JB Sentral"


def _build_main_menu() -> tuple[str, dict]:
    active_custom = [r for r in load_custom_snipes() if r.get("status") == "active"]
    mode = os.getenv("BOOKING_MODE", "single_sun")
    text = (
        "🤖 KTMB Shuttle Tebrau Bot Menu\n\n"
        f"• Active Mode: {mode}\n"
        f"• Custom Snipes Scheduled: {len(active_custom)}\n\n"
        "Tap an option below:"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🎯 Add Snipe Schedule", "callback_data": "menu:snipe_add"},
                {"text": "📋 My Schedule & Status", "callback_data": "menu:status"},
            ],
            [
                {"text": "📊 Scrap PDF Report", "callback_data": "menu:scrap"},
                {"text": "⚡ Instant Buy Guide", "callback_data": "menu:buy"},
            ],
            [
                {"text": "🗑 Remove Custom Snipe", "callback_data": "menu:snipe_del"},
                {"text": "🔄 Update Bot", "callback_data": "menu:update"},
            ],
            [
                {"text": "🔁 Refresh Menu", "callback_data": "menu:main"},
            ],
        ]
    }
    return text, keyboard


def _build_snipe_date_menu(today: datetime.date | None = None) -> tuple[str, dict]:
    today = today or datetime.date.today()
    fri0 = _next_date_for_weekday(4, 0, today)
    sat0 = _next_date_for_weekday(5, 0, today)
    sun0 = _next_date_for_weekday(6, 0, today)
    fri1 = _next_date_for_weekday(4, 1, today)
    sat1 = _next_date_for_weekday(5, 1, today)
    sun1 = _next_date_for_weekday(6, 1, today)

    def _btn(dt: datetime.date, tag: str) -> dict:
        return {
            "text": f"📅 {dt.strftime('%a %d %b')} ({tag})",
            "callback_data": f"snipe_date:{dt.strftime('%Y%m%d')}",
        }

    text = (
        "🎯 Add Custom Snipe — Step 1/3: Pick Date\n\n"
        "Choose a date below (it will be added to your live sniping schedule and stop automatically after snatching 1 slot on that day).\n\n"
        "Or send a custom command anytime:\n"
        "/snipe YYYYMMDD HH:MM-HH:MM route\n"
        "Example: /snipe 20261120 19:00-22:00 woodlands-tebrau"
    )
    keyboard = {
        "inline_keyboard": [
            [_btn(fri0, "This Fri"), _btn(sat0, "This Sat")],
            [_btn(sun0, "This Sun"), _btn(fri1, "Next Fri")],
            [_btn(sat1, "Next Sat"), _btn(sun1, "Next Sun")],
            [{"text": "⬅️ Back to Menu", "callback_data": "menu:main"}],
        ]
    }
    return text, keyboard


def _build_snipe_dir_menu(ymd: str) -> tuple[str, dict]:
    dt = datetime.datetime.strptime(ymd, "%Y%m%d").date()
    label = dt.strftime("%a, %d %b %Y")
    text = (
        f"🎯 Add Custom Snipe — Step 2/3: Pick Direction\n\n"
        f"• Date: {label}\n\n"
        f"Which direction do you want to travel?"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "🇸🇬➔🇲🇾 Woodlands CIQ ➔ JB Sentral", "callback_data": f"snipe_dir:{ymd}:w2j"}],
            [{"text": "🇲🇾➔🇸🇬 JB Sentral ➔ Woodlands CIQ", "callback_data": f"snipe_dir:{ymd}:j2w"}],
            [{"text": "⬅️ Back (Change Date)", "callback_data": "menu:snipe_add"}],
        ]
    }
    return text, keyboard


def _get_or_init_draft(chat_id: str, message_id: int | None, ymd: str, dir_code: str, seed_existing: bool = False) -> dict:
    key = (str(chat_id), int(message_id or 0))
    with _MENU_DRAFTS_LOCK:
        draft = _MENU_DRAFTS.get(key)
        if draft is None or draft.get("ymd") != ymd or draft.get("dir") != dir_code:
            hours: set[int] = set()
            exact: set[str] = set()
            if seed_existing:
                origin, destination, _ = _dir_label(dir_code)
                for r in load_custom_snipes():
                    if (
                        r.get("status") == "active"
                        and r.get("ymd") == ymd
                        and r.get("origin") == origin
                        and r.get("destination") == destination
                    ):
                        for spec in r.get("slots") or []:
                            if "-" in spec:
                                s, e = spec.split("-", 1)
                                if s.endswith(":00") and e.endswith(":59") and s[:2] == e[:2]:
                                    hours.add(int(s[:2]))
                            else:
                                exact.add(spec)
                        break
            draft = {
                "ymd": ymd,
                "dir": dir_code,
                "hours": hours,
                "exact": exact,
                "period": "eve",
                "live_trains": None,
            }
            if chat_id and message_id:
                _MENU_DRAFTS[key] = draft
        return {
            "ymd": draft["ymd"],
            "dir": draft["dir"],
            "hours": set(draft["hours"]),
            "exact": set(draft["exact"]),
            "period": draft["period"],
            "live_trains": list(draft["live_trains"]) if draft["live_trains"] is not None else None,
        }


def _draft_selected_slots(draft: dict) -> list[str]:
    slots: list[str] = []
    for h in sorted(draft.get("hours") or []):
        slots.append(f"{h:02d}:00-{h:02d}:59")
    for t in sorted(draft.get("exact") or []):
        slots.append(t)
    return slots


def _build_snipe_time_menu(
    ymd: str,
    dir_code: str,
    chat_id: str = "",
    message_id: int | None = None,
    seed_existing: bool = False,
    notice: str = "",
) -> tuple[str, dict]:
    dt = datetime.datetime.strptime(ymd, "%Y%m%d").date()
    label = dt.strftime("%a, %d %b %Y")
    _, _, route_str = _dir_label(dir_code)
    draft = _get_or_init_draft(chat_id, message_id, ymd, dir_code, seed_existing=seed_existing)
    selected_slots = _draft_selected_slots(draft)
    selected_summary = format_slots_summary(selected_slots) if selected_slots else "None yet"
    period = draft.get("period", "eve")

    notice_block = f"\n⚠️ {notice}\n" if notice else ""
    text = (
        f"🎯 Add / Narrow Snipe — Step 3/3: Choose Wanted Slot(s)\n\n"
        f"• Date: {label}\n"
        f"• Route: {route_str}\n"
        f"• Selected: {selected_summary}\n"
        f"{notice_block}\n"
        f"Tap 1 or more slots below (e.g. 7pm, 8pm, 9pm), then tap Save. "
        f"The bot snatches the earliest available slot you picked and stops for that day.\n\n"
        f"Or type directly:\n"
        f"/snipe {ymd} 7pm,8pm {'woodlands-tebrau' if dir_code == 'w2j' else 'tebrau-woodlands'}"
    )

    rows: list[list[dict]] = []
    if period == "live" and draft.get("live_trains"):
        live_trains = draft["live_trains"]
        exact_set = draft["exact"]
        row: list[dict] = []
        for tr in live_trains:
            t_fmt = tr["time"]
            hm = tr["hm"]
            seats = tr["seats"]
            mark = "☑️" if t_fmt in exact_set else "⬜️"
            row.append(
                {
                    "text": f"{mark} {t_fmt} ({seats})",
                    "callback_data": f"se:{ymd}:{dir_code}:{hm}",
                }
            )
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "🕒 Hour Slots (7pm, 8pm...)", "callback_data": f"sp:{ymd}:{dir_code}:eve"}])
    elif period == "day":
        hours_sel = draft["hours"]
        day_hours = list(range(5, 17))  # 5am .. 4pm
        for i in range(0, len(day_hours), 3):
            rows.append(
                [
                    {
                        "text": f"{'☑️' if h in hours_sel else '⬜️'} {_hour_label(h)}",
                        "callback_data": f"st:{ymd}:{dir_code}:{h:02d}",
                    }
                    for h in day_hours[i : i + 3]
                ]
            )
        rows.append(
            [
                {"text": "🌆 Evening (5pm–11pm)", "callback_data": f"sp:{ymd}:{dir_code}:eve"},
                {"text": "🔍 Exact KTMB Trains", "callback_data": f"sl:{ymd}:{dir_code}"},
            ]
        )
    else:
        hours_sel = draft["hours"]
        eve_hours = list(range(17, 24))  # 5pm .. 11pm
        rows.append(
            [
                {
                    "text": f"{'☑️' if h in hours_sel else '⬜️'} {_hour_label(h)}",
                    "callback_data": f"st:{ymd}:{dir_code}:{h:02d}",
                }
                for h in eve_hours[:3]
            ]
        )
        rows.append(
            [
                {
                    "text": f"{'☑️' if h in hours_sel else '⬜️'} {_hour_label(h)}",
                    "callback_data": f"st:{ymd}:{dir_code}:{h:02d}",
                }
                for h in eve_hours[3:6]
            ]
        )
        rows.append(
            [
                {
                    "text": f"{'☑️' if 23 in hours_sel else '⬜️'} 11pm",
                    "callback_data": f"st:{ymd}:{dir_code}:23",
                },
                {"text": "☀️ Morning / Afternoon", "callback_data": f"sp:{ymd}:{dir_code}:day"},
            ]
        )
        rows.append([{"text": "🔍 Load Exact Train Times (KTMB)", "callback_data": f"sl:{ymd}:{dir_code}"}])

    if selected_slots:
        rows.append(
            [
                {"text": f"✅ Save Selected ({len(selected_slots)})", "callback_data": f"ss:{ymd}:{dir_code}"},
                {"text": "🧹 Clear", "callback_data": f"sc:{ymd}:{dir_code}"},
            ]
        )
    else:
        rows.append([{"text": "🌆 Quick: 7pm–10pm", "callback_data": f"sq:{ymd}:{dir_code}"}])

    rows.append([{"text": "⬅️ Back (Change Direction)", "callback_data": f"snipe_date:{ymd}"}])
    return text, {"inline_keyboard": rows}


def _build_scrap_menu() -> tuple[str, dict]:
    text = (
        "📊 On-Demand Available Seats PDF Reports\n\n"
        "Tap any report below to scrape live seats and receive a PDF:\n\n"
        "For a specific date, send:\n"
        "/scrap-day/YYYYMMDD-woodlands-tebrau\n"
        "/scrap-day/YYYYMMDD-tebrau-woodlands"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📄 Fridays: Woodlands ➔ JB", "callback_data": "scrap_run:fri_w2j"},
                {"text": "📄 Fridays: JB ➔ Woodlands", "callback_data": "scrap_run:fri_j2w"},
            ],
            [
                {"text": "📄 Saturdays: Woodlands ➔ JB", "callback_data": "scrap_run:sat_w2j"},
                {"text": "📄 Saturdays: JB ➔ Woodlands", "callback_data": "scrap_run:sat_j2w"},
            ],
            [
                {"text": "📄 Sundays: JB ➔ Woodlands", "callback_data": "scrap_run:sun_j2w"},
                {"text": "📄 Sundays: Woodlands ➔ JB", "callback_data": "scrap_run:sun_w2j"},
            ],
            [{"text": "⬅️ Back to Menu", "callback_data": "menu:main"}],
        ]
    }
    return text, keyboard


def _build_status_menu() -> tuple[str, dict]:
    custom_rules = load_custom_snipes()
    state = load_state()

    custom_lines = []
    for r in custom_rules:
        status = r.get("status", "active")
        icon = {"active": "🟢", "completed": "✅", "expired": "⌛"}.get(status, "•")
        route = f"{'Woodlands➔JB' if r.get('origin') == 'WOODLANDS CIQ' else 'JB➔Woodlands'}"
        booked = int(r.get("booked_pax", 0))
        target = int(r.get("target_pax", 2))
        locked = f" @ {r.get('locked_train_time')}" if r.get("locked_train_time") else ""
        custom_lines.append(
            f"{icon} {r.get('display_date', r.get('date'))} | {route} | [{format_slots_summary(r)}] | {booked}/{target} pax{locked}"
        )

    state_lines = []
    for k, v in state.items():
        if not isinstance(v, dict):
            continue
        skipped = v.get("skipped_times")
        skip_note = f" (skipped: {', '.join(skipped)})" if skipped else ""
        state_lines.append(
            f"• {k}: {v.get('booked_pax', 0)} pax booked at {v.get('locked_train_time')}{skip_note}"
        )

    custom_block = "\n".join(custom_lines) if custom_lines else "None scheduled."
    state_block = "\n".join(state_lines) if state_lines else "No bookings recorded yet."
    text = (
        f"📋 Current Snipe Schedule & Bookings\n\n"
        f"🎯 Custom One-Off Snipes:\n{custom_block}\n\n"
        f"🔁 Recorded Bookings:\n{state_block}"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "➕ Add / Edit Snipe", "callback_data": "menu:snipe_add"},
                {"text": "🗑 Remove Snipe", "callback_data": "menu:snipe_del"},
            ],
            [
                {"text": "🔄 Refresh", "callback_data": "menu:status"},
                {"text": "🏠 Main Menu", "callback_data": "menu:main"},
            ],
        ]
    }
    return text, keyboard


def _build_snipe_del_menu() -> tuple[str, dict]:
    rules = load_custom_snipes()
    rows = []
    for r in rules:
        route = "W➔JB" if r.get("origin") == "WOODLANDS CIQ" else "JB➔W"
        status = r.get("status", "active")
        rows.append(
            [
                {
                    "text": f"❌ [{status}] {r.get('date')} {route} ({format_slots_summary(r)})",
                    "callback_data": f"snipe_rm:{r.get('id')}",
                }
            ]
        )
    rows.append([{"text": "⬅️ Back to Menu", "callback_data": "menu:main"}])
    text = (
        "🗑 Remove Custom Snipe\n\n"
        + ("Tap a rule below to remove it:" if rules else "No custom snipes found.")
    )
    return text, {"inline_keyboard": rows}


def _build_buy_guide_menu() -> tuple[str, dict]:
    text = (
        "⚡ Instant Ticket Buy Guide\n\n"
        "Send a command to immediately check, lock & pay via KTM Wallet:\n\n"
        "Format:\n"
        "/buy YYYYMMDD HH:MM [route]\n\n"
        "Examples:\n"
        "• /buy 20261120 19:15 woodlands-tebrau\n"
        "• /buy 20270221 22:45 tebrau-woodlands\n"
        "• /buy 20261114 12:30 woodlands-tebrau"
    )
    keyboard = {
        "inline_keyboard": [[{"text": "⬅️ Back to Menu", "callback_data": "menu:main"}]]
    }
    return text, keyboard


_SCRAP_CALLBACK_MAP = {
    "fri_w2j": ("/scrap-friday-woodlands-tebrau", "Fridays (Woodlands CIQ ➔ JB Sentral)"),
    "fri_j2w": ("/scrap-friday-tebrau-woodlands", "Fridays (JB Sentral ➔ Woodlands CIQ)"),
    "sat_w2j": ("/scrap-saturday-woodlands-tebrau", "Saturdays (Woodlands CIQ ➔ JB Sentral)"),
    "sat_j2w": ("/scrap-saturday-tebrau-woodlands", "Saturdays (JB Sentral ➔ Woodlands CIQ)"),
    "sun_j2w": ("/scrap-sunday-tebrau-woodlands", "Sundays (JB Sentral ➔ Woodlands CIQ)"),
    "sun_w2j": ("/scrap-sunday-woodlands-tebrau", "Sundays (Woodlands CIQ ➔ JB Sentral)"),
}


def _save_and_reply_snipe(
    cb_id: str,
    chat_id: str,
    message_id: int | None,
    ymd: str,
    dir_code: str,
    slots: list[str],
):
    origin, destination, route_str = _dir_label(dir_code)
    try:
        rule = add_custom_snipe(ymd, origin, destination, slots=slots)
        if chat_id and message_id:
            with _MENU_DRAFTS_LOCK:
                _MENU_DRAFTS.pop((str(chat_id), int(message_id)), None)
        answer_callback_query(cb_id, "✅ Snipe saved to schedule!")
        text = (
            f"✅ Saved to Live Sniping Schedule!\n\n"
            f"• Date: {rule['display_date']}\n"
            f"• Route: {route_str}\n"
            f"• Wanted Slots: {format_slots_summary(rule)} (earliest available)\n"
            f"• Target: {rule['target_pax']} pax\n"
            f"• Rule: Auto-stops once a slot is snatched on this day."
        )
    except Exception as e:
        answer_callback_query(cb_id, f"❌ {e}", show_alert=True)
        text = f"❌ Could not save snipe: {e}"
    kb = {
        "inline_keyboard": [
            [
                {"text": "📋 View Schedule", "callback_data": "menu:status"},
                {"text": "🏠 Main Menu", "callback_data": "menu:main"},
            ]
        ]
    }
    if chat_id and message_id:
        edit_telegram_message(chat_id, message_id, text, reply_markup=kb)


def _load_live_trains_for_menu(ymd: str, dir_code: str, chat_id: str, message_id: int | None):
    origin, destination, _ = _dir_label(dir_code)
    notice = ""
    with _SIDE_JOB_LOCK:
        try:
            trains = fetch_day_timetable(ymd, origin, destination)
            if trains:
                key = (str(chat_id), int(message_id or 0))
                with _MENU_DRAFTS_LOCK:
                    draft = _MENU_DRAFTS.get(key)
                    if draft and draft.get("ymd") == ymd and draft.get("dir") == dir_code:
                        draft["live_trains"] = trains
                        draft["period"] = "live"
            else:
                notice = "KTMB has not released this date yet. Use Hour Slots (7pm, 8pm...) instead."
        except Exception as e:
            notice = f"Could not load KTMB timetable: {e}"
    text, kb = _build_snipe_time_menu(ymd, dir_code, chat_id=chat_id, message_id=message_id, notice=notice)
    if chat_id and message_id:
        edit_telegram_message(chat_id, message_id, text, reply_markup=kb)


def handle_menu_callback(cb_id: str, cb_data: str, chat_id: str, message_id: int | None):
    """Handle interactive /help menu button callbacks."""
    if cb_data == "menu:main":
        answer_callback_query(cb_id)
        text, kb = _build_main_menu()
        if chat_id and message_id:
            edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data == "menu:snipe_add":
        answer_callback_query(cb_id)
        text, kb = _build_snipe_date_menu()
        if chat_id and message_id:
            edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data == "menu:scrap":
        answer_callback_query(cb_id)
        text, kb = _build_scrap_menu()
        if chat_id and message_id:
            edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data == "menu:status":
        answer_callback_query(cb_id)
        text, kb = _build_status_menu()
        if chat_id and message_id:
            edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data == "menu:snipe_del":
        answer_callback_query(cb_id)
        text, kb = _build_snipe_del_menu()
        if chat_id and message_id:
            edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data == "menu:buy":
        answer_callback_query(cb_id)
        text, kb = _build_buy_guide_menu()
        if chat_id and message_id:
            edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data.startswith("snipe_date:"):
        ymd = cb_data.split(":", 1)[1]
        answer_callback_query(cb_id)
        text, kb = _build_snipe_dir_menu(ymd)
        if chat_id and message_id:
            edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data.startswith("snipe_dir:"):
        parts = cb_data.split(":")
        if len(parts) == 3:
            _, ymd, dir_code = parts
            answer_callback_query(cb_id)
            if chat_id and message_id:
                with _MENU_DRAFTS_LOCK:
                    _MENU_DRAFTS.pop((str(chat_id), int(message_id)), None)
            text, kb = _build_snipe_time_menu(
                ymd, dir_code, chat_id=chat_id, message_id=message_id, seed_existing=True
            )
            if chat_id and message_id:
                edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data.startswith("st:"):
        parts = cb_data.split(":")
        if len(parts) == 4:
            _, ymd, dir_code, hh_str = parts
            hour = int(hh_str)
            _get_or_init_draft(chat_id, message_id, ymd, dir_code)
            key = (str(chat_id), int(message_id or 0))
            with _MENU_DRAFTS_LOCK:
                draft = _MENU_DRAFTS.get(key)
                if draft is not None:
                    if hour in draft["hours"]:
                        draft["hours"].remove(hour)
                    else:
                        draft["hours"].add(hour)
            answer_callback_query(cb_id)
            text, kb = _build_snipe_time_menu(ymd, dir_code, chat_id=chat_id, message_id=message_id)
            if chat_id and message_id:
                edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data.startswith("se:"):
        parts = cb_data.split(":")
        if len(parts) == 4:
            _, ymd, dir_code, hm_str = parts
            t_fmt = f"{hm_str[:2]}:{hm_str[2:]}"
            _get_or_init_draft(chat_id, message_id, ymd, dir_code)
            key = (str(chat_id), int(message_id or 0))
            with _MENU_DRAFTS_LOCK:
                draft = _MENU_DRAFTS.get(key)
                if draft is not None:
                    if t_fmt in draft["exact"]:
                        draft["exact"].remove(t_fmt)
                    else:
                        draft["exact"].add(t_fmt)
            answer_callback_query(cb_id)
            text, kb = _build_snipe_time_menu(ymd, dir_code, chat_id=chat_id, message_id=message_id)
            if chat_id and message_id:
                edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data.startswith("sp:"):
        parts = cb_data.split(":")
        if len(parts) == 4:
            _, ymd, dir_code, period = parts
            _get_or_init_draft(chat_id, message_id, ymd, dir_code)
            key = (str(chat_id), int(message_id or 0))
            with _MENU_DRAFTS_LOCK:
                draft = _MENU_DRAFTS.get(key)
                if draft is not None:
                    draft["period"] = period
            answer_callback_query(cb_id)
            text, kb = _build_snipe_time_menu(ymd, dir_code, chat_id=chat_id, message_id=message_id)
            if chat_id and message_id:
                edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data.startswith("sc:"):
        parts = cb_data.split(":")
        if len(parts) == 3:
            _, ymd, dir_code = parts
            _get_or_init_draft(chat_id, message_id, ymd, dir_code)
            key = (str(chat_id), int(message_id or 0))
            with _MENU_DRAFTS_LOCK:
                draft = _MENU_DRAFTS.get(key)
                if draft is not None:
                    draft["hours"].clear()
                    draft["exact"].clear()
            answer_callback_query(cb_id, "Cleared selection")
            text, kb = _build_snipe_time_menu(ymd, dir_code, chat_id=chat_id, message_id=message_id)
            if chat_id and message_id:
                edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data.startswith("sl:"):
        parts = cb_data.split(":")
        if len(parts) == 3:
            _, ymd, dir_code = parts
            _get_or_init_draft(chat_id, message_id, ymd, dir_code)
            answer_callback_query(cb_id, "⏳ Fetching live KTMB trains (~4s)...")
            if chat_id and message_id:
                dt = datetime.datetime.strptime(ymd, "%Y%m%d").date()
                _, _, route_str = _dir_label(dir_code)
                edit_telegram_message(
                    chat_id,
                    message_id,
                    f"⏳ Loading live KTMB train schedule for {dt.strftime('%a, %d %b %Y')} ({route_str})...",
                    reply_markup={"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": f"snipe_date:{ymd}"}]]},
                )
            threading.Thread(
                target=_load_live_trains_for_menu,
                args=(ymd, dir_code, chat_id, message_id),
                daemon=True,
            ).start()
    elif cb_data.startswith("ss:"):
        parts = cb_data.split(":")
        if len(parts) == 3:
            _, ymd, dir_code = parts
            draft = _get_or_init_draft(chat_id, message_id, ymd, dir_code)
            slots = _draft_selected_slots(draft)
            if not slots:
                answer_callback_query(cb_id, "⚠️ Tap at least one slot (e.g. 7pm, 8pm) first!", show_alert=True)
                return
            _save_and_reply_snipe(cb_id, chat_id, message_id, ymd, dir_code, slots)
    elif cb_data.startswith("sq:"):
        parts = cb_data.split(":")
        if len(parts) == 3:
            _, ymd, dir_code = parts
            _save_and_reply_snipe(cb_id, chat_id, message_id, ymd, dir_code, ["19:00-22:00"])
    elif cb_data.startswith("snipe_time:"):
        parts = cb_data.split(":")
        if len(parts) == 4:
            _, ymd, dir_code, time_range = parts
            start_raw, end_raw = time_range.split("-", 1)
            _save_and_reply_snipe(
                cb_id,
                chat_id,
                message_id,
                ymd,
                dir_code,
                [f"{start_raw[:2]}:{start_raw[2:]}-{end_raw[:2]}:{end_raw[2:]}"],
            )
    elif cb_data.startswith("snipe_rm:"):
        rule_id = cb_data.split(":", 1)[1]
        removed = remove_custom_snipe(rule_id)
        if removed:
            answer_callback_query(cb_id, f"🗑 Removed {removed.get('date')}")
        else:
            answer_callback_query(cb_id, "Rule already removed.")
        text, kb = _build_snipe_del_menu()
        if chat_id and message_id:
            edit_telegram_message(chat_id, message_id, text, reply_markup=kb)
    elif cb_data.startswith("scrap_run:"):
        key = cb_data.split(":", 1)[1]
        entry = _SCRAP_CALLBACK_MAP.get(key)
        if not entry:
            answer_callback_query(cb_id, "Unknown report option.")
            return
        scrap_cmd, label = entry
        answer_callback_query(cb_id, "⏳ Generating PDF report...")
        send_telegram(
            f"⏳ Generating available seats PDF report for {label}... Please wait ~10-15s.",
            chat_id=chat_id,
        )
        threading.Thread(
            target=run_side_job,
            args=(handle_scrape_command, scrap_cmd, chat_id),
            daemon=True,
        ).start()
    elif cb_data == "menu:update":
        answer_callback_query(cb_id, "⏳ Checking GitHub for updates...")
        send_telegram("⏳ Checking GitHub for updates...", chat_id=chat_id)
        threading.Thread(
            target=perform_bot_update,
            args=(chat_id, False),
            daemon=True,
        ).start()
    elif cb_data == "bot_restart:now":
        answer_callback_query(cb_id, "♻️ Restarting bot...")
        threading.Thread(
            target=perform_bot_update,
            args=(chat_id, True),
            daemon=True,
        ).start()


def _ack_telegram_offset(token: str, offset: int):
    """Commit `offset` to Telegram getUpdates so the next boot does not replay the /update command."""
    if not token or offset <= 0:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=0"
        urllib.request.urlopen(urllib.request.Request(url), timeout=5).read()
    except Exception:
        pass


def _notify_if_just_updated():
    """If the previous process exited for /update, send a back-online confirmation."""
    if not UPDATE_NOTIFY_FILE.exists():
        return
    try:
        info = json.loads(UPDATE_NOTIFY_FILE.read_text())
    except Exception:
        info = {}
    try:
        UPDATE_NOTIFY_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    sha = info.get("sha", "")
    target_chat = info.get("chat_id") or None
    if sha:
        action = "restarted" if info.get("restarted_only") else "updated & restarted"
        send_telegram(f"✅ Bot {action} and online (commit {sha})!", chat_id=target_chat)


def start_telegram_command_listener():
    """Background listener for Telegram commands (/help, /menu, /snipe, /scrap..., /buy, /status, /update)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return

    register_telegram_bot_commands(token)
    _notify_if_just_updated()
    print("[*] Telegram command listener started (listening for /help, /menu, /snipe, /scrap, /buy, /status, /update)")
    last_update_id = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=25"
            if last_update_id > 0:
                url += f"&offset={last_update_id + 1}"
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=35)
            data = json.loads(resp.read().decode("utf-8"))

            for update in data.get("result", []):
                last_update_id = update["update_id"]
                cb = update.get("callback_query")
                if cb:
                    cb_id = cb.get("id", "")
                    cb_data = cb.get("data", "")
                    cb_msg = cb.get("message") or {}
                    cb_chat_id = str(cb_msg.get("chat", {}).get("id", ""))
                    cb_msg_id = cb_msg.get("message_id")
                    cb_orig_text = cb_msg.get("text", "")

                    if cb_data.startswith("confirm_buy:"):
                        parts = cb_data.split(":")
                        if len(parts) == 3:
                            _, req_id, choice = parts
                            approved = choice == "yes"
                            with _PENDING_CONFIRMATIONS_LOCK:
                                entry = _PENDING_CONFIRMATIONS.get(req_id)
                            if entry is not None:
                                entry["approved"] = approved
                                entry["event"].set()
                                ack_text = "✅ Confirmed! Booking now..." if approved else "❌ Skipped."
                                answer_callback_query(cb_id, ack_text)
                                status_line = "✅ Confirmed by user — booking now!" if approved else "❌ Skipped by user."
                                if cb_chat_id and cb_msg_id:
                                    edit_telegram_message(cb_chat_id, cb_msg_id, f"{cb_orig_text}\n\n{status_line}")
                            else:
                                answer_callback_query(cb_id, "This confirmation prompt has expired.")
                                if cb_chat_id and cb_msg_id:
                                    edit_telegram_message(cb_chat_id, cb_msg_id, f"{cb_orig_text}\n\n⏱️ Expired.")
                    else:
                        if cb_data in ("menu:update", "bot_restart:now"):
                            _ack_telegram_offset(token, last_update_id + 1)
                        handle_menu_callback(cb_id, cb_data, cb_chat_id, cb_msg_id)
                    continue

                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue
                text = msg.get("text", "").strip()
                if not text:
                    continue
                chat_id = str(msg["chat"]["id"])

                # Normalize command if bot username is appended (e.g. /help@MyBot)
                cmd_name = text.split()[0].split("@")[0].lower()

                if cmd_name.startswith("/buy"):
                    print(f"[*] Received Telegram /buy command: '{text}' from chat {chat_id}")
                    threading.Thread(
                        target=run_side_job,
                        args=(execute_direct_buy, text, chat_id),
                        daemon=True,
                    ).start()
                elif cmd_name.startswith("/scrap"):
                    print(f"[*] Received Telegram command: '{text}' from chat {chat_id}")
                    if parse_scrape_command(text) is None:
                        send_telegram(
                            f"❌ Unrecognized /scrap command: {text}\n\n"
                            "Examples:\n"
                            "• /scrap-friday-woodlands-tebrau\n"
                            "• /scrap-saturday-woodlands-tebrau\n"
                            "• /scrap-sunday-tebrau-woodlands\n"
                            "• /scrap-day/20261120-woodlands-tebrau",
                            reply_markup={"inline_keyboard": [[{"text": "📊 Open PDF Menu", "callback_data": "menu:scrap"}]]},
                            chat_id=chat_id,
                        )
                    else:
                        send_telegram(
                            f"⏳ Generating available seats PDF report for {text}... Please wait ~10-15s.",
                            chat_id=chat_id,
                        )
                        threading.Thread(
                            target=run_side_job,
                            args=(handle_scrape_command, text, chat_id),
                            daemon=True,
                        ).start()
                elif cmd_name == "/snipe":
                    print(f"[*] Received Telegram /snipe command: '{text}' from chat {chat_id}")
                    if len(text.split()) == 1:
                        menu_text, menu_kb = _build_snipe_date_menu()
                        send_telegram(menu_text, reply_markup=menu_kb, chat_id=chat_id)
                    else:
                        parsed_snipe = parse_snipe_command(text)
                        if not parsed_snipe:
                            send_telegram(
                                "❌ Invalid /snipe format.\n\n"
                                "Format: /snipe <date|weekday> <slots_or_range> [route]\n"
                                "Examples:\n"
                                "• /snipe friday 7pm,8pm,9pm woodlands-tebrau\n"
                                "• /snipe friday 20:15 woodlands-tebrau\n"
                                "• /snipe 20261120 19:00-22:00 tebrau-woodlands",
                                reply_markup={"inline_keyboard": [[{"text": "🎯 Pick via Menu", "callback_data": "menu:snipe_add"}]]},
                                chat_id=chat_id,
                            )
                        else:
                            try:
                                rule = add_custom_snipe(
                                    parsed_snipe["ymd"],
                                    parsed_snipe["origin"],
                                    parsed_snipe["destination"],
                                    slots=parsed_snipe["slots"],
                                )
                                send_telegram(
                                    f"✅ Saved to Live Sniping Schedule!\n\n"
                                    f"• Date: {rule['display_date']}\n"
                                    f"• Route: {rule['origin']} ➔ {rule['destination']}\n"
                                    f"• Wanted Slots: {format_slots_summary(rule)} (earliest available)\n"
                                    f"• Target: {rule['target_pax']} pax\n"
                                    f"• Rule: Auto-stops once a slot is snatched on this day.",
                                    reply_markup={
                                        "inline_keyboard": [
                                            [
                                                {"text": "📋 View Schedule", "callback_data": "menu:status"},
                                                {"text": "🏠 Main Menu", "callback_data": "menu:main"},
                                            ]
                                        ]
                                    },
                                    chat_id=chat_id,
                                )
                            except Exception as e:
                                send_telegram(f"❌ Could not add snipe: {e}", chat_id=chat_id)
                elif cmd_name in ("/update", "/restart"):
                    print(f"[*] Received Telegram {cmd_name} command from chat {chat_id}")
                    _ack_telegram_offset(token, last_update_id + 1)
                    force = cmd_name == "/restart" or "force" in text.lower().split()[1:]
                    if not force:
                        send_telegram("⏳ Checking GitHub for updates...", chat_id=chat_id)
                    threading.Thread(
                        target=perform_bot_update,
                        args=(chat_id, force),
                        daemon=True,
                    ).start()
                elif cmd_name == "/status":
                    status_text, status_kb = _build_status_menu()
                    send_telegram(status_text, reply_markup=status_kb, chat_id=chat_id)
                elif cmd_name in ("/help", "/menu", "/start"):
                    menu_text, menu_kb = _build_main_menu()
                    send_telegram(menu_text, reply_markup=menu_kb, chat_id=chat_id)
        except Exception as e:
            print(f"[!] Telegram listener loop error: {e}")
            time.sleep(3)


def main():
    load_dotenv()

    # Start Telegram command listener in background thread
    threading.Thread(target=start_telegram_command_listener, daemon=True).start()

    parser = argparse.ArgumentParser(description="KTMB Shuttle Tebrau Ticket Sniper & Auto-Buyer")
    parser.add_argument(
        "--mode",
        choices=["auto_weekend", "single_sat", "single_sun"],
        default=os.getenv("BOOKING_MODE", "single_sun"),
        help="Booking mode: 'single_sun' (all Sundays JB->SG), 'single_sat' (all Saturdays SG->JB), or 'auto_weekend'",
    )
    parser.add_argument("--sat-date", default=os.getenv("TARGET_SATURDAY_DATE", ""), help="Optional single Saturday date override")
    parser.add_argument("--sun-date", default=os.getenv("TARGET_SUNDAY_DATE", ""), help="Optional single Sunday date override")
    parser.add_argument("--max-weeks", type=int, default=int(os.getenv("MAX_SCAN_WEEKS", "36")), help="Max weeks ahead to scan until end of released calendar")
    parser.add_argument("--interval", type=float, default=float(os.getenv("POLL_INTERVAL_SECONDS", "15")), help="Daytime pause in seconds between full calendar sweeps (peak/night windows override it)")
    parser.add_argument("--wait-for-drop", action="store_true", help="Wait until 08:29:55 AM SGT/MYT before starting high-speed sniping")
    args = parser.parse_args()

    email = os.getenv("KTMB_EMAIL", "")
    password = os.getenv("KTMB_PASSWORD", "")
    total_pax = int(os.getenv("PAX_COUNT", "2"))
    split_pax = os.getenv("SPLIT_PAX_SNIPE", "false").lower() in ("1", "true", "yes")
    sat_times = [t.strip() for t in os.getenv("SATURDAY_PREFERRED_TIMES", "12:30,13:45").split(",") if t.strip()]
    sun_times = [t.strip() for t in os.getenv("SUNDAY_PREFERRED_TIMES", "20:15,19:00,21:30").split(",") if t.strip()]
    payment_method = os.getenv("PAYMENT_METHOD", "KTM_WALLET")
    wallet_pin = os.getenv("KTM_WALLET_PIN", "")
    headless = os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes")
    pax_names = [
        os.getenv("PASSENGER_1_MATCH", "").strip(),
        os.getenv("PASSENGER_2_MATCH", "").strip(),
    ]

    sat_dates = [args.sat_date] if args.sat_date else get_upcoming_weekday_dates(5, args.max_weeks)
    sun_dates = [args.sun_date] if args.sun_date else get_upcoming_weekday_dates(6, args.max_weeks)

    print("=" * 72)
    print("KTMB SHUTTLE TEBRAU MULTI-WEEK SNIPER (LINUX LXC / XVFB EDITION)")
    print(f"  Mode               : {args.mode}")
    if args.mode in ("auto_weekend", "single_sat"):
        print(f"  Saturday (SG -> JB): Scanning all released Saturdays ({sat_dates[0]} -> ...) | Trains: {sat_times}")
    if args.mode in ("auto_weekend", "single_sun"):
        print(f"  Sunday   (JB -> SG): Scanning all released Sundays ({sun_dates[0]} -> ...) | Trains: {sun_times}")
    print(f"  Custom Snipes      : {len([r for r in load_custom_snipes() if r.get('status') == 'active'])} active (editable via Telegram /help)")
    print(f"  Total Tickets/Date : {total_pax} Pax (Split 1+1 Snipe: {split_pax}) | Passengers: {pax_names}")
    print(
        f"  Sweep Interval     : {args.interval:g}s daytime | "
        f"{os.getenv('PEAK_POLL_INTERVAL_SECONDS', '2')}s in {os.getenv('PEAK_WINDOW', '08:25-08:45')} | "
        f"{os.getenv('NIGHT_POLL_INTERVAL_SECONDS', '60')}s in {os.getenv('NIGHT_WINDOW', '00:00-07:00')}"
    )
    if os.getenv("INVOCATION_ID"):
        print(f"  Recycle            : every {os.getenv('RECYCLE_MINUTES', '60')} min (0 = off)")
    else:
        print("  Recycle            : off (not running under systemd)")
    print("=" * 72)

    with sync_playwright() as p:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()

        if args.wait_for_drop:
            print("[*] Waiting for 08:29:55 AM ticket release window...")
            while True:
                now = datetime.datetime.now()
                if now.hour == 8 and now.minute == 29 and now.second >= 55:
                    print("[!] Release window reached! Starting high-speed sniper!")
                    break
                time.sleep(0.5)

        if args.mode == "single_sun":
            while True:
                scan_custom_snipes(
                    page=page,
                    total_pax=total_pax,
                    split_pax=split_pax,
                    payment_method=payment_method,
                    wallet_pin=wallet_pin,
                    pax_names=pax_names,
                    email=email,
                    password=password,
                )
                ran = scan_and_snipe_all_dates(
                    page=page,
                    leg_prefix="SUN",
                    origin="JB SENTRAL",
                    destination="WOODLANDS CIQ",
                    candidate_dates=sun_dates,
                    preferred_times=sun_times,
                    total_pax=total_pax,
                    split_pax=split_pax,
                    poll_interval=args.interval,
                    payment_method=payment_method,
                    wallet_pin=wallet_pin,
                    pax_names=pax_names,
                    email=email,
                    password=password,
                    single_sweep=True,
                    is_manual_date=bool(args.sun_date),
                    max_weeks=args.max_weeks,
                )
                if not ran:
                    maybe_recycle(page)
                    pause = current_poll_interval(args.interval)
                    print(f"[*] Finished sweep. Waiting {pause:g}s before next sweep...")
                    if sleep_or_wake(pause):
                        maybe_recycle(page)
        elif args.mode == "single_sat":
            while True:
                scan_custom_snipes(
                    page=page,
                    total_pax=total_pax,
                    split_pax=split_pax,
                    payment_method=payment_method,
                    wallet_pin=wallet_pin,
                    pax_names=pax_names,
                    email=email,
                    password=password,
                )
                ran = scan_and_snipe_all_dates(
                    page=page,
                    leg_prefix="SAT",
                    origin="WOODLANDS CIQ",
                    destination="JB SENTRAL",
                    candidate_dates=sat_dates,
                    preferred_times=sat_times,
                    total_pax=total_pax,
                    split_pax=split_pax,
                    poll_interval=args.interval,
                    payment_method=payment_method,
                    wallet_pin=wallet_pin,
                    pax_names=pax_names,
                    email=email,
                    password=password,
                    single_sweep=True,
                    is_manual_date=bool(args.sat_date),
                    max_weeks=args.max_weeks,
                )
                if not ran:
                    maybe_recycle(page)
                    pause = current_poll_interval(args.interval)
                    print(f"[*] Finished sweep. Waiting {pause:g}s before next sweep...")
                    if sleep_or_wake(pause):
                        maybe_recycle(page)
        else:  # auto_weekend
            # Sweep custom snipes, then alternate Saturdays and Sundays
            while True:
                scan_custom_snipes(
                    page=page,
                    total_pax=total_pax,
                    split_pax=split_pax,
                    payment_method=payment_method,
                    wallet_pin=wallet_pin,
                    pax_names=pax_names,
                    email=email,
                    password=password,
                )
                ran_sat = scan_and_snipe_all_dates(
                    page=page,
                    leg_prefix="SAT",
                    origin="WOODLANDS CIQ",
                    destination="JB SENTRAL",
                    candidate_dates=sat_dates,
                    preferred_times=sat_times,
                    total_pax=total_pax,
                    split_pax=split_pax,
                    poll_interval=args.interval,
                    payment_method=payment_method,
                    wallet_pin=wallet_pin,
                    pax_names=pax_names,
                    email=email,
                    password=password,
                    single_sweep=True,
                    is_manual_date=bool(args.sat_date),
                    max_weeks=args.max_weeks,
                )
                ran_sun = scan_and_snipe_all_dates(
                    page=page,
                    leg_prefix="SUN",
                    origin="JB SENTRAL",
                    destination="WOODLANDS CIQ",
                    candidate_dates=sun_dates,
                    preferred_times=sun_times,
                    total_pax=total_pax,
                    split_pax=split_pax,
                    poll_interval=args.interval,
                    payment_method=payment_method,
                    wallet_pin=wallet_pin,
                    pax_names=pax_names,
                    email=email,
                    password=password,
                    single_sweep=True,
                    is_manual_date=bool(args.sun_date),
                    max_weeks=args.max_weeks,
                )
                if not ran_sat and not ran_sun:
                    maybe_recycle(page)
                    pause = current_poll_interval(args.interval)
                    print(f"[*] Finished sweep. Waiting {pause:g}s before next sweep...")
                    if sleep_or_wake(pause):
                        maybe_recycle(page)


if __name__ == "__main__":
    try:
        main()
    except RecycleRequested:
        sys.exit(0)
