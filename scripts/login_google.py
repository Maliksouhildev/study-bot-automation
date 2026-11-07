import os
import sys
import json
import shutil
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
EXAMPLE_FILE = os.path.join(BASE_DIR, "config.example.json")

# Auto-initialize config.json if not present
if not os.path.exists(CONFIG_FILE) and os.path.exists(EXAMPLE_FILE):
    shutil.copy(EXAMPLE_FILE, CONFIG_FILE)
    print("[SETUP] Created config.json from config.example.json")

with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
    config = json.load(f)

# Resolve profile dir portably
profile_dir = config.get("settings", {}).get("chrome_user_data_dir", "./chrome_bot_profile")
if not os.path.isabs(profile_dir):
    profile_dir = os.path.normpath(os.path.join(BASE_DIR, profile_dir))

print("=" * 60)
print("GOOGLE ACCOUNT LOGIN SETUP")
print("=" * 60)
print("1. Log into your Google account (university / personal) in Chrome.")
print("2. Once logged in, simply CLOSE the browser window.")
print(f"Profile will be saved permanently to:\n{profile_dir}\n")

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        channel="chrome",
        headless=False,
        args=["--start-maximized"],
        no_viewport=True
    )
    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto("https://accounts.google.com")
    
    try:
        page.wait_for_timeout(300000)  # up to 5 minutes
    except Exception:
        pass
    browser.close()

print("\n[OK] Google account login completed and saved successfully!")
