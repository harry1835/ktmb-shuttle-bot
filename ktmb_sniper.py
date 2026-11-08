#!/usr/bin/env python3
"""
KTMB Shuttle Tebrau Automated Sniper & Checkout Bot (Linux LXC & macOS Ready)
Portal: https://shuttleonline.ktmb.com.my/Home/Shuttle

Key Features:
1. Designed for 24/7 execution inside a Linux LXC container using Xvfb + NoVNC
   (virtual X11 display so Chromium runs with `headless=False` to evade bot detection).
2. Telegram Bot notifications (with live screenshots) whenever a seat is locked,
   a reCAPTCHA challenge appears, or a ticket purchase completes.
3. Smart 2-Ticket Split Sniping (`SPLIT_PAX_SNIPE=true`):
   - On sold-out trains, single cancelled tickets (1 seat) drop much more frequently
     than 2 seats at once.
   - Searches with `1 Pax` if only 1 seat is available (or `2 Pax` if >=2 are available),
     books Passenger 1 first, and automatically loops back to book Passenger 2 on the
     same train!
4. Persistent State (`bookings_state.json`):
   - Remembers which weekend legs and passengers have already been booked so a
     systemd restart never double-books the same ticket.
"""

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

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
            # Use curl for simple multipart upload without extra dependencies
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


def get_next_weekend_dates(weeks_ahead: int = 0) -> tuple[str, str]:
    """
    Calculate the upcoming Saturday and Sunday dates formatted as 'D MMM YYYY'
    (matching KTMB's Lightpick format, e.g., '14 Nov 2026' or '5 Dec 2026').
    """
    today = datetime.date.today()
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0 and datetime.datetime.now().hour >= 14:
        days_until_saturday = 7
    target_sat = today + datetime.timedelta(days=days_until_saturday + (weeks_ahead * 7))
    target_sun = target_sat + datetime.timedelta(days=1)
    sat_str = f"{target_sat.day} {target_sat.strftime('%b %Y')}"
    sun_str = f"{target_sun.day} {target_sun.strftime('%b %Y')}"
    return sat_str, sun_str


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

        # Check if login modal reported an error (e.g., Incorrect email or password)
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
    page.wait_for_timeout(300)


def submit_search(
    page: Page,
    origin: str,
    destination: str,
    onward_date: str,
    return_date: str = "",
    pax: int = 1,
):
    """Fill the KTMB Shuttle search form and navigate to /ShuttleTrip (no login required to poll seats!)."""
    print(f"[*] Navigating to {SHUTTLE_URL} for search...")
    page.goto(SHUTTLE_URL, wait_until="domcontentloaded")
    page.wait_for_selector("#FromStationId", state="visible", timeout=15_000)
    page.wait_for_timeout(400)

    current_from = page.input_value("#FromStationId").strip().upper()
    if current_from != origin.upper():
        print(f"[*] Swapping terminal direction to: {origin} -> {destination}")
        page.evaluate("SwapFromToTerminal()")
        page.wait_for_timeout(200)

    print(
        f"[*] Searching: {origin} -> {destination} | Depart: {onward_date} | Return: {return_date or 'One-Way'} | Pax: {pax}"
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


def refresh_trips_in_place(page: Page, onward_date: str, return_date: str = ""):
    """
    Fast AJAX refresh of train availability on /ShuttleTrip without full page reload.
    Calls KTMB's internal ChangeDate() / GetTripToken flow (~300ms response).
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
        [onward_date, return_date],
    )
    page.wait_for_timeout(600)
    wait_for_trips_loaded(page)


def find_matching_train_button(
    page: Page,
    section_selector: str,
    preferred_times: list[str],
    min_seats_required: int = 1,
):
    """
    Scan `.DepartSection` or `.ReturnSection` for the first available train matching `preferred_times`.
    Works whether logged in (`.btn-seat-layout`) OR not logged in yet (`.btn-login`)!
    Returns `(button_locator, time_str, seats_avail, is_login_btn)` or `(None, None, max_seen_seats, False)`.
    """
    max_seen = 0
    for time_str in preferred_times:
        hm = time_str.replace(":", "").strip()
        row_selector = f"{section_selector} tbody tr[data-hourminute='{hm}']"
        row = page.locator(row_selector)
        if row.count() == 0:
            continue

        row_el = row.first
        seats_text = row_el.locator("td").nth(4).inner_text().strip()
        seats_match = re.search(r"(\d+)", seats_text)
        seats_avail = int(seats_match.group(1)) if seats_match else 0
        max_seen = max(max_seen, seats_avail)

        if seats_avail >= min_seats_required:
            train_name = row_el.locator("td").nth(0).inner_text().strip()
            select_btn = row_el.locator(".btn-seat-layout:not(.disabled)")
            login_btn = row_el.locator(".btn-login")
            if select_btn.count() > 0:
                print(f"[+] FOUND AVAILABLE TRAIN: {train_name} at {time_str} ({seats_avail} seats left!)")
                return select_btn.first, time_str, seats_avail, False
            elif login_btn.count() > 0:
                print(f"[+] FOUND AVAILABLE TRAIN (unauthenticated): {train_name} at {time_str} ({seats_avail} seats left!)")
                return login_btn.first, time_str, seats_avail, True
        else:
            print(f"    [-] {time_str}: {seats_avail} seats (need >= {min_seats_required})")

    return None, None, max_seen, False


def handle_recaptcha_if_prompted(page: Page):
    """
    Handle KTMB's `#recaptcha-modal` on `/ShuttleTrip/Reserve`.
    Attempts to click the reCAPTCHA checkbox automatically; if a visual challenge pops up,
    sends a Telegram alert with screenshot + NoVNC link so you can tap it on your phone.
    """
    page.wait_for_timeout(800)
    modal = page.locator("#recaptcha-modal")
    if modal.count() > 0 and modal.is_visible():
        print("[*] reCAPTCHA modal detected. Attempting checkbox click...")
        try:
            frame = page.frame_locator("iframe[title='reCAPTCHA']").first
            checkbox = frame.locator("#recaptcha-anchor")
            if checkbox.count() > 0:
                checkbox.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass

        # Check if still visible (image challenge triggered)
        if modal.is_visible() and "/BookShuttle" not in page.url:
            vnc_url = os.getenv("VNC_PUBLIC_URL", "")
            notify(
                "reCAPTCHA Challenge",
                f"Train found! Please solve the reCAPTCHA in NoVNC to lock the seat: {vnc_url}",
                page=page,
            )
            page.wait_for_function(
                """() => {
                    const m = document.querySelector('#recaptcha-modal');
                    return (!m || (!m.classList.contains('show') && m.style.display === 'none')) || window.location.pathname.includes('BookShuttle');
                }""",
                timeout=180_000,
            )
            print("[+] reCAPTCHA solved!")


def automate_passenger_and_checkout(
    page: Page,
    payment_method: str,
    wallet_pin: str,
    passenger_matches: list[str] | None = None,
) -> bool:
    """
    Complete `/BookShuttle` passenger selection and KTM Wallet payment.
    Returns True if checkout flow was triggered/completed.
    """
    print("[*] Waiting for Passenger Details page (/BookShuttle)...")
    page.wait_for_url("**/BookShuttle**", timeout=30_000)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)

    notify(
        "KTMB Seat Locked!",
        "Seat(s) reserved in cart for 10 minutes! Auto-filling passenger & payment details...",
        page=page,
    )

    # 1. Select favorite passenger(s)
    try:
        if passenger_matches:
            for p_match in passenger_matches:
                if not p_match:
                    continue
                matched_el = page.locator(f"label:has-text('{p_match}'), option:has-text('{p_match}'), tr:has-text('{p_match}')")
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

    # 2. Check Terms & Conditions checkboxes
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

    # 3. Proceed to KTM Wallet checkout
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
                    notify("KTMB Checkout Triggered", "KTM Wallet payment submitted! Check browser/Telegram screenshot.", page=page)
                    return True
        except Exception as e:
            print(f"[!] Partial auto-checkout note: {e}")

    vnc_url = os.getenv("VNC_PUBLIC_URL", "")
    notify(
        "Complete Checkout",
        f"Seat is held in cart for 10 mins. Verify/finish checkout at {vnc_url}",
        page=page,
    )
    # Wait up to 10 minutes (cart hold duration) for checkout completion
    try:
        page.wait_for_url("**/Payment/**|**/Summary**", timeout=600_000)
        return True
    except PlaywrightTimeoutError:
        return False


def snipe_one_way_leg(
    page: Page,
    leg_key: str,
    origin: str,
    destination: str,
    target_date: str,
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
    Snipes `total_pax` seats for a single direction (`WOODLANDS CIQ -> JB SENTRAL` or reverse).
    If `split_pax=True` and `total_pax=2`:
    - Searches with `1 Pax` so even a 1-seat cancellation is clickable!
    - Books Passenger 1 on the first drop, then loops back to book Passenger 2 on the exact same train time!
    """
    state = load_state()
    leg_state = state.get(leg_key, {"booked_pax": 0, "locked_train_time": None})
    booked_so_far = int(leg_state.get("booked_pax", 0))
    locked_time = leg_state.get("locked_train_time")

    if booked_so_far >= total_pax:
        print(f"[+] {leg_key} ({target_date}) already has {booked_so_far}/{total_pax} tickets booked. Skipping!")
        return

    while booked_so_far < total_pax:
        remaining = total_pax - booked_so_far
        search_pax = 1 if (split_pax and remaining >= 1) else remaining
        times_to_check = [locked_time] if locked_time else preferred_times

        print(
            f"\n[*] Sniping {origin} -> {destination} on {target_date} "
            f"(Booked: {booked_so_far}/{total_pax} | Searching {search_pax} Pax | Target times: {times_to_check})"
        )
        submit_search(page, origin, destination, target_date, "", pax=search_pax)

        attempt = 1
        while True:
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{now_str}] Poll #{attempt} for {target_date} ({origin} -> {destination})...")

            btn, chosen_time, seats_avail, is_login_btn = find_matching_train_button(
                page, ".DepartSection", times_to_check, min_seats_required=search_pax
            )

            # Bonus: if we need 2 tickets, split_pax is on (so we searched 1 Pax),
            # and >=2 seats suddenly dropped at once, we can book 1 now and immediately grab the 2nd!
            if btn:
                if is_login_btn:
                    print(f"[!] Seat found on {chosen_time} ({seats_avail} available)! Logging in via SSO now...")
                    btn.click()
                    page.wait_for_url("**/Account/Login**", timeout=30_000)
                    ensure_logged_in(page, email, password)
                    # Re-locate actual .btn-seat-layout after SSO redirects back to /ShuttleTrip
                    btn, chosen_time, seats_avail, _ = find_matching_train_button(
                        page, ".DepartSection", [chosen_time], min_seats_required=search_pax
                    )

                if btn:
                    print(f"[!] Clicking Select on {chosen_time} ({seats_avail} available)...")
                    btn.click()
                    handle_recaptcha_if_prompted(page)

                    proceed_btn = page.locator(".btn-passenger")
                    if proceed_btn.count() > 0 and proceed_btn.is_visible():
                        proceed_btn.click()

                    current_pax_match = [pax_names[booked_so_far]] if booked_so_far < len(pax_names) else None
                    ok = automate_passenger_and_checkout(page, payment_method, wallet_pin, current_pax_match)
                    if ok:
                        booked_so_far += search_pax
                        leg_state["booked_pax"] = booked_so_far
                        leg_state["locked_train_time"] = chosen_time
                        state[leg_key] = leg_state
                        save_state(state)
                        print(f"[+] Booked {booked_so_far}/{total_pax} for {leg_key} at {chosen_time}!")
                    break

            attempt += 1
            time.sleep(poll_interval)
            if attempt % 20 == 0:
                submit_search(page, origin, destination, target_date, "", pax=search_pax)
            else:
                refresh_trips_in_place(page, target_date, "")


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="KTMB Shuttle Tebrau Ticket Sniper & Auto-Buyer")
    parser.add_argument(
        "--mode",
        choices=["auto_weekend", "roundtrip", "single_sat", "single_sun"],
        default=os.getenv("BOOKING_MODE", "auto_weekend"),
        help="Booking mode: 'auto_weekend' (snipes Sat then Sun), 'single_sat', 'single_sun', or 'roundtrip'",
    )
    parser.add_argument("--sat-date", default=os.getenv("TARGET_SATURDAY_DATE", ""), help="Saturday date, e.g. '14 Nov 2026'")
    parser.add_argument("--sun-date", default=os.getenv("TARGET_SUNDAY_DATE", ""), help="Sunday date, e.g. '15 Nov 2026'")
    parser.add_argument("--weeks-ahead", type=int, default=0, help="Target weekend N weeks ahead (0 = upcoming weekend)")
    parser.add_argument("--interval", type=float, default=float(os.getenv("POLL_INTERVAL_SECONDS", "4")), help="Polling interval in seconds")
    parser.add_argument("--wait-for-drop", action="store_true", help="Wait until 08:29:55 AM SGT/MYT before starting high-speed sniping")
    args = parser.parse_args()

    email = os.getenv("KTMB_EMAIL", "")
    password = os.getenv("KTMB_PASSWORD", "")
    total_pax = int(os.getenv("PAX_COUNT", "2"))
    split_pax = os.getenv("SPLIT_PAX_SNIPE", "true").lower() in ("1", "true", "yes")
    sat_times = [t.strip() for t in os.getenv("SATURDAY_PREFERRED_TIMES", "12:30,13:45").split(",") if t.strip()]
    sun_times = [t.strip() for t in os.getenv("SUNDAY_PREFERRED_TIMES", "20:15,19:00,21:30").split(",") if t.strip()]
    payment_method = os.getenv("PAYMENT_METHOD", "KTM_WALLET")
    wallet_pin = os.getenv("KTM_WALLET_PIN", "")
    headless = os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes")
    pax_names = [
        os.getenv("PASSENGER_1_MATCH", "").strip(),
        os.getenv("PASSENGER_2_MATCH", "").strip(),
    ]

    sat_date, sun_date = args.sat_date, args.sun_date
    if not sat_date or not sun_date:
        auto_sat, auto_sun = get_next_weekend_dates(args.weeks_ahead)
        sat_date = sat_date or auto_sat
        sun_date = sun_date or auto_sun

    print("=" * 72)
    print("KTMB SHUTTLE TEBRAU SNIPER (LINUX LXC / XVFB EDITION)")
    print(f"  Mode               : {args.mode}")
    print(f"  Saturday (SG -> JB): {sat_date} | Trains: {sat_times}")
    print(f"  Sunday   (JB -> SG): {sun_date} | Trains: {sun_times}")
    print(f"  Total Tickets      : {total_pax} Pax (Split 1+1 Snipe: {split_pax})")
    print(f"  Poll Interval      : {args.interval}s")
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

        if args.mode in ("auto_weekend", "single_sat"):
            snipe_one_way_leg(
                page=page,
                leg_key=f"SAT_{sat_date}",
                origin="WOODLANDS CIQ",
                destination="JB SENTRAL",
                target_date=sat_date,
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

        if args.mode in ("auto_weekend", "single_sun"):
            snipe_one_way_leg(
                page=page,
                leg_key=f"SUN_{sun_date}",
                origin="JB SENTRAL",
                destination="WOODLANDS CIQ",
                target_date=sun_date,
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

        if args.mode == "roundtrip":
            submit_search(page, "WOODLANDS CIQ", "JB SENTRAL", sat_date, sun_date, pax=total_pax)
            attempt = 1
            while True:
                print(f"\n[Roundtrip Poll #{attempt}] Checking {sat_date} & {sun_date} ({total_pax} Pax)...")
                sat_btn, sat_time, _, sat_is_login = find_matching_train_button(page, ".DepartSection", sat_times, total_pax)
                sun_btn, sun_time, _, _ = find_matching_train_button(page, ".ReturnSection", sun_times, total_pax)
                if sat_btn and sun_btn:
                    if sat_is_login:
                        sat_btn.click()
                        page.wait_for_url("**/Account/Login**", timeout=30_000)
                        ensure_logged_in(page, email, password)
                        sat_btn, sat_time, _, _ = find_matching_train_button(page, ".DepartSection", [sat_time], total_pax)
                    if sat_btn:
                        sat_btn.click()
                        page.wait_for_timeout(300)
                        sun_btn, _, _, _ = find_matching_train_button(page, ".ReturnSection", [sun_time], total_pax)
                        if sun_btn:
                            sun_btn.click()
                            handle_recaptcha_if_prompted(page)
                            proceed_btn = page.locator(".btn-passenger")
                            if proceed_btn.count() > 0 and proceed_btn.is_visible():
                                proceed_btn.click()
                            automate_passenger_and_checkout(page, payment_method, wallet_pin, pax_names)
                            break
                attempt += 1
                time.sleep(args.interval)
                if attempt % 20 == 0:
                    submit_search(page, "WOODLANDS CIQ", "JB SENTRAL", sat_date, sun_date, pax=total_pax)
                else:
                    refresh_trips_in_place(page, sat_date, sun_date)


if __name__ == "__main__":
    main()
