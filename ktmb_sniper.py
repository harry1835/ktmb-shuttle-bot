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
import urllib.parse
import urllib.request
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

from pdf_scraper import handle_scrape_command

BASE_URL = "https://shuttleonline.ktmb.com.my"
SHUTTLE_URL = f"{BASE_URL}/Home/Shuttle"
LOGIN_URL = f"{BASE_URL}/Account/Login"

ROOT_DIR = Path(__file__).parent.resolve()
USER_DATA_DIR = ROOT_DIR / "browser_profile"
STATE_FILE = ROOT_DIR / "bookings_state.json"
SCREENSHOT_DIR = ROOT_DIR / "screenshots"


def send_telegram(message: str, photo_path: Path | None = None):
    """Send a Telegram alert (and optional screenshot) if TELEGRAM_BOT_TOKEN is configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return

    try:
        if photo_path and photo_path.exists():
            subprocess.run(
                [
                    "curl",
                    "-s",
                    "-X",
                    "POST",
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    "-F",
                    f"chat_id={chat_id}",
                    "-F",
                    f"photo=@{photo_path}",
                    "-F",
                    f"caption={message}",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[!] Telegram notification error: {e}")


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


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


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
    page.wait_for_timeout(500)
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

    notify(
        "KTMB Seat Locked!",
        f"Locked {date_label} ({train_time}) in cart for 10 mins! Selecting passenger {passenger_matches} & paying...",
        page=page,
    )

    try:
        if passenger_matches:
            for p_match in passenger_matches:
                if not p_match:
                    continue
                matched_el = page.locator(
                    f"label:has-text('{p_match}'), option:has-text('{p_match}'), tr:has-text('{p_match}')"
                )
                if matched_el.count() > 0:
                    matched_el.first.click()
                    page.wait_for_timeout(400)

        fav_btns = page.locator(
            ".btn-favorite, .select-passenger, button:has-text('Favorite'), a:has-text('Favorite'), input[type='checkbox'].passenger-select, select[id*='Passenger'], select[id*='Favourite']"
        )
        if fav_btns.count() > 0:
            first_el = fav_btns.first
            tag = first_el.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                first_el.select_option(index=1)
            else:
                first_el.click()
            page.wait_for_timeout(600)
    except Exception as e:
        print(f"[*] Passenger auto-fill note: {e}")

    try:
        tnc_boxes = page.locator(
            "input[type='checkbox'][id*='Term'], input[type='checkbox'][name*='Term'], input[type='checkbox'][id*='Agree']"
        )
        for i in range(tnc_boxes.count()):
            box = tnc_boxes.nth(i)
            if box.is_visible() and not box.is_checked():
                box.check()
    except Exception:
        pass

    if payment_method.upper() == "KTM_WALLET":
        print("[*] Proceeding with automated KTM Wallet checkout...")
        try:
            pay_proceed = page.locator(
                "button:has-text('PROCEED'), button:has-text('Proceed'), button:has-text('Pay'), input[type='submit'][value*='Proceed'], #btnProceed, #btnSubmit"
            )
            if pay_proceed.count() > 0 and pay_proceed.first.is_visible():
                pay_proceed.first.click()
                page.wait_for_timeout(1500)

            wallet_opt = page.locator(
                "text=/KTM Wallet/i, input[value*='Wallet'], label:has-text('KTM Wallet'), .payment-method:has-text('Wallet')"
            )
            if wallet_opt.count() > 0 and wallet_opt.first.is_visible():
                wallet_opt.first.click()
                page.wait_for_timeout(500)

                if wallet_pin:
                    pin_input = page.locator("input[type='password'], input[id*='Pin'], input[name*='Pin']")
                    if pin_input.count() > 0 and pin_input.first.is_visible():
                        pin_input.first.fill(wallet_pin)

                final_pay_btn = page.locator("button:has-text('Pay'), button:has-text('Confirm'), #btnPay")
                if final_pay_btn.count() > 0 and final_pay_btn.first.is_visible():
                    final_pay_btn.first.click()
                    page.wait_for_timeout(3000)
                    notify(
                        "KTMB Checkout Triggered",
                        f"KTM Wallet payment submitted for {date_label} ({train_time})!",
                        page=page,
                    )
                    return True
        except Exception as e:
            print(f"[!] Partial auto-checkout note: {e}")

    vnc_url = os.getenv("VNC_PUBLIC_URL", "")
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
):
    """
    Continuously scans through `candidate_dates` (all upcoming Sundays or Saturdays
    until the website's last released date) and books up to `total_pax` tickets on each date!
    """
    search_pax = 1 if split_pax else total_pax
    cycle = 1

    while True:
        state = load_state()
        # Filter out dates that already have total_pax booked
        pending_dates = [
            d for d in candidate_dates if int(state.get(f"{leg_prefix}_{d}", {}).get("booked_pax", 0)) < total_pax
        ]
        if not pending_dates:
            print(f"[+] All target dates for {leg_prefix} have {total_pax}/{total_pax} tickets booked!")
            return

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

            if booked_so_far >= total_pax:
                continue

            if idx > 0:
                is_released = switch_date_in_place(page, target_date, "")
                if not is_released:
                    print(f"    [{target_date:<12}] Reached end of KTMB released calendar. Wrapping back to start!")
                    break

            times_to_check = [locked_time] if locked_time else preferred_times
            btn, chosen_time, seats_avail, is_login_btn = find_matching_train_button(
                page,
                ".DepartSection",
                times_to_check,
                min_seats_required=search_pax,
                date_label=target_date,
            )

            if btn:
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
        print(f"[*] Finished full calendar sweep. Waiting {poll_interval}s before next sweep...")
        time.sleep(poll_interval)


def start_telegram_command_listener():
    """Background listener for Telegram commands (/scrap-saturday-woodlands-tebrau, etc.)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return

    print("[*] Telegram command listener started (listening for /scrap-saturday..., /scrap-day/...)")
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
                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue
                text = msg.get("text", "").strip()
                chat_id = str(msg["chat"]["id"])

                if text.startswith("/scrap"):
                    print(f"[*] Received Telegram command: '{text}' from chat {chat_id}")
                    send_telegram(f"⏳ Generating available seats PDF report for `{text}`... Please wait ~10-15s.")
                    threading.Thread(
                        target=handle_scrape_command,
                        args=(text, chat_id),
                        daemon=True,
                    ).start()
                elif text in ("/help", "/start"):
                    help_msg = (
                        "🤖 *KTMB Shuttle Tebrau PDF Reports Bot*\n\n"
                        "Send me any of these commands to get a live PDF of available seats:\n\n"
                        "1️⃣ `/scrap-saturday-woodlands-tebrau`\n"
                        "   Scrapes all released Saturdays (Woodlands ➔ JB Sentral)\n\n"
                        "2️⃣ `/scrap-sunday-tebrau-woodlands`\n"
                        "   Scrapes all released Sundays (JB Sentral ➔ Woodlands)\n\n"
                        "3️⃣ `/scrap-day/YYYYMMDD-departure-arrival`\n"
                        "   Scrapes a single date, e.g.:\n"
                        "   `/scrap-day/20261115-woodlands-tebrau`\n"
                        "   `/scrap-day/20261115-tebrau-woodlands`"
                    )
                    send_telegram(help_msg)
        except Exception:
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
    parser.add_argument("--max-weeks", type=int, default=int(os.getenv("MAX_SCAN_WEEKS", "22")), help="Max weeks ahead to scan until end of released calendar")
    parser.add_argument("--interval", type=float, default=float(os.getenv("POLL_INTERVAL_SECONDS", "4")), help="Pause in seconds between full calendar sweeps")
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
    print(f"  Total Tickets/Date : {total_pax} Pax (Split 1+1 Snipe: {split_pax}) | Passengers: {pax_names}")
    print(f"  Sweep Interval     : {args.interval}s")
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
            scan_and_snipe_all_dates(
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
            )
        elif args.mode == "single_sat":
            scan_and_snipe_all_dates(
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
            )
        else:  # auto_weekend
            # Alternate sweeping all Saturdays and all Sundays
            while True:
                scan_and_snipe_all_dates(
                    page=page,
                    leg_prefix="SAT",
                    origin="WOODLANDS CIQ",
                    destination="JB SENTRAL",
                    candidate_dates=sat_dates[:1],
                    preferred_times=sat_times,
                    total_pax=total_pax,
                    split_pax=split_pax,
                    poll_interval=args.interval,
                    payment_method=payment_method,
                    wallet_pin=wallet_pin,
                    pax_names=pax_names,
                    email=email,
                    password=password,
                )
                scan_and_snipe_all_dates(
                    page=page,
                    leg_prefix="SUN",
                    origin="JB SENTRAL",
                    destination="WOODLANDS CIQ",
                    candidate_dates=sun_dates[:1],
                    preferred_times=sun_times,
                    total_pax=total_pax,
                    split_pax=split_pax,
                    poll_interval=args.interval,
                    payment_method=payment_method,
                    wallet_pin=wallet_pin,
                    pax_names=pax_names,
                    email=email,
                    password=password,
                )


if __name__ == "__main__":
    main()
