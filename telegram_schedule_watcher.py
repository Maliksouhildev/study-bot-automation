import asyncio
import sys
import json
import os
import re
import requests
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

# New official timetable was issued Oct 1 2026.
# Schedule time/day changes from messages BEFORE this date are ignored
# (only URL updates from older messages are still applied).
TIMETABLE_EPOCH = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)


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
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
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


def update_meeting_url(subject, meet_url):
    """Update only the Google Meet URL for all matched meetings. No date changes."""
    config = load_config()
    meetings = config.get("meetings", [])

    matched_list = find_meeting(meetings, subject)
    if not matched_list:
        print(f" [SKIP] No existing meeting matched '{subject}' for URL update")
        return False

    updated = False
    for m in matched_list:
        if meet_url and meet_url != m.get("url"):
            print(f" [URL UPDATED] {m['name']}: {m['url']} -> {meet_url}")
            m["url"] = meet_url
            updated = True
        else:
            print(f" [URL unchanged] {m['name']}: already {meet_url}")

    if updated:
        save_config(config)
    return updated


def apply_schedule_change(subject, day_str, time_str, meet_url, msg_date):
    """
    Apply a schedule change with strict date validation anchored to msg_date.
    Never creates same-time conflicts. Never applies past-date changes.
    Messages before TIMETABLE_EPOCH cannot override day/time (new timetable supersedes them).
    """
    config = load_config()
    meetings = config.get("meetings", [])

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
    subject_lower = subject.lower()
    if "(td)" in subject_lower or " td" in subject_lower:
        td_matches = [m for m in matched_list if "(td" in m.get("name", "").lower()]
        if td_matches:
            matched = td_matches[0]
    elif "(c)" in subject_lower or " cours" in subject_lower:
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



# ── n8n Gemini parser ──────────────────────────────────────────────────────────

def parse_with_n8n(message_text, retries=2):
    for attempt in range(retries):
        try:
            resp = requests.post(N8N_PARSER_URL, json={"message_text": message_text}, timeout=20)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            if attempt == retries - 1:
                print(f" [!] Error calling n8n parser: {e}")
            import time
            time.sleep(1)
    return {"is_schedule_update": False}


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
                update_meeting_url(sub, url)

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
                apply_schedule_change(
                    subject=parsed["subject"],
                    day_str=day_raw,
                    time_str=time_raw,
                    meet_url=url_raw,
                    msg_date=m.date
                )

    # 3. Discussion — only if has meet link or explicit reschedule
    print("\n[3/3] Scanning Discussion topic...")
    disc_msgs = await client.get_messages(entity, reply_to=TOPIC_DISCUSSION, limit=40)
    for m in reversed(disc_msgs):
        if m.date >= start_date and m.text:
            has_meet = "meet.google.com" in m.text
            has_reschedule = any(kw in m.text.lower() for kw in [
                "reporté", "reporte", "annulé", "annule", "bedlo", "changement", "décalé"
            ])
            if has_meet or has_reschedule:
                parsed = parse_with_n8n(m.text)
                if parsed.get("is_schedule_update") and parsed.get("subject") and parsed.get("subject") != "unknown":
                    notes = parsed.get("notes", m.text[:80])
                    print(f" -> [{m.date.date()}] Discussion: '{notes}'")
                    apply_schedule_change(
                        subject=parsed["subject"],
                        day_str=parsed.get("day_of_week"),
                        time_str=parsed.get("time"),
                        meet_url=parsed.get("meet_url"),
                        msg_date=m.date
                    )

    print("\nInitial scan complete!\n")


# ── Live watcher ───────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("TELEGRAM SCHEDULE & MEET LINK LIVE WATCHER")
    print(f"Now (Algeria time): {NOW_ALG.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.connect()

    entity = await client.get_entity(GROUP_ID)
    print(f"Connected to: {entity.title}")

    await initial_scan(client, entity)

    @client.on(events.NewMessage(chats=entity))
    async def handler(event):
        m = event.message
        if not m.text:
            return

        topic_id = getattr(m.reply_to, 'reply_to_top_id', None) or getattr(m.reply_to, 'reply_to_msg_id', None)
        text = m.text

        has_meet = "meet.google.com" in text.lower()
        has_keywords = any(kw in text.lower() for kw in [
            "cours", "séance", "seance", "td", "tp", "ghodwa", "demain", "dimanche", "lundi",
            "mardi", "mercredi", "jeudi", "reporté", "reporte", "annulé", "annule", "heure", "bedlo",
            "changement", "décalé"
        ])

        if topic_id in [TOPIC_MEET, TOPIC_IMPORTANT] or (topic_id == TOPIC_DISCUSSION and (has_meet or has_keywords)):
            print(f"\n[LIVE] [{m.date.date()}] (Topic {topic_id}): {text[:100]}")

            if topic_id == TOPIC_MEET and has_meet:
                # URL-only update from meet channel
                url_match = re.search(r'https?://meet\.google\.com/[a-z0-9\-]+', text)
                if url_match:
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    sub = lines[0].replace("Lien", "").strip(" :")
                    update_meeting_url(sub, url_match.group(0))
            else:
                parsed = parse_with_n8n(text)
                if parsed.get("is_schedule_update"):
                    subj = parsed.get("subject")
                    # If subject is omitted/unknown (e.g. "Mazal mabdatch td mazelna m3a l cours"),
                    # infer the active session happening right now
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
                        apply_schedule_change(
                            subject=subj,
                            day_str=parsed.get("day_of_week"),
                            time_str=parsed.get("time"),
                            meet_url=parsed.get("meet_url"),
                            msg_date=m.date
                        )

    print("Live watcher is RUNNING. Monitoring Automatique L3...\n")
    await client.run_until_disconnected()


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
