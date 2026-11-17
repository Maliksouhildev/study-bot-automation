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
    "pas de td", "pas de cours", "pas cours", "aura pas cours", "n'y aura pas", "n’y aura pas",
    "pas de seance", "pas de séance", "pas de classe",
    "professeur est malade", "professeur e est malade", "professeure est malade", "prof malade", "malade",
    "supprimé", "supprime", "supprimée",
    "m3ndhach cnx", "m3andhach cnx", "makach cnx", "probleme de connexion", "problème de connexion",
    "problème ta connexion", "probleme ta connexion", "cnx b sebat les travaux",
    "khayro nhar", "t3awd la seance", "t3awd la séance", "n3awdo fih", "y3awdhalna",
    "reporté", "reporte", "reportee", "reportée"
]


def load_config(config_file="config.json"):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(base_dir)
    for candidate in [
        config_file if os.path.isabs(config_file) else None,
        os.path.join(root_dir, config_file),
        os.path.join(base_dir, config_file)
    ]:
        if candidate and os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def save_config(config, config_file="config.json"):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(base_dir)
    target_path = config_file if os.path.isabs(config_file) else os.path.join(root_dir, config_file)
    try:
        with open(target_path, "w", encoding="utf-8") as f:
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
    root_dir = os.path.dirname(base_dir)

    now_alg = datetime.now(ALG_TZ)
    today_alg = now_alg.date()
    today_str = today_alg.strftime("%Y-%m-%d")

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

    # 1. Fast Check against persistent messages_memory.json (catches announcements from previous days)
    for mem_candidate in [os.path.join(root_dir, "messages_memory.json"), os.path.join(base_dir, "messages_memory.json")]:
        if os.path.exists(mem_candidate):
            try:
                with open(mem_candidate, "r", encoding="utf-8-sig") as f:
                    mem_data = json.load(f)
                for mem_msg in mem_data.get("messages", []):
                    m_subj = mem_msg.get("subject")
                    m_target_d = mem_msg.get("target_date")
                    m_cat = mem_msg.get("category")
                    raw_lower = (mem_msg.get("raw_text") or "").lower()

                    if canonical and m_subj and (canonical.lower() in m_subj.lower() or m_subj.lower() in canonical.lower()):
                        if m_target_d == today_str or (not m_target_d and mem_msg.get("date_alg", "").startswith(today_str)):
                            # Check micro-interro during TD
                            is_exam_kw = any(k in raw_lower for k in ["micro intero", "micro-intero", "micro interro", "micro-interro", "micro-interrogation", "micro interrogation", "intero", "interro", "exam", "quiz", "fard", "imti7an"])
                            is_td_target = is_td and (mem_msg.get("session_type") == "TD" or "td" in raw_lower or "seance de td" in raw_lower or "séance de td" in raw_lower or "pendant la seance de td" in raw_lower)
                            if (m_cat == "exam" or is_exam_kw) and is_td_target:
                                print(f"[{now_alg.strftime('%H:%M:%S')}] [PRE-FLIGHT] 🚫 Detected scheduled micro-interrogation during TD for {m_name} from memory #{mem_msg.get('id')}.")
                                return {"action": "CANCEL", "reason": f"Micro-interrogation during TD: {mem_msg.get('raw_text', '')[:100]}"}
            except Exception as e:
                print(f"[PRE-FLIGHT] Memory pre-check warning: {e}")
            break

    session_src = os.path.join(base_dir, f"{SESSION_NAME}.session")
    if not os.path.exists(session_src):
        session_src = os.path.join(root_dir, f"{SESSION_NAME}.session")
    if not os.path.exists(session_src):
        print(f"[PRE-FLIGHT] Notice: {SESSION_NAME}.session not found, skipping Telegram check.")
        return {"action": "PROCEED"}

    # Use unique temp session file copy in scripts/ to avoid SQLite lock contention with live watcher
    temp_session_name = f"preflight_temp_{os.getpid()}"
    temp_session_base = os.path.join(base_dir, temp_session_name)
    temp_session_file = f"{temp_session_base}.session"
    try:
        shutil.copy(session_src, temp_session_file)
    except Exception as e:
        print(f"[PRE-FLIGHT] Notice: could not copy session ({e}), proceeding.")
        return {"action": "PROCEED"}

    from telethon.sync import TelegramClient

    now_alg = datetime.now(ALG_TZ)
    today_alg = now_alg.date()

    all_messages = []
    client = TelegramClient(temp_session_base, API_ID, API_HASH)
    try:
        # Use connect() rather than `with client:` so an unauthorized session never blocks on stdin input().
        client.connect()
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
        try:
            client.disconnect()
        except Exception:
            pass
        for stale_file in [temp_session_file, os.path.join(root_dir, f"{temp_session_name}.session")]:
            try:
                if os.path.exists(stale_file):
                    os.remove(stale_file)
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

        # Check cancellation (guard against future dates like next week / 15/11)
        is_future_date = any(w in txt_lower for w in ["semaine prochaine", "next week", "demain", "ghodwa"])
        date_match = re.search(r'\(?(\d{1,2})[/.-](\d{1,2})\)?', txt_lower)
        if date_match:
            d, m = int(date_match.group(1)), int(date_match.group(2))
            if d != today_alg.day or m != today_alg.month:
                is_future_date = True

        is_cancel = any(kw in txt_lower for kw in CANCEL_KEYWORDS) and not is_future_date
        # Check for Micro-Interrogation / Exam during TD (User rule: if micro-interro during TD, skip TD)
        is_exam_td = is_td and any(k in txt_lower for k in [
            "micro-interrogation", "micro interrogation", "micro interogation", "micro-interogation",
            "micro-interro", "micro interro", "micro-intero", "micro intero", "interrogation",
            "interro", "intero", "examen", "controle", "control", "quiz", "test", "fard", "imti7an"
        ]) and not is_future_date

        if is_exam_td:
            cancelled = True
            cancel_reason = f"Micro-interrogation scheduled during TD: {text}"
            cancel_scope = "single"
        elif is_cancel:
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
