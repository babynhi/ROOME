# 🏠 ROOME

**ROOME** is a Discord bot for requesting room and space bookings at Concordia, without leaving Discord.

Members fill out a booking form right inside Discord. Every request is saved to a Google Sheet. When an admin changes a request's status (from Discord or straight in the sheet), ROOME posts the update in that request's own thread and pings the person who asked.

---

## ✨ Features

- **Booking form in Discord**: `/book-space` opens a two-step pop-up form (Discord allows at most 5 fields per pop-up).
- **Checks answers**: rejects invalid emails, phone numbers and attendance counts, and lets people fix their answers without retyping them.
- **Google Sheets as the database**: each request becomes a row, with a Status dropdown and a Notes column.
- **One thread per request**: every request gets its own Discord thread for updates and questions.
- **Change status from either side**: use `/set-status` in Discord, or edit the Status cell in the sheet.
- **No duplicate pings**: each status change is announced exactly once, however it was made.
- **Instant sheet updates (optional)**: a small Google Apps Script tells ROOME as soon as you edit the sheet.

---

## 💬 Commands

| Command | Who | What it does |
|---|---|---|
| `/book-space` | Everyone | Opens the booking request form |
| `/my-requests` | Everyone | Shows your requests, their status and thread links |
| `/set-status` | Admins | Changes a request's status, with optional notes (room, time…). Inside a request's thread, no ID is needed. |

**Admins** are members with the **Manage Server** permission or the role set as `ADMIN_ROLE_ID`.

---

## 🔄 How a request flows

1. **Submit:** a member runs `/book-space` and fills out the form.
2. **Save:** ROOME adds a row to the sheet with Status **Pending**.
3. **Thread:** ROOME posts the request and opens a thread on it, named like `R-3F9A1C · Movie Night`. If the command was used inside a thread, that thread is used instead.
4. **Update:** an admin changes the status, either with `/set-status` or by editing the sheet.
5. **Notify:** ROOME posts the new status and notes in the thread and pings the requester.

**Statuses:** ⏳ Pending · 🔍 In Review · ✅ Booked · ❌ Rejected · 🚫 Cancelled

---

## 📁 Files

| File | Purpose |
|---|---|
| `booking_bot.py` | The bot |
| `apps_script.gs` | Optional Google Apps Script for instant sheet updates |
| `requirements.txt` | Python dependencies |
| `.env` | Your secrets and settings (**never commit this**) |
| `service_account.json` | Google credentials (**never commit this**) |

---

## 🚀 Setup

### 1. Install

Requires **Python 3.10+**.

```bash
pip install -r requirements.txt
pip install python-dotenv
```

If you keep your settings in a `.env` file, make sure `booking_bot.py` loads it near the top, just after the imports:

```python
from dotenv import load_dotenv
load_dotenv()
```

### 2. Create the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications), click **New Application**, and name it **ROOME**.
2. Under **Bot**, click **Reset Token** and copy the token. This is your `DISCORD_TOKEN`.
3. Under **OAuth2 → URL Generator**:
   - **Scopes:** `bot`, `applications.commands`
   - **Bot permissions:** View Channels, Send Messages, Embed Links, Create Public Threads, Send Messages in Threads
4. Open the generated URL and add ROOME to your server.

### 3. Give ROOME access to Google Sheets

1. In the [Google Cloud Console](https://console.cloud.google.com), create a project (or pick an existing one).
2. Go to **APIs & Services → Enable APIs** and enable the **Google Sheets API**.
3. Go to **IAM & Admin → Service Accounts**, create one, then open it and click **Keys → Add key → JSON**.
4. Save the downloaded file next to the bot as `service_account.json`.
5. Open that file and copy the `client_email` value (it ends in `@….iam.gserviceaccount.com`).

### 4. Prepare the Google Sheet

1. Create a Google Sheet.
2. Click **Share** and give the `client_email` address **Editor** access.
3. Copy the sheet ID from its URL, the part between `/d/` and `/edit`:
   ```
   https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_ID/edit
   ```

On first run, ROOME creates a **Requests** tab with headers, a Status dropdown and two hidden bookkeeping columns.

### 5. Configure

Create a `.env` file next to the bot:

```dotenv
DISCORD_TOKEN="your-bot-token"
GOOGLE_SHEET_ID="your-sheet-id"
SUBMISSIONS_CHANNEL_ID="channel-id"
ADMIN_ROLE_ID="role-id"
POLL_SECONDS=30
```

To copy channel and role IDs, turn on **User Settings → Advanced → Developer Mode** in Discord. Then right-click the channel or role and choose **Copy ID**.

### 6. Run

```bash
python booking_bot.py
```

You should see `Logged in as ROOME#…` and `[sheet] Connected to …`. Slash commands can take a minute to appear the first time.

---

## ⚙️ Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | | Bot token from the Developer Portal |
| `GOOGLE_SHEET_ID` | ✅ | | ID of the Google Sheet |
| `GOOGLE_CREDENTIALS_FILE` | | `service_account.json` | Path to the service account key |
| `WORKSHEET_NAME` | | `Requests` | Tab the bot uses |
| `SUBMISSIONS_CHANNEL_ID` | | | Fallback channel for posts when a thread can't be used |
| `ADMIN_ROLE_ID` | | | Role allowed to use `/set-status` (Manage Server members always can) |
| `POLL_SECONDS` | | `60` | How often ROOME checks the sheet for status changes |
| `WEBHOOK_SECRET` | | | Turns on instant sheet updates (see below). Leave empty to rely on polling only. |
| `WEBHOOK_PORT` | | `8080` | Port ROOME listens on for sheet updates |

---

## 📊 The Google Sheet

| Column | Filled by | Notes |
|---|---|---|
| Request ID | Bot | e.g. `R-3F9A1C` |
| Submitted At, Discord User, Discord User ID | Bot | |
| Onsite Contact, Email, Phone | Form | |
| Event Name, Description | Form | |
| Expected Attendance, Attendance Type | Form | |
| **Status** | **You** | Dropdown: Pending, In Review, Booked, Rejected, Cancelled |
| **Notes** | **You** | Shown in the thread update, e.g. "MB 3.430, 6–8pm" |
| Bot: Last Notified Status | Bot | Hidden. **Don't edit or delete.** |
| Bot: Discord Thread ID | Bot | Hidden. **Don't edit or delete.** |

You can reorder, resize or sort columns freely, because ROOME finds them by their header names. Just don't rename the headers.

---

## ⚡ Instant updates from the sheet (optional)

By default, ROOME checks the sheet every `POLL_SECONDS`, so a status you change in the sheet shows up in Discord within that time. `/set-status` is always instant.

To make sheet edits instant too, the Apps Script pings ROOME whenever a Status cell changes. This only works if Google can reach the bot over the internet.

1. **Set a shared password.** Generate one and put it in `.env` as `WEBHOOK_SECRET`:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. **Make ROOME reachable from the internet:**
   - **Hosted** (Railway, Render, a VPS…): use the URL your host gives you.
   - **On your own computer:** run a tunnel such as `cloudflared tunnel --url http://localhost:8080` and use the `https://…` address it prints. The free quick tunnel gets a new address every time it restarts.
3. **Install the script in the sheet:**
   1. In the sheet, open **Extensions → Apps Script**.
   2. Paste in `apps_script.gs`, and set `BOT_URL` (your address followed by `/sheet-edit`) and `SECRET`.
   3. Save. There is no need to click Deploy.
   4. Open **Triggers** (the clock icon) → **Add Trigger**: function `onStatusEdit`, source **From spreadsheet**, type **On edit**.
   5. Approve the permissions Google asks for.

Once this works, you can raise `POLL_SECONDS` to `600`. The regular check stays as a safety net for anything missed while the bot was offline.

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---|---|
| Slash commands don't show up | Wait a minute and restart Discord. Check that the bot was invited with the `applications.commands` scope. |
| `SpreadsheetNotFound` / `PermissionError` | Share the sheet with the service account's `client_email` as **Editor**, and double-check `GOOGLE_SHEET_ID`. |
| "I couldn't open a thread here" | Give ROOME **Create Public Threads** and **Send Messages in Threads** in that channel. |
| Sheet edits never reach Discord | Make sure the bot is running and that the edit was to the **Status** column. With the Apps Script, check **Executions** in the Apps Script editor for errors. |
| Apps Script shows a 401 error | `SECRET` in the script doesn't match `WEBHOOK_SECRET`. |
| Running `onStatusEdit` by hand errors | That's expected. It only works when triggered by a real edit. |
| Same update posted twice | Someone edited or cleared the hidden **Bot: Last Notified Status** column. Leave it alone. |

---

## 🔒 Security

Never share or commit your `.env` or `service_account.json` files. Add this to `.gitignore`:

```gitignore
.env
service_account.json
__pycache__/
```

If a token or key ever leaks, reset the bot token in the Discord Developer Portal and delete the key in Google Cloud. Then create new ones.
