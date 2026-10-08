@echo off
title Study Bot + Telegram Auto-Updater
cd /d "%~dp0"
echo ============================================================
echo Starting Study Bot with Telegram Live Schedule Sync...
echo ============================================================
echo [1/2] Launching Telegram Auto-Updater (Watches Automatique L3)...
start "Telegram Live Watcher" py telegram_schedule_watcher.py
echo [2/2] Launching Study Bot Meeting Scheduler...
py bot.py
pause
