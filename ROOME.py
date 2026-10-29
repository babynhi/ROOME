import asyncio
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import discord
import gspread
from aiohttp import web  # installed with discord.py
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from gspread.utils import rowcol_to_a1

# ---------------------------------------------------------------- Config
load_dotenv()  # reads .env next to this script
TOKEN = os.environ["DISCORD_TOKEN"]
# the long ID in the sheet's URL (pasting the whole URL also works)
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"].strip()
if _m := re.search(r"/spreadsheets/d/([\w-]+)", GOOGLE_SHEET_ID):
    GOOGLE_SHEET_ID = _m.group(1)
GOOGLE_CREDENTIALS_FILE = os.environ.get(
    "GOOGLE_CREDENTIALS_FILE", "service_account.json")
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Requests")
SUBMISSIONS_CHANNEL_ID = int(os.environ.get("SUBMISSIONS_CHANNEL_ID", "0"))
# 600 is plenty once the Apps Script is set up
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
# role allowed to use /set-status: its ID or its exact name (e.g. "internal")
ADMIN_ROLE = os.environ.get("ADMIN_ROLE_ID", "").strip()
# Instant updates from the sheet (Apps Script). Leave WEBHOOK_SECRET empty to disable.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8080"))

ATTENDANCE_TYPES = [
    "Internal Members (by invitation only)",
    "External Members (by invitation only)",
    "Internal and External Members (by invitation only)",
    "Open to Internal Community",
    "Open to Public",
]
EXTERNAL_ATTENDANCE = ATTENDANCE_TYPES[1:3]  # these need a speaker list

BOOKING_TYPES = ["Classroom", "Event Space", "Information Table/ Kiosk",
                 "Reggie's", "Not sure what's best", "Other"]
# booking type -> (minimum notice, counted in business days?)
LEAD_TIMES = {
    "Classroom": (10, True),
    "Event Space": (20, True),
    "Information Table/ Kiosk": (15, False),
    "Reggie's": (25, False),
}
ROOM_CATALOGUE_URL = "https://docs.google.com/document/d/1D3kHhFFzJaAwt7RProGksD9EQZlnFwLykDCgqECllTE/edit?usp=sharing"
DSA_LIST_URL = "https://www.concordia.ca/content/dam/concordia/services/event-planning/docs/Concordia-DSA-List.pdf"
FOOD_PROCEDURES_URL = "https://www.concordia.ca/content/dam/concordia/services/event-planning/docs/Procedures-for-saleor-service-of-food.pdf"
FOOD_WAIVER_URL = "https://www.concordia.ca/content/dam/concordia/services/event-planning/docs/Concordia-Food-Waiver-Form.pdf"

STATUSES = ["Pending", "In Review", "Booked", "Rejected", "Cancelled"]
STATUS_COLORS = {
    "Pending": discord.Color.light_grey(),
    "In Review": discord.Color.gold(),
    "Booked": discord.Color.green(),
    "Rejected": discord.Color.red(),
    "Cancelled": discord.Color.dark_grey(),
}

# Sheet columns. The bot finds columns by header name, so you can reorder them.
COL_ID = "Request ID"
COL_SUBMITTED = "Submitted At"
COL_USER = "Discord User"
COL_USER_ID = "Discord User ID"
COL_CONTACT = "Onsite Contact"
COL_EMAIL = "Email"
COL_PHONE = "Phone"
COL_EVENT = "Event Name"
COL_DESC = "Description"
COL_ATTENDANCE = "Expected Attendance"
COL_ATT_TYPE = "Attendance Type"
COL_STATUS = "Status"
COL_NOTES = "Notes"
COL_NOTIFIED = "Bot: Last Notified Status"  # managed by the bot, hidden
COL_THREAD = "Bot: Discord Thread ID"        # managed by the bot, hidden

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_RE = re.compile(
    r"^(\d{1,2})(?:\s*([:h.])\s*(\d{2})?)?\s*([ap])?\.?\s*(?:m\.?)?$", re.I)
DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%B %d %Y",
                "%b %d %Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"]


# ---------------------------------------------------------------- Answer checks
# Each takes a page's answers, may tidy them in place,
# and returns a list of problems to show the user.
def parse_date(text: str) -> date | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


def parse_time(text: str) -> str | None:
    """'6pm', '6:30 PM', '18:00', '18h' -> 'HH:MM' (24h), or None."""
    m = TIME_RE.match(text.strip())
    if not m:
        return None
    hour, sep, minute, ampm = int(m[1]), m[2], int(m[3] or 0), (m[4] or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "p" else 0)
    elif not sep:  # a bare "6" could be morning or evening
        return None
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def check_contact(a: dict) -> list[str]:
    errors = []
    if not EMAIL_RE.match(a["contact_email"]):
        errors.append("• The email address doesn't look valid.")
    if len(re.sub(r"\D", "", a["contact_phone"])) < 10:
        errors.append("• The phone number needs at least 10 digits.")
    return errors


def check_attendance(a: dict) -> list[str]:
    value = a["expected_attendance"]
    if not value.isdigit() or int(value) <= 0:
        return ["• Expected attendance must be a positive whole number."]
    a["expected_attendance"] = str(int(value))
    return []


def check_av(a: dict) -> list[str]:
    if "No IT equipment required" in a["av_equipment"] and len(a["av_equipment"]) > 1:
        return ["• You picked equipment *and* \"No IT equipment required\". Pick one or the other."]
    return []


def check_date_time(a: dict) -> list[str]:
    errors = []
    day = parse_date(a["event_date"])
    if day is None:
        errors.append("• Write the date like 2026-11-20.")
    elif day < date.today():
        errors.append("• The date is in the past.")
    else:
        a["event_date"] = day.isoformat()
    start, end = parse_time(a["start_time"]), parse_time(a["end_time"])
    if start is None:
        errors.append("• The start time doesn't look valid (try 18:00 or 6pm).")
    if end is None:
        errors.append("• The end time doesn't look valid (try 20:00 or 8pm).")
    if start and end:
        if end <= start:
            errors.append("• The end time must be after the start time.")
        a["start_time"], a["end_time"] = start, end
    return errors


def check_location(a: dict) -> list[str]:
    if "Other" in a["locations"] and not a["venue"]:
        return ["• You ticked **Other**: name the building in the venue box."]
    return []


# ---------------------------------------------------------------- Form definition
@dataclass
class Field:
    key: str          # name in the answers dict
    column: str       # sheet header
    label: str        # max 45 characters
    kind: str = "short"  # short | paragraph | radio | checkbox
    description: str | None = None  # max 100 characters
    placeholder: str | None = None
    options: tuple[str, ...] = ()
    required: bool = True
    max_length: int | None = None
    when: Callable[[dict], bool] | None = None  # only asked if this returns True

    def applies(self, d: dict) -> bool:
        return self.when is None or self.when(d)

    def build(self, value) -> discord.ui.Item:
        if self.kind == "radio":
            return discord.ui.RadioGroup(required=self.required, options=[
                discord.RadioGroupOption(label=o, default=o == value) for o in self.options])
        if self.kind == "checkbox":
            chosen = value or []
            return discord.ui.CheckboxGroup(
                required=self.required, min_values=1 if self.required else 0,
                max_values=len(self.options), options=[
                    discord.CheckboxGroupOption(label=o, default=o in chosen) for o in self.options])
        paragraph = self.kind == "paragraph"
        return discord.ui.TextInput(
            style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
            placeholder=self.placeholder, required=self.required, default=value or None,
            max_length=self.max_length or (1000 if paragraph else 100),
        )


@dataclass
class Step:
    title: str
    fields: list[Field]
    intro: str = ""  # shown above the button that opens this page
    check: Callable[[dict], list[str]] | None = None

    def fields_for(self, d: dict) -> list[Field]:
        return [f for f in self.fields if f.applies(d)]


def _booking(*types: str) -> Callable[[dict], bool]:
    return lambda d: d.get("booking_type") in types


def _self_catering(d: dict) -> bool:
    return d.get("food") == "Self-catering"


def _external(d: dict) -> bool:
    return d.get("attendance_type") in EXTERNAL_ATTENDANCE


# Mirrors the Google Form "Space Booking 26/27". Discord allows 5 fields per pop-up,
# and a page whose fields all have a `when` that doesn't apply is skipped.
FORM = [
    Step("Contact & Event", [
        Field("contact_name", COL_CONTACT, "Onsite contact name",
              description="Person responsible for the event"),
        Field("contact_email", COL_EMAIL, "Onsite contact email",
              placeholder="name@example.com"),
        Field("contact_phone", COL_PHONE, "Onsite contact phone number",
              placeholder="514-555-0123", max_length=25),
        Field("event_name", COL_EVENT, "Name of event"),
        Field("event_description", COL_DESC, "Describe the event", "paragraph",
              description="Purpose & activities"),
    ], check=check_contact),
    Step("Attendance", [
        Field("expected_attendance", COL_ATTENDANCE, "Total expected attendance",
              placeholder="e.g. 40", max_length=6),
        Field("attendance_type", COL_ATT_TYPE, "Type of attendance", "radio",
              options=tuple(ATTENDANCE_TYPES)),
    ], check=check_attendance),
    Step("External Members", [
        Field("speakers", "Speakers", "Expected speakers", "paragraph", when=_external,
              description="Names and a brief bio of each expected speaker (a list of potential speakers is fine)"),
    ]),
    Step("Food & Alcohol", [
        Field("alcohol", "Alcohol", "Will you serve or sell alcohol?", "radio",
              options=("Yes", "No")),
        Field("food", "Food", "Food at your event?", "radio",
              description="Will you be self-catering or using an approved caterer?",
              options=("Self-catering", "Working with an approved caterer", "No food")),
    ]),
    Step("Self-Catered Event", [
        Field("food_risk", "Food Risk", "Will the food be high or low risk?", "radio",
              when=_self_catering, options=(
                  "Low risk (doesn't need to be kept hot/cold)",
                  "High risk (needs to be kept hot/cold)")),
        Field("food_equipment", "Food Equipment", "Food preparation & temperature equipment",
              "paragraph", when=_self_catering,
              description="What will be used onsite to prepare and/or keep foods at proper temperature?"),
        Field("food_items", "Food Items", "Food items you will serve", "paragraph",
              when=_self_catering, description="List all of them, e.g. cream cheese with bagels"),
    ], intro=(
        "A Food Waiver is **not** required for self-catered events serving or selling **low risk** food, "
        f"but you must follow the [Procedures for the Sale or Service of Food]({FOOD_PROCEDURES_URL}).\n"
        "**High risk** food contains milk products, eggs, meat or anything else that can grow bacteria, "
        "including cooked vegetables and grains and warm iced tea. If you are **selling** high risk food, "
        f"fill out the [Concordia Food Waiver]({FOOD_WAIVER_URL}) and request a Special Event Permit "
        "from the MAPAQ."
    )),
    Step("Event Details (1/2)", [
        Field("teaching", "Teaching / Third Party", "Teaching & learning activity?", "paragraph",
              description="If yes, are you working with a third party (internal or external)? Name the organization(s)."),
        Field("advertising", "Advertising", "Where will the event be advertised?", "paragraph"),
        Field("selling", "Selling Items", "Selling items on-site?", "paragraph",
              description="If yes, describe what items you plan to sell on-site."),
        Field("music", "Music", "Music during your event?", "paragraph",
              description="If yes, describe what you're planning in terms of music."),
        Field("hybrid", "Hybrid / Online", "Hybrid or online component?", "paragraph",
              description="e.g. speakers or attendees joining remotely. If yes, give as much detail as possible."),
    ]),
    Step("Event Details (2/2)", [
        Field("av_equipment", "AV / IT Equipment", "Audiovisual / IT equipment needed", "checkbox",
              options=("Projector", "Computer", "Audio (Speakers, Microphone)",
                       "Visual (Camera)", "No IT equipment required")),
        Field("scs_responsibility", "SCS Responsibility", "Is SCS fully responsible for this event?",
              "paragraph",
              description="Is SCS making the key decisions? If not, name the department or organization."),
        Field("other_details", "Other Details", "Any other details about your event?", "paragraph",
              required=False),
    ], check=check_av),
    Step("Time, Date & Location", [
        Field("event_date", "Date", "Date of event", placeholder="YYYY-MM-DD", max_length=20),
        Field("start_time", "Start Time", "Start time", placeholder="e.g. 18:00 or 6pm", max_length=10),
        Field("end_time", "End Time", "End time", placeholder="e.g. 20:00 or 8pm", max_length=10),
        Field("booking_type", "Booking Type", "What are you booking?", "radio",
              options=tuple(BOOKING_TYPES)),
    ], check=check_date_time, intro=(
        "Book ahead:\n"
        "• **Classroom**: at least 10 business days before (e.g. Algotime, info sessions, workshops, "
        "movie nights) in H, EV, MB or FG\n"
        "• **Event space**: at least 20 business days before (e.g. Wine & Cheese, career panel) in EV or MB\n"
        "• **Information table / kiosk**: at least 15 days before (e.g. clothing sales, recruitment booth) "
        "in H, EV or MB\n"
        "• **Reggie's**: at least 25 days before (e.g. Lizard Lounges)\n"
        f"For other rooms or set-ups, see the [Room Booking Catalogue]({ROOM_CATALOGUE_URL}) "
        f"and the [DSA List]({DSA_LIST_URL})."
    )),
    Step("Location", [
        Field("locations", "Preferred Locations", "Preferred building(s)", "checkbox",
              when=_booking("Classroom"), options=("H", "EV", "MB", "FG", "No preference", "Other")),
        Field("locations", "Preferred Locations", "Preferred building(s)", "checkbox",
              when=_booking("Event Space"), options=("EV", "MB", "No preference", "Other")),
        Field("locations", "Preferred Locations", "Preferred building(s)", "checkbox",
              when=_booking("Information Table/ Kiosk"), options=("H", "MB", "EV", "No preference", "Other")),
        Field("venue", "Desired Venue", "Desired classroom", required=False,
              when=_booking("Classroom"),
              description="Leave empty if you have no preference. Ticked Other? Name the building here."),
        Field("venue", "Desired Venue", "Desired type of venue", when=_booking("Event Space"),
              description="e.g. conference room, atrium, salon, lounge, MB Green Roof Terrace, Cloud Deck",
              placeholder="No preference"),
        Field("venue", "Desired Venue", "Desired type of venue",
              when=_booking("Information Table/ Kiosk"),
              description="e.g. H Mezzanine Tables A-D, H 7th floor large lounge or cafeteria",
              placeholder="No preference"),
    ], check=check_location),
    Step("Additional Information", [
        Field("booking_other", "Other Booking Request", "What would you like to book?",
              when=_booking("Other")),
        Field("additional_info", "Additional Info", "Anything else we should know?", "paragraph",
              required=False),
    ]),
]

# Sheet columns: the bot's own, then one per form question, then the admin ones
COLUMNS = [
    COL_ID, COL_SUBMITTED, COL_USER, COL_USER_ID,
    *dict.fromkeys(f.column for step in FORM for f in step.fields),
    COL_STATUS, COL_NOTES, COL_NOTIFIED, COL_THREAD,
]
HIDDEN_COLUMNS = [COL_NOTIFIED, COL_THREAD]


def as_text(value) -> str:
    return ", ".join(value) if isinstance(value, list) else str(value)


def lead_time_warning(d: dict) -> str | None:
    """A note if the event is sooner than the booking type's minimum notice."""
    if d.get("booking_type") not in LEAD_TIMES or not d.get("event_date"):
        return None
    needed, business = LEAD_TIMES[d["booking_type"]]
    today, day = date.today(), date.fromisoformat(d["event_date"])
    days = range(1, (day - today).days + 1)
    notice = sum(1 for i in days if not business or (today + timedelta(i)).weekday() < 5)
    if notice >= needed:
        return None
    unit = "business days" if business else "days"
    return (f"⚠️ {d['booking_type']} bookings need at least {needed} {unit} of notice "
            f"and your event is in {notice}. It may not be possible to book in time.")


# ---------------------------------------------------------------- Google Sheet
class RequestSheet:
    """Thin wrapper around the worksheet. All methods are blocking (run them in a thread)."""

    def __init__(self):
        client = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
        self.spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            self.ws = self.spreadsheet.worksheet(WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            self.ws = self.spreadsheet.add_worksheet(
                WORKSHEET_NAME, rows=1000, cols=len(COLUMNS))
        self._ensure_layout()

    # -- setup
    def _ensure_layout(self):
        header = self.ws.row_values(1)
        if not header:
            self.ws.update(range_name="A1", values=[COLUMNS])
            self._format_new_sheet()
            return
        missing = [c for c in COLUMNS if c not in header]
        if missing:
            # Add any new columns (e.g. after a bot update) to the end of the header
            needed = len(header) + len(missing)
            if self.ws.col_count < needed:
                self.ws.add_cols(needed - self.ws.col_count)
            self.ws.update(range_name=rowcol_to_a1(
                1, len(header) + 1), values=[missing])
            hidden = [len(header) + i for i,
                      c in enumerate(missing) if c in HIDDEN_COLUMNS]
            if hidden:
                self.spreadsheet.batch_update(
                    {"requests": [self._hide_column(i) for i in hidden]})

    def _hide_column(self, index: int) -> dict:
        return {"updateDimensionProperties": {
            "range": {"sheetId": self.ws.id, "dimension": "COLUMNS",
                      "startIndex": index, "endIndex": index + 1},
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser",
        }}

    def _format_new_sheet(self):
        sheet_id = self.ws.id
        status_col = COLUMNS.index(COL_STATUS)
        self.ws.format("1:1", {"textFormat": {"bold": True}})
        self.spreadsheet.batch_update({"requests": [
            # Freeze header row
            {"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }},
            # Status dropdown
            {"setDataValidation": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1,
                          "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
                "rule": {
                    "condition": {"type": "ONE_OF_LIST",
                                  "values": [{"userEnteredValue": s} for s in STATUSES]},
                    "showCustomUi": True,
                    "strict": False,
                },
            }},
            # Hide the bot's bookkeeping columns
            *[self._hide_column(COLUMNS.index(c)) for c in HIDDEN_COLUMNS],
        ]})

    # -- helpers
    def _read(self) -> tuple[dict[str, int], list[tuple[int, dict]]]:
        """Returns (column index by header, [(sheet row number, record), ...])."""
        values = self.ws.get_all_values()
        if not values:
            return {}, []
        header = values[0]
        cols = {name: i for i, name in enumerate(header)}
        records = []
        for row_number, row in enumerate(values[1:], start=2):
            row = row + [""] * (len(header) - len(row))
            records.append(
                (row_number, {name: row[i].strip() for name, i in cols.items()}))
        return cols, records

    # -- public API
    def add_request(self, user: str, user_id: int, d: dict) -> str:
        request_id = "R-" + secrets.token_hex(3).upper()
        record = {
            COL_ID: request_id,
            COL_SUBMITTED: datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
            COL_USER: user,
            COL_USER_ID: str(user_id),
            **{f.column: as_text(d[f.key]) for step in FORM for f in step.fields if f.key in d},
            COL_STATUS: "Pending",
            COL_NOTES: "",
            COL_NOTIFIED: "Pending",
        }
        header = self.ws.row_values(1)
        row = [record.get(name, "") for name in header]
        # RAW keeps phone numbers as text and stops "=..." being run as a formula
        self.ws.append_row(row, value_input_option="RAW", table_range="A1")
        return request_id

    def set_thread(self, request_id: str, thread_id: int):
        cols, records = self._read()
        for row_number, r in records:
            if r.get(COL_ID) == request_id:
                self.ws.update(
                    range_name=rowcol_to_a1(row_number, cols[COL_THREAD] + 1),
                    values=[[str(thread_id)]],
                    value_input_option="RAW",
                )
                return

    def update_status(self, request_id: str, status: str, notes: str | None = None) -> dict | None:
        """Write a new Status (and optionally Notes). Returns the row, or None if not found."""
        cols, records = self._read()
        for row_number, r in records:
            if r.get(COL_ID) == request_id:
                data = [{"range": rowcol_to_a1(
                    row_number, cols[COL_STATUS] + 1), "values": [[status]]}]
                if notes is not None:
                    data.append({"range": rowcol_to_a1(
                        row_number, cols[COL_NOTES] + 1), "values": [[notes]]})
                self.ws.batch_update(data, value_input_option="RAW")
                return r
        return None

    def request_id_for_thread(self, thread_id: int) -> str | None:
        _, records = self._read()
        for _, r in records:
            if r.get(COL_THREAD) == str(thread_id):
                return r.get(COL_ID)
        return None

    def find_status_changes(self) -> list[dict]:
        _, records = self._read()
        return [
            r for _, r in records
            if r.get(COL_ID) and r.get(COL_USER_ID) and r.get(COL_STATUS)
            and r[COL_STATUS] != r.get(COL_NOTIFIED)
        ]

    def mark_notified(self, updates: list[tuple[str, str]]):
        """updates = [(request_id, status_announced), ...]. Looked up by ID, so sorting is safe."""
        cols, records = self._read()
        row_by_id = {r.get(COL_ID): row_number for row_number, r in records}
        col = cols[COL_NOTIFIED] + 1
        data = [
            {"range": rowcol_to_a1(row_by_id[rid], col), "values": [[status]]}
            for rid, status in updates if rid in row_by_id
        ]
        if data:
            self.ws.batch_update(data, value_input_option="RAW")

    def requests_for(self, user_id: int) -> list[dict]:
        _, records = self._read()
        return [r for _, r in records if r.get(COL_USER_ID) == str(user_id)]


sheet: RequestSheet | None = None

# In-progress form answers, keyed by user id (cleared once submitted)
drafts: dict[int, dict] = {}


# ---------------------------------------------------------------- Form pages
def next_step(start: int, d: dict) -> int | None:
    """Index of the next page with at least one question that applies, or None when done."""
    for i in range(start, len(FORM)):
        if FORM[i].fields_for(d):
            return i
    return None


async def session_expired(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Your session expired — please run `/book-space` again.", ephemeral=True
    )


class StepModal(discord.ui.Modal):
    """One page of the booking form, built from FORM[index]."""

    def __init__(self, index: int, d: dict, retry: dict | None = None):
        self.step = FORM[index]
        super().__init__(title=f"Space Booking · {self.step.title}"[:45])
        self.index = index
        self.inputs: dict[str, discord.ui.Item] = {}
        # Pre-fill with earlier answers, or with the rejected ones after a validation error
        prefill = {**d, **(retry or {})}
        for f in self.step.fields_for(d):
            component = f.build(prefill.get(f.key))
            self.add_item(discord.ui.Label(
                text=f.label, description=f.description, component=component))
            self.inputs[f.key] = component

    async def on_submit(self, interaction: discord.Interaction):
        draft = drafts.get(interaction.user.id)
        if draft is None:
            await session_expired(interaction)
            return

        answers = {}
        for key, c in self.inputs.items():
            if isinstance(c, discord.ui.CheckboxGroup):
                answers[key] = list(c.values)
            elif isinstance(c, discord.ui.RadioGroup):
                answers[key] = c.value or ""
            else:
                answers[key] = c.value.strip()

        errors = self.step.check(answers) if self.step.check else []
        if errors:
            await interaction.response.send_message(
                "Please fix the following:\n" + "\n".join(errors),
                view=StepView(self.index, "Fix my answers", retry=answers),
                ephemeral=True,
            )
            return

        draft.update(answers)
        index = next_step(self.index + 1, draft)
        if index is None:
            await submit_request(interaction, draft)
            return
        step = FORM[index]
        await interaction.response.send_message(
            f"**Next: {step.title}**" + (f"\n{step.intro}" if step.intro else ""),
            view=StepView(index, "Continue"),
            ephemeral=True,
            suppress_embeds=True,
        )


class StepView(discord.ui.View):
    """A button that opens a form page (a modal can't be opened straight from another)."""

    def __init__(self, index: int, label: str, retry: dict | None = None):
        super().__init__(timeout=600)
        self.index = index
        self.retry = retry
        self.open_step.label = label

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def open_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        draft = drafts.get(interaction.user.id)
        if draft is None:
            await session_expired(interaction)
            return
        await interaction.response.send_modal(StepModal(self.index, draft, self.retry))


async def submit_request(interaction: discord.Interaction, draft: dict):
    # Writing to Google can take a few seconds; Discord needs an answer within 3
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        request_id = await asyncio.to_thread(
            sheet.add_request, str(interaction.user), interaction.user.id, draft
        )
    except Exception as exc:
        print(f"[sheet] Failed to save request: {exc!r}")
        await interaction.followup.send(
            "⚠️ Couldn't save your request to the booking sheet. Please try again in a minute "
            "(your answers are kept — just reopen the last page).",
            view=StepView(len(FORM) - 1, "Try again"),
            ephemeral=True,
        )
        return

    drafts.pop(interaction.user.id, None)
    embed = build_request_embed(interaction.user, draft, request_id)

    thread = await open_request_thread(interaction, embed, request_id, draft["event_name"])
    if thread is not None:
        try:
            for details in build_detail_embeds(draft):
                await thread.send(embed=details)
        except discord.HTTPException as exc:
            print(f"[thread] Couldn't post details for {request_id}: {exc!r}")
        try:
            await asyncio.to_thread(sheet.set_thread, request_id, thread.id)
        except Exception as exc:
            print(
                f"[sheet] Couldn't save thread for {request_id}: {exc!r}")
        where = f"Status updates will be posted in {thread.mention}."
    else:
        where = ("⚠️ I couldn't open a forum post for it "
                 "(use `/book-space` inside a forum post, or check my permissions).")

    warning = lead_time_warning(draft)
    await interaction.followup.send(
        f"✅ Request **{request_id}** was sent! {where}" + (f"\n{warning}" if warning else ""),
        ephemeral=True,
    )


def submissions_forum() -> discord.ForumChannel | None:
    channel = bot.get_channel(SUBMISSIONS_CHANNEL_ID)
    return channel if isinstance(channel, discord.ForumChannel) else None


async def create_forum_post(
    forum: discord.ForumChannel, name: str, content: str, embed: discord.Embed
) -> discord.Thread:
    result = await forum.create_thread(
        name=name[:100], content=content, embed=embed, auto_archive_duration=10080,
        allowed_mentions=discord.AllowedMentions(
            users=True, roles=False, everyone=False),
    )
    return result.thread


async def open_request_thread(
    interaction: discord.Interaction, embed: discord.Embed, request_id: str, event_name: str
) -> discord.Thread | None:
    """Post the request in a forum post and return the post its status updates will go to."""
    channel = interaction.channel
    in_forum_post = isinstance(channel, discord.Thread) and isinstance(
        channel.parent, discord.ForumChannel)
    try:
        if in_forum_post:
            # Reuse this post only if it doesn't already belong to another request,
            # so /set-status inside a post always means exactly one request
            try:
                existing = await asyncio.to_thread(sheet.request_id_for_thread, channel.id)
            except Exception as exc:
                print(f"[sheet] Couldn't check post {channel.id}: {exc!r}")
                existing = None
            if existing is None:
                await channel.send(content=interaction.user.mention, embed=embed)
                return channel
            forum = channel.parent
        else:
            forum = submissions_forum()
        if forum is None:
            print(f"[thread] No forum to post {request_id} in "
                  "(use /book-space in a forum post or set SUBMISSIONS_CHANNEL_ID to a forum)")
            return None
        return await create_forum_post(
            forum, f"{request_id} · {event_name}", interaction.user.mention, embed)
    except discord.HTTPException as exc:
        print(f"[thread] Couldn't open forum post for {request_id}: {exc!r}")
    return None


# ---------------------------------------------------------------- Embeds
def build_request_embed(user: discord.abc.User, d: dict, request_id: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Space Booking Request: {d['event_name']}",
        description=d["event_description"],
        color=STATUS_COLORS["Pending"],
    )
    embed.add_field(name="Request ID", value=request_id, inline=True)
    embed.add_field(name="Status", value="Pending", inline=True)
    embed.add_field(name="​", value="​", inline=True)
    embed.add_field(name="Onsite contact",
                    value=d["contact_name"], inline=True)
    embed.add_field(name="Email", value=d["contact_email"], inline=True)
    embed.add_field(name="Phone", value=d["contact_phone"], inline=True)
    embed.add_field(name="Date", value=d["event_date"], inline=True)
    embed.add_field(
        name="Time", value=f"{d['start_time']}–{d['end_time']}", inline=True)
    booking = d["booking_type"]
    if d.get("booking_other"):
        booking += f": {d['booking_other']}"
    embed.add_field(name="Booking", value=booking[:1024], inline=True)
    if d.get("locations"):
        embed.add_field(name="Preferred locations",
                        value=as_text(d["locations"]), inline=True)
        embed.add_field(name="Desired venue",
                        value=d.get("venue") or "No preference", inline=True)
        embed.add_field(name="​", value="​", inline=True)
    embed.add_field(name="Expected attendance",
                    value=d["expected_attendance"], inline=True)
    embed.add_field(name="Attendance type",
                    value=d["attendance_type"], inline=True)
    embed.set_footer(text=f"Submitted by {user} ({user.id})")
    return embed


# Answers already shown in the request embed
SUMMARY_KEYS = {
    "contact_name", "contact_email", "contact_phone", "event_name", "event_description",
    "expected_attendance", "attendance_type", "event_date", "start_time", "end_time",
    "booking_type", "booking_other", "locations", "venue",
}


def build_detail_embeds(d: dict) -> list[discord.Embed]:
    """The rest of the answers. Split over several embeds, since each message
    allows at most 25 fields and 6000 characters."""
    embeds: list[discord.Embed] = []
    size = 0
    for step in FORM:
        for f in step.fields_for(d):
            if f.key in SUMMARY_KEYS or not d.get(f.key):
                continue
            value = as_text(d[f.key])[:1024]
            if not embeds or len(embeds[-1].fields) == 25 or size + len(f.label) + len(value) > 5500:
                embeds.append(discord.Embed(
                    title=f"Request details ({len(embeds) + 1})", color=STATUS_COLORS["Pending"]))
                size = 0
            embeds[-1].add_field(name=f.label, value=value, inline=False)
            size += len(f.label) + len(value)
    if len(embeds) == 1:
        embeds[0].title = "Request details"
    return embeds


def build_status_embed(r: dict) -> discord.Embed:
    status = r[COL_STATUS]
    embed = discord.Embed(
        title=f"Booking update: {r.get(COL_EVENT) or 'your event'}",
        description=f"Your request **{r.get(COL_ID, '?')}** is now **{status}**.",
        color=STATUS_COLORS.get(status, discord.Color.blurple()),
    )
    if r.get(COL_NOTES):
        embed.add_field(name="Notes", value=r[COL_NOTES][:1024], inline=False)
    return embed


# ---------------------------------------------------------------- Bot
class BookingBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        global sheet
        sheet = await asyncio.to_thread(RequestSheet)
        print(
            f"[sheet] Connected to '{sheet.spreadsheet.title}' / '{WORKSHEET_NAME}'")
        await self.tree.sync()
        poll_status_changes.start()
        if WEBHOOK_SECRET:
            await start_webhook_server()
        else:
            print(
                "[webhook] WEBHOOK_SECRET not set: sheet edits are picked up by polling only")


bot = BookingBot()


_announce_lock = asyncio.Lock()


async def announce_status_changes():
    """Post every not-yet-announced status change. Used by the poll, the sheet
    webhook and /set-status; the lock stops them announcing the same change twice."""
    await bot.wait_until_ready()
    async with _announce_lock:
        try:
            changes = await asyncio.to_thread(sheet.find_status_changes)
        except Exception as exc:
            print(f"[sheet] Reading changes failed: {exc!r}")
            return

        notified: list[tuple[str, str]] = []
        for r in changes:
            if await notify_user(r):
                notified.append((r[COL_ID], r[COL_STATUS]))

        if notified:
            try:
                await asyncio.to_thread(sheet.mark_notified, notified)
            except Exception as exc:
                print(f"[sheet] Couldn't record notifications: {exc!r}")


@tasks.loop(seconds=POLL_SECONDS)
async def poll_status_changes():
    await announce_status_changes()


@poll_status_changes.before_loop
async def _wait_until_ready():
    await bot.wait_until_ready()


# ---------------------------------------------------------------- Sheet webhook
_background_tasks: set[asyncio.Task] = set()


async def handle_sheet_edit(request: web.Request) -> web.Response:
    if not secrets.compare_digest(request.headers.get("X-Secret", ""), WEBHOOK_SECRET):
        return web.Response(status=401, text="bad secret")

    async def run():
        await asyncio.sleep(1)  # give Google a moment to commit the edit
        await announce_status_changes()

    task = asyncio.create_task(run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return web.Response(text="ok")


async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/sheet-edit", handle_sheet_edit)
    app.router.add_get(
        "/", lambda _: web.Response(text="booking bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT).start()
    print(f"[webhook] Listening on port {WEBHOOK_PORT} at /sheet-edit")


async def notify_user(r: dict) -> bool:
    """Post the status update in the request's thread, pinging the requester. True if delivered."""
    embed = build_status_embed(r)
    mention = f"<@{r[COL_USER_ID]}>" if r.get(
        COL_USER_ID, "").isdigit() else ""
    allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)

    target = None
    thread_id = r.get(COL_THREAD, "")
    if thread_id.isdigit():
        target = bot.get_channel(int(thread_id))
        if target is None:  # archived threads aren't cached -> fetch it
            try:
                target = await bot.fetch_channel(int(thread_id))
            except discord.NotFound:
                print(f"[notify] Thread for {r.get(COL_ID)} was deleted")
            except discord.HTTPException as exc:
                print(
                    f"[notify] Couldn't fetch thread for {r.get(COL_ID)}: {exc!r}")
                return False  # temporary problem, retry next poll

    if target is None:  # no post saved / post deleted -> new post in the submissions forum
        forum = submissions_forum()
        if forum is None:
            print(
                f"[notify] No forum post or submissions forum for {r.get(COL_ID)}; skipping")
            return True  # nowhere to post, don't retry forever
        try:
            thread = await create_forum_post(
                forum, f"{r.get(COL_ID)} · {r.get(COL_EVENT, '')}", mention, embed)
        except discord.HTTPException as exc:
            print(
                f"[notify] Couldn't create forum post for {r.get(COL_ID)}: {exc!r}")
            return False
        try:
            await asyncio.to_thread(sheet.set_thread, r.get(COL_ID), thread.id)
        except Exception as exc:
            print(
                f"[sheet] Couldn't save forum post for {r.get(COL_ID)}: {exc!r}")
        return True

    try:
        # Sending in an archived (not locked) thread re-opens it automatically
        await target.send(content=mention, embed=embed, allowed_mentions=allowed)
        return True
    except discord.HTTPException as exc:
        print(f"[notify] Couldn't post update for {r.get(COL_ID)}: {exc!r}")
        return False


@bot.tree.command(name="book-space", description="Request a space booking for an event")
async def book_space(interaction: discord.Interaction):
    drafts[interaction.user.id] = {}
    await interaction.response.send_modal(StepModal(0, {}))


def is_admin(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    return bool(ADMIN_ROLE) and any(
        str(role.id) == ADMIN_ROLE or role.name.lower() == ADMIN_ROLE.lower()
        for role in member.roles)


@bot.tree.command(name="set-status", description="(Admins) Change a booking request's status")
@app_commands.guild_only()
@app_commands.describe(
    status="The new status",
    notes="Optional: room, time or other details (saved in the sheet's Notes column)",
    request_id="e.g. R-3F9A1C. Leave empty when using this inside the request's thread",
)
@app_commands.choices(status=[app_commands.Choice(name=s, value=s) for s in STATUSES])
async def set_status(
    interaction: discord.Interaction,
    status: app_commands.Choice[str],
    notes: str | None = None,
    request_id: str | None = None,
):
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "Only booking admins can change a request's status.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if not request_id and isinstance(interaction.channel, discord.Thread):
            request_id = await asyncio.to_thread(sheet.request_id_for_thread, interaction.channel.id)
        if not request_id:
            await interaction.followup.send(
                "Which request? Give a `request_id`, or run this inside the request's thread.",
                ephemeral=True,
            )
            return

        request_id = request_id.strip().upper()
        record = await asyncio.to_thread(sheet.update_status, request_id, status.value, notes)
    except Exception as exc:
        print(f"[sheet] /set-status failed: {exc!r}")
        await interaction.followup.send("⚠️ Couldn't reach the booking sheet. Try again soon.", ephemeral=True)
        return

    if record is None:
        await interaction.followup.send(f"No request with ID **{request_id}** in the sheet.", ephemeral=True)
        return

    if record.get(COL_STATUS) == status.value and record.get(COL_NOTIFIED) == status.value:
        # Same status as before (e.g. only the notes changed): post it anyway
        if notes is not None:
            await notify_user({**record, COL_NOTES: notes})
    else:
        await announce_status_changes()

    thread_id = record.get(COL_THREAD, "")
    where = f" Update posted in <#{thread_id}>." if thread_id.isdigit() else ""
    await interaction.followup.send(
        f"✅ **{request_id}** ({record.get(COL_EVENT)}) is now **{status.value}**.{where}",
        ephemeral=True,
    )


@bot.tree.command(name="my-requests", description="See the status of your space booking requests")
async def my_requests(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        records = await asyncio.to_thread(sheet.requests_for, interaction.user.id)
    except Exception as exc:
        print(f"[sheet] Lookup failed: {exc!r}")
        await interaction.followup.send("⚠️ Couldn't reach the booking sheet. Try again soon.", ephemeral=True)
        return

    if not records:
        await interaction.followup.send("You have no booking requests yet. Use `/book-space`!", ephemeral=True)
        return

    embed = discord.Embed(title="Your space booking requests",
                          color=discord.Color.blurple())
    for r in records[-25:]:  # embeds allow max 25 fields
        value = f"Status: **{r.get(COL_STATUS) or '—'}**\nSubmitted: {r.get(COL_SUBMITTED, '')}"
        if r.get(COL_THREAD, "").isdigit():
            value += f"\nThread: <#{r[COL_THREAD]}>"
        if r.get(COL_NOTES):
            value += f"\nNotes: {r[COL_NOTES][:200]}"
        embed.add_field(name=f"{r.get(COL_ID)} · {r.get(COL_EVENT)}"[
                        :256], value=value, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if submissions_forum() is None:
        print(f"[forum] ⚠️ SUBMISSIONS_CHANNEL_ID ({SUBMISSIONS_CHANNEL_ID}) is not a forum the bot "
              "can see; /book-space will only work inside existing forum posts")


if __name__ == "__main__":
    bot.run(TOKEN)
