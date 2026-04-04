import json
import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

API_URL = "https://www.elal.com/api/SeatAvailability/lang/heb/flights"
STATE_FILE = Path("state.json")
TRACKED_FILE = Path("tracked.json")
CHECK_INTERVAL = 15 * 60  # seconds

CONTINENT_HE = {
    "\u05d0\u05d9\u05e8\u05d5\u05e4\u05d4": "Europe",
    "\u05d0\u05de\u05e8\u05d9\u05e7\u05d4": "America",
    "\u05d0\u05e1\u05d9\u05d4": "Asia",
    "\u05d0\u05e4\u05e8\u05d9\u05e7\u05d4": "Africa",
    "\u05d0\u05d5\u05e7\u05d9\u05d0\u05e0\u05d9\u05d4": "Oceania",
}

CONTINENT_FLAG = {
    "Europe": "\U0001f30d",
    "America": "\U0001f30e",
    "Asia": "\U0001f30f",
    "Africa": "\U0001f30d",
    "Oceania": "\U0001f30f",
}

# ---------------------------------------------------------------------------
# Logging
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
def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            log.warning("%s is corrupt or empty — starting fresh.", path)
    return {}


def load_state() -> dict:
    return _load_json(STATE_FILE)


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_tracked() -> dict:
    return _load_json(TRACKED_FILE)


def save_tracked(tracked: dict) -> None:
    TRACKED_FILE.write_text(json.dumps(tracked, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# El Al API
# ---------------------------------------------------------------------------
def fetch_api_data() -> dict:
    resp = requests.get(API_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_available_to_israel(data: dict) -> dict:
    """
    Returns a dict keyed by "<flightNumber>|<date>" with flight details
    for every flight *to Israel* that currently has at least one available seat.
    """
    available = {}
    for route in data.get("flightsToIsrael", []):
        continent_he = route.get("continent", "")
        continent = CONTINENT_HE.get(continent_he, continent_he)
        city = route.get("routeFromCityName", route.get("routeFrom", "?"))
        for flight in route.get("flights", []):
            if not flight.get("isFlightAvailable"):
                continue
            for fd in flight.get("flightsDates", []):
                seat_count = fd.get("seatCount", 0)
                seat_type = fd.get("seatType", "")
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
                    "continent": continent,
                    "city": city,
                }
    return available


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _flight_line(f: dict) -> str:
    seat_label = f"{f['seatCount']} seat(s)"
    if f.get("seatType") and f["seatType"] not in ("", "N/A"):
        seat_label += f" [{f['seatType']}]"
    flag = CONTINENT_FLAG.get(f.get("continent", ""), "\u2708\ufe0f")
    return (
        f"{flag} <b>{f['date']}</b>  {f['flightNumber']}\n"
        f"   {f.get('city', f['from'])} ({f['from']}) \u2192 {f['to']}  \U0001f550 {f['depTime']}\n"
        f"   \U0001f4ba {seat_label}"
    )


def format_alert(new_flights: list) -> str:
    lines = [f"\u2708\ufe0f <b>El Al \u2014 {len(new_flights)} new flight(s) to Israel!</b>\n"]
    for f in sorted(new_flights, key=lambda x: (x["date"], x["flightNumber"])):
        lines.append(_flight_line(f))
    lines.append("\n\U0001f517 https://www.elal.com/heb/seat-availability")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Inline keyboard builders
# ---------------------------------------------------------------------------
def _continent_keyboard(available: dict) -> InlineKeyboardMarkup:
    counts: dict = {}
    for f in available.values():
        c = f.get("continent", "Other")
        counts[c] = counts.get(c, 0) + 1
    buttons = []
    row = []
    for continent, count in sorted(counts.items()):
        flag = CONTINENT_FLAG.get(continent, "\u2708\ufe0f")
        row.append(InlineKeyboardButton(f"{flag} {continent} ({count})", callback_data=f"filter:{continent}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    total = len(available)
    buttons.append([InlineKeyboardButton(f"\u2708\ufe0f All ({total})", callback_data="filter:__all__")])
    return InlineKeyboardMarkup(buttons)


def _track_keyboard(flights: list, tracked: dict) -> InlineKeyboardMarkup:
    buttons = []
    for f in flights[:25]:
        key = f"{f['flightNumber']}|{f['date']}"
        if key in tracked:
            label = f"\u274c Untrack {f['flightNumber']} {f['date']}"
            cb = f"untrack:{key}"
        else:
            label = f"\U0001f4cd Track {f['flightNumber']} {f['date']}"
            cb = f"track:{key}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "\U0001f44b <b>El Al Monitor Bot</b>\n\n"
        "/flights \u2014 Browse available flights to Israel\n"
        "/tracked \u2014 View &amp; manage tracked flights\n"
        "/help \u2014 Show this message",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_flights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = fetch_api_data()
        available = parse_available_to_israel(data)
    except Exception as e:
        await update.message.reply_text(f"\u274c Failed to fetch flights: {e}")
        return
    if not available:
        await update.message.reply_text("No available flights to Israel right now.")
        return
    kb = _continent_keyboard(available)
    context.user_data["available"] = available
    await update.message.reply_text(
        f"\U0001f30d <b>{len(available)} available flight+date pairs.</b>\nSelect a region:",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def cmd_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tracked = load_tracked()
    if not tracked:
        await update.message.reply_text("You are not tracking any flights.")
        return
    lines = ["\U0001f514 <b>Tracked flights:</b>\n"]
    for f in tracked.values():
        lines.append(_flight_line(f))
    text = "\n\n".join(lines)
    buttons = []
    for key in tracked:
        fn, date = key.split("|", 1)
        buttons.append([InlineKeyboardButton(f"\u274c Untrack {fn} {date}", callback_data=f"untrack:{key}")])
    kb = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data.startswith("filter:"):
        continent = data[len("filter:"):]
        available = context.user_data.get("available", {})
        if not available:
            try:
                api_data = fetch_api_data()
                available = parse_available_to_israel(api_data)
                context.user_data["available"] = available
            except Exception as e:
                await query.edit_message_text(f"\u274c Failed to fetch flights: {e}")
                return
        if continent == "__all__":
            flights = list(available.values())
        else:
            flights = [f for f in available.values() if f.get("continent") == continent]
        if not flights:
            await query.edit_message_text("No flights found for that region.")
            return
        tracked = load_tracked()
        label = continent if continent != "__all__" else "All regions"
        lines = [f"<b>{label} \u2014 {len(flights)} flight(s):</b>\n"]
        for f in sorted(flights, key=lambda x: (x["date"], x["flightNumber"])):
            lines.append(_flight_line(f))
        text = "\n\n".join(lines)
        kb = _track_keyboard(sorted(flights, key=lambda x: (x["date"], x["flightNumber"])), tracked)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif data.startswith("track:"):
        key = data[len("track:"):]
        available = context.user_data.get("available", {})
        flight = available.get(key)
        if not flight:
            await query.answer("Flight data expired \u2014 use /flights again.", show_alert=True)
            return
        tracked = load_tracked()
        tracked[key] = {**flight, "tracked_seat_count": flight["seatCount"]}
        save_tracked(tracked)
        fn, date = key.split("|", 1)
        await query.answer(f"\u2705 Tracking {fn} on {date}! You'll be notified of seat changes.", show_alert=True)

    elif data.startswith("untrack:"):
        key = data[len("untrack:"):]
        tracked = load_tracked()
        if key in tracked:
            del tracked[key]
            save_tracked(tracked)
        fn, date = key.split("|", 1)
        await query.answer(f"\U0001f5d1 Stopped tracking {fn} on {date}.", show_alert=True)


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------
async def check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    chat_id = TELEGRAM_CHAT_ID
    try:
        data = fetch_api_data()
        current = parse_available_to_israel(data)
        previous = load_state()

        new_keys = set(current.keys()) - set(previous.keys())
        gone_keys = set(previous.keys()) - set(current.keys())

        if new_keys:
            new_flights = [current[k] for k in new_keys]
            message = format_alert(new_flights)
            await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
            log.info("Alert sent for %d new flight(s): %s", len(new_keys), sorted(new_keys))
        else:
            msg = f"\u2705 No new flights. {len(current)} available flight+date pair(s) currently."
            if gone_keys:
                msg += f"\n\U0001fa91 {len(gone_keys)} pair(s) sold out since last check."
            await bot.send_message(chat_id=chat_id, text=msg)
            log.info("No new flights. %d available, %d gone.", len(current), len(gone_keys))

        save_state(current)

        # Check tracked flights for seat count changes
        tracked = load_tracked()
        changed = False
        for key, tf in list(tracked.items()):
            if key not in current:
                fn, date = key.split("|", 1)
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"\u26a0\ufe0f Tracked flight <b>{fn}</b> on <b>{date}</b> is no longer available.",
                    parse_mode="HTML",
                )
                del tracked[key]
                changed = True
            else:
                new_count = current[key]["seatCount"]
                old_count = tf.get("tracked_seat_count", tf.get("seatCount", 0))
                if new_count != old_count:
                    fn, date = key.split("|", 1)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"\U0001f514 <b>Tracked flight \u2014 seat count changed!</b>\n\n"
                            f"{_flight_line(current[key])}\n\n"
                            f"Seats: {old_count} \u2192 {new_count}"
                        ),
                        parse_mode="HTML",
                    )
                    tracked[key]["tracked_seat_count"] = new_count
                    tracked[key]["seatCount"] = new_count
                    changed = True
        if changed:
            save_tracked(tracked)

    except Exception as e:
        log.error("Error during check_job: %s", e)


# ---------------------------------------------------------------------------
# Post-init (startup message)
# ---------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "\U0001f7e2 <b>El Al monitor started.</b>\n"
            "Checking every 15 minutes for flights to Israel.\n"
            "Use /flights to browse now."
        ),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("flights", cmd_flights))
    app.add_handler(CommandHandler("tracked", cmd_tracked))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.job_queue.run_repeating(check_job, interval=CHECK_INTERVAL, first=15)

    log.info("El Al monitor starting up.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
