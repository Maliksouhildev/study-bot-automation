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
    last_err = None
    for _ in range(retries):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
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
    
    print(f"Launching Google Chrome for {meeting['name']} ({meeting['url']})...")
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=current_config["settings"]["chrome_user_data_dir"],
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
                    print(f"🔄 Link updated via Telegram during wait! Navigating to new URL: {m['url']}")
                    meeting["url"] = m["url"]
                    page.goto(meeting["url"])
                    time.sleep(6)
                    break

            join_buttons = page.locator("button:has-text('Ask to join'), button:has-text('Demander à participer'), button:has-text('Join now'), button:has-text('Participer')")
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

        while True:
            elapsed_minutes = (time.time() - meeting_start_time) / 60
            
            # Scrape participants from Google Meet DOM
            current_count = 0
            try:
                # Open people panel if not already open
                people_btn = page.locator("button[aria-label*='People'], button[aria-label*='personne'], button[aria-label*='Afficher les participants'], button[aria-label*='Show everyone']").first
                if people_btn.count() > 0:
                    panel = page.locator("div[role='listitem']")
                    if panel.count() == 0:
                        people_btn.click()
                        time.sleep(1)
                    
                    names = page.locator("div[role='listitem'] span").all_inner_texts()
                    for n in names:
                        cleaned = n.strip()
                        if len(cleaned) > 2 and not cleaned.isdigit() and "présentation" not in cleaned.lower() and "presentation" not in cleaned.lower():
                            unique_participants.add(cleaned)
                    
                    current_count = max(panel.count(), len(unique_participants))
            except Exception:
                pass

            if current_count > peak_participants:
                peak_participants = current_count

            # Check if host ended call
            try:
                if page.locator("text='You left the meeting', text='L’appel est terminé', text='Vous avez quitté la réunion'").count() > 0:
                    print(f"[{datetime.now()}] Meeting was ended by host.")
                    break
            except Exception:
                pass

            # Smart Exit Logic
            if elapsed_minutes >= 50:
                # If class had 10+ students and dropped to <= 4 (majority left, only AFKs remain)
                if peak_participants >= 10 and current_count <= 4 and current_count < peak_participants * 0.35:
                    print(f"[{datetime.now()}] 🚪 Smart Exit Triggered: Participants dropped from peak of {peak_participants} down to {current_count} (only AFKs remain). Leaving call.")
                    break
                # If small class from start (< 10) but now down to <= 2
                elif peak_participants < 10 and current_count <= 2 and current_count > 0:
                    print(f"[{datetime.now()}] 🚪 Smart Exit Triggered: Small class ended (participants: {current_count}). Leaving call.")
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
                ["ffmpeg", "-y", "-i", temp_audio_wav, "-acodec", "libmp3lame", "-q:a", "5", audio_filename],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120  # 2 min max for audio conversion
            )
            if os.path.exists(temp_video_filename):
                subprocess.run(
                    ["ffmpeg", "-y", "-i", temp_video_filename, "-i", audio_filename, "-c:v", "copy", "-c:a", "aac", video_filename],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=180  # 3 min max for video mux
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
    threading.Thread(target=_runner, daemon=True).start()

def catch_up_missed():
    """Handle a class already in progress (bot started late or schedule just changed).
    Within late_join_cutoff_minutes (default 10) after start -> join now.
    Later than that -> skip it (don't ask the prof to admit a late student)."""
    now = datetime.now()
    today = DAYS[now.weekday()]
    cfg = load_config()
    grace = timedelta(minutes=cfg["settings"].get("late_join_cutoff_minutes", 10))
    for meeting in cfg.get("meetings", []):
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

if __name__ == "__main__":
    print("=" * 60)
    print("STUDY BOT SCHEDULER ACTIVE")
    print("=" * 60)
    schedule_meetings()
    catch_up_missed()
    last_mtime = os.path.getmtime(CONFIG_FILE)

    print("\nWaiting for scheduled classes... (Press Ctrl+C to stop)")
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
        time.sleep(1)
