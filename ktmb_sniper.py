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

    # 2. Fill each passenger's "Select Friends and Family" dropdown.
    #    Dropdown N gets passenger_matches[N]; a person already picked in an earlier
    #    dropdown is never reused (KTMB rejects "Duplicated passport number").
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
                print(f"[*] Found passenger dropdown #{dropdown_no + 1} with options: {opts_text}")

                def _pick(label_filter) -> int | None:
                    for idx, opt_label in enumerate(opts_text):
                        if idx == 0 or not opt_label or opt_label in used_labels:
                            continue
                        if label_filter(opt_label):
                            return idx
                    return None

                # Preferred: the name configured for this passenger slot.
                chosen = None
                if dropdown_no < len(names):
                    target = names[dropdown_no].lower()
                    chosen = _pick(lambda o: target in o.lower())
                # Fallback: any remaining name from the configured list, then any unused option.
                if chosen is None:
                    for target_name in names:
                        chosen = _pick(lambda o, t=target_name.lower(): t in o.lower())
                        if chosen is not None:
                            break
                if chosen is None:
                    chosen = _pick(lambda o: True)

                if chosen is not None:
                    sel.select_option(index=chosen)
                    sel.dispatch_event("change")
                    used_labels.add(opts_text[chosen])
                    print(f"[+] Passenger {dropdown_no + 1}: selected '{opts_text[chosen]}'")
                else:
                    print(f"[!] Passenger {dropdown_no + 1}: no unused saved passenger available")
                dropdown_no += 1
                page.wait_for_timeout(400)
            except Exception as ex:
                print(f"[*] Friends & Family dropdown item error: {ex}")
    except Exception as e:
        print(f"[*] Friends and Family selection note: {e}")

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

    # Wait for navigation to payment page
    try:
        page.wait_for_url("**/Payment**|**/Checkout**|**/Cart**|**/Summary**", timeout=20_000)
        print(f"[+] Navigated to payment page: {page.url}")
    except Exception as e:
        print(f"[*] Navigation to payment page note: {e}")

    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)

    # 6. Proceed to KTM Wallet checkout on payment page
    if payment_method.upper() == "KTM_WALLET":
        print("[*] Proceeding with automated KTM Wallet checkout...")
        try:
            wallet_opt = page.locator(
                "text=/KTM Wallet/i, input[value*='Wallet'], label:has-text('KTM Wallet'), label:has-text('Wallet'), .payment-method:has-text('Wallet'), div:has-text('KTM Wallet')"
            ).first
            if wallet_opt.count() > 0 and wallet_opt.is_visible():
                print("[*] Selecting KTM Wallet...")
                wallet_opt.click()
                page.wait_for_timeout(800)

                # Check payment T&C if any
                ptnc = page.locator("input[type='checkbox']")
                for i in range(ptnc.count()):
                    cb = ptnc.nth(i)
                    if cb.is_visible() and not cb.is_checked():
                        cb.check(force=True)

                if wallet_pin:
                    pin_input = page.locator("input[type='password'], input[id*='Pin' i], input[name*='Pin' i]").first
                    if pin_input.count() > 0 and pin_input.is_visible():
                        print("[*] Entering KTM Wallet PIN...")
                        pin_input.fill(wallet_pin)
                        page.wait_for_timeout(300)

                final_pay_btn = page.locator(
                    "button:has-text('Pay'), button:has-text('Bayar'), button:has-text('Confirm'), a:has-text('Pay'), input[value*='Pay'], #btnPay"
                ).first
                if final_pay_btn.count() > 0 and final_pay_btn.is_visible():
                    print("[*] Submitting final payment...")
                    final_pay_btn.click()
                    page.wait_for_timeout(4000)
                    notify(
                        "🎉 KTMB Payment Completed!",
                        f"KTM Wallet payment successfully processed for {date_label} ({train_time})!",
                        page=page,
                    )
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
):
    """
    Continuously scans through `candidate_dates` (all upcoming Sundays or Saturdays
    until the website's last released date) and books up to `total_pax` tickets on each date!
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
        # Filter out dates that already have total_pax booked
        pending_dates = [
            d for d in active_dates if int(state.get(f"{leg_prefix}_{d}", {}).get("booked_pax", 0)) < total_pax
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
        if single_sweep:
            return


def register_telegram_bot_commands(token: str):
    """Registers slash commands with Telegram so they appear in the autocomplete popup."""
    try:
        commands = [
            {"command": "help", "description": "Show available commands & examples"},
            {"command": "buy", "description": "Instant buy: /buy YYYYMMDD HH:MM (e.g. /buy 20270221 22:45)"},
            {"command": "scrap_saturday_woodlands_tebrau", "description": "PDF report for all Saturdays (Woodlands -> JB)"},
            {"command": "scrap_sunday_tebrau_woodlands", "description": "PDF report for all Sundays (JB -> Woodlands)"},
            {"command": "status", "description": "Show current bot booking state"},
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
            "❌ *Invalid /buy format.*\n\n"
            "Format:\n`/buy YYYYMMDD HH:MM`\n\n"
            "Examples:\n"
            "• `/buy 20270221 22:45`\n"
            "• `/buy 20270221 22:45 tebrau-woodlands`\n"
            "• `/buy-day/20261114-1230-woodlands-tebrau`"
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
        f"⚡ *Processing Instant Buy Request:*\n"
        f"• Date: *{display_date}*\n"
        f"• Train Time: *{time_fmt}*\n"
        f"• Route: *{origin} ➔ {destination}*\n"
        f"• Passengers: *{total_pax} Pax* ({pax_names})\n\n"
        f"Checking availability & booking via KTM Wallet now..."
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        submit_search(page, origin, destination, ktmb_date, "", pax=total_pax)

        btn, chosen_time, seats_avail, is_login_btn = find_matching_train_button(
            page, ".DepartSection", [time_fmt], min_seats_required=total_pax, date_label=ktmb_date
        )

        if not btn:
            send_telegram(
                f"❌ *Instant Buy Unavailable:*\n"
                f"{display_date} at {time_fmt} has fewer than {total_pax} seats available right now.\n"
                f"The 24/7 background sniper will continue monitoring for drops!"
            )
            browser.close()
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
                send_telegram(f"❌ Timed out waiting for reCAPTCHA/reservation on {display_date} at {time_fmt}.")
                browser.close()
                return

            ok = automate_passenger_and_checkout(
                page, payment_method, wallet_pin, pax_names, date_label=ktmb_date, train_time=chosen_time
            )
            if ok:
                state = load_state()
                prefix = "SUN" if "JB" in origin else "SAT"
                leg_key = f"{prefix}_{ktmb_date}"
                state[leg_key] = {"booked_pax": total_pax, "locked_train_time": chosen_time}
                save_state(state)
                notify(
                    "🎉 Instant Purchase Confirmed!",
                    f"Successfully booked {total_pax} tickets for {display_date} at {chosen_time} ({origin} ➔ {destination}) via KTM Wallet!",
                    page=page,
                )

        browser.close()


def start_telegram_command_listener():
    """Background listener for Telegram commands (/scrap-saturday-woodlands-tebrau, /help, etc.)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return

    register_telegram_bot_commands(token)
    print("[*] Telegram command listener started (listening for /scrap, /help, /status)")
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

                # Normalize command if bot username is appended (e.g. /help@MyBot)
                cmd_clean = text.split("@")[0].lower()

                if cmd_clean.startswith("/buy"):
                    print(f"[*] Received Telegram /buy command: '{text}' from chat {chat_id}")
                    threading.Thread(
                        target=execute_direct_buy,
                        args=(text, chat_id),
                        daemon=True,
                    ).start()
                elif cmd_clean.startswith("/scrap"):
                    print(f"[*] Received Telegram command: '{text}' from chat {chat_id}")
                    send_telegram(f"⏳ Generating available seats PDF report for `{text}`... Please wait ~10-15s.")
                    threading.Thread(
                        target=handle_scrape_command,
                        args=(text, chat_id),
                        daemon=True,
                    ).start()
                elif cmd_clean == "/status":
                    state = load_state()
                    summary_lines = []
                    for k, v in state.items():
                        summary_lines.append(f"• *{k}*: {v.get('booked_pax', 0)} pax booked at {v.get('locked_train_time')}")
                    state_msg = "\n".join(summary_lines) if summary_lines else "No bookings recorded yet."
                    send_telegram(f"📋 *Current Bot Booking State:*\n\n{state_msg}")
                elif cmd_clean in ("/help", "/start"):
                    help_msg = (
                        "🤖 *KTMB Shuttle Tebrau Bot Commands*\n\n"
                        "⚡ *Instant Buy Tickets:*\n"
                        "• `/buy YYYYMMDD HH:MM`\n"
                        "  _Instantly checks, locks & pays via KTM Wallet for 2 Pax. Examples:_\n"
                        "  `/buy 20270221 22:45`\n"
                        "  `/buy 20270207 20:15 tebrau-woodlands`\n"
                        "  `/buy 20261114 12:30 woodlands-tebrau`\n\n"
                        "📌 *On-Demand PDF Reports:*\n"
                        "• `/scrap-saturday-woodlands-tebrau`\n"
                        "  _Generates a PDF of all released Saturdays (Woodlands CIQ ➔ JB Sentral) showing only available seats._\n\n"
                        "• `/scrap-sunday-tebrau-woodlands`\n"
                        "  _Generates a PDF of all released Sundays (JB Sentral ➔ Woodlands CIQ) showing only available seats._\n\n"
                        "• `/scrap-day/YYYYMMDD-departure-arrival`\n"
                        "  _Generates a PDF for a specific date and route. Examples:_\n"
                        "  `/scrap-day/20261114-woodlands-tebrau`\n"
                        "  `/scrap-day/20261115-tebrau-woodlands`\n"
                        "  `/scrap-day/20270207-jb-woodlands`\n\n"
                        "📌 *Status & Monitoring:*\n"
                        "• `/status` — View which dates/trains have been booked so far."
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
    parser.add_argument("--max-weeks", type=int, default=int(os.getenv("MAX_SCAN_WEEKS", "36")), help="Max weeks ahead to scan until end of released calendar")
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
                single_sweep=False,
                is_manual_date=bool(args.sun_date),
                max_weeks=args.max_weeks,
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
                single_sweep=False,
                is_manual_date=bool(args.sat_date),
                max_weeks=args.max_weeks,
            )
        else:  # auto_weekend
            # Alternate sweeping all Saturdays and all Sundays
            while True:
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
                    single_sweep=True,
                    is_manual_date=bool(args.sat_date),
                    max_weeks=args.max_weeks,
                )
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
                    single_sweep=True,
                    is_manual_date=bool(args.sun_date),
                    max_weeks=args.max_weeks,
                )


if __name__ == "__main__":
    main()
