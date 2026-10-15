# 🤖 Study Bot — Autonomous University Lecture Recorder & AI Study Assistant

> Automatically joins your online Google Meet classes, records audio & video, transcribes with Whisper, generates AI-powered course summaries, and serves everything through a live web dashboard.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      TELEGRAM GROUP                         │
│              (e.g. "Automatique L3" group)                  │
│  Professors post Meet links, schedule changes, files, etc.  │
└───────────────────────┬─────────────────────────────────────┘
                        │ Live monitoring (Telethon)
                        ▼
           ┌────────────────────────┐
           │ telegram_schedule_     │
           │ watcher.py             │
           │ • Watches topics       │
           │ • Extracts Meet URLs   │
           │ • Updates config.json  │
           │ • Detects schedule     │
           │   changes (time/day)   │
           └──────────┬─────────────┘
                      │ writes to
                      ▼
              ┌──────────────┐
              │ config.json  │ ◄── Your class schedule
              └──────┬───────┘     (auto-updated by watcher)
                     │ reads
                     ▼
            ┌──────────────────┐      ┌───────────────────┐
            │     bot.py       │      │    n8n Workflow    │
            │ • Schedules jobs │─────►│ • Whisper STT      │
            │ • Joins Meet     │ POST │ • Gemini summary   │
            │ • Records audio  │      │ • Generates study  │
            │ • Records video  │      │   prompt & report  │
            │ • Sends to n8n   │      └───────┬───────────┘
            └──────────────────┘              │ writes
                                              ▼
                                    ┌────────────────────┐
                                    │  recordings/       │
                                    │  *_report.md       │
                                    │  *_transcript.txt  │
                                    │  *_master_prompt.md│
                                    └─────────┬──────────┘
                                              │ served by
                                              ▼
                                    ┌────────────────────┐
                                    │  dashboard/        │
                                    │  server.py :5050   │
                                    │  index.html        │
                                    │  course_materials/  │
                                    └────────────────────┘
```

---

## 📋 Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| **Python** | 3.10+ | Tested on 3.14 |
| **Node.js** | 18+ | Only if using n8n |
| **FFmpeg** | Latest | Must be in system `PATH` |
| **Google Chrome** | Latest | Used by Playwright |
| **n8n** | 1.0+ | Self-hosted, for AI pipeline |
| **Telegram account** | — | To monitor your class group |
| **Google account** | — | To join Google Meet classes |

### Windows-Specific
- The bot uses `msvcrt` for file locking (Windows only). Linux/Mac would need a small tweak to use `fcntl`.

---

## 🚀 Quick Start (For Your Friend)

### Step 1: Clone the Repo

```bash
git clone https://github.com/Maliksouhildev/study-bot-automation.git
cd study-bot-automation
```

### Step 2: Install Python Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### Step 3: Install FFmpeg

Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to your system `PATH`.

Verify:
```bash
ffmpeg -version
```

### Step 4: Create Your Own `config.json`

Copy the template and fill in your schedule:

```bash
copy config.example.json config.json
```

Edit `config.json` with your own:
- Class names, days, times
- Google Meet URLs (will be auto-updated by the Telegram watcher)
- Professor names
- Your Chrome profile path

> [!IMPORTANT]
> Each person has their **own** `config.json` — it's in `.gitignore` and won't be pushed. Your schedule and Meet links are completely independent.

### Step 5: Log In to Google (One-Time)

```bash
python login_google.py
```

A Chrome window opens → log into your Google account → close the window. Your session is saved in `chrome_bot_profile/` permanently.

### Step 6: Log In to Telegram (One-Time)

```bash
pip install qrcode[pil]
python telegram_login_qr.py
```

Scan the QR code with your Telegram app (Settings → Devices → Link Desktop Device). Your session is saved in `telegram_user.session`.

> [!NOTE]
> Each person creates their **own** `telegram_user.session`. This file is in `.gitignore` and is never shared. Your Telegram login is completely separate from anyone else's.

### Step 7: Configure Your Telegram Group

In `telegram_schedule_watcher.py`, update these constants to match **your** Telegram group:

```python
GROUP_ID = -100XXXXXXXXXX  # Your Telegram group ID
TOPIC_IMPORTANT = 989       # Topic ID for important announcements
TOPIC_MEET = 993            # Topic ID for Meet links
TOPIC_DISCUSSION = 987      # Topic ID for discussion
```

> **How to find your Group ID:** Add [@userinfobot](https://t.me/userinfobot) to your group, or forward a message from the group to it.

### Step 8: Set Up n8n (AI Pipeline — Optional)

1. Install n8n: `npm install -g n8n`
2. Start n8n: `n8n start`
3. Open `http://localhost:5678`
4. Import the workflow: go to **Workflows → Import** and select `Study_Assistant_Workflow.json`
5. Configure your API keys in n8n (OpenAI/Gemini for transcription & summarization)

> [!TIP]
> The bot works without n8n — it will still join and record classes. The n8n pipeline adds AI transcription and summaries on top.

### Step 9: Run Everything

**Option A — Batch file (Windows):**
```bash
run_study_bot.bat
```

**Option B — Manual (two terminals):**
```bash
# Terminal 1: Telegram watcher
python telegram_schedule_watcher.py

# Terminal 2: Meeting bot
python bot.py
```

**Option C — Dashboard only:**
```bash
cd dashboard
python server.py
# Open http://localhost:5050
```

---

## 📁 Project Structure

```
study_bot/
├── bot.py                          # Core: schedules & joins Meet, records audio/video
├── telegram_schedule_watcher.py    # Core: watches Telegram group for schedule updates
├── config.json                     # Your class schedule (auto-updated, gitignored)
├── config.example.json             # Template config for new users
├── login_google.py                 # One-time Google login helper
├── telegram_login_qr.py           # One-time Telegram QR login helper
├── requirements.txt                # Python dependencies
├── run_study_bot.bat               # Windows launcher (both scripts)
├── toggle_bot.py                   # Enable/disable bot via config toggle
├── toggle_study_bot.bat            # Quick toggle shortcut
├── Study_Assistant_Workflow.json   # n8n workflow (AI pipeline)
├── study_bot_workflow.json         # n8n workflow (alternate)
├── dashboard/
│   ├── index.html                  # Web dashboard UI
│   ├── server.py                   # Python HTTP server (port 5050)
│   └── course_materials/           # Uploaded lecture PDFs
├── recordings/                     # (gitignored) Recorded sessions
│   ├── *_report.md                 # AI-generated session reports
│   ├── *_transcript.txt            # Raw transcriptions
│   └── *_master_prompt.md          # Generated study prompts
├── chrome_bot_profile/             # (gitignored) Chrome session data
└── telegram_user.session           # (gitignored) Telegram auth session
```

---

## ⚙️ How Each Component Works

### `bot.py` — The Meeting Bot
- Reads `config.json` for the weekly schedule
- Uses `schedule` library to trigger at the right day/time
- Adds a random delay (4–6 min) to appear human
- Uses Playwright to launch Chrome, join Google Meet, disable mic/cam
- Captures audio via virtual audio cable + FFmpeg
- Records screen via FFmpeg
- When class ends (duration elapsed), stops recording and POSTs files to n8n webhook
- Watches `config.json` for live changes (hot reload)

### `telegram_schedule_watcher.py` — Telegram Monitor
- Connects to your Telegram account via Telethon
- Monitors your class group for:
  - New Google Meet links → updates `config.json` URLs
  - Schedule changes (day/time) → updates `config.json` schedule
  - File uploads → can be forwarded/saved
- Scans message history on startup for any missed updates
- Runs live listener for real-time updates

### `dashboard/` — Web Dashboard
- Simple Python HTTP server on port 5050
- Shows your weekly timetable with color-coded modules
- Highlights current/active sessions
- Displays AI-generated session reports and transcripts
- Links to uploaded course PDFs
- API endpoints: `/api/status`, `/api/data`

### n8n Workflow — AI Pipeline
- Receives recorded audio/video from `bot.py` via webhook
- Runs Whisper for speech-to-text transcription
- Feeds transcript to Gemini/GPT for intelligent summarization
- Generates structured study reports and revision prompts
- Saves everything back to `recordings/` folder

---

## 🔒 Privacy & Security

| File | Contains | Shared? |
|---|---|---|
| `config.json` | Your meet URLs, schedule, Chrome path | ❌ gitignored |
| `telegram_user.session` | Your Telegram auth token | ❌ gitignored |
| `chrome_bot_profile/` | Your Google login cookies | ❌ gitignored |
| `recordings/` | Recorded lectures | ❌ gitignored |
| `config.example.json` | Empty template | ✅ in repo |

> [!CAUTION]
> **NEVER** commit `telegram_user.session` or `chrome_bot_profile/`. They contain your active login sessions. Each user generates their own during setup.

---

## 🤝 Multi-User Setup

This bot is designed so multiple people can use the **same codebase** independently:

1. Each person clones the repo
2. Each person creates their own `config.json` from the template
3. Each person logs into their own Google & Telegram accounts
4. All personal data stays local (gitignored)
5. No interference between users — everything is isolated by design

---

## ❓ Troubleshooting

| Problem | Solution |
|---|---|
| `ProcessSingleton` Chrome error | Close all Chrome instances, delete `chrome_bot_profile/SingletonLock` |
| `Another watcher instance is already running` | Delete `watcher.lock` file |
| Bot doesn't join on time | Check `config.json` times match your timezone (Algeria/UTC+1) |
| n8n webhook fails | Make sure n8n is running on `localhost:5678` and workflow is active |
| Telegram connection fails | Run `python telegram_login_qr.py` again to re-authenticate |
| No audio recorded | Install a virtual audio cable (e.g., VB-CABLE) or check FFmpeg |

---

## 📜 License

MIT — Use it, share it, modify it freely.
