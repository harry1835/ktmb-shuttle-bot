#!/usr/bin/env python3
"""
KTMB Shuttle Tebrau PDF Availability Report Generator
Scrapes live available seats for requested dates and generates a formatted PDF sent via Telegram.
"""

import datetime
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_URL = "https://shuttleonline.ktmb.com.my"
SHUTTLE_URL = f"{BASE_URL}/Home/Shuttle"
REPORTS_DIR = Path(__file__).parent / "reports"


def send_telegram_document(pdf_path: Path, caption: str, chat_id: str | None = None):
    """Sends a PDF document to Telegram using the bot API."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not target_chat:
        print("[!] Cannot send Telegram document: missing token or chat ID.")
        return

    try:
        subprocess.run(
            [
                "curl",
                "-s",
                "-X",
                "POST",
                f"https://api.telegram.org/bot{token}/sendDocument",
                "-F",
                f"chat_id={target_chat}",
                "-F",
                f"document=@{pdf_path}",
                "-F",
                f"caption={caption}",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[+] Sent PDF report to Telegram ({target_chat}): {pdf_path.name}")
    except Exception as e:
        print(f"[!] Error sending Telegram document: {e}")


def format_ktmb_date(d: datetime.date) -> str:
    """Format date as 'D MMM YYYY' (e.g. '14 Nov 2026')."""
    return f"{d.day} {d.strftime('%b %Y')}"


def parse_day_argument(date_str: str) -> tuple[str, str]:
    """
    Parses 'YYYYMMDD' (e.g. '20261115') into ('15 Nov 2026', 'Sun, 15 Nov 2026').
    """
    cleaned = re.sub(r"[^\d]", "", date_str)
    if len(cleaned) == 8:
        y, m, d = int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:8])
        dt = datetime.date(y, m, d)
        return format_ktmb_date(dt), dt.strftime("%a, %d %b %Y")
    raise ValueError(f"Invalid date format: {date_str}. Expected YYYYMMDD (e.g. 20261115).")


def get_all_weekday_dates(weekday: int, max_weeks: int = 24) -> list[tuple[str, str]]:
    """Returns list of (ktmb_date_str, display_label) for upcoming weekdays (5=Sat, 6=Sun)."""
    today = datetime.date.today()
    days_ahead = (weekday - today.weekday()) % 7
    if days_ahead == 0 and datetime.datetime.now().hour >= 20:
        days_ahead = 7
    first_date = today + datetime.timedelta(days=days_ahead)
    results = []
    for w in range(max_weeks):
        dt = first_date + datetime.timedelta(days=w * 7)
        results.append((format_ktmb_date(dt), dt.strftime("%a, %d %b %Y")))
    return results


def normalize_station_name(name: str) -> str:
    """Map common station aliases to KTMB internal station names."""
    n = name.strip().lower()
    if any(k in n for k in ["woodland", "woodlands", "sg", "singapore", "ciq"]):
        return "WOODLANDS CIQ"
    if any(k in n for k in ["tebrau", "jb", "johor", "sentral"]):
        return "JB SENTRAL"
    return "WOODLANDS CIQ"


def generate_pdf_report(
    origin: str,
    destination: str,
    date_tuples: list[tuple[str, str]],
    output_filename: str = "ktmb_available_seats.pdf",
) -> Path:
    """
    Scrapes KTMB Shuttle for the given dates, filters only slots with available seats,
    renders a PDF report using Playwright, and returns the output path.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = REPORTS_DIR / output_filename
    available_data = []  # list of dicts: {date_label, train, dep, arr, seats, fare}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        print(f"[*] Scraping availability: {origin} -> {destination} across {len(date_tuples)} date(s)...")
        page.goto(SHUTTLE_URL, wait_until="domcontentloaded")
        page.wait_for_selector("#FromStationId", state="visible", timeout=15_000)

        current_from = page.input_value("#FromStationId").strip().upper()
        if current_from != origin.upper():
            page.evaluate("SwapFromToTerminal()")
            page.wait_for_timeout(200)

        first_date, _ = date_tuples[0]
        page.select_option("#PassengerCount", "1")
        page.evaluate(
            """([onward]) => {
                document.getElementById("OnwardDate").value = onward;
                document.getElementById("ReturnDate").value = "";
                if (window.jQuery && $('#theForm').length) {
                    $('#theForm').submit();
                } else {
                    document.getElementById('btnSubmit').click();
                }
            }""",
            [first_date],
        )
        page.wait_for_url("**/ShuttleTrip**", timeout=30_000)
        page.wait_for_timeout(1000)

        for idx, (ktmb_date, display_label) in enumerate(date_tuples):
            if idx > 0:
                page.evaluate(f"ChangeDate($(id_formSearchData).val(), '{ktmb_date}', false, 1)")
                try:
                    page.wait_for_function(
                        f"() => {{ const el = document.querySelector('.depart-date-input'); return el && el.innerText.includes('{ktmb_date}'); }}",
                        timeout=12_000,
                    )
                except Exception:
                    pass
                page.wait_for_timeout(300)

            rows = page.locator(".depart-trips tr[data-hourminute]")
            count = rows.count()
            if count == 0:
                # End of released calendar
                print(f"    [{display_label}] Reached end of released calendar.")
                break

            day_avail_count = 0
            for r_idx in range(count):
                row = rows.nth(r_idx)
                train_name = row.locator("td").nth(0).inner_text().strip()
                dep_time = row.locator("td").nth(1).inner_text().strip()
                arr_time = row.locator("td").nth(2).inner_text().strip()
                seats_text = row.locator("td").nth(4).inner_text().strip()
                seats_match = re.search(r"(\d+)", seats_text)
                seats = int(seats_match.group(1)) if seats_match else 0
                fare = row.locator("td").nth(5).inner_text().strip()

                if seats > 0:
                    day_avail_count += 1
                    available_data.append(
                        {
                            "date_label": display_label,
                            "train": train_name,
                            "dep": dep_time,
                            "arr": arr_time,
                            "seats": seats,
                            "fare": fare,
                        }
                    )
            print(f"    [{display_label}] Scanned {count} trains -> {day_avail_count} slots with seats.")

        # Build clean HTML for PDF rendering
        now_str = datetime.datetime.now().strftime("%d %b %Y, %H:%M MYT")
        rows_html = ""
        if available_data:
            current_date = ""
            for item in available_data:
                date_cell = item["date_label"] if item["date_label"] != current_date else ""
                current_date = item["date_label"]
                badge_color = "#10b981" if item["seats"] >= 10 else "#f59e0b"
                rows_html += f"""
                <tr>
                    <td style="font-weight: 600;">{date_cell}</td>
                    <td>{item['train']}</td>
                    <td style="font-weight: 700; color: #1e3a8a;">{item['dep']}</td>
                    <td>{item['arr']}</td>
                    <td><span class="badge" style="background-color: {badge_color};">{item['seats']} seats</span></td>
                    <td>{item['fare']}</td>
                </tr>
                """
        else:
            rows_html = """
            <tr>
                <td colspan="6" style="text-align: center; padding: 30px; color: #ef4444; font-weight: 600;">
                    No available seats found across the requested date(s). (All slots are currently Sold Out)
                </td>
            </tr>
            """

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="utf-8">
        <style>
          @page {{ size: A4; margin: 15mm; }}
          body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
            color: #1e293b;
            margin: 0;
            padding: 0;
          }}
          .header {{
            border-bottom: 3px solid #2563eb;
            padding-bottom: 12px;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
          }}
          h1 {{
            margin: 0 0 6px 0;
            color: #1e3a8a;
            font-size: 20px;
          }}
          .subtitle {{
            font-size: 13px;
            color: #475569;
          }}
          .timestamp {{
            font-size: 11px;
            color: #64748b;
            text-align: right;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            margin-top: 10px;
          }}
          th {{
            background-color: #f1f5f9;
            color: #334155;
            text-align: left;
            padding: 8px 10px;
            border-bottom: 2px solid #cbd5e1;
            font-weight: 600;
          }}
          td {{
            padding: 8px 10px;
            border-bottom: 1px solid #e2e8f0;
          }}
          tr:nth-child(even) {{
            background-color: #f8fafc;
          }}
          .badge {{
            color: white;
            padding: 3px 8px;
            border-radius: 9999px;
            font-weight: 600;
            font-size: 11px;
            display: inline-block;
          }}
          .footer {{
            margin-top: 25px;
            font-size: 10px;
            color: #94a3b8;
            text-align: center;
            border-top: 1px solid #e2e8f0;
            padding-top: 8px;
          }}
        </style>
        </head>
        <body>
          <div class="header">
            <div>
              <h1>KTMB Shuttle Tebrau &bull; Available Seats Report</h1>
              <div class="subtitle">Route: <b>{origin} &rarr; {destination}</b> | Filter: <b>Available Timeslots Only</b></div>
            </div>
            <div class="timestamp">
              Report Generated:<br><b>{now_str}</b>
            </div>
          </div>

          <table>
            <thead>
              <tr>
                <th style="width: 22%;">Date</th>
                <th style="width: 26%;">Train Service</th>
                <th style="width: 12%;">Departure</th>
                <th style="width: 12%;">Arrival</th>
                <th style="width: 16%;">Available</th>
                <th style="width: 12%;">Fare</th>
              </tr>
            </thead>
            <tbody>
              {rows_html}
            </tbody>
          </table>

          <div class="footer">
            Generated by KTMB Shuttle Bot &bull; Data source: https://shuttleonline.ktmb.com.my/Home/Shuttle
          </div>
        </body>
        </html>
        """

        page.set_content(html_content)
        page.pdf(path=str(pdf_path), format="A4", print_background=True)
        browser.close()

    print(f"[+] PDF report generated at: {pdf_path}")
    return pdf_path


def handle_scrape_command(command_text: str, chat_id: str | None = None) -> bool:
    """
    Parses user command and generates/sends the requested PDF report.
    Supported patterns:
      1. /scrap-saturday-woodlands-tebrau (or woodlands-jb)
      2. /scrap-sunday-tebrau-woodlands (or jb-woodlands)
      3. /scrap-day/YYYYMMDD-departure-arrival (e.g. /scrap-day/20261115-woodlands-tebrau)
      4. /scrap-YYYYMMDD-departure-arrival
    """
    cmd = command_text.strip().lower()

    # Pattern 1: /scrap-saturday-woodlands-tebrau
    if "saturday" in cmd and "woodland" in cmd:
        origin = "WOODLANDS CIQ"
        destination = "JB SENTRAL"
        dates = get_all_weekday_dates(5, max_weeks=22)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = f"saturday_woodlands_tebrau_{ts}.pdf"
        caption = f"📊 *KTMB Available Seats Report*\nRoute: Woodlands CIQ ➔ JB Sentral\nAll Released Saturdays"
        pdf = generate_pdf_report(origin, destination, dates, output_filename=fn)
        send_telegram_document(pdf, caption, chat_id)
        return True

    # Pattern 2: /scrap-sunday-tebrau-woodlands
    if "sunday" in cmd and ("tebrau" in cmd or "jb" in cmd):
        origin = "JB SENTRAL"
        destination = "WOODLANDS CIQ"
        dates = get_all_weekday_dates(6, max_weeks=22)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = f"sunday_tebrau_woodlands_{ts}.pdf"
        caption = f"📊 *KTMB Available Seats Report*\nRoute: JB Sentral ➔ Woodlands CIQ\nAll Released Sundays"
        pdf = generate_pdf_report(origin, destination, dates, output_filename=fn)
        send_telegram_document(pdf, caption, chat_id)
        return True

    # Pattern 3: /scrap-day/YYYYMMDD-departure-arrival
    match = re.search(r"(\d{8})-([a-z]+)-([a-z]+)", cmd)
    if match:
        raw_date, raw_dep, raw_arr = match.group(1), match.group(2), match.group(3)
        origin = normalize_station_name(raw_dep)
        destination = normalize_station_name(raw_arr)
        if origin == destination:
            destination = "JB SENTRAL" if origin == "WOODLANDS CIQ" else "WOODLANDS CIQ"

        try:
            ktmb_date, display_label = parse_day_argument(raw_date)
            fn = f"ktmb_{raw_date}_{raw_dep}_{raw_arr}.pdf"
            caption = f"📊 *KTMB Available Seats Report*\nDate: {display_label}\nRoute: {origin} ➔ {destination}"
            pdf = generate_pdf_report(origin, destination, [(ktmb_date, display_label)], output_filename=fn)
            send_telegram_document(pdf, caption, chat_id)
            return True
        except Exception as e:
            print(f"[!] Error handling date command: {e}")
            return False

    return False


if __name__ == "__main__":
    import sys
    test_cmd = sys.argv[1] if len(sys.argv) > 1 else "/scrap-saturday-woodlands-tebrau"
    print(f"Testing command: {test_cmd}")
    handle_scrape_command(test_cmd)
