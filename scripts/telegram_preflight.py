import os
import sys
import json
import re
import shutil
from datetime import datetime, timezone, timedelta

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Constants ──────────────────────────────────────────────────────────────────
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSION_NAME = "telegram_user"
GROUP_ID = -1002250295347  # Automatique L3

TOPIC_IMPORTANT = 989
TOPIC_MEET = 993
TOPIC_DISCUSSION = 987

ALG_TZ = timezone(timedelta(hours=1))

SUBJECT_ALIASES = {
    "Control Systems 2": ["systeme asservi", "asservi", "sa2", "control system", "gherbi"],
    "Robotique": ["robotique", "ramdani"],
    "Electrical Machines and Drives": ["machines electriques", "machine electrique", "machines et drives", "chelli", "machines"],
    "Power Electronics": ["electronique de puissance", "puissance", "mokhnache"],
    "Embedded Computer 1": ["calculateur embarque", "embedded computer", "embedded", "embarque", "merabti"],
    "Industrial Sensors and Instrumentation": ["capteurs et instrumentation", "capteurs", "capteur", "hais", "hafs", "instrumentation"],
    "Innovation Management": ["management", "innovation", "babes"],
    "Industrial Programmable Automation 1": ["automates programmables", "automate programmable", "automates", "automate", "api", "righi", "programmable"],
}

CANCEL_KEYWORDS = [
    "mat9rawch", "matqrawch", "makanesh", "makach", "makanx", "machi lyoum",
    "annulé", "annule", "annulee", "annulée", "annulation",
    "pas de td", "pas de cours", "pas de seance", "pas de séance", "pas de classe",
    "m3ndhach cnx", "m3andhach cnx", "makach cnx", "probleme de connexion", "problème de connexion",
    "problème ta connexion", "probleme ta connexion", "cnx b sebat les travaux",
    "khayro nhar", "t3awd la seance", "t3awd la séance", "n3awdo fih", "y3awdhalna",
    "reporté", "reporte", "reportee", "reportée"
]


def load_config(config_file="config.json"):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    target_path = os.path.join(base_dir, config_file) if not os.path.isabs(config_file) else config_file
    if not os.path.exists(target_path):
        ex_path = os.path.join(base_dir, "config.example.json")
        if os.path.exists(ex_path):
            try:
                shutil.copy(ex_path, target_path)
            except Exception:
                pass
    try:
        with open(target_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(config, config_file="config.json"):
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[PRE-FLIGHT] Error saving config: {e}")
        return False


def run_telegram_preflight_check(meeting, config_file="config.json"):
    """
    Mandatory Telegram pre-flight check executed right before starting preparation/joining a session.
    Inspects today's announcements in Important, Discussion, and Meet topics.
    Returns:
        {
            "action": "CANCEL" | "RESCHEDULE" | "PROCEED",
            "reason": str,
            "new_time": str (if RESCHEDULE),
            "new_url": str (if meet URL changed),
            "url_updated": bool
        }
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    session_src = os.path.join(base_dir, f"{SESSION_NAME}.session")
    if not os.path.exists(session_src):
        print(f"[PRE-FLIGHT] Notice: {SESSION_NAME}.session not found, skipping Telegram check.")
        return {"action": "PROCEED"}

    # Use unique temp session file copy to avoid SQLite database lock contention with live watcher
    temp_session_name = f"preflight_temp_{os.getpid()}"
    temp_session_file = os.path.join(base_dir, f"{temp_session_name}.session")
    try:
        shutil.copy(session_src, temp_session_file)
    except Exception as e:
        print(f"[PRE-FLIGHT] Notice: could not copy session ({e}), proceeding.")
        return {"action": "PROCEED"}

    from telethon.sync import TelegramClient

    now_alg = datetime.now(ALG_TZ)
    today_alg = now_alg.date()

    all_messages = []
    try:
        with TelegramClient(temp_session_name, API_ID, API_HASH) as client:
            if not client.is_user_authorized():
                print("[PRE-FLIGHT] Client not authorized.")
                return {"action": "PROCEED"}

            for tid in [TOPIC_IMPORTANT, TOPIC_DISCUSSION, TOPIC_MEET]:
                try:
                    msgs = client.get_messages(GROUP_ID, limit=35, reply_to=tid)
                    for m in msgs:
                        if m.text:
                            m_alg = m.date.astimezone(ALG_TZ)
                            if m_alg.date() == today_alg:
                                all_messages.append((m_alg, tid, m.text.strip()))
                except Exception as ex:
                    print(f"[PRE-FLIGHT] Warning reading topic {tid}: {ex}")
    except Exception as e:
        print(f"[PRE-FLIGHT] Telegram connection error ({e}), proceeding with normal schedule.")
        return {"action": "PROCEED"}
    finally:
        # Clean up temporary session file
        try:
            if os.path.exists(temp_session_file):
                os.remove(temp_session_file)
        except Exception:
            pass

    if not all_messages:
        print(f"[{now_alg.strftime('%H:%M:%S')}] [PRE-FLIGHT] Telegram checked: 0 messages posted today. Proceeding.")
        return {"action": "PROCEED"}

    # Sort chronologically so later messages override earlier ones
    all_messages.sort(key=lambda x: x[0])

    m_name = meeting["name"]
    m_name_lower = m_name.lower()
    is_td = "(td" in m_name_lower or " td" in m_name_lower
    is_cours = "(c)" in m_name_lower or " cours" in m_name_lower
    prof = meeting.get("professor", "").lower()

    canonical = None
    for subj, aliases in SUBJECT_ALIASES.items():
        if subj.lower() in m_name_lower or any(a in m_name_lower for a in aliases):
            canonical = subj
            break
    aliases = list(SUBJECT_ALIASES.get(canonical, [])) if canonical else []
    if prof:
        aliases.append(prof)

    start_time_str = meeting.get("time", "00:00")
    try:
        sh, sm = map(int, start_time_str.split(":"))
        m_start_dt = now_alg.replace(hour=sh, minute=sm, second=0, microsecond=0)
    except Exception:
        m_start_dt = now_alg
    m_window_start = m_start_dt - timedelta(minutes=60)
    m_window_end = m_start_dt + timedelta(minutes=meeting.get("duration_minutes", 90))

    cancelled = False
    cancel_reason = None
    cancel_scope = "single"  # "single" or "both"
    rescheduled_time = None
    new_url = None

    for m_dt, tid, text in all_messages:
        txt_lower = text.lower()

        # Check if text names a DIFFERENT subject explicitly
        other_subject = False
        for subj, other_aliases in SUBJECT_ALIASES.items():
            if subj != canonical and any(a in txt_lower for a in other_aliases):
                other_subject = True
                break
        if other_subject:
            continue

        # Check if text matches this meeting
        direct_match = any(a in txt_lower for a in aliases)
        in_time_window = m_window_start <= m_dt <= m_window_end
        context_match = in_time_window and any(w in txt_lower for w in ["prof", "seance", "séance", "cours", "td", "mat9rawch", "cnx"])

        if not (direct_match or context_match):
            continue

        # Check for Google Meet link update
        meet_match = re.search(r'https?://meet\.google\.com/[a-z0-9\-]+', text)
        if meet_match:
            new_url = meet_match.group(0)

        # Check cancellation
        is_cancel = any(kw in txt_lower for kw in CANCEL_KEYWORDS)
        if is_cancel:
            both = ("cours et td" in txt_lower or "cours w td" in txt_lower or
                    ("cours" in txt_lower and "td" in txt_lower) or
                    "m3ndhach cnx" in txt_lower or "probleme de connexion" in txt_lower or
                    "khayro nhar" in txt_lower)
            if both:
                cancelled = True
                cancel_reason = text
                cancel_scope = "both"
            elif is_td and any(w in txt_lower for w in ["pas de td", "makach td", "mat9rawch td"]):
                cancelled = True
                cancel_reason = text
                cancel_scope = "single"
            elif is_cours and any(w in txt_lower for w in ["pas de cours", "makach cours", "mat9rawch cours"]):
                cancelled = True
                cancel_reason = text
                cancel_scope = "single"
            elif not ("pas de cours" in txt_lower and is_td) and not ("pas de td" in txt_lower and is_cours):
                cancelled = True
                cancel_reason = text
                cancel_scope = "both" if "mat9rawch" in txt_lower else "single"

        # Check reschedule time for today
        time_match = re.search(r'(?:le td est à|cours à|à|a|3la)\s*([0-1]?[0-9]|2[0-3])(?:h|:|\s*h\s*)([0-5][0-9])?', txt_lower)
        if time_match and not ("demain" in txt_lower or "ghodwa" in txt_lower):
            hr = int(time_match.group(1))
            mn = int(time_match.group(2)) if time_match.group(2) else 0
            if is_td and "cours" in txt_lower and "td" not in txt_lower:
                pass
            elif is_cours and "td" in txt_lower and "cours" not in txt_lower:
                pass
            else:
                rescheduled_time = f"{hr:02d}:{mn:02d}"

    # Apply findings to config.json
    cfg = load_config(config_file)
    today_str = today_alg.strftime("%Y-%m-%d")
    cfg_updated = False

    if cancelled:
        clean_notes = cancel_reason.replace("\n", " ")[:100]
        # Mark cancelled in config.json
        for m in cfg.get("meetings", []):
            name_check = m.get("name", "").lower()
            should_cancel = False
            if m.get("name") == meeting["name"]:
                should_cancel = True
            elif cancel_scope == "both" and canonical and canonical.lower() in name_check:
                should_cancel = True

            if should_cancel:
                cd = m.setdefault("cancelled_dates", [])
                if today_str not in cd:
                    cd.append(today_str)
                    cfg_updated = True
                    print(f"[PRE-FLIGHT] 🚫 Marked {m['name']} cancelled for today ({today_str}) in config.json")

        if cfg_updated:
            save_config(cfg, config_file)

        return {
            "action": "CANCEL",
            "reason": clean_notes,
            "meeting_name": meeting["name"]
        }

    if rescheduled_time and rescheduled_time != meeting.get("time"):
        for m in cfg.get("meetings", []):
            if m.get("name") == meeting["name"]:
                print(f"[PRE-FLIGHT] ⏰ Updating {m['name']} scheduled time in config.json: {m.get('time')} -> {rescheduled_time}")
                m["time"] = rescheduled_time
                cfg_updated = True
        if cfg_updated:
            save_config(cfg, config_file)
        return {
            "action": "RESCHEDULE",
            "new_time": rescheduled_time,
            "reason": f"Rescheduled via Telegram to {rescheduled_time}",
            "meeting_name": meeting["name"]
        }

    if new_url and new_url != meeting.get("url"):
        for m in cfg.get("meetings", []):
            if m.get("name") == meeting["name"]:
                m["url"] = new_url
                cfg_updated = True
        if cfg_updated:
            save_config(cfg, config_file)
        return {
            "action": "PROCEED",
            "url_updated": True,
            "url": new_url
        }

    print(f"[{now_alg.strftime('%H:%M:%S')}] [PRE-FLIGHT] Telegram verified: No cancellations or reschedules for {meeting['name']}. Proceeding.")
    return {"action": "PROCEED"}


if __name__ == "__main__":
    test_meeting = {
        "name": "Industrial Programmable Automation 1 (TD)",
        "time": "09:30",
        "duration_minutes": 90,
        "professor": "RIGHI"
    }
    result = run_telegram_preflight_check(test_meeting)
    print("Pre-flight check execution result:", result)
