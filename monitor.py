import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

API_URL = "https://www.elal.com/api/SeatAvailability/lang/heb/flights"
SEAT_PAGE_URL = "https://www.elal.com/heb/seat-availability"

STATE_FILE = Path("state.json")
CHECK_INTERVAL_SECONDS = 15 * 60  # 15 minutes

# ---------------------------------------------------------------------------
# Logging — writes to both console and monitor.log
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            log.warning("state.json is corrupt or empty — starting fresh.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# El Al API
# ---------------------------------------------------------------------------
def fetch_api_data() -> dict:
    """
    Uses Playwright to load the El Al seat page (which passes Reblaze WAF),
    and captures the API response that the page naturally makes during load.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            api_data: dict = {}

            def on_response(response):
                if "SeatAvailability" in response.url:
                    log.info(f"[browser] Intercepted API call → HTTP {response.status} from {response.url}")
                    if response.status == 200:
                        try:
                            api_data["result"] = response.json()
                        except Exception as exc:
                            log.warning(f"[browser] Could not parse API response body: {exc}")

            page.on("response", on_response)
            log.info("[browser] Loading seat page...")
            nav = page.goto(SEAT_PAGE_URL, wait_until="networkidle", timeout=120_000)
            log.info(f"[browser] Page HTTP status: {nav.status if nav else 'unknown'}")

            if api_data.get("result"):
                log.info("[browser] Successfully captured API data from page load.")
                return api_data["result"]

            raise Exception("Page did not call the SeatAvailability API — cannot obtain data")
        finally:
            browser.close()


def parse_available_to_israel(data: dict) -> dict:
    """
    Returns a dict keyed by "<flightNumber>|<date>" containing flight details
    for every flight *to Israel* that currently has at least one available seat.
    """
    available = {}
    for route in data.get("flightsToIsrael", []):
        for flight in route.get("flights", []):
            if not flight.get("isFlightAvailable"):
                continue
            for fd in flight.get("flightsDates", []):
                seat_count = fd.get("seatCount", 0)
                seat_type = fd.get("seatType", "")
                # Skip dates with no seats or blocked seats
                if not seat_count or seat_count <= 0 or seat_type == "N/A":
                    continue
                key = f"{flight['flightNumber']}|{fd['flightsDate']}"
                available[key] = {
                    "flightNumber": flight["flightNumber"],
                    "date": fd["flightsDate"],
                    "seatCount": seat_count,
                    "seatType": seat_type,
                    "from": flight.get("routeFrom", "?"),
                    "to": flight.get("routeTo", "TLV"),
                    "depTime": flight.get("segmentDepTime", "?"),
                }
    return available


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def format_alert(new_flights: list[dict]) -> str:
    lines = [f"✈️ <b>El Al — {len(new_flights)} new flight(s) to Israel!</b>\n"]
    for f in sorted(new_flights, key=lambda x: (x["date"], x["flightNumber"])):
        seat_label = f"{f['seatCount']} seat(s)"
        if f["seatType"] and f["seatType"] not in ("", "N/A"):
            seat_label += f" [{f['seatType']}]"
        lines.append(
            f"🗓 <b>{f['date']}</b>   {f['flightNumber']}\n"
            f"   {f['from']} → {f['to']}   🕐 {f['depTime']}\n"
            f"   💺 {seat_label}"
        )
    lines.append("\n🔗 https://www.elal.com/heb/seat-availability")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------
def check_once() -> None:
    data = fetch_api_data()
    current = parse_available_to_israel(data)
    previous = load_state()

    new_keys = set(current.keys()) - set(previous.keys())
    gone_keys = set(previous.keys()) - set(current.keys())

    if new_keys:
        new_flights = [current[k] for k in new_keys]
        message = format_alert(new_flights)
        send_telegram(message)
        log.info("Alert sent for %d new flight(s): %s", len(new_keys), sorted(new_keys))
    else:
        msg = f"✅ No new flights. {len(current)} available flight+date pair(s) currently."
        if gone_keys:
            msg += f"\n🪑 {len(gone_keys)} pair(s) sold out since last check."
        send_telegram(msg)
        log.info("No new flights. %d available, %d gone.", len(current), len(gone_keys))

    save_state(current)


def main() -> None:
    log.info("El Al monitor starting up.")
    send_telegram("🟢 <b>El Al monitor started.</b>\nChecking every 15 minutes for flights to Israel.")

    while True:
        try:
            check_once()
        except Exception as e:
            log.error("Error during check: %s — will retry next cycle.", e)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
