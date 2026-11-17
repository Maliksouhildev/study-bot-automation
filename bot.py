import os
import sys
import re
import json
import time
import random

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
import warnings
warnings.filterwarnings("ignore")
import schedule
import requests
import subprocess
import threading
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

# ── Config helpers ──────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"

def load_config(retries=10):
    """Load config.json at runtime, handling BOM, empty-file and mid-write edge cases."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(base_dir, CONFIG_FILE)
    if not os.path.exists(cfg_path):
        ex_path = os.path.join(base_dir, "config.example.json")
        if os.path.exists(ex_path):
            try:
                import shutil
                shutil.copy(ex_path, cfg_path)
                print("[SETUP] Initialized config.json from config.example.json")
            except Exception:
                pass

    last_err = None
    for _ in range(retries):
        try:
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                raw = f.read().strip()
            if not raw:
                raise ValueError("config.json is empty")
            return json.loads(raw)
        except Exception as e:
            last_err = e
            time.sleep(0.5)  # watcher may be mid-write
    print(f"[ERROR] Could not load {CONFIG_FILE}: {last_err}")
    sys.exit(1)

config = load_config()
import soundcard as sc
import soundfile as sf
import numpy as np

class DualAudioRecorder:
    """
    Bulletproof audio recorder:
    1. Captures system loopback (what you hear: the professor, meeting participants, video playback)
    2. Captures microphone (the student: auto-detects Saramonic SR-MV2000 if plugged in, or any active mic)
    3. Seamlessly mixes both streams into one clear track.
    """
    def __init__(self, output_wav, samplerate=16000):
        self.output_wav = output_wav
        self.samplerate = samplerate
        self.stop_event = threading.Event()
        self.lb_frames = []
        self.mic_frames = []
        
        # 1. System Loopback (Professor / Meeting)
        self.loopback = None
        try:
            spk = sc.default_speaker()
            self.loopback = sc.get_microphone(id=str(spk.name), include_loopback=True)
            print(f"[Audio] Loopback initialized on: {self.loopback.name}")
        except Exception as e:
            print(f"[Audio] Warning: Could not init loopback: {e}")

        # 2. Microphone (Student voice)
        self.mic = None
        try:
            mics = sc.all_microphones()
            # Prioritize Saramonic SR-MV2000 if plugged in
            for m in mics:
                if "mv2000" in m.name.lower() or "saramonic" in m.name.lower():
                    self.mic = m
                    break
            # Otherwise pick any physical mic (avoid Virtual Mic if another exists)
            if not self.mic:
                for m in mics:
                    if "virtual" not in m.name.lower():
                        self.mic = m
                        break
            if not self.mic:
                self.mic = sc.default_microphone()
            print(f"[Audio] Microphone initialized on: {self.mic.name if self.mic else 'None'}")
        except Exception as e:
            print(f"[Audio] Warning: Could not init mic: {e}")

    def _record_loopback(self):
        if not self.loopback:
            return
        try:
            with self.loopback.recorder(samplerate=self.samplerate, channels=2) as r:
                while not self.stop_event.is_set():
                    data = r.record(numframes=int(self.samplerate * 0.2))
                    self.lb_frames.append(data)
        except Exception as e:
            print(f"[Audio] Loopback capture finished: {e}")

    def _record_mic(self):
        if not self.mic:
            return
        try:
            with self.mic.recorder(samplerate=self.samplerate, channels=2) as r:
                while not self.stop_event.is_set():
                    data = r.record(numframes=int(self.samplerate * 0.2))
                    self.mic_frames.append(data)
        except Exception as e:
            print(f"[Audio] Mic capture finished: {e}")

    def start(self):
        self.t_lb = threading.Thread(target=self._record_loopback, daemon=True)
        self.t_mic = threading.Thread(target=self._record_mic, daemon=True)
        self.t_lb.start()
        self.t_mic.start()
        print("[Audio] Dual audio recording started.")

    def stop(self):
        self.stop_event.set()
        self.t_lb.join(timeout=3)
        self.t_mic.join(timeout=3)
        
        lb_audio = np.concatenate(self.lb_frames, axis=0) if self.lb_frames else None
        mic_audio = np.concatenate(self.mic_frames, axis=0) if self.mic_frames else None

        if lb_audio is not None and mic_audio is not None:
            min_len = min(len(lb_audio), len(mic_audio))
            mixed = lb_audio[:min_len] + mic_audio[:min_len]
        elif lb_audio is not None:
            mixed = lb_audio
        elif mic_audio is not None:
            mixed = mic_audio
        else:
            mixed = np.zeros((self.samplerate, 2), dtype=np.float32)

        # Audio Normalization: ensure speech is loud and clear for Whisper
        peak = np.max(np.abs(mixed))
        if peak > 0.005:
            mixed = (mixed / peak) * 0.95
        final_audio = np.clip(mixed, -1.0, 1.0)

        sf.write(self.output_wav, final_audio, self.samplerate)
        print(f"[Audio] Saved {len(final_audio)/self.samplerate:.1f}s of normalized audio to {self.output_wav}")

# Scheduling globals (DAYS, _launched, _launch_lock, _class_window) defined above join_meeting

def extract_meet_code(url):
    """Extract standard 10-char Google Meet room code (e.g. joq-kgbf-mki)."""
    if not url:
        return ""
    m = re.search(r'([a-z0-9]{3}-[a-z0-9]{4}-[a-z0-9]{3})', url.lower())
    if m:
        return m.group(1)
    return url.split("/")[-1].split("?")[0].strip().lower()

def clean_prof_name(prof_str):
    """Normalize professor name by stripping honorifics and titles."""
    if not prof_str:
        return ""
    cleaned = re.sub(r'^(pr|dr|prof|mme|mr|m)\.?\s+', '', prof_str.strip(), flags=re.IGNORECASE)
    return cleaned.strip().lower()

def professor_matches(prof_config_str, participant_name):
    """Fuzzy check if participant name in Meet DOM corresponds to professor."""
    if not prof_config_str or not participant_name:
        return False
    cfg_clean = clean_prof_name(prof_config_str)
    part_clean = participant_name.strip().lower()
    if not cfg_clean:
        return False
    if cfg_clean in part_clean:
        return True
    parts = [p for p in re.split(r'[\s\-_]+', cfg_clean) if len(p) >= 3]
    return any(p in part_clean for p in parts)

def find_consecutive_meeting(current_meeting, config, ref_datetime=None):
    """
    Check if another scheduled class today immediately follows current_meeting
    with the same professor, same Meet link, or same subject base name.
    """
    ref_datetime = ref_datetime or datetime.now()
    today_name = DAYS[ref_datetime.weekday()]
    today_str = ref_datetime.strftime("%Y-%m-%d")
    curr_start, curr_end = _class_window(current_meeting, ref_datetime)
    curr_code = extract_meet_code(current_meeting.get("url", ""))
    curr_prof = clean_prof_name(current_meeting.get("professor", ""))
    curr_base = re.sub(r'\(.*?\)', '', current_meeting["name"]).strip().lower()

    for m in config.get("meetings", []):
        if m["name"].lower() == current_meeting["name"].lower():
            continue
        if m.get("day_of_week", "").lower() != today_name:
            continue
        if today_str in m.get("cancelled_dates", []):
            continue

        cand_start, cand_end = _class_window(m, ref_datetime)
        time_diff_min = (cand_start - curr_end).total_seconds() / 60.0
        # Contiguous in time: starts within [-15, +25] minutes of current class end
        if not (-15 <= time_diff_min <= 25):
            continue

        cand_code = extract_meet_code(m.get("url", ""))
        cand_prof = clean_prof_name(m.get("professor", ""))
        cand_base = re.sub(r'\(.*?\)', '', m["name"]).strip().lower()

        is_same_code = bool(curr_code and cand_code and curr_code == cand_code)
        is_same_prof = bool(curr_prof and cand_prof and (curr_prof in cand_prof or cand_prof in curr_prof))
        is_same_subject = bool(curr_base and cand_base and curr_base == cand_base)

        if is_same_code or is_same_prof or is_same_subject:
            return m, cand_start, cand_end

    return None

def find_next_different_class(current_meeting, config, ref_datetime=None):
    """
    Find upcoming class scheduled today with a DIFFERENT professor / room code.
    Used to ensure the bot leaves an overtime call in time to switch rooms.
    """
    ref_datetime = ref_datetime or datetime.now()
    today_name = DAYS[ref_datetime.weekday()]
    today_str = ref_datetime.strftime("%Y-%m-%d")
    curr_code = extract_meet_code(current_meeting.get("url", ""))
    curr_prof = clean_prof_name(current_meeting.get("professor", ""))

    upcoming = []
    for m in config.get("meetings", []):
        if m["name"].lower() == current_meeting["name"].lower():
            continue
        if m.get("day_of_week", "").lower() != today_name:
            continue
        if today_str in m.get("cancelled_dates", []):
            continue

        cand_start, cand_end = _class_window(m, ref_datetime)
        if cand_start <= ref_datetime:
            continue

        cand_code = extract_meet_code(m.get("url", ""))
        cand_prof = clean_prof_name(m.get("professor", ""))

        is_same_code = bool(curr_code and cand_code and curr_code == cand_code)
        is_same_prof = bool(curr_prof and cand_prof and (curr_prof in cand_prof or cand_prof in curr_prof))

        if not (is_same_code or is_same_prof):
            upcoming.append((cand_start, m, cand_end))

    if upcoming:
        upcoming.sort(key=lambda x: x[0])
        return upcoming[0][1], upcoming[0][0], upcoming[0][2]
    return None

def _ensure_mic_cam_off(page):
    """Safely ensure mic & camera are turned off without blindly toggling already-off controls."""
    try:
        toggles = page.locator(
            "div[role='button'][aria-label*='micro' i], "
            "div[role='button'][aria-label*='caméra' i], "
            "div[role='button'][aria-label*='camera' i], "
            "button[aria-label*='micro' i], "
            "button[aria-label*='caméra' i], "
            "button[aria-label*='camera' i]"
        )
        for i in range(toggles.count()):
            try:
                btn = toggles.nth(i)
                if not btn.is_visible():
                    continue
                aria = (btn.get_attribute("aria-label") or "").lower()
                if any(w in aria for w in ["turn off", "désactiver", "couper"]):
                    btn.click(timeout=3000)
                    time.sleep(0.3)
            except Exception:
                pass
    except Exception as e:
        print(f"Warning during mic/cam toggle: {e}")


def _check_telegram_chat_signal(since_dt, seen_ids, meeting):
    """
    Poll messages_memory.json (updated in real time by telegram_schedule_watcher.py)
    for live group-chat signals while waiting for the professor.
    Returns (signal_type, message_text) where signal_type is:
      'PROF_ARRIVED' | 'PROF_NOT_YET' | 'CANCELLED' | None
    """
    mem_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages_memory.json")
    if not os.path.exists(mem_path):
        return None, None

    try:
        with open(mem_path, "r", encoding="utf-8-sig") as f:
            mem_data = json.load(f)
    except Exception:
        return None, None

    m_name_lower = meeting.get("name", "").lower()
    prof_lower = clean_prof_name(meeting.get("professor", ""))

    # Quick alias map to ignore messages explicitly talking about a DIFFERENT subject
    other_subject_keywords = {
        "systeme asservi": "control systems",
        "asservi": "control systems",
        "robotique": "robotique",
        "machine": "electrical machines",
        "puissance": "power electronics",
        "mokhnache": "power electronics",
        "calculateur": "embedded computer",
        "embedded": "embedded computer",
        "embarque": "embedded computer",
        "merabti": "embedded computer",
        "capteur": "industrial sensors",
        "hafs": "industrial sensors",
        "management": "innovation management",
        "babes": "innovation management",
        "automate": "industrial programmable automation",
        "righi": "industrial programmable automation",
    }

    arrived_kws = [
        "ja lprof", "ja prof", "aw ja", "rahou ja", "raho ja", "rahu ja",
        "dkhl", "dkhel", "dkhoul", "tdkhlo", "dkholo", "dakhl", "dakhel",
        "kan tdkhlo", "t9dro tdkhlo", "ta9dro tdkhlo", "prof est la", "prof est là",
        "il est la", "il est là", "accepta", "acceptakom", "yaccepti",
        "bdat", "bdina", "bdaw", "dkhlna", "dkhalna", "dkhelna",
        "c bon", "c'est bon", "prof arrived", "prof joined", "prof is here",
        "راهو جا", "جا البروف", "دخل", "تقدرو تدخلو"
    ]
    not_yet_kws = [
        "mazal", "mezel", "mjach", "majach", "ma ja", "madkhlch",
        "ma dkhlch", "ma bda", "mabdach", "pas encore", "not yet",
        "pour moi no", "mazalou"
    ]

    latest_signal = (None, None)
    msgs = sorted(mem_data.get("messages", []), key=lambda x: x.get("date_alg", ""))
    for msg in msgs:
        mid = msg.get("id")
        if mid in seen_ids:
            continue
        date_alg_str = msg.get("date_alg") or ""
        if len(date_alg_str) < 19:
            continue
        try:
            msg_dt = datetime.strptime(date_alg_str[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if msg_dt < since_dt:
            continue

        seen_ids.add(mid)
        raw_text = (msg.get("raw_text") or "").strip()
        txt_lower = raw_text.lower()

        # Skip if explicitly naming a different subject
        mentions_other = False
        for kw, subj_tag in other_subject_keywords.items():
            if kw in txt_lower and subj_tag not in m_name_lower and (not prof_lower or prof_lower not in kw):
                mentions_other = True
                break
        if mentions_other:
            continue

        if msg.get("category") == "cancellation" and msg.get("target_date") == datetime.now().strftime("%Y-%m-%d"):
            return "CANCELLED", raw_text

        is_negated = any(nk in txt_lower for nk in not_yet_kws) or bool(re.search(r'\b(no|nn|non)\b', txt_lower))
        if is_negated:
            latest_signal = ("PROF_NOT_YET", raw_text)
        elif any(ak in txt_lower for ak in arrived_kws):
            latest_signal = ("PROF_ARRIVED", raw_text)

    return latest_signal


def join_meeting(meeting, skip_delay=False, class_end=None, class_start=None, skip_preflight=False):
    current_config = load_config()
    if current_config.get("enabled", True) is not True:
        time.sleep(3)  # watcher may have been mid-write; confirm before skipping
        current_config = load_config()
    if current_config.get("enabled", True) is False:
        print(f"[{datetime.now()}] ⏸️ Study Bot is DISABLED in config. Skipping {meeting['name']}.")
        return

    # Check if a newer URL exists for this meeting in config
    for m in current_config.get("meetings", []):
        if m["name"].lower() == meeting["name"].lower():
            meeting = m
            break

    today_str = datetime.now().strftime("%Y-%m-%d")
    if today_str in meeting.get("cancelled_dates", []):
        print(f"[{datetime.now()}] 🚫 {meeting['name']} is cancelled for today ({today_str}). Skipping.")
        return

    # ── Mandatory Telegram Pre-Flight Verification ──────────────────────────
    if not skip_preflight:
        try:
            from telegram_preflight import run_telegram_preflight_check
            chk = run_telegram_preflight_check(meeting)
            if chk.get("action") == "CANCEL":
                print(f"[{datetime.now()}] 🚫 [TELEGRAM PRE-FLIGHT] Aborting {meeting['name']}: cancelled per Telegram ({chk.get('reason')}).")
                return
            elif chk.get("action") == "RESCHEDULE":
                print(f"[{datetime.now()}] ⏰ [TELEGRAM PRE-FLIGHT] Aborting current join for {meeting['name']}: rescheduled to {chk.get('new_time')}.")
                return
            elif chk.get("url_updated"):
                meeting["url"] = chk.get("url")
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ Telegram pre-flight check exception: {e}")

    print(f"[{datetime.now()}] Starting preparation for {meeting['name']}")
    
    # 4 to 6 minutes humanizer delay
    min_delay = current_config["settings"].get("random_delay_min_seconds", 240)
    max_delay = current_config["settings"].get("random_delay_max_seconds", 360)
    delay = 0 if skip_delay else random.randint(min_delay, max_delay)
    print(f"Waiting {delay} seconds ({delay//60}m {delay%60}s) after class start to join like a real student...")
    time.sleep(delay)
    
    os.makedirs(current_config["settings"]["output_dir"], exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', meeting['name']).strip('_')
    video_filename = os.path.join(current_config["settings"]["output_dir"], f"{clean_name}_{timestamp}.mp4")
    temp_video_filename = video_filename.replace(".mp4", "_temp.mp4")
    temp_audio_wav = video_filename.replace(".mp4", ".wav")
    audio_filename = video_filename.replace(".mp4", ".mp3")
    
    # Ensure thread is attached to user's interactive desktop so Chrome is visible on monitor
    try:
        import ctypes
        u32 = ctypes.windll.user32
        h_default = u32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_default:
            u32.SetThreadDesktop(h_default)
    except Exception as e:
        pass

    base_script_dir = os.path.dirname(os.path.abspath(__file__))
    raw_profile = current_config.get("settings", {}).get("chrome_user_data_dir", "./chrome_bot_profile")
    if not os.path.isabs(raw_profile) or not os.path.exists(os.path.dirname(os.path.abspath(raw_profile))):
        resolved_user_data_dir = os.path.normpath(os.path.join(base_script_dir, "chrome_bot_profile"))
    else:
        resolved_user_data_dir = raw_profile

    print(f"Launching Google Chrome for {meeting['name']} ({meeting['url']})...")
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=resolved_user_data_dir,
            channel="chrome",
            headless=False,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--start-maximized',
                '--window-position=0,0',
                '--use-fake-ui-for-media-stream'
            ],
            no_viewport=True
        )
        
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.bring_to_front()
        page.goto(meeting["url"])
        time.sleep(6)
        
        # 1. Guarantee Mic & Camera are Disabled BEFORE entering
        print("Disabling camera and microphone on pre-join screen...")
        _ensure_mic_cam_off(page)

        # 2. Continuous retry loop on 'Ask to join' until admitted, cancelled, or max_join_wait_minutes (~30m) elapsed
        max_wait_min = current_config.get("settings", {}).get("max_join_wait_minutes", 30)
        base_start = class_start or datetime.now()
        join_deadline = base_start + timedelta(minutes=max_wait_min)
        if datetime.now() + timedelta(minutes=8) > join_deadline:
            join_deadline = datetime.now() + timedelta(minutes=10)
        if class_end:
            join_deadline = min(join_deadline, class_end - timedelta(minutes=5))

        watch_since_dt = base_start - timedelta(minutes=5)
        seen_chat_ids = set()
        admitted = False
        aborted_by_cancel = False
        attempt = 0

        print(f"[{datetime.now()}] 🔄 Will keep requesting to join until admitted or {join_deadline.strftime('%H:%M:%S')} (watching Telegram group chat).")

        while datetime.now() < join_deadline:
            attempt += 1
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] [Attempt {attempt}] Requesting to enter meeting (until {join_deadline.strftime('%H:%M')})...")

            # Check config.json for cancellation, disable, or Meet link update
            updated_cfg = load_config()
            if updated_cfg.get("enabled", True) is False:
                print(f"[{datetime.now()}] ⏸️ Study Bot disabled during wait loop. Stopping.")
                aborted_by_cancel = True
                break

            url_changed = False
            for m in updated_cfg.get("meetings", []):
                if m["name"].lower() == meeting["name"].lower():
                    if today_str in m.get("cancelled_dates", []):
                        print(f"[{datetime.now()}] 🚫 {meeting['name']} marked cancelled in config during wait. Stopping.")
                        aborted_by_cancel = True
                        break
                    if m["url"] != meeting["url"]:
                        print(f"🔄 Link updated via Telegram during wait! Updating target to: {m['url']}")
                        meeting["url"] = m["url"]
                        url_changed = True
                    break
            if aborted_by_cancel:
                break

            # Navigate if URL changed or page left the meeting room (e.g. after Cancel/home redirect)
            target_meet_id = meeting["url"].split("/")[-1].split("?")[0]
            if url_changed or target_meet_id not in (page.url or ""):
                print(f"Navigating to {meeting['url']}...")
                page.goto(meeting["url"])
                time.sleep(5)
                _ensure_mic_cam_off(page)

            # Check for permission popups or notices (Dismiss / Continue without mic / etc.)
            try:
                popups = page.locator("button:has-text('Dismiss'), button:has-text('Ignorer'), button:has-text('Got it'), button:has-text('Compris'), button:has-text('Continue without'), button:has-text('Continuer sans')")
                for pi in range(popups.count()):
                    pbtn = popups.nth(pi)
                    if pbtn.is_visible():
                        pbtn.click()
                        time.sleep(0.5)
            except Exception:
                pass

            # Check if Guest Name field exists and fill it
            try:
                name_inputs = page.locator("input[placeholder*='name' i], input[placeholder*='nom' i], input[aria-label*='name' i], input[aria-label*='nom' i]")
                for ni in range(name_inputs.count()):
                    n_inp = name_inputs.nth(ni)
                    if n_inp.is_visible():
                        val = n_inp.input_value()
                        if not val or val.strip() == "":
                            print("Filling participant name: Malik Mehdid...")
                            n_inp.fill("Malik Mehdid")
                            time.sleep(1)
                            n_inp.press("Enter")
                            time.sleep(1)
            except Exception as e:
                print(f"Name input check exception: {e}")

            try:
                page.screenshot(path="current_screen.png")
            except Exception:
                pass

            join_buttons = page.locator("button:has-text('Ask to join'), button:has-text('Demander à participer'), button:has-text('Join now'), button:has-text('Participer'), button:has-text('Passer ici'), button:has-text('Switch here'), button[aria-label*='Ask to join' i], button[aria-label*='Demander à participer' i]")
            if join_buttons.count() > 0:
                clicked = False
                for j_idx in range(join_buttons.count()):
                    btn = join_buttons.nth(j_idx)
                    try:
                        if btn.is_visible():
                            btn.click(timeout=6000)
                            print("Clicked 'Ask to join' / 'Join now'.")
                            clicked = True
                            break
                    except Exception:
                        try:
                            btn.click(force=True, timeout=3000)
                            print("Force-clicked 'Ask to join' / 'Join now'.")
                            clicked = True
                            break
                        except Exception:
                            try:
                                page.evaluate("el => el.click()", btn.element_handle())
                                print("JS clicked 'Ask to join' / 'Join now'.")
                                clicked = True
                                break
                            except Exception:
                                pass
                if not clicked:
                    print("Could not click join button.")

            # Poll up to 75 seconds for professor to admit (also watching Telegram group chat every 3s)
            check_start = time.time()
            prof_arrived_wake = False
            while time.time() - check_start < 75 and datetime.now() < join_deadline:
                # If leave call button exists, we are IN!
                if page.locator("button[aria-label*='Leave call'], button[aria-label*='Quitter l’appel'], button[aria-label*='Quitter']").count() > 0:
                    admitted = True
                    break
                if page.locator("text='You can\\'t join this call', text='Votre demande a été refusée', text='No one responded'").count() > 0:
                    print("Join request refused or timed out on Meet side.")
                    break

                sig, sig_txt = _check_telegram_chat_signal(watch_since_dt, seen_chat_ids, meeting)
                if sig == "CANCELLED":
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚫 [TELEGRAM SIGNAL] Class cancelled in chat: {sig_txt[:100]}")
                    aborted_by_cancel = True
                    break
                elif sig == "PROF_NOT_YET":
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 💬 [TELEGRAM CHAT] Prof not here yet: \"{sig_txt[:90]}\" — continuing loop.")
                elif sig == "PROF_ARRIVED":
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 [TELEGRAM CHAT] Prof arrived / class started: \"{sig_txt[:90]}\"!")
                    if time.time() - check_start >= 12:
                        print("Refreshing 'Ask to join' request immediately so host sees fresh knock...")
                        prof_arrived_wake = True
                        break

                time.sleep(3)

            if admitted or aborted_by_cancel:
                if admitted:
                    print("🎉 Successfully admitted into the meeting!")
                break

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Attempt {attempt} not admitted yet.")
            cancel_btn = page.locator("button:has-text('Cancel'), button:has-text('Annuler')")
            if cancel_btn.count() > 0:
                try:
                    cancel_btn.first.click()
                    time.sleep(1)
                except Exception:
                    pass

            if prof_arrived_wake or datetime.now() >= join_deadline:
                continue

            # Cooldown up to 35s between attempts, waking immediately on Telegram "prof arrived" or URL update
            cooldown_end = min(datetime.now() + timedelta(seconds=35), join_deadline)
            print(f"Waiting up to 35s before next attempt (monitoring Telegram group chat until {join_deadline.strftime('%H:%M')})...")
            while datetime.now() < cooldown_end:
                sig, sig_txt = _check_telegram_chat_signal(watch_since_dt, seen_chat_ids, meeting)
                if sig == "CANCELLED":
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚫 [TELEGRAM SIGNAL] Class cancelled in chat: {sig_txt[:100]}")
                    aborted_by_cancel = True
                    break
                elif sig == "PROF_NOT_YET":
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 💬 [TELEGRAM CHAT] Prof still not here: \"{sig_txt[:90]}\" — keeping wait loop alive.")
                elif sig == "PROF_ARRIVED":
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 [TELEGRAM CHAT] Prof arrived: \"{sig_txt[:90]}\" -> Rejoining immediately!")
                    break

                # Also wake early if config.json got a new Meet URL or cancellation
                cfg_tick = load_config()
                wake_for_cfg = False
                for m in cfg_tick.get("meetings", []):
                    if m["name"].lower() == meeting["name"].lower():
                        if today_str in m.get("cancelled_dates", []):
                            aborted_by_cancel = True
                            wake_for_cfg = True
                        elif m["url"] != meeting["url"]:
                            wake_for_cfg = True
                        break
                if wake_for_cfg:
                    break
                time.sleep(3)

            if aborted_by_cancel:
                break

        if not admitted:
            if aborted_by_cancel:
                print("\n🚫 Session aborted due to cancellation/disable.")
            else:
                print(f"\n❌ Could not join by {join_deadline.strftime('%H:%M')} (~{max_wait_min}m wait window elapsed). Waiting for next scheduled class.")
            browser.close()
            return

        # 3. Start Video & Dual Audio Recording
        print(f"Starting video recording to {temp_video_filename}...")
        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-f', 'gdigrab',
            '-framerate', '15',
            '-i', 'desktop',
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            temp_video_filename
        ]
        
        try:
            recording_process = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("Video recording active.")
        except Exception as e:
            print(f"Failed to start FFmpeg video: {e}")
            recording_process = None

        audio_recorder = DualAudioRecorder(temp_audio_wav)
        audio_recorder.start()

        # 4. Monitoring Loop & Participant Scraping
        print("Monitoring live meeting participants & class activity...")
        meeting_start_time = time.time()
        start_time_str = datetime.now().strftime("%H:%M")
        peak_participants = 0
        unique_participants = set()
        low_count_consecutive = 0

        # Intelligent session chaining & professor tracking state
        active_meeting = meeting
        chained_sessions = [meeting["name"]]
        target_prof = (meeting.get("professor") or "").strip()
        prof_seen = False
        prof_missing_consecutive = 0
        initial_class_start = datetime.now()
        current_class_end = class_end

        while True:
            elapsed_minutes = (time.time() - meeting_start_time) / 60
            
            # Scrape live participants from Google Meet DOM
            current_count = 0
            alone_detected = False

            # Check direct Google Meet "Alone in call" indicators
            try:
                alone_loc = page.locator("text='You\\'re the only one here', text='Vous êtes le seul participant', text='Vous êtes seul dans la réunion', text='You\\'re the only one in the call', text='seule personne à participer'")
                if alone_loc.count() > 0:
                    alone_detected = True
                    current_count = 1
            except Exception:
                pass

            if not alone_detected:
                try:
                    # 1. Read live count directly from People button badge / aria-label
                    people_btn = page.locator("button[aria-label*='People'], button[aria-label*='personne'], button[aria-label*='Afficher les participants'], button[aria-label*='Show everyone']").first
                    if people_btn.count() > 0:
                        btn_aria = people_btn.get_attribute("aria-label") or ""
                        btn_txt = people_btn.inner_text() or ""
                        nums = re.findall(r'\b\d+\b', f"{btn_aria} {btn_txt}")
                        if nums:
                            current_count = int(nums[0])
                        
                        # Open panel if not open to scrape names and cross-verify count
                        panel = page.locator("div[role='listitem']")
                        if panel.count() == 0:
                            try:
                                people_btn.click()
                                time.sleep(1)
                            except Exception:
                                pass
                        
                        panel_count = page.locator("div[role='listitem']").count()
                        if panel_count > 0:
                            current_count = max(current_count, panel_count)
                            try:
                                names = page.locator("div[role='listitem'] span").all_inner_texts()
                                current_scrape_names = []
                                for n in names:
                                    cleaned = n.strip()
                                    if len(cleaned) > 2 and not cleaned.isdigit() and "présentation" not in cleaned.lower() and "presentation" not in cleaned.lower():
                                        unique_participants.add(cleaned)
                                        current_scrape_names.append(cleaned)
                                
                                # Track professor presence in current DOM
                                if target_prof:
                                    prof_present = any(professor_matches(target_prof, n) for n in current_scrape_names)
                                    if prof_present:
                                        if not prof_seen:
                                            print(f"[{datetime.now()}] 👨‍🏫 Professor '{target_prof}' confirmed present in meeting.")
                                        prof_seen = True
                                        prof_missing_consecutive = 0
                                    elif prof_seen and (panel_count >= current_count or current_count <= 8):
                                        prof_missing_consecutive += 1
                            except Exception:
                                pass
                except Exception:
                    pass

            if current_count > peak_participants:
                peak_participants = current_count

            # Check if host ended call or kicked out
            try:
                if page.locator("text='You left the meeting', text='L’appel est terminé', text='Vous avez quitté la réunion', text='Your request to join was denied', text='You were removed from the meeting'").count() > 0:
                    print(f"[{datetime.now()}] Meeting was ended or closed by host.")
                    break
            except Exception:
                pass

            # Smart Exit Logic (grace period: 5 minutes after joining)
            if elapsed_minutes >= 5:
                # Case 1: Alone in the call (or Google Meet says you're alone)
                if alone_detected or (current_count <= 1 and peak_participants >= 1):
                    low_count_consecutive += 1
                    print(f"[{datetime.now()}] ⚠️ Lone participant detected (count={current_count}, peak={peak_participants}, consecutive={low_count_consecutive}/2)")
                    if low_count_consecutive >= 2:
                        print(f"[{datetime.now()}] 🚪 Smart Exit Triggered: Alone in call. Leaving meeting.")
                        break
                # Case 2: Only 1 other person left with the bot (all others left, only 1 AFK remains)
                elif peak_participants >= 3 and current_count <= 2:
                    low_count_consecutive += 1
                    print(f"[{datetime.now()}] ⚠️ Near-empty call detected (count={current_count}, peak={peak_participants}, consecutive={low_count_consecutive}/2)")
                    if low_count_consecutive >= 2:
                        print(f"[{datetime.now()}] 🚪 Smart Exit Triggered: All other students left (count={current_count}). Leaving meeting.")
                        break
                # Case 3: Major drop from a large class (e.g. 10+ dropped down to <= 3)
                elif peak_participants >= 10 and current_count <= 3 and current_count < peak_participants * 0.25:
                    low_count_consecutive += 1
                    print(f"[{datetime.now()}] ⚠️ Class mass exit detected (count={current_count}, peak={peak_participants}, consecutive={low_count_consecutive}/2)")
                    if low_count_consecutive >= 2:
                        print(f"[{datetime.now()}] 🚪 Smart Exit Triggered: Class concluded (dropped from {peak_participants} to {current_count}). Leaving meeting.")
                        break
                else:
                    low_count_consecutive = 0

            # Check for web dashboard exit signal
            signal_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exit_meeting.signal")
            if os.path.exists(signal_file):
                print(f"[{datetime.now()}] 🛑 User requested exit from dashboard. Leaving call.")
                try:
                    os.remove(signal_file)
                except Exception:
                    pass
                break

            # ── Intelligent Session Continuation & Smart Overtime Logic ───
            now = datetime.now()
            if current_class_end and now >= current_class_end:
                # 1. Check for immediate consecutive session (same prof / same meet URL / same subject)
                cfg = load_config()
                consecutive = find_consecutive_meeting(active_meeting, cfg, now)
                if consecutive:
                    next_m, next_start, next_end = consecutive
                    next_key = (next_m["name"].lower(), now.date())
                    if next_m["name"] not in chained_sessions:
                        with _launch_lock:
                            _launched.add(next_key)  # Prevent scheduler from starting duplicate thread
                        chained_sessions.append(next_m["name"])
                        print(f"\n{'='*65}")
                        print(f"[{datetime.now()}] 🔗 CONSECUTIVE SESSION DETECTED: '{next_m['name']}'")
                        print(f"[{datetime.now()}] Seamlessly chaining session with Prof: {next_m.get('professor', 'N/A')}")
                        print(f"[{datetime.now()}] Remaining in active Google Meet call without disconnecting!")
                        print(f"[{datetime.now()}] Extended session window until {next_end.strftime('%H:%M')}.")
                        print(f"{'='*65}\n")
                        current_class_end = next_end
                        active_meeting = next_m
                        if next_m.get("professor"):
                            target_prof = next_m["professor"].strip()
                        continue

                # 2. No consecutive session: Check if a DIFFERENT class is upcoming today
                next_diff = find_next_different_class(active_meeting, cfg, now)
                if next_diff:
                    diff_m, diff_start, diff_end = next_diff
                    # Leave 3 minutes before next class so Chrome profile & audio are released
                    if now >= diff_start - timedelta(minutes=3):
                        print(f"[{datetime.now()}] 🚪 Smart Exit: Upcoming class '{diff_m['name']}' with Prof: {diff_m.get('professor', 'N/A')} starts at {diff_m['time']}. Leaving current call to switch rooms.")
                        break

                # 3. Check if professor has left the call (missing for >= 3 checks after being seen)
                if prof_seen and prof_missing_consecutive >= 3:
                    print(f"[{datetime.now()}] 🚪 Smart Exit: Professor '{target_prof}' has left the call. Class concluded.")
                    break

                # 4. Check if participants have drastically declined
                if current_count <= 2:
                    print(f"[{datetime.now()}] 🚪 Smart Exit: Class ended and participants dropped to {current_count}. Leaving meeting.")
                    break
                if peak_participants >= 6 and current_count < peak_participants * 0.30:
                    print(f"[{datetime.now()}] 🚪 Smart Exit: Class ended and audience drastically declined ({current_count}/{peak_participants} left). Leaving meeting.")
                    break

                # 5. Overtime buffer cap: max 35 minutes past scheduled end
                if now >= current_class_end + timedelta(minutes=35):
                    print(f"[{datetime.now()}] ⏰ Max overtime buffer (+35m past {current_class_end.strftime('%H:%M')}) reached. Leaving call.")
                    break

                # 6. If none of the above, professor and students are still in call: continue recording!
                overtime_min = int((now - current_class_end).total_seconds() // 60)
                if overtime_min > 0 and int(time.time() - meeting_start_time) % 60 < 20:
                    print(f"[{datetime.now()}] ⏳ Overtime active (+{overtime_min}m): Professor/students still in call ({current_count} present). Continuing recording...")

            # Hard Max: total scheduled minutes + 40 min buffer
            total_sched_min = int((current_class_end - initial_class_start).total_seconds() // 60) if current_class_end else 90
            if elapsed_minutes >= total_sched_min + 40:
                print(f"[{datetime.now()}] ⏰ Absolute max duration reached ({elapsed_minutes:.0f} minutes). Leaving call.")
                break

            time.sleep(20)

        # 5. Leave Call & Stop Recording
        end_time_str = datetime.now().strftime("%H:%M")
        total_duration_minutes = round((time.time() - meeting_start_time) / 60, 1)
        print(f"Leaving meeting. Total session duration: {total_duration_minutes} minutes.")

        try:
            hangup = page.locator("button[aria-label*='Leave call'], button[aria-label*='Quitter l’appel'], button[aria-label*='Quitter']")
            if hangup.count() > 0:
                hangup.first.click()
        except Exception:
            pass

        if recording_process:
            try:
                recording_process.communicate(b'q', timeout=30)
            except subprocess.TimeoutExpired:
                recording_process.kill()
                recording_process.wait()
                print("[WARNING] ffmpeg screen recorder force-killed after 30s timeout")


        audio_recorder.stop()
        browser.close()

        # 6. Save Attendance Log to CSV
        attendance_file = os.path.join(current_config["settings"]["output_dir"], "attendance_log.csv")
        try:
            is_new_csv = not os.path.exists(attendance_file)
            with open(attendance_file, "a", encoding="utf-8") as f:
                if is_new_csv:
                    f.write("Date,Subject,StartTime,EndTime,DurationMin,PeakStudents,Attendees\n")
                attendees_str = "; ".join(sorted(list(unique_participants))).replace(",", " ")
                for s_name in chained_sessions:
                    f.write(f"{datetime.now().strftime('%Y-%m-%d')},{s_name},{start_time_str},{end_time_str},{total_duration_minutes},{peak_participants},\"{attendees_str}\"\n")
            print(f"[OK] Attendance saved for {len(chained_sessions)} session(s): {', '.join(chained_sessions)} (Peak: {peak_participants} students)")
        except Exception as e:
            print(f"Warning saving attendance CSV: {e}")

        # 7. Convert Audio & Mux Final Video
        print("Processing audio & video...")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", temp_audio_wav, "-ac", "1", "-acodec", "libmp3lame", "-b:a", "32k", audio_filename],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=300  # 5 min max for audio conversion
            )
            if os.path.exists(temp_video_filename):
                subprocess.run(
                    ["ffmpeg", "-y", "-i", temp_video_filename, "-i", audio_filename, "-c:v", "copy", "-c:a", "aac", video_filename],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=400  # ~6.5 min max for video mux
                )
                print(f"Final video saved: {video_filename}")
                os.remove(temp_video_filename)
            if os.path.exists(temp_audio_wav):
                os.remove(temp_audio_wav)
        except subprocess.TimeoutExpired:
            print("[WARNING] ffmpeg timed out during audio/video processing — skipping mux, will still try to upload audio")
        except Exception as e:
            print(f"Failed during audio/video processing: {e}")


        # 8. Upload to n8n Webhook
        print(f"[{datetime.now()}] Uploading to n8n AI webhook...")
        try:
            webhook_url = current_config["settings"]["n8n_webhook_url"]
            combined_name = " & ".join(chained_sessions)
            data_payload = {
                "meeting_name": combined_name,
                "chained_sessions": json.dumps(chained_sessions),
                "video_path": os.path.abspath(video_filename),
                "timestamp": timestamp,
                "start_time": start_time_str,
                "end_time": end_time_str,
                "duration_minutes": str(total_duration_minutes),
                "participants_count": str(peak_participants),
                "participants_list": ", ".join(sorted(list(unique_participants)))
            }
            if audio_filename and os.path.exists(audio_filename):
                with open(audio_filename, "rb") as f:
                    files = {"file": (os.path.basename(audio_filename), f, "audio/mpeg")}
                    response = requests.post(webhook_url, data=data_payload, files=files, timeout=600)
            else:
                response = requests.post(webhook_url, json=data_payload, timeout=60)

            
            response.raise_for_status()
            resp_json = response.json()
            if "report_markdown" in resp_json:
                report_path = video_filename + "_report.md"
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(resp_json["report_markdown"])
                print(f"[{datetime.now()}] [OK] Comprehensive Study Report generated: {report_path}")

            if "transcript_text" in resp_json:
                transcript_path = video_filename + "_transcript.txt"
                with open(transcript_path, "w", encoding="utf-8") as f:
                    f.write(resp_json["transcript_text"])
                print(f"[{datetime.now()}] [OK] Full Transcript saved: {transcript_path}")

            if "master_prompt" in resp_json:
                prompt_path = video_filename + "_master_prompt.md"
                with open(prompt_path, "w", encoding="utf-8") as f:
                    f.write(resp_json["master_prompt"])
                print(f"[{datetime.now()}] [OK] Master Study Prompt auto-generated: {prompt_path}")

            print(f"[{datetime.now()}] Webhook processing completed.")
                
        except Exception as e:
            print(f"Failed to send webhook: {e}")

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_launched = set()          # (meeting name, date) already started today
_launch_lock = threading.Lock()

def _class_window(meeting, ref=None):
    """Return (start, end) datetimes for this meeting's occurrence on ref's date."""
    ref = ref or datetime.now()
    start = datetime.combine(ref.date(), datetime.strptime(meeting["time"], "%H:%M").time())
    return start, start + timedelta(minutes=meeting.get("duration_minutes", 90))

def launch_meeting(meeting, skip_delay=False):
    today_str = datetime.now().strftime("%Y-%m-%d")
    if today_str in meeting.get("cancelled_dates", []):
        print(f"[{datetime.now()}] 🚫 Skipping {meeting['name']}: marked cancelled for today ({today_str}).")
        return

    # ── Mandatory Telegram Pre-Flight Verification ──────────────────────────
    try:
        from telegram_preflight import run_telegram_preflight_check
        chk = run_telegram_preflight_check(meeting)
        if chk.get("action") == "CANCEL":
            print(f"[{datetime.now()}] 🚫 [TELEGRAM PRE-FLIGHT] {meeting['name']} is CANCELLED per Telegram announcement: {chk.get('reason')}")
            return
        elif chk.get("action") == "RESCHEDULE":
            print(f"[{datetime.now()}] ⏰ [TELEGRAM PRE-FLIGHT] {meeting['name']} was rescheduled to {chk.get('new_time')}. Deferring join.")
            return
        elif chk.get("url_updated"):
            meeting["url"] = chk.get("url")
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Telegram pre-flight check exception: {e}")

    key = (meeting["name"].lower(), datetime.now().date())
    with _launch_lock:
        if key in _launched:
            return
        _launched.add(key)
    start, end = _class_window(meeting)

    def _runner():
        try:
            join_meeting(meeting, skip_delay, end, class_start=start, skip_preflight=True)
        except BaseException:
            import traceback
            print(f"[{datetime.now()}] ❌ Session thread for {meeting['name']} crashed:")
            traceback.print_exc()

    print(f"[{datetime.now()}] ▶️ Launching session thread for {meeting['name']}")
    threading.Thread(target=_runner, daemon=True, name="session_runner").start()

def catch_up_missed():
    """Handle a class already in progress (bot started late or schedule just changed).
    Within late_join_cutoff_minutes (default 10) after start -> join now.
    Later than that -> skip it (don't ask the prof to admit a late student)."""
    now = datetime.now()
    today = DAYS[now.weekday()]
    today_str = now.strftime("%Y-%m-%d")
    cfg = load_config()
    grace = timedelta(minutes=cfg["settings"].get("late_join_cutoff_minutes", 10))
    for meeting in cfg.get("meetings", []):
        if today_str in meeting.get("cancelled_dates", []):
            continue
        if meeting["day_of_week"].lower() != today:
            continue
        start, end = _class_window(meeting, now)
        if not (start <= now < end):
            continue
        key = (meeting["name"].lower(), now.date())
        if key in _launched:
            continue
        if now - start <= grace:
            print(f"⏱️ {meeting['name']} started at {meeting['time']} — still within join window, joining now.")
            launch_meeting(meeting, skip_delay=True)
        else:
            late_min = int((now - start).total_seconds() // 60)
            print(f"⏭️ Skipping {meeting['name']}: already {late_min} min late. Waiting for next session.")
            with _launch_lock:
                _launched.add(key)

def schedule_meetings():
    schedule.clear()
    current_config = load_config()
    for meeting in current_config.get("meetings", []):
        day = meeting["day_of_week"].lower()
        time_str = meeting["time"]
        getattr(schedule.every(), day).at(time_str).do(launch_meeting, meeting)
        print(f"📅 Scheduled: {meeting['name']} every {day.capitalize()} at {time_str}")

def get_pending_sessions_count_today():
    now = datetime.now()
    today = DAYS[now.weekday()]
    today_str = now.strftime("%Y-%m-%d")
    cfg = load_config()
    if cfg.get("enabled", True) is False:
        return 0
    count = 0
    for m in cfg.get("meetings", []):
        if today_str in m.get("cancelled_dates", []):
            continue
        if m.get("day_of_week", "").lower() != today:
            continue
        _, end = _class_window(m, now)
        if now < end:
            count += 1
    return count

def is_session_active():
    return any(t.name == "session_runner" and t.is_alive() for t in threading.enumerate())

if __name__ == "__main__":
    print("=" * 60)
    print("STUDY BOT ACTIVE (ON-DEMAND RUNNER)")
    print("=" * 60)

    pending = get_pending_sessions_count_today()
    if pending == 0:
        print(f"[{datetime.now()}] [RAM SAVER] 💤 No upcoming study sessions programmed for today.")
        print(f"[{datetime.now()}] [RAM SAVER] Shutting down bot to free RAM. (Telegram watcher remains active in background).")
        sys.exit(0)

    schedule_meetings()
    catch_up_missed()
    last_mtime = os.path.getmtime(CONFIG_FILE)

    print("\nWaiting for scheduled classes... (Press Ctrl+C to stop)")
    idle_checks = 0
    while True:
        schedule.run_pending()
        try:
            mtime = os.path.getmtime(CONFIG_FILE)
            if mtime != last_mtime:
                last_mtime = mtime
                time.sleep(1)  # let the writer finish
                print(f"\n[{datetime.now()}] 🔄 config.json changed — reloading schedule...")
                schedule_meetings()
                catch_up_missed()
        except (Exception, SystemExit) as e:
            print(f"[WARN] Config reload failed: {e}")

        idle_checks += 1
        if idle_checks % 10 == 0:
            active = is_session_active()
            pending = get_pending_sessions_count_today()
            if not active and pending == 0:
                print(f"\n[{datetime.now()}] [RAM SAVER] 💤 All study sessions for today are complete/cancelled.")
                print(f"[{datetime.now()}] [RAM SAVER] Shutting down bot to free RAM. (Telegram watcher remains active in background).")
                sys.exit(0)

        time.sleep(1)
