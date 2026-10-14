"""
Discord bot version of the "Request a Space Booking" Google Form,
connected to a Google Sheet.

How it works
  * /book-space opens the form (split in 2 steps, Discord allows max 5 fields per pop-up).
  * Each submitted request is added as a new row in the sheet with Status = "Pending".
  * You change the Status cell in the sheet (dropdown: Pending, In Review, Booked,
    Rejected, Cancelled) and can write the room / details in the "Notes" column.
  * Each request gets its own forum post (the bot only works with forum channels):
      - /book-space used inside a forum post with no request yet -> that post is used
      - otherwise (post already has a request, or used outside a forum)
                                    -> a new post is created in the same forum, or
                                       in the SUBMISSIONS_CHANNEL_ID forum
    The post (thread) ID is saved in the sheet.
  * Status can be changed two ways, and both post the update in the request's
    thread (pinging the requester):
      - In Discord: /set-status (admins). Inside a request thread, no ID needed.
      - In the sheet: edit the Status cell. The Apps Script in apps_script.gs pings
        the bot instantly; the bot also re-checks the sheet every POLL_SECONDS as a
        safety net.
    A hidden "last notified" column makes sure each change is announced once.
  * /my-requests shows a user their requests, current status and thread link.

Setup — see README section at the bottom of this file.
"""

import asyncio
import os
import re
import secrets
from datetime import datetime

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

COLUMNS = [
    COL_ID, COL_SUBMITTED, COL_USER, COL_USER_ID, COL_CONTACT, COL_EMAIL, COL_PHONE,
    COL_EVENT, COL_DESC, COL_ATTENDANCE, COL_ATT_TYPE, COL_STATUS, COL_NOTES,
    COL_NOTIFIED, COL_THREAD,
]
HIDDEN_COLUMNS = [COL_NOTIFIED, COL_THREAD]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
            COL_CONTACT: d["contact_name"],
            COL_EMAIL: d["contact_email"],
            COL_PHONE: d["contact_phone"],
            COL_EVENT: d["event_name"],
            COL_DESC: d["event_description"],
            COL_ATTENDANCE: str(d["expected_attendance"]),
            COL_ATT_TYPE: d["attendance_type"],
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


# ---------------------------------------------------------------- Form step 1
class ContactEventModal(discord.ui.Modal, title="Request a Space Booking (1/2)"):
    contact_name = discord.ui.TextInput(
        label="Onsite contact name",
        placeholder="Person responsible for the event",
        max_length=100,
    )
    contact_email = discord.ui.TextInput(
        label="Onsite contact email",
        placeholder="name@example.com",
        max_length=100,
    )
    contact_phone = discord.ui.TextInput(
        label="Onsite contact phone number",
        placeholder="514-555-0123",
        max_length=25,
    )
    event_name = discord.ui.TextInput(
        label="Name of event",
        max_length=100,
    )
    event_description = discord.ui.TextInput(
        label="Describe the event (purpose & activities)",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, defaults: dict | None = None):
        super().__init__()
        # Pre-fill when reopening after a validation error
        for key, value in (defaults or {}).items():
            field = getattr(self, key, None)
            if isinstance(field, discord.ui.TextInput):
                field.default = value

    async def on_submit(self, interaction: discord.Interaction):
        answers = {
            "contact_name": self.contact_name.value.strip(),
            "contact_email": self.contact_email.value.strip(),
            "contact_phone": self.contact_phone.value.strip(),
            "event_name": self.event_name.value.strip(),
            "event_description": self.event_description.value.strip(),
        }

        errors = []
        if not EMAIL_RE.match(answers["contact_email"]):
            errors.append("• The email address doesn't look valid.")
        if len(re.sub(r"\D", "", answers["contact_phone"])) < 10:
            errors.append("• The phone number needs at least 10 digits.")

        if errors:
            await interaction.response.send_message(
                "Please fix the following:\n" + "\n".join(errors),
                view=RetryView(answers),
                ephemeral=True,
            )
            return

        drafts[interaction.user.id] = answers
        await interaction.response.send_message(
            "**Step 2 of 2** — select the type of attendance for this event:",
            view=AttendanceTypeView(),
            ephemeral=True,
        )


class RetryView(discord.ui.View):
    """Lets the user reopen step 1 without retyping everything."""

    def __init__(self, answers: dict):
        super().__init__(timeout=600)
        self.answers = answers

    @discord.ui.button(label="Fix my answers", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ContactEventModal(defaults=self.answers))


# ---------------------------------------------------------------- Form step 2
class AttendanceTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    @discord.ui.select(
        placeholder="Select the type of attendance…",
        options=[discord.SelectOption(label=t) for t in ATTENDANCE_TYPES],
    )
    async def pick_type(self, interaction: discord.Interaction, select: discord.ui.Select):
        draft = drafts.get(interaction.user.id)
        if draft is None:
            await interaction.response.send_message(
                "Your session expired — please run `/book-space` again.", ephemeral=True
            )
            return
        draft["attendance_type"] = select.values[0]
        await interaction.response.send_modal(AttendanceModal())


class AttendanceModal(discord.ui.Modal, title="Request a Space Booking (2/2)"):
    expected_attendance = discord.ui.TextInput(
        label="Total expected attendance",
        placeholder="e.g. 40",
        max_length=6,
    )

    async def on_submit(self, interaction: discord.Interaction):
        value = self.expected_attendance.value.strip()
        if not value.isdigit() or int(value) <= 0:
            await interaction.response.send_message(
                "Expected attendance must be a positive whole number. "
                "Pick the attendance type again to retry.",
                view=AttendanceTypeView(),
                ephemeral=True,
            )
            return

        draft = drafts.get(interaction.user.id)
        if draft is None:
            await interaction.response.send_message(
                "Your session expired — please run `/book-space` again.", ephemeral=True
            )
            return
        draft["expected_attendance"] = int(value)

        # Writing to Google can take a few seconds; Discord needs an answer within 3
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            request_id = await asyncio.to_thread(
                sheet.add_request, str(
                    interaction.user), interaction.user.id, draft
            )
        except Exception as exc:
            print(f"[sheet] Failed to save request: {exc!r}")
            await interaction.followup.send(
                "⚠️ Couldn't save your request to the booking sheet. Please try again in a minute "
                "(your answers are kept — just pick the attendance type again).",
                view=AttendanceTypeView(),
                ephemeral=True,
            )
            return

        drafts.pop(interaction.user.id, None)
        embed = build_request_embed(interaction.user, draft, request_id)

        thread = await open_request_thread(interaction, embed, request_id, draft["event_name"])
        if thread is not None:
            try:
                await asyncio.to_thread(sheet.set_thread, request_id, thread.id)
            except Exception as exc:
                print(
                    f"[sheet] Couldn't save thread for {request_id}: {exc!r}")
            where = f"Status updates will be posted in {thread.mention}."
        else:
            where = ("⚠️ I couldn't open a forum post for it "
                     "(use `/book-space` inside a forum post, or check my permissions).")

        await interaction.followup.send(
            f"✅ Request **{request_id}** was sent! {where}",
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
    embed.add_field(name="Expected attendance", value=str(
        d["expected_attendance"]), inline=True)
    embed.add_field(name="Attendance type",
                    value=d["attendance_type"], inline=True)
    embed.set_footer(text=f"Submitted by {user} ({user.id})")
    return embed


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
    await interaction.response.send_modal(ContactEventModal())


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
