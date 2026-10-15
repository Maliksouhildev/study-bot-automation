import asyncio
import sys
import json
import os
import re
import base64
import requests
import subprocess
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events

sys.stdout.reconfigure(encoding='utf-8')

# ── Single-instance lock ───────────────────────────────────────────────────────
LOCK_FILE = "watcher.lock"

def acquire_lock():
    """Prevent two watcher instances from running simultaneously (SQLite session conflict)."""
    import msvcrt
    lock_fd = open(LOCK_FILE, "w")
    try:
        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except OSError:
        lock_fd.close()
        print("[ABORT] Another watcher instance is already running. Exiting.")
        sys.exit(0)

# ── Constants ──────────────────────────────────────────────────────────────────
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSION_NAME = "telegram_user"
GROUP_ID = -1002250295347  # Automatique L3
N8N_PARSER_URL = "http://localhost:5678/webhook/parse-schedule-update"
CONFIG_FILE = "config.json"

TOPIC_IMPORTANT = 989
TOPIC_MEET = 993
TOPIC_DISCUSSION = 987

# New official timetable was issued Oct 12 2026.
# Schedule time/day changes from messages BEFORE this date are ignored
# (only URL updates from older messages are still applied).
TIMETABLE_EPOCH = datetime(2026, 10, 12, 0, 0, 0, tzinfo=timezone.utc)


# Day name maps (French + Arabic Darija -> weekday number, 0=Monday)
DAY_NAMES = {
    "monday": 0, "lundi": 0,
    "tuesday": 1, "mardi": 1,
    "wednesday": 2, "mercredi": 2,
    "thursday": 3, "jeudi": 3,
    "friday": 4, "vendredi": 4,
    "saturday": 5, "samedi": 5,
    "sunday": 6, "dimanche": 6,
    # Darija
    "lethnayn": 0, "lethnin": 0,
    "thalatha": 1, "thalata": 1,
    "larba": 2, "larbaa": 2,
    "lkhamis": 3, "khamis": 3,
    "ljemaa": 4, "jemaa": 4,
    "sebt": 5,
    "lhad": 6, "had": 6,
}

NOW_ALG = datetime.now(timezone.utc) + timedelta(hours=1)  # UTC+1 Algeria

# ── Bilingual subject alias table ──────────────────────────────────────────────
# Maps any known French / Arabic / short variant -> canonical English config name
SUBJECT_ALIASES = {
    # Control Systems 2
    "systeme asservi":                          "Control Systems 2",
    "asservi":                                  "Control Systems 2",
    "sa2":                                      "Control Systems 2",
    "control system":                           "Control Systems 2",
    # Robotique
    "robotique":                                "Robotique",
    # Electrical Machines
    "machines electriques":                     "Electrical Machines",
    "machine electrique":                       "Electrical Machines",
    "machines et drives":                       "Electrical Machines",
    # Power Electronics
    "electronique de puissance":                "Power Electronics",
    "puissance":                                "Power Electronics",
    "mokhnache":                                "Power Electronics",
    # Embedded Computer
    "calculateur embarque":                     "Embedded Computer 1",
    "embedded computer":                        "Embedded Computer 1",
    "embedded":                                 "Embedded Computer 1",
    "embarque":                                 "Embedded Computer 1",
    "merabti":                                  "Embedded Computer 1",
    # Industrial Sensors — require multi-word to avoid false matches
    "capteurs et instrumentation":              "Industrial Sensors",
    "capteurs instrumentation":                 "Industrial Sensors",
    "capteurs industriels":                     "Industrial Sensors",
    "capteurs":                                 "Industrial Sensors",
    "capteur":                                  "Industrial Sensors",
    "hais":                                     "Industrial Sensors",
    # NOTE: "instrumentation" alone is intentionally NOT here — too ambiguous
    # Innovation Management
    "management de l innovation":               "Innovation Management",
    "management innovation":                    "Innovation Management",
    "management":                               "Innovation Management",
    "innovation":                               "Innovation Management",
    "babes":                                    "Innovation Management",
    # Programmable Automation — longer keys listed FIRST (longest-match wins)
    "automates programmables industriels":      "Industrial Programmable Automation",
    "automates programmable industriel":        "Industrial Programmable Automation",
    "automates programmables":                  "Industrial Programmable Automation",
    "automate programmable":                    "Industrial Programmable Automation",
    "programmable industriel":                  "Industrial Programmable Automation",
    "api":                                      "Industrial Programmable Automation",
    "righi":                                    "Industrial Programmable Automation",
}


def find_meeting(meetings, subject_hint):
    """
    Find matching meeting(s) for a subject hint string.
    1. Check SUBJECT_ALIASES for a canonical name prefix
    2. Fall back to fuzzy word-overlap on meeting names
    Returns list of matched meetings (may be multiple e.g. C + TD of same subject).
    """
    # Normalise: strip accents/special chars, lowercase
    def norm(s):
        s = s.lower()
        for a, b in [("é","e"),("è","e"),("ê","e"),("à","a"),("â","a"),
                     ("ù","u"),("û","u"),("î","i"),("ô","o"),("ç","c"),("'",""),("'","")]:
            s = s.replace(a, b)
        return re.sub(r'[^a-z0-9 ]', ' ', s).strip()

    hint_norm = norm(subject_hint)

    # Step 1: alias lookup (longest match wins)
    canonical = None
    best_len = 0
    for alias, canon in SUBJECT_ALIASES.items():
        alias_norm = norm(alias)
        if alias_norm in hint_norm and len(alias_norm) > best_len:
            canonical = norm(canon)
            best_len = len(alias_norm)

    results = []
    for m in meetings:
        name_norm = norm(m["name"])
        if canonical:
            # Require the canonical string to appear as a prefix in the config name.
            # This prevents "Industrial Programmable Automation" from matching
            # "Industrial Sensors and Instrumentation" via the shared word "industrial".
            if name_norm.startswith(canonical) or canonical in name_norm[:len(canonical) + 5]:
                results.append(m)
        else:
            # Fuzzy fallback: word overlap (only used when alias table has no match)
            hint_words = [w for w in hint_norm.split() if len(w) > 4]
            name_words = [w for w in name_norm.split() if len(w) > 4]
            if hint_words and any(w in name_words for w in hint_words):
                results.append(m)

    return results



# ── Date resolution ────────────────────────────────────────────────────────────

def resolve_day_from_message(day_str, msg_date):
    """
    Resolve a day string relative to msg_date (the message send date).
    Returns a datetime.date or None if can't resolve.

    day_str examples: "Tomorrow", "demain", "Sunday", "Dimanche", "next week", ...
    msg_date: a timezone-aware datetime (UTC) of when the message was sent.
    """
    if not day_str:
        return None

    # Localise message date to Algeria time (UTC+1)
    msg_local = msg_date.astimezone(timezone(timedelta(hours=1)))
    msg_day = msg_local.date()

    day_lower = day_str.strip().lower()

    # Relative keywords
    if day_lower in ("tomorrow", "demain", "ghodwa", "ghda"):
        return msg_day + timedelta(days=1)

    if day_lower in ("today", "aujourd'hui", "lyoum"):
        return msg_day

    if "next week" in day_lower or "semaine prochaine" in day_lower or "usbu3" in day_lower:
        # Return None — "next week" is too vague, skip the update
        return None

    # Named weekday
    for name, weekday_num in DAY_NAMES.items():
        if name in day_lower:
            # Find next occurrence of that weekday on or after the message date
            days_ahead = (weekday_num - msg_day.weekday()) % 7
            return msg_day + timedelta(days=days_ahead)

    return None


def is_date_in_past(resolved_date):
    """Returns True if the resolved date has already passed (before today Algeria time)."""
    today = NOW_ALG.date()
    return resolved_date < today


def date_to_weekday_name(resolved_date):
    """Convert a date to the weekday name used in config.json."""
    return resolved_date.strftime("%A")  # e.g. "Wednesday"


# ── Config helpers ──────────────────────────────────────────────────────────────

def load_config():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(base_dir, CONFIG_FILE)
    if not os.path.exists(cfg_path):
        ex_path = os.path.join(base_dir, "config.example.json")
        if os.path.exists(ex_path):
            try:
                import shutil
                shutil.copy(ex_path, cfg_path)
            except Exception:
                pass
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8-sig") as f:
            raw = f.read().strip()
        if not raw:
            return {"meetings": [], "settings": {}}
        return json.loads(raw)
    return {"meetings": [], "settings": {}}


DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

def _class_window(meeting, ref=None):
    ref = ref or datetime.now()
    start = datetime.combine(ref.date(), datetime.strptime(meeting["time"], "%H:%M").time())
    return start, start + timedelta(minutes=meeting.get("duration_minutes", 90))

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def check_time_conflict(meetings, day, time, exclude_name=None):
    """
    Returns list of meetings that already occupy the same day+time slot.
    Excludes the meeting being updated itself.
    """
    conflicts = []
    for m in meetings:
        if m.get("name") == exclude_name:
            continue
        if m.get("day_of_week", "").lower() == day.lower() and m.get("time") == time:
            conflicts.append(m["name"])
    return conflicts


def update_meeting_url(subject, meet_url, raw_text=""):
    """Update Google Meet URL with specific (C) vs (TD) discrimination and live config sync."""
    # Guard against obsolete/dead link recycled from previous weeks
    OBSOLETE_URLS = ["https://meet.google.com/mce-kjtq-cep"]
    if meet_url in OBSOLETE_URLS:
        print(f" [REJECT OBSOLETE LINK] Ignoring dead/recycled meet link: {meet_url}")
        return False

    config = load_config()
    meetings = config.get("meetings", [])

    matched_list = find_meeting(meetings, subject)
    if not matched_list:
        print(f" [SKIP] No existing meeting matched '{subject}' for URL update")
        return False

    combined_text = f"{subject} {raw_text}".lower()
    targets = matched_list

    # Specifically discriminate TD vs Cours
    if "(td)" in combined_text or " td" in combined_text or "td " in combined_text:
        td_only = [m for m in matched_list if "(td" in m.get("name", "").lower()]
        if td_only:
            targets = td_only
    elif "(c)" in combined_text or " cours" in combined_text or "cour " in combined_text:
        c_only = [m for m in matched_list if "(c)" in m.get("name", "").lower()]
        if c_only:
            targets = c_only

    updated = False
    for m in targets:
        if meet_url and meet_url != m.get("url"):
            print(f" [URL UPDATED] {m['name']}: {m['url']} -> {meet_url}")
            m["url"] = meet_url
            updated = True
        else:
            print(f" [URL unchanged] {m['name']}: already {meet_url}")

    if updated:
        save_config(config)
        sync_task_to_dashboard(
            f"New Meet Link: {targets[0]['name']}",
            f"Updated Google Meet URL to {meet_url}",
            targets[0]['name'],
            datetime.now().strftime("%Y-%m-%d"),
            "high"
        )
    return updated


def apply_schedule_change(subject, day_str, time_str, meet_url, msg_date, notes=""):
    """
    Apply a schedule change with strict date validation anchored to msg_date.
    Never creates same-time conflicts. Never applies past-date changes.
    Messages before TIMETABLE_EPOCH cannot override day/time (new timetable supersedes them).
    """
    config = load_config()
    meetings = config.get("meetings", [])

    combined_hint = f"{subject} {notes}".lower()
    if not day_str and ("demain" in combined_hint or "ghodwa" in combined_hint):
        day_str = "demain"

    # Block day/time changes from messages predating the new official timetable (Oct 1 2026).
    # URL updates from old messages are still fine.
    if msg_date < TIMETABLE_EPOCH and (day_str or time_str):
        print(f" [EPOCH SKIP] '{subject}' day/time change from {msg_date.date()} blocked — predates new timetable (Oct 1)")
        day_str = None
        time_str = None
        if not meet_url:
            return False  # nothing left to update


    # 1. Resolve the actual date from the message send time
    resolved_date = resolve_day_from_message(day_str, msg_date) if day_str else None

    if day_str and resolved_date is None:
        print(f" [SKIP] '{day_str}' is too vague or unresolvable (msg sent {msg_date.date()})")
        return False

    if resolved_date and is_date_in_past(resolved_date):
        print(f" [SKIP] Resolved date {resolved_date} is in the PAST (msg sent {msg_date.date()}, day='{day_str}')")
        return False

    resolved_day_name = date_to_weekday_name(resolved_date) if resolved_date else None

    # 2. Find the matching existing meeting(s)
    matched_list = find_meeting(meetings, subject)

    if not matched_list:
        print(f" [SKIP] No existing meeting matched '{subject}' — won't add unknown course")
        return False

    # Prefer TD or C specifically if indicated in text or subject
    matched = matched_list[0]
    if "(td)" in combined_hint or " td" in combined_hint or "td " in combined_hint:
        td_matches = [m for m in matched_list if "(td" in m.get("name", "").lower()]
        if td_matches:
            matched = td_matches[0]
    elif "(c)" in combined_hint or " cours" in combined_hint:
        c_matches = [m for m in matched_list if "(c)" in m.get("name", "").lower()]
        if c_matches:
            matched = c_matches[0]

    if resolved_day_name and time_str:
        conflicts = check_time_conflict(meetings, resolved_day_name, time_str,
                                        exclude_name=matched["name"] if matched else None)
        if conflicts:
            print(f" [CONFLICT BLOCKED] {subject} at {resolved_day_name} {time_str} "
                  f"conflicts with: {', '.join(conflicts)} — NOT applied. Verify manually.")
            return False

    # 4. Apply changes (matched is guaranteed non-None at this point)
    changes = []

    if resolved_day_name and resolved_day_name != matched.get("day_of_week"):
        changes.append(f"Day: {matched['day_of_week']} -> {resolved_day_name} (resolved from '{day_str}' in msg of {msg_date.date()})")
        matched["day_of_week"] = resolved_day_name
    if time_str and time_str != matched.get("time"):
        changes.append(f"Time: {matched['time']} -> {time_str}")
        matched["time"] = time_str
    if meet_url and meet_url != matched.get("url"):
        changes.append(f"URL: {matched['url']} -> {meet_url}")
        matched["url"] = meet_url

    if changes:
        save_config(config)
        for c in changes:
            print(f" [UPDATED] {matched['name']}: {c}")
        return True
    else:
        print(f" [NO CHANGE] {subject} — nothing new to update")
        return False


def apply_schedule_cancellation(subject, day_str, msg_date, notes=""):
    """
    Mark a session cancelled for a specific date (add to cancelled_dates in config.json).
    """
    config = load_config()
    meetings = config.get("meetings", [])

    resolved_date = resolve_day_from_message(day_str, msg_date) if day_str else None
    if not resolved_date:
        msg_local = msg_date.astimezone(timezone(timedelta(hours=1)))
        resolved_date = msg_local.date()

    date_str = resolved_date.strftime("%Y-%m-%d")
    matched_list = find_meeting(meetings, subject)
    if not matched_list:
        print(f" [SKIP CANCEL] No meeting matched '{subject}'")
        return False

    combined_hint = f"{subject} {notes}".lower()
    both = ("cours et td" in combined_hint or "cours w td" in combined_hint or
            ("cours" in combined_hint and "td" in combined_hint) or
            "m3ndhach cnx" in combined_hint or "mat9rawch" in combined_hint)

    targets = []
    if both:
        targets = matched_list
    elif "(td)" in combined_hint or " td" in combined_hint:
        targets = [m for m in matched_list if "(td" in m.get("name", "").lower()] or matched_list[:1]
    elif "(c)" in combined_hint or " cours" in combined_hint:
        targets = [m for m in matched_list if "(c)" in m.get("name", "").lower()] or matched_list[:1]
    else:
        targets = matched_list[:1]

    updated = False
    for matched in targets:
        cancelled_dates = matched.setdefault("cancelled_dates", [])
        if date_str not in cancelled_dates:
            cancelled_dates.append(date_str)
            updated = True
            print(f" [CANCEL APPLIED] 🚫 {matched['name']} cancelled for {date_str} (Notes: {notes})")
        else:
            print(f" [CANCEL UNCHANGED] {matched['name']} already marked cancelled for {date_str}")

    if updated:
        save_config(config)
        return True
    return False


def sync_task_to_dashboard(title, description, category, due_date, priority="medium"):
    """Auto-sync parsed announcements or tasks directly into the website dashboard's tasks_data.json."""
    tasks_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "tasks_data.json")
    if os.path.exists(tasks_path):
        try:
            with open(tasks_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            tasks = data.setdefault("tasks", [])
            for t in tasks:
                if t.get("title", "").strip().lower() == title.strip().lower():
                    return
            task_id = f"task_auto_{int(datetime.now().timestamp())}"
            tasks.append({
                "id": task_id,
                "title": title,
                "description": description,
                "category": category,
                "type": "academic",
                "due_date": due_date,
                "original_date": due_date,
                "rolled_over": False,
                "priority": priority,
                "status": "pending",
                "source": "telegram_auto_watcher"
            })
            with open(tasks_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f" [DASHBOARD SYNC] Added task '{title}' to website tasks_data.json")
        except Exception as e:
            print(f" [DASHBOARD SYNC ERROR] {e}")


def process_parsed_announcement(parsed, raw_text, msg_date):
    """Unified handler for parsed schedule updates (reschedule vs cancellation)."""
    subj = parsed.get("subject")
    if not subj or subj == "unknown":
        return False

    notes = parsed.get("notes", "")
    day_raw = parsed.get("day_of_week")
    time_raw = parsed.get("time")
    url_raw = parsed.get("meet_url")

    check_str = f"{raw_text} {notes}".lower()
    cancel_keywords = [
        "annulation", "annulé", "annule", "pas de", "makanesh",
        "annulee", "annulée", "will not take place", "cancelled",
        "canceled", "supprimé", "supprime"
    ]
    is_cancelled = any(kw in check_str for kw in cancel_keywords)

    due_d = msg_date.strftime("%Y-%m-%d")
    if is_cancelled:
        res = apply_schedule_cancellation(subject=subj, day_str=day_raw, msg_date=msg_date, notes=notes or raw_text[:80])
        if res:
            sync_task_to_dashboard(f"Cancelled Session: {subj}", f"Class cancelled: {notes or raw_text[:100]}", subj, due_d, "low")
        return res
    else:
        res = apply_schedule_change(subject=subj, day_str=day_raw, time_str=time_raw, meet_url=url_raw, msg_date=msg_date, notes=notes or raw_text[:80])
        if res:
            sync_task_to_dashboard(f"Schedule Changed: {subj}", f"Rescheduled to {day_raw or 'updated slot'} at {time_raw or 'updated time'}. Notes: {notes or raw_text[:100]}", subj, due_d, "medium")
        return res


def extract_text_from_image_gemini(image_bytes, caption=""):
    """Use Gemini Flash Vision to read screenshots / photos sent on Telegram."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(" [VISION] GEMINI_API_KEY not found in environment, skipping OCR.")
        return ""

    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={api_key}"
        prompt = (
            "You are an assistant analyzing a university schedule announcement, email screenshot, or chat screenshot for Algerian L3 Automatique students. "
            "Transcribe and extract all relevant schedule details: course name, date, time, whether any class/TD/TP is cancelled, rescheduled, or moved. "
            f"Optional caption: {caption}"
        )
        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                    {"text": prompt}
                ]
            }]
        }
        resp = requests.post(url, json=payload, timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        else:
            print(f" [VISION ERROR] Gemini returned status {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f" [VISION EXCEPTION] Failed to run vision OCR: {e}")
    return ""



# ── n8n Gemini parser ──────────────────────────────────────────────────────────

def parse_locally_fallback(message_text):
    """
    Local NLP heuristic for Algerian university announcements (French + Darija).
    Activates when n8n parser times out or is unreachable.
    """
    lower = message_text.lower()
    cancel_keywords = [
        "mat9rawch", "matqrawch", "makanesh", "makach", "makanx",
        "annulé", "annule", "annulee", "annulée", "annulation",
        "reporté", "reporte", "reportee", "reportée", "report",
        "pas de td", "pas de cours", "pas de seance", "pas de séance",
        "y3awdhalna", "n3awdoha", "déplacement", "deplacement"
    ]
    is_cancel = any(kw in lower for kw in cancel_keywords)
    reschedule_keywords = ["bedlo", "badlo", "changement", "décalé", "decale", "tbadal", "tbadel"]
    is_reschedule = any(kw in lower for kw in reschedule_keywords)

    if not (is_cancel or is_reschedule):
        return {"is_schedule_update": False}

    cfg = load_config()
    now_dt = datetime.now()
    today_name = DAYS[now_dt.weekday()]
    found_subject = None

    for alias, canon in SUBJECT_ALIASES.items():
        if alias in lower:
            found_subject = canon
            break

    if not found_subject:
        is_td_msg = "td" in lower
        is_cours_msg = any(w in lower for w in ["cours", "cour", "cours magistral"])
        for m in cfg.get("meetings", []):
            if m.get("day_of_week", "").lower() == today_name:
                s_dt, e_dt = _class_window(m, now_dt)
                if s_dt - timedelta(minutes=60) <= now_dt <= e_dt + timedelta(minutes=30):
                    base_name = re.sub(r'\s*\((?:c|td|tp|td/tp)\)', '', m.get("name", ""), flags=re.I).strip()
                    if is_td_msg and is_cours_msg:
                        found_subject = base_name
                        break
                    elif is_td_msg and "(td" in m.get("name", "").lower():
                        found_subject = m["name"]
                        break
                    elif is_cours_msg and "(c)" in m.get("name", "").lower():
                        found_subject = m["name"]
                        break
                    elif not found_subject:
                        found_subject = base_name

    if not found_subject:
        return {"is_schedule_update": False}

    print(f" [LOCAL PARSER] Inferred active subject: '{found_subject}', cancelled={is_cancel}")
    return {
        "is_schedule_update": True,
        "subject": found_subject,
        "day_of_week": today_name.capitalize(),
        "time": None,
        "meet_url": None,
        "notes": message_text[:120]
    }


def parse_with_n8n(message_text, retries=2):
    for attempt in range(retries):
        try:
            resp = requests.post(N8N_PARSER_URL, json={"message_text": message_text}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("is_schedule_update"):
                    return data
        except Exception as e:
            if attempt == retries - 1:
                print(f" [!] n8n parser unavailable ({e}), engaging local heuristic parser...")
            import time
            time.sleep(0.5)

    # Seamless fallback to local Darija/French NLP heuristic
    return parse_locally_fallback(message_text)


# ── Initial scan ───────────────────────────────────────────────────────────────

async def initial_scan(client, entity):
    print("\n" + "="*60)
    print("RUNNING SMART SCAN (Sep 20 to present) — date-anchored mode")
    print("="*60)
    start_date = datetime(2026, 9, 20, 0, 0, 0, tzinfo=timezone.utc)

    # 1. Google Meet channel — URL updates only (no date changes)
    print("\n[1/3] Scanning Google Meet topic for URL updates...")
    meet_msgs = await client.get_messages(entity, reply_to=TOPIC_MEET, limit=50)
    # Process oldest first so latest URL wins
    for m in reversed(meet_msgs):
        if m.date >= start_date and m.text and "meet.google.com" in m.text:
            url_match = re.search(r'https?://meet\.google\.com/[a-z0-9\-]+', m.text)
            if url_match:
                url = url_match.group(0)
                # Extract subject from first non-empty line
                lines = [l.strip() for l in m.text.split("\n") if l.strip()]
                sub = lines[0]
                for junk in ["Lien du cours", "Lien TD", "Lien cours", "Lien", "🔗", "📎", ":"]:
                    sub = sub.replace(junk, "").strip()
                if len(sub) < 3:
                    sub = lines[1] if len(lines) > 1 else "Cours"
                print(f" -> [{m.date.date()}] Meet link for '{sub}': {url}")
                update_meeting_url(sub, url, raw_text=lines[0] if lines else "")

    # 2. Important topic — schedule announcements, date-anchored
    print("\n[2/3] Scanning Important topic for schedule changes...")
    imp_msgs = await client.get_messages(entity, reply_to=TOPIC_IMPORTANT, limit=40)
    for m in reversed(imp_msgs):
        if m.date >= start_date and m.text:
            parsed = parse_with_n8n(m.text)
            if parsed.get("is_schedule_update") and parsed.get("subject") and parsed.get("subject") != "unknown":
                day_raw = parsed.get("day_of_week")
                time_raw = parsed.get("time")
                url_raw = parsed.get("meet_url")
                notes = parsed.get("notes", m.text[:80])
                print(f" -> [{m.date.date()}] '{notes}' | subject={parsed['subject']} day={day_raw} time={time_raw}")
                process_parsed_announcement(
                    parsed=parsed,
                    raw_text=m.text,
                    msg_date=m.date
                )

    # 3. Discussion — only if has meet link or explicit reschedule
    print("\n[3/3] Scanning Discussion topic...")
    disc_msgs = await client.get_messages(entity, reply_to=TOPIC_DISCUSSION, limit=40)
    for m in reversed(disc_msgs):
        if m.date >= start_date and m.text:
            has_meet = "meet.google.com" in m.text
            has_reschedule = any(kw in m.text.lower() for kw in [
                "reporté", "reporte", "annulé", "annule", "bedlo", "changement", "décalé", "pas de"
            ])
            if has_meet or has_reschedule:
                parsed = parse_with_n8n(m.text)
                if parsed.get("is_schedule_update") and parsed.get("subject") and parsed.get("subject") != "unknown":
                    notes = parsed.get("notes", m.text[:80])
                    print(f" -> [{m.date.date()}] Discussion: '{notes}'")
                    process_parsed_announcement(
                        parsed=parsed,
                        raw_text=m.text,
                        msg_date=m.date
                    )

    print("\nInitial scan complete!\n")


# ── Live watcher & RAM-Optimized Session Supervisor ───────────────────────────

_bot_subprocess = None

def is_bot_running():
    global _bot_subprocess
    if _bot_subprocess is not None and _bot_subprocess.poll() is None:
        return True
    try:
        import psutil
        for p in psutil.process_iter(['name', 'cmdline']):
            if 'python' in (p.info.get('name') or '').lower():
                cmd = ' '.join(p.info.get('cmdline') or [])
                if 'bot.py' in cmd:
                    return True
    except Exception:
        pass
    return False


async def session_supervisor():
    """
    On-Demand RAM Optimizer:
    Monitors today's schedule from config.json.
    Only spawns bot.py when a class is due (or active).
    When no classes are active, bot.py exits, keeping RAM free.
    """
    global _bot_subprocess
    print("[SUPERVISOR] 🧠 On-Demand Session Supervisor started. Monitoring for upcoming classes...")
    while True:
        try:
            cfg = load_config()
            if cfg.get("enabled", True) is not False:
                now_alg = datetime.now(timezone(timedelta(hours=1)))
                today_name = DAYS[now_alg.weekday()]
                today_str = now_alg.strftime("%Y-%m-%d")

                should_run = False
                active_session_name = None

                for m in cfg.get("meetings", []):
                    if today_str in m.get("cancelled_dates", []):
                        continue
                    if m.get("day_of_week", "").lower() != today_name:
                        continue

                    time_str = m.get("time")
                    if not time_str:
                        continue

                    sh, sm = map(int, time_str.split(":"))
                    class_start = now_alg.replace(hour=sh, minute=sm, second=0, microsecond=0)
                    class_end = class_start + timedelta(minutes=m.get("duration_minutes", 90))

                    # If within 5 minutes before class up to class end:
                    if (class_start - timedelta(minutes=5)) <= now_alg < class_end:
                        should_run = True
                        active_session_name = m["name"]
                        break

                if should_run:
                    if not is_bot_running():
                        print(f"[{now_alg.strftime('%H:%M:%S')}] [SUPERVISOR] 🚀 Class due/active: '{active_session_name}'. Spawning bot.py...")
                        _bot_subprocess = subprocess.Popen([sys.executable, "bot.py"], cwd=os.path.dirname(os.path.abspath(__file__)))
        except Exception as e:
            print(f"[SUPERVISOR ERROR] {e}")
        await asyncio.sleep(20)


async def main():
    print("=" * 60)
    print("TELEGRAM SCHEDULE & MEET LINK LIVE WATCHER")
    now_str = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
    print(f"Now (Algeria time): {now_str}")
    print("=" * 60)

    # Start the on-demand RAM optimizer supervisor
    asyncio.create_task(session_supervisor())

    while True:
        client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        try:
            await client.connect()
            entity = await client.get_entity(GROUP_ID)
            print(f"Connected to: {entity.title}")

            await initial_scan(client, entity)

            @client.on(events.NewMessage(chats=entity))
            async def handler(event):
                m = event.message
                text = m.text or ""

                # Check for photo or image media
                is_photo = bool(m.photo or (m.file and getattr(m.file, 'mime_type', '').startswith('image/')))
                if is_photo:
                    print(f"\n[LIVE PHOTO] Received image attachment in Telegram. Downloading for Gemini Vision OCR...")
                    try:
                        img_bytes = await client.download_media(m.media, bytes)
                        if img_bytes:
                            extracted_text = extract_text_from_image_gemini(img_bytes, caption=text)
                            if extracted_text:
                                print(f" -> Vision extracted: {extracted_text[:120]}...")
                                text = f"{text}\n\n[Screenshot OCR Text]:\n{extracted_text}".strip()
                    except Exception as e:
                        print(f" -> Vision OCR error: {e}")

                if not text:
                    return

                topic_id = getattr(m.reply_to, 'reply_to_top_id', None) or getattr(m.reply_to, 'reply_to_msg_id', None)

                has_meet = "meet.google.com" in text.lower()
                has_keywords = is_photo or any(kw in text.lower() for kw in [
                    "cours", "séance", "seance", "td", "tp", "ghodwa", "demain", "dimanche", "lundi",
                    "mardi", "mercredi", "jeudi", "reporté", "reporte", "annulé", "annule", "heure", "bedlo",
                    "changement", "décalé", "pas de", "makanesh"
                ])

                if topic_id in [TOPIC_MEET, TOPIC_IMPORTANT] or (topic_id == TOPIC_DISCUSSION and (has_meet or has_keywords)) or is_photo:
                    print(f"\n[LIVE] [{m.date.date()}] (Topic {topic_id}): {text[:100]}")

                    if topic_id == TOPIC_MEET and has_meet:
                        # URL-only update from meet channel
                        url_match = re.search(r'https?://meet\.google\.com/[a-z0-9\-]+', text)
                        if url_match:
                            lines = [l.strip() for l in text.split("\n") if l.strip()]
                            sub = lines[0].replace("Lien", "").strip(" :")
                            update_meeting_url(sub, url_match.group(0), raw_text=text)
                    else:
                        parsed = parse_with_n8n(text)
                        if parsed.get("is_schedule_update"):
                            subj = parsed.get("subject")
                            if not subj or subj == "unknown":
                                cfg = load_config()
                                now_dt = datetime.now()
                                today_name = DAYS[now_dt.weekday()]
                                is_td_msg = "td" in text.lower()
                                is_cours_msg = any(w in text.lower() for w in ["cours", "cours magistral", "cour"])
                                for m in cfg.get("meetings", []):
                                    if m.get("day_of_week", "").lower() == today_name:
                                        s_dt, e_dt = _class_window(m, now_dt)
                                        if s_dt - timedelta(minutes=45) <= now_dt <= e_dt:
                                            if is_td_msg and "(td" in m.get("name", "").lower():
                                                subj = m["name"]
                                                break
                                            elif is_cours_msg and "(c)" in m.get("name", "").lower():
                                                subj = m["name"]
                                                break
                                            elif not subj:
                                                subj = m["name"]
                            if subj and subj != "unknown":
                                print(f" -> AI: {parsed.get('notes', '')} (Target: {subj})")
                                process_parsed_announcement(
                                    parsed=parsed,
                                    raw_text=text,
                                    msg_date=m.date
                                )

            print("Live watcher is RUNNING. Monitoring Automatique L3...\n")
            await client.run_until_disconnected()
        except Exception as e:
            print(f"\n[WATCHER RECOVERY] Disconnected or error: {e}. Reconnecting in 15 seconds...")
            await asyncio.sleep(15)
        finally:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    _lock = acquire_lock()  # exits immediately if another instance is running
    try:
        asyncio.run(main())
    finally:
        _lock.close()
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass
