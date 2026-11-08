# -*- coding: utf-8 -*-
"""
Telegram Message Memory & Multilingual Semantic Engine
Preserves full Arabic (Darija / MSA) and French text integrity with date tracking,
automatic validity lifecycle management, and anti-reversal console formatting.
"""

import os
import sys
import json
import re
from datetime import datetime, timezone, timedelta, date

# ── Safe Encoding Setup ────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(BASE_DIR, "messages_memory.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
TASKS_FILE = os.path.join(BASE_DIR, "dashboard", "tasks_data.json")

ALG_TZ = timezone(timedelta(hours=1))

# ── Multilingual Dictionaries ─────────────────────────────────────────────────

# Common Algerian Darija / Arabic student vocabulary with transliteration and English meaning
DARIJA_TERMS = {
    "بديتو": {"translit": "bditou", "meaning": "Did you start?"},
    "بدات": {"translit": "bdat", "meaning": "Did it start?"},
    "وقتاش": {"translit": "waktach", "meaning": "When?"},
    "وقتاه": {"translit": "waktah", "meaning": "When?"},
    "شكون": {"translit": "chkoun", "meaning": "Who?"},
    "كاش": {"translit": "kach", "meaning": "Any / Is there?"},
    "ماكانش": {"translit": "makanesh", "meaning": "There is no / Cancelled"},
    "مكاش": {"translit": "makach", "meaning": "There is no / Cancelled"},
    "ماتقراوش": {"translit": "mat9rawch", "meaning": "No class / Do not study"},
    "ماتقراوش td": {"translit": "mat9rawch TD", "meaning": "No TD class"},
    "ماتقراوش cours": {"translit": "mat9rawch Cours", "meaning": "No lecture"},
    "كملتو": {"translit": "kmltou", "meaning": "Did you finish?"},
    "غدوة": {"translit": "ghodwa", "meaning": "Tomorrow"},
    "اليوم": {"translit": "lyoum", "meaning": "Today"},
    "السمانة الجاية": {"translit": "semana jaya", "meaning": "Next week"},
    "مريضة": {"translit": "mrida", "meaning": "Sick (female)"},
    "مريض": {"translit": "mrid", "meaning": "Sick (male)"},
    "كونكسيون": {"translit": "cnx", "meaning": "Internet connection"},
    "رابط": {"translit": "rabit", "meaning": "Link"},
    "ليان": {"translit": "lien", "meaning": "Link"},
    "امتحان": {"translit": "imti7an", "meaning": "Exam"},
    "فرض": {"translit": "fard", "meaning": "Quiz / Micro-interro"}
}

# Bilingual Subject Aliases (French, Arabic, Darija, English)
SUBJECT_ALIASES = {
    "Control Systems 2": [
        "systeme asservi", "systemes asservis", "asservi", "sa2", "control systems",
        "gherbi", "سيستام اسارفي", "أنظمة التحكم", "غيربي"
    ],
    "Robotique": [
        "robotique", "robot", "ramdani", "روبوتيك", "رمداني"
    ],
    "Electrical Machines": [
        "machines electriques", "machine electrique", "machines et drives",
        "electriques", "chelli", "ماكينات كهربائية", "ماشين الكتريك", "شلي"
    ],
    "Power Electronics": [
        "electronique de puissance", "puissance", "mokhnache", "الكترونيك دو بويسونس", "مخناش"
    ],
    "Embedded Computer 1": [
        "calculateur embarque", "calculateur", "embedded computer", "embedded",
        "embarque", "merabti", "كمبيوتر مدمج", "مرابطي"
    ],
    "Industrial Sensors and Instrumentation": [
        "capteurs et instrumentation industriels", "capteurs et instrumentation",
        "capteurs instrumentation", "capteurs industriels", "capteur", "capteurs",
        "instrumentation", "hais", "hafs", "كابتور", "حفص", "حايس"
    ],
    "Innovation Management": [
        "management de l'innovation", "management de l innovation", "management de linnovation",
        "management de l’innovation", "management innovation", "management", "innovation",
        "babes", "ماناجمونت", "إدارة الابتكار", "بابس"
    ],
    "Industrial Programmable Automation": [
        "automates programmables industriels", "automates programmables", "automate programmable",
        "api", "righi", "أوتومات", "ريغي"
    ]
}

DAY_MAP = {
    "monday": 0, "lundi": 0, "lethnayn": 0, "lethnin": 0, "الإثنين": 0, "الاثنين": 0,
    "tuesday": 1, "mardi": 1, "thalatha": 1, "thalata": 1, "الثلاثاء": 1,
    "wednesday": 2, "mercredi": 2, "larba": 2, "larbaa": 2, "الأربعاء": 2, "الاربعاء": 2,
    "thursday": 3, "jeudi": 3, "lkhamis": 3, "khamis": 3, "الخميس": 3,
    "friday": 4, "vendredi": 4, "ljemaa": 4, "jemaa": 4, "الجمعة": 4,
    "saturday": 5, "samedi": 5, "sebt": 5, "السبت": 5,
    "sunday": 6, "dimanche": 6, "lhad": 6, "had": 6, "الأحد": 6, "الاحد": 6
}


def normalize_text_for_search(text):
    """Normalize accents, typographic quotes, and casing for keyword matching."""
    if not text:
        return ""
    t = text.lower()
    t = t.replace("’", "'").replace("`", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r'[*_#~`]', ' ', t)
    for a, b in [("é","e"),("è","e"),("ê","e"),("à","a"),("â","a"),
                 ("ù","u"),("û","u"),("î","i"),("ô","o"),("ç","c")]:
        t = t.replace(a, b)
    return t


def contains_arabic(text):
    """Check if text contains Arabic Unicode characters."""
    return bool(re.search(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]', text or ""))


def format_text_for_display(text):
    """
    Format text for display in terminal/logs without mutating underlying memory.
    If Arabic text is present, appends Darija transliteration and English meaning
    so even terminals with broken RTL/shaping render completely clear information.
    """
    if not text:
        return ""
    
    # Check for known Arabic terms to append readable transliteration
    annotations = []
    for ar_term, meta in DARIJA_TERMS.items():
        if ar_term in text:
            annotations.append(f"{meta['translit']} ({meta['meaning']})")
    
    if annotations:
        return f"{text} [Darija/Arabic: {', '.join(annotations)}]"
    return text


def extract_explicit_or_relative_date(text, msg_date):
    """
    Robustly extract the target date of an announcement.
    Anchors to msg_date (timezone-aware UTC or Alg).
    Supports formats:
      - (15/11), 15/11, 15/11/2026, 15-11
      - 'aujourd'hui', 'lyoum', 'today' -> msg_date
      - 'demain', 'ghodwa', 'tomorrow' -> msg_date + 1
      - 'la semaine prochaine (15/11)' -> 2026-11-15
      - 'la semaine prochaine' -> msg_date + 7
      - Named days (dimanche, lundi, etc.)
    """
    if not text or not msg_date:
        return None

    msg_local = msg_date.astimezone(ALG_TZ) if hasattr(msg_date, "astimezone") else msg_date
    msg_day = msg_local.date() if isinstance(msg_local, datetime) else msg_local
    norm = normalize_text_for_search(text)

    # 1. Explicit calendar date: DD/MM or DD/MM/YYYY
    date_match = re.search(r'\(?(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\)?', text)
    if date_match:
        try:
            d = int(date_match.group(1))
            m = int(date_match.group(2))
            y_raw = date_match.group(3)
            y = int(y_raw) if y_raw else msg_day.year
            if y < 100:
                y += 2000
            # Validate valid calendar date
            res = date(y, m, d)
            return res
        except Exception:
            pass

    # 2. Relative keywords
    if any(k in norm for k in ["aujourd'hui", "aujourd hui", "lyoum", "today", "ce jour", "ce matin", "cet apres-midi"]):
        return msg_day

    if any(k in norm for k in ["demain", "ghodwa", "ghda", "tomorrow"]):
        return msg_day + timedelta(days=1)

    if any(k in norm for k in ["apres-demain", "apres demain", "ba3d ghodwa"]):
        return msg_day + timedelta(days=2)

    # 3. Next week (la semaine prochaine / usbu3 jaya)
    if any(k in norm for k in ["semaine prochaine", "next week", "usbu3"]):
        # Check if a weekday was specified with it (e.g. "dimanche prochain")
        for day_name, w_idx in DAY_MAP.items():
            if day_name in norm:
                days_ahead = (w_idx - msg_day.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                return msg_day + timedelta(days=days_ahead)
        return msg_day + timedelta(days=7)

    # 4. Standalone weekday
    for day_name, w_idx in DAY_MAP.items():
        if day_name in norm:
            days_ahead = (w_idx - msg_day.weekday()) % 7
            return msg_day + timedelta(days=days_ahead)

    return None


def extract_target_time(text):
    """Extract explicit class or exam time, e.g. 'à 9:30', '11:00', '13h', '14h30'."""
    if not text:
        return None
    time_match = re.search(r'(?:à|a|at|3la|sur)\s*([0-1]?[0-9]|2[0-3])(?:h|:|\s*h\s*)([0-5][0-9])?', text.lower())
    if time_match:
        hr = int(time_match.group(1))
        mn = int(time_match.group(2)) if time_match.group(2) else 0
        return f"{hr:02d}:{mn:02d}"
    
    # Fallback to direct time pattern: 09:30 or 14:00
    direct = re.search(r'\b([0-1]?[0-9]|2[0-3]):([0-5][0-9])\b', text)
    if direct:
        return f"{int(direct.group(1)):02d}:{int(direct.group(2)):02d}"
    return None


def match_canonical_subject(text):
    """Match subject from text using bilingual and alias lookup."""
    norm = normalize_text_for_search(text)
    best_subject = None
    best_len = 0

    for canonical, aliases in SUBJECT_ALIASES.items():
        for alias in aliases:
            a_norm = normalize_text_for_search(alias)
            if a_norm in norm:
                if len(a_norm) > best_len:
                    best_subject = canonical
                    best_len = len(a_norm)

    return best_subject


def classify_message_semantics(text, topic_id=None):
    """
    Deep semantic classification:
    - cancellation: class/session cancelled or prof sick/unavailable
    - reschedule: moved to different date/time or make-up session
    - exam: micro-interrogation, control, quiz, devoir
    - meet_link: Google Meet URL announcement
    - question: student asking a question (e.g. bditou td?)
    - announcement: general notice
    """
    if not text:
        return "announcement"

    norm = normalize_text_for_search(text)

    # 1. Meet Link
    if "meet.google.com" in text:
        return "meet_link"

    # 2. Exam / Interrogation / Devoir
    exam_keywords = [
        "micro-interrogation", "micro interrogation", "interrogation", "interro",
        "examen", "controle", "control", "ctrl", "devoir", "test", "quiz",
        "qcm", "evaluation", "imti7an", "fard", "micro-interro"
    ]
    if any(k in norm for k in exam_keywords):
        return "exam"

    # 3. Cancellation
    cancel_keywords = [
        "pas cours", "pas de cours", "pas de seance", "pas de td", "pas de tp",
        "annule", "annulee", "annulation", "supprime", "supprimee", "reporte",
        "professeur est malade", "professeur e est malade", "professeure est malade",
        "prof malade", "malade", "n'y aura pas", "aura pas cours", "aucun cours",
        "mat9rawch", "matqrawch", "makanesh", "makach", "makanx", "machi lyoum",
        "makanch", "m3ndhach cnx", "m3andhach cnx", "probleme de connexion"
    ]
    if any(k in norm for k in cancel_keywords):
        return "cancellation"

    # 4. Reschedule / Rattrapage
    reschedule_keywords = [
        "decale", "decalee", "deplacement", "changement", "rattrapage",
        "reprogramme", "bedlo", "badlo", "tbadal", "tbadel", "y3awdhalna",
        "n3awdoha", "khayro nhar", "t3awd la seance"
    ]
    if any(k in norm for k in reschedule_keywords):
        return "reschedule"

    # 5. Question / Discussion
    if "?" in text or any(k in norm for k in ["bditou", "bdito", "chkoun", "wach", "est-ce", "est ce", "waktach", "waqtach"]):
        return "question"

    return "announcement"


# ── Message Memory Manager ─────────────────────────────────────────────────────

class MessageMemoryManager:
    """
    Thread-safe, robust message memory manager.
    Stores and tracks Telegram messages, extracts structured intent,
    evaluates validity, and provides query interfaces for bot and dashboard.
    """
    def __init__(self, memory_file=MEMORY_FILE):
        self.memory_file = memory_file
        self.messages = {}
        self.load()

    def load(self):
        """Load messages from disk safely."""
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "messages" in data:
                    self.messages = {str(m["id"]): m for m in data["messages"]}
                elif isinstance(data, list):
                    self.messages = {str(m["id"]): m for m in data}
            except Exception as e:
                print(f"[MEMORY LOAD ERROR] {e}")

    def save(self):
        """Save messages atomically with 100% Arabic Unicode fidelity (ensure_ascii=False)."""
        try:
            sorted_msgs = sorted(self.messages.values(), key=lambda x: x.get("date", ""), reverse=True)
            payload = {
                "version": "1.0",
                "last_updated": datetime.now(ALG_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "total_messages": len(sorted_msgs),
                "messages": sorted_msgs
            }
            tmp_path = self.memory_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            if os.path.exists(tmp_path):
                # Atomic replace
                if os.path.exists(self.memory_file):
                    os.replace(tmp_path, self.memory_file)
                else:
                    os.rename(tmp_path, self.memory_file)
        except Exception as e:
            print(f"[MEMORY SAVE ERROR] {e}")

    def record_message(self, msg_id, msg_date, topic_id, raw_text, sender_id=None):
        """
        Ingest a message into memory.
        Extracts subject, dates, times, categories, and assigns initial validity.
        """
        if not raw_text or not msg_id:
            return None

        # Clean string key
        str_id = str(msg_id)

        # Parse message date
        if isinstance(msg_date, str):
            try:
                dt_obj = datetime.fromisoformat(msg_date.replace("Z", "+00:00"))
            except Exception:
                dt_obj = datetime.now(timezone.utc)
        elif isinstance(msg_date, datetime):
            dt_obj = msg_date
        else:
            dt_obj = datetime.now(timezone.utc)

        dt_alg = dt_obj.astimezone(ALG_TZ) if hasattr(dt_obj, "astimezone") else dt_obj
        date_iso = dt_obj.isoformat()
        date_alg_str = dt_alg.strftime("%Y-%m-%d %H:%M:%S")

        category = classify_message_semantics(raw_text, topic_id)
        subject = match_canonical_subject(raw_text)
        target_date_obj = extract_explicit_or_relative_date(raw_text, dt_obj)
        target_date_str = target_date_obj.strftime("%Y-%m-%d") if target_date_obj else None
        target_time_str = extract_target_time(raw_text)

        # Discriminate TD vs Cours if applicable
        sub_type = None
        lower_raw = raw_text.lower()
        if "(td)" in lower_raw or " td" in lower_raw or "td " in lower_raw:
            sub_type = "TD"
        elif "(c)" in lower_raw or " cours" in lower_raw or "cour " in lower_raw:
            sub_type = "Cours"

        # Determine meet url
        url_match = re.search(r'https?://meet\.google\.com/[a-z0-9\-]+', raw_text)
        meet_url = url_match.group(0) if url_match else None

        # Determine validity
        today_alg = datetime.now(ALG_TZ).date()
        is_valid = True
        status = "active"
        validity_note = "Active announcement"

        if target_date_obj and target_date_obj < today_alg:
            is_valid = False
            status = "expired"
            validity_note = f"Event date ({target_date_str}) has passed"
        elif category == "question":
            validity_note = "Student discussion query"
        elif category == "cancellation":
            validity_note = f"Cancellation active for {target_date_str or 'today'}"
        elif category == "exam":
            validity_note = f"Scheduled exam on {target_date_str or 'upcoming'}"

        msg_entry = {
            "id": int(msg_id) if str(msg_id).isdigit() else msg_id,
            "date": date_iso,
            "date_alg": date_alg_str,
            "topic_id": topic_id,
            "topic_name": "Important" if topic_id == 989 else ("Meet Links" if topic_id == 993 else ("Discussion" if topic_id == 987 else f"Topic {topic_id}")),
            "raw_text": raw_text.strip(),
            "display_text": format_text_for_display(raw_text.strip()),
            "category": category,
            "subject": subject,
            "session_type": sub_type,
            "target_date": target_date_str,
            "target_time": target_time_str,
            "meet_url": meet_url,
            "has_arabic": contains_arabic(raw_text),
            "is_valid": is_valid,
            "status": status,
            "validity_note": validity_note,
            "superseded_by": None
        }

        # Check if this new message supersedes any existing messages in memory
        self._check_and_supersede(msg_entry)

        self.messages[str_id] = msg_entry
        self.save()

        # Cross-sync to config.json and dashboard tasks_data.json
        self._sync_actions_to_system(msg_entry)

        return msg_entry

    def _check_and_supersede(self, new_entry):
        """Supersede older conflicting announcements for the same subject and target date."""
        if not new_entry.get("subject") or not new_entry.get("target_date"):
            return

        for mid, old_entry in self.messages.items():
            if old_entry.get("id") == new_entry.get("id"):
                continue
            if (old_entry.get("subject") == new_entry.get("subject") and
                old_entry.get("target_date") == new_entry.get("target_date")):
                # If newer message cancels or reschedules, old announcement is superseded
                if new_entry.get("category") in ("cancellation", "reschedule"):
                    old_entry["is_valid"] = False
                    old_entry["status"] = "superseded"
                    old_entry["superseded_by"] = new_entry.get("id")
                    old_entry["validity_note"] = f"Superseded by message #{new_entry.get('id')}"

    def _sync_actions_to_system(self, entry):
        """Automatically propagate cancellations, exams, and links to config and tasks."""
        category = entry.get("category")
        subject = entry.get("subject")
        target_date = entry.get("target_date")
        target_time = entry.get("target_time")
        raw_text = entry.get("raw_text", "")

        # 1. Propagate CANCELLATIONS to config.json
        if category == "cancellation" and subject and target_date:
            self._apply_cancellation_to_config(subject, target_date, entry.get("session_type"))
            self._sync_task(
                title=f"🚫 Cancelled: {subject}",
                description=f"Class cancelled for {target_date}. Reason: {raw_text[:120]}",
                category=subject,
                due_date=target_date,
                priority="high" if target_date == datetime.now(ALG_TZ).strftime("%Y-%m-%d") else "medium"
            )

        # 2. Propagate EXAMS / QUIZZES to dashboard tasks_data.json
        elif category == "exam" and subject:
            due = target_date or datetime.now(ALG_TZ).strftime("%Y-%m-%d")
            time_part = f" at {target_time}" if target_time else ""
            self._sync_task(
                title=f"📝 Micro-Interrogation: {subject}",
                description=f"{raw_text[:160]}{time_part}",
                category=subject,
                due_date=due,
                priority="high"
            )

        # 3. Propagate MEET URLS to config.json
        elif category == "meet_link" and subject and entry.get("meet_url"):
            self._apply_meet_url_to_config(subject, entry.get("meet_url"), entry.get("session_type"))

    def _apply_cancellation_to_config(self, subject, date_str, session_type=None):
        """Safely append cancellation date to config.json."""
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            changed = False
            for m in cfg.get("meetings", []):
                name = m.get("name", "")
                if subject.lower() in name.lower() or name.lower() in subject.lower():
                    # Discriminate TD vs C if indicated
                    if session_type == "TD" and "(c)" in name.lower():
                        continue
                    if session_type == "Cours" and "(td" in name.lower():
                        continue
                    cd = m.setdefault("cancelled_dates", [])
                    if date_str not in cd:
                        cd.append(date_str)
                        changed = True
                        print(f"[MEMORY SYNC] 🚫 Added {date_str} to cancelled_dates for {name}")
            if changed:
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[CONFIG CANCEL SYNC ERROR] {e}")

    def _apply_meet_url_to_config(self, subject, meet_url, session_type=None):
        """Update meeting URL in config.json."""
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            changed = False
            for m in cfg.get("meetings", []):
                name = m.get("name", "")
                if subject.lower() in name.lower() or name.lower() in subject.lower():
                    if session_type == "TD" and "(c)" in name.lower():
                        continue
                    if session_type == "Cours" and "(td" in name.lower():
                        continue
                    if m.get("url") != meet_url:
                        m["url"] = meet_url
                        changed = True
                        print(f"[MEMORY SYNC] 🔗 Updated URL for {name} -> {meet_url}")
            if changed:
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[CONFIG URL SYNC ERROR] {e}")

    def _sync_task(self, title, description, category, due_date, priority="medium"):
        """Sync announcement or exam as a task in tasks_data.json."""
        if not os.path.exists(TASKS_FILE):
            return
        try:
            with open(TASKS_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            tasks = data.setdefault("tasks", [])
            for t in tasks:
                if t.get("title", "").strip().lower() == title.strip().lower() and t.get("due_date") == due_date:
                    return
            task_id = f"task_memo_{int(datetime.now().timestamp())}_{abs(hash(title)) % 1000}"
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
                "source": "telegram_message_memory"
            })
            with open(TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"[MEMORY SYNC] 📋 Added task '{title}' (Due: {due_date}) to dashboard tasks_data.json")
        except Exception as e:
            print(f"[TASK SYNC ERROR] {e}")

    def is_session_cancelled_on_date(self, subject_name, check_date_str):
        """
        Check if an active, valid cancellation applies to this session on check_date_str.
        Returns (is_cancelled, reason).
        """
        norm_subj = normalize_text_for_search(subject_name)
        for m in self.messages.values():
            if not m.get("is_valid"):
                continue
            if m.get("category") != "cancellation":
                continue
            if m.get("target_date") == check_date_str:
                m_sub = normalize_text_for_search(m.get("subject", ""))
                if m_sub and (m_sub in norm_subj or norm_subj in m_sub):
                    # Check TD vs Cours match
                    stype = m.get("session_type")
                    if stype == "TD" and "(c)" in norm_subj:
                        continue
                    if stype == "Cours" and "(td" in norm_subj:
                        continue
                    return True, m.get("raw_text", "")
        return False, None

    def get_upcoming_announcements(self, from_date_str=None):
        """Get all currently valid, active announcements and exams."""
        if not from_date_str:
            from_date_str = datetime.now(ALG_TZ).strftime("%Y-%m-%d")
        results = []
        for m in self.messages.values():
            if m.get("is_valid") and m.get("category") in ("exam", "cancellation", "reschedule"):
                t_date = m.get("target_date")
                if not t_date or t_date >= from_date_str:
                    results.append(m)
        results.sort(key=lambda x: x.get("target_date") or x.get("date_alg", ""))
        return results

    def get_all(self):
        """Return all messages sorted by date descending."""
        return sorted(self.messages.values(), key=lambda x: x.get("date", ""), reverse=True)


# Global singleton instance
memory_manager = MessageMemoryManager()
