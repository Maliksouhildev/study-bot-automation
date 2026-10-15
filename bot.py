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

def join_meeting(meeting, skip_delay=False, class_end=None):
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
        try:
            page.keyboard.press("Control+d")
            time.sleep(1)
            page.keyboard.press("Control+e")
            time.sleep(1)
            # Extra safeguard: click toggle buttons if visible
            toggles = page.locator("div[role='button'][aria-label*='micro'], div[role='button'][aria-label*='Micro'], div[role='button'][aria-label*='caméra'], div[role='button'][aria-label*='Camera']")
            for i in range(toggles.count()):
                try:
                    aria = toggles.nth(i).get_attribute("aria-label") or ""
                    # Click only if it says 'turn off' or 'désactiver'
                    if any(w in aria.lower() for w in ["turn off", "désactiver", "couper"]):
                        toggles.nth(i).click()
                except Exception:
                    pass
        except Exception as e:
            print(f"Warning during mic/cam toggle: {e}")

        # 2. Retry loop on 'Ask to join' (up to 5 attempts)
        admitted = False
        for attempt in range(1, 6):
            print(f"\n[Attempt {attempt}/5] Requesting to enter meeting...")
            
            # Check if link was updated in config by Telegram watcher while we were waiting
            updated_cfg = load_config()
            for m in updated_cfg.get("meetings", []):
                if m["name"].lower() == meeting["name"].lower() and m["url"] != meeting["url"]:
                    print(f"🔄 Link updated via Telegram during wait! Updating target to: {m['url']}")
                    meeting["url"] = m["url"]
                    break

            # If page is not on target meeting room (e.g. after clicking Cancel which redirects to home), navigate to meeting URL
            target_meet_id = meeting["url"].split("/")[-1].split("?")[0]
            if target_meet_id not in (page.url or ""):
                print(f"Navigating to {meeting['url']}...")
                page.goto(meeting["url"])
                time.sleep(6)
                try:
                    page.keyboard.press("Control+d")
                    time.sleep(0.5)
                    page.keyboard.press("Control+e")
                except Exception:
                    pass

            join_buttons = page.locator("button:has-text('Ask to join'), button:has-text('Demander à participer'), button:has-text('Join now'), button:has-text('Participer'), button:has-text('Passer ici'), button:has-text('Switch here')")
            if join_buttons.count() > 0:
                try:
                    join_buttons.first.click()
                    print("Clicked 'Ask to join' / 'Join now'.")
                except Exception as e:
                    print(f"Could not click join button: {e}")

            # Wait up to 90 seconds for professor to admit
            check_start = time.time()
            while time.time() - check_start < 90:
                # If leave call button exists, we are IN!
                if page.locator("button[aria-label*='Leave call'], button[aria-label*='Quitter l’appel'], button[aria-label*='Quitter']").count() > 0:
                    admitted = True
                    break
                if page.locator("text='You can\\'t join this call', text='Votre demande a été refusée'").count() > 0:
                    print("Join request explicitly refused by host.")
                    break
                time.sleep(4)

            if admitted:
                print("🎉 Successfully admitted into the meeting!")
                break
            else:
                print(f"Attempt {attempt} timed out (professor has not admitted yet).")
                cancel_btn = page.locator("button:has-text('Cancel'), button:has-text('Annuler')")
                if cancel_btn.count() > 0:
                    try: cancel_btn.first.click()
                    except Exception: pass
                
                if attempt < 5:
                    print("Waiting 60 seconds before next attempt (monitoring group chat in background)...")
                    time.sleep(60)

        if not admitted:
            print("\n❌ Could not join after 5 attempts. Professor did not accept. Waiting for next scheduled class.")
            browser.close()
            key = (meeting["name"].lower(), datetime.now().date())
            with _launch_lock:
                _launched.discard(key)
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
                                for n in names:
                                    cleaned = n.strip()
                                    if len(cleaned) > 2 and not cleaned.isdigit() and "présentation" not in cleaned.lower() and "presentation" not in cleaned.lower():
                                        unique_participants.add(cleaned)
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

            # Scheduled class end reached (next class may need the Chrome profile)
            if class_end and datetime.now() >= class_end:
                print(f"[{datetime.now()}] ⏰ Scheduled class end reached. Leaving call.")
                break

            # Hard Max: 105 minutes (90 min normal class + 15 min buffer)
            if elapsed_minutes >= 105:
                print(f"[{datetime.now()}] ⏰ Max duration reached (105 minutes). Leaving call.")
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
                f.write(f"{datetime.now().strftime('%Y-%m-%d')},{meeting['name']},{start_time_str},{end_time_str},{total_duration_minutes},{peak_participants},\"{attendees_str}\"\n")
            print(f"[OK] Attendance saved to {attendance_file} (Peak: {peak_participants} students)")
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
            data_payload = {
                "meeting_name": meeting["name"],
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
                    response = requests.post(webhook_url, data=data_payload, files=files, timeout=300)
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
    _, end = _class_window(meeting)

    def _runner():
        try:
            join_meeting(meeting, skip_delay, end)
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
