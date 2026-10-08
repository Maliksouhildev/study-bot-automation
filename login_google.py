import json
from playwright.sync_api import sync_playwright

with open("config.json", "r") as f:
    config = json.load(f)

print("=" * 60)
print("OPENING CHROME TO LOG IN TO GOOGLE")
print("=" * 60)
print("1. Log into your Google account in the browser that opens.")
print("2. Once logged in, simply CLOSE the browser window.")
print("Your login session will be saved permanently for all future classes!\n")

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=config["settings"]["chrome_user_data_dir"],
        channel="chrome",
        headless=False,
        args=["--start-maximized"],
        no_viewport=True
    )
    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto("https://accounts.google.com")
    
    # Keep open until user closes browser
    try:
        page.wait_for_timeout(300000) # 5 minutes max
    except Exception:
        pass
    browser.close()

print("\nGoogle login setup completed!")
