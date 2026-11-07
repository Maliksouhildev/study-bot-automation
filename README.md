# 🤖 Study Bot — Autonomous University Lecture Recorder & AI Study Assistant

<div align="center">

[![Python](https://img.shields.io/badge/Runtime-Python%203.10+-3776ab?style=for-the-badge&logo=python)](https://python.org)
[![Playwright](https://img.shields.io/badge/Automation-Playwright%20Chromium-2eaa4b?style=for-the-badge&logo=playwright)](https://playwright.dev)
[![Telethon](https://img.shields.io/badge/Telegram-Telethon%20MTProto-2ca5e0?style=for-the-badge&logo=telegram)](https://telethon.readthedocs.io)
[![FFmpeg](https://img.shields.io/badge/Audio%2FVideo-FFmpeg-red?style=for-the-badge&logo=ffmpeg)](https://ffmpeg.org)
[![n8n](https://img.shields.io/badge/AI%20Orchestrator-n8n-ea4b71?style=for-the-badge&logo=n8n)](https://n8n.io)
[![License](https://img.shields.io/badge/License-MIT-orange?style=for-the-badge)](LICENSE)

**An autonomous study platform that monitors university Telegram groups for Google Meet links and schedule updates, automatically joins online lectures, records HD video & internal audio, transcribes with Whisper, and synthesizes study notes into a live web dashboard.**

[Architecture](#-system-architecture) • [Features](#-key-features) • [Quick Start](#-quick-start) • [Configuration](#-configuration-guide) • [Dashboard](#-live-web-dashboard) • [Components](#-component-reference)

</div>

---

## 📖 Overview

Online university lectures frequently suffer from last-minute schedule changes, missed links posted across cluttered Telegram channels, and lengthy unindexed recordings.

**Study Bot** runs 24/7 as an autonomous academic assistant:
1. **Listens to Telegram in real time**: Scans class channels and topic threads for new Google Meet links, cancellations, and rescheduled times.
2. **Auto-joins Google Meet**: Uses Playwright to launch a persistent Chromium browser profile, automatically joins meetings at scheduled times, and mutes mic/camera.
3. **Lossless Recording**: Records screen and captures internal system audio directly via virtual loopback and FFmpeg.
4. **AI Transcription & Synthesis**: Sends completed session audio to an n8n pipeline for Whisper STT transcription and Gemini lecture notes summarization.
5. **Interactive Student Dashboard**: Visualizes class timetables, AI summaries, downloadable PDFs, and task rollover tracking at `http://localhost:5050`.

---

## 🖥️ System Flow & Dashboard Interface

<div align="center">

![Study Bot Meeting Session](docs/images/meeting_recorder_screen.png)

*Study Bot Active Meeting Screen: Automated Playwright session joined to lecture room with camera/mic muted, screen audio capture active, and real-time buffer sync.*

</div>

---

## ⚡ The Autonomous Lecture Recording Pipeline

| Phase | Engine | Responsibility |
|:------|:-------|:---------------|
| **1. Schedule & URL Monitoring** | `telegram_schedule_watcher.py` (Telethon) | 24/7 background listener watching class group topics; extracts Google Meet links and detects last-minute timetable changes in real time. |
| **2. Hot Reload Scheduling** | `bot.py` | Reads `config.json` with hot-reloading; injects randomized 4–6 min jitter to join meetings naturally without looking robotic. |
| **3. Meeting Automation** | Playwright Chromium | Launches persistent authenticated student profile, navigates to Meet URL, toggles off camera and microphone, and clicks 'Join'. |
| **4. High-Fidelity Recording** | FFmpeg Loopback | Records screen stream and captures internal stereo speaker audio directly via virtual audio cable (no room echo). |
| **5. AI Transcription & Notes** | n8n + Whisper + Gemini | Dispatches audio upon lecture conclusion to n8n; transcribes full lecture and synthesizes structured study sheets. |
| **6. Student Web Control Room** | `dashboard/server.py` (:5050) | Local web interface displaying weekly timetable, session status countdowns, course PDFs, and rollover task assignments. |

---

## ✨ Key Features

- ⚡ **Zero-Miss Class Attendance**: Automatically joins Google Meet sessions using your authenticated Google Chrome profile.
- 📡 **Real-Time Telegram Schedule Watcher**: Tracks pinned messages and topic threads; updates Meet links dynamically without manual config changes.
- 🎙️ **Internal Loopback Recording**: Captures crisp professor audio directly without microphone room echo.
- 📝 **Structured AI Lecture Summaries**: Generates Markdown executive summaries, formulas, homework assignments, and key takeaways.
- 📊 **Local Academic Dashboard**: Web interface showing weekly schedule, live session status countdowns, course PDFs, and pending tasks.
- 🔒 **Zero-Leakage Privacy**: All credentials, Chrome user data, Telegram sessions, and recorded videos remain strictly local (`.gitignore` enforced).

---

## 🚀 Quick Start

### 1. Prerequisites
- **Python**: 3.10+ (tested on Python 3.11/3.12)
- **Node.js & n8n**: 18+ (optional, for AI pipeline)
- **FFmpeg**: Configured in system `PATH`
- **Google Chrome**: Installed on the host machine

### 2. Installation

```bash
# Clone repository
git clone https://github.com/Maliksouhildev/study-bot-automation.git
cd study-bot-automation

# Install Python requirements
pip install -r requirements.txt
playwright install chromium
```

### 3. Verify FFmpeg

```bash
ffmpeg -version
```

### 4. Configure Your Schedule

Create your local `config.json` from the provided template:

```bash
cp config.example.json config.json
```

---

## ⚙️ Configuration Guide

### 1. One-Time Google Authentication
Launch the browser authenticator to save your student Google account session:

```bash
python scripts/login_google.py
```
*Sign in to Google once in the browser window that opens, then close it. The session is saved to `chrome_bot_profile/`.*

### 2. One-Time Telegram Authentication
Connect your Telegram account via secure QR code login:

```bash
pip install qrcode[pil]
python scripts/telegram_login_qr.py
```
*Scan the generated QR code in your Telegram mobile app (`Settings → Devices → Link Desktop Device`). The session is saved to `telegram_user.session`.*

### 3. Configure Telegram Group IDs
In `telegram_schedule_watcher.py`, configure your target group:

```python
GROUP_ID = -100XXXXXXXXXX   # Your Telegram group ID
TOPIC_IMPORTANT = 989        # Announcements topic ID
TOPIC_MEET = 993             # Meet links topic ID
```

---

## 🖥️ Running Study Bot

### Option A: Complete Launcher (Windows)
```bash
run_study_bot.bat
```

### Option B: Manual Multi-Terminal Execution
```bash
# Terminal 1: Telegram Schedule Watcher
python telegram_schedule_watcher.py

# Terminal 2: Autonomous Meeting Bot
python bot.py

# Terminal 3: Web Dashboard
cd dashboard && python server.py
```
Access the dashboard at **[http://localhost:5050](http://localhost:5050)**.

### Quick Toggle
To temporarily toggle bot automation on/off without closing terminals:
```bash
python scripts/toggle_bot.py
```

---

## 🗂️ Project Structure

```text
study-bot-automation/
├── bot.py                          # Core: schedules & joins Meet, records audio/video
├── telegram_schedule_watcher.py    # Core: watches Telegram group for schedule updates
├── config.example.json             # Template config for courses and timetable
├── requirements.txt                # Python dependencies
├── run_study_bot.bat               # Windows launcher script
├── toggle_study_bot.bat            # Quick toggle shortcut
├── workflows/                      # Modular n8n workflow JSONs
│   ├── Study_Assistant_Workflow.json
│   ├── schedule_parser_wf.json
│   └── study_bot_workflow.json
├── scripts/                        # Setup & utility scripts
│   ├── login_google.py             # One-time Google login helper
│   ├── telegram_login_qr.py        # One-time Telegram QR login helper
│   ├── telegram_preflight.py       # Telegram connection validator
│   ├── record_current_session.py   # Manual session recording runner
│   └── toggle_bot.py               # Enable/disable bot via config toggle
├── dashboard/                      # Web control room & task monitor
│   ├── index.html                  # Responsive dark web dashboard UI
│   ├── server.py                   # Lightweight HTTP server (port 5050)
│   ├── tasks_data.json             # Task and assignment rollover state
│   └── course_materials/           # Uploaded syllabus & lecture PDFs
```

---

## 🤝 Contributing & License

Contributions, improvements, and issues are welcome.

Distributed under the **MIT License**. Created & maintained by [Maliksouhildev](https://github.com/Maliksouhildev).
