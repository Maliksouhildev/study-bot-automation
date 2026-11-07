import json
import os

CONFIG_FILE = "config.json"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

current_status = config.get("enabled", True)
new_status = not current_status
config["enabled"] = new_status

with open(CONFIG_FILE, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print("=" * 60)
if new_status:
    print("▶️  STUDY BOT STATUS: [ON] (ACTIVE)")
    print("=" * 60)
    print("The bot will automatically join all scheduled university classes!")
else:
    print("⏸️  STUDY BOT STATUS: [OFF] (PAUSED)")
    print("=" * 60)
    print("The bot is PAUSED. It will NOT join any calls until you turn it back ON.")
print("=" * 60)
input("\nPress Enter to close this window...")
