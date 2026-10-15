import os
import sys
import time
import json
import subprocess
import threading
from datetime import datetime
import soundcard as sc
import numpy as np
import soundfile as sf
import requests

if sys.stdout:
    try: sys.stdout.reconfigure(encoding='utf-8')
    except Exception: pass
if sys.stderr:
    try: sys.stderr.reconfigure(encoding='utf-8')
    except Exception: pass

MEETING_NAME = "Power Electronics (TD/TP)"
OUTPUT_DIR = "recordings"
END_TIME_STR = "17:00:00"
WEBHOOK_URL = "http://localhost:5678/webhook/process-study-session"

os.makedirs(OUTPUT_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
clean_name = "Power_Electronics__TD_TP"
wav_filename = os.path.join(OUTPUT_DIR, f"{clean_name}_{timestamp}.wav")
mp3_filename = os.path.join(OUTPUT_DIR, f"{clean_name}_{timestamp}.mp3")

class DualAudioRecorder:
    def __init__(self, filename, samplerate=48000):
        self.filename = filename
        self.samplerate = samplerate
        self.stop_event = threading.Event()
        self.lb_frames = []
        self.mic_frames = []
        
        self.loopback = None
        for spk in sc.all_speakers():
            if "fxsound" in spk.name.lower():
                self.loopback = spk
                break
        if not self.loopback:
            self.loopback = sc.default_speaker()
        print(f"[Audio] Loopback speaker: {self.loopback.name if self.loopback else 'None'}")
        
        self.mic = None
        for m in sc.all_microphones(include_loopback=True):
            if "stereo mix" in m.name.lower() or "mixage" in m.name.lower():
                self.mic = m
                break
        if not self.mic:
            for m in sc.all_microphones():
                if "virtual" not in m.name.lower():
                    self.mic = m
                    break
        if not self.mic:
            self.mic = sc.default_microphone()
        print(f"[Audio] Microphone: {self.mic.name if self.mic else 'None'}")

    def _record_loopback(self):
        if not self.loopback:
            return
        try:
            with self.loopback.recorder(samplerate=self.samplerate, channels=2) as r:
                while not self.stop_event.is_set():
                    data = r.record(numframes=int(self.samplerate * 0.2))
                    self.lb_frames.append(data)
        except Exception as e:
            print(f"[Audio] Loopback note: {e}")

    def _record_mic(self):
        if not self.mic:
            return
        try:
            with self.mic.recorder(samplerate=self.samplerate, channels=2) as r:
                while not self.stop_event.is_set():
                    data = r.record(numframes=int(self.samplerate * 0.2))
                    self.mic_frames.append(data)
        except Exception as e:
            print(f"[Audio] Mic note: {e}")

    def start(self):
        self.t_lb = threading.Thread(target=self._record_loopback, daemon=True)
        self.t_mic = threading.Thread(target=self._record_mic, daemon=True)
        self.t_lb.start()
        self.t_mic.start()
        print("[Audio] Dual audio capture active.")

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

        peak = np.max(np.abs(mixed))
        if peak > 0.005:
            mixed = (mixed / peak) * 0.95
        final_audio = np.clip(mixed, -1.0, 1.0)
        sf.write(self.filename, final_audio, self.samplerate)
        dur = len(final_audio) / self.samplerate
        print(f"[Audio] Saved {dur:.1f}s of normalized audio to {self.filename}")
        return dur

def main():
    print(f"============================================================")
    print(f"🔴 Starting Live Capture for: {MEETING_NAME}")
    print(f"Target End Time: {END_TIME_STR} (or Ctrl+C to stop early)")
    print(f"============================================================")
    
    start_time = time.time()
    start_time_str = datetime.now().strftime("%H:%M")
    recorder = DualAudioRecorder(wav_filename)
    recorder.start()
    
    # Calculate stop target
    now = datetime.now()
    end_dt = datetime.strptime(f"{now.strftime('%Y-%m-%d')} {END_TIME_STR}", "%Y-%m-%d %H:%M:%S")
    
    try:
        while datetime.now() < end_dt:
            # Check for web dashboard exit signal
            signal_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exit_meeting.signal")
            if os.path.exists(signal_file):
                print("🛑 Exit signal received from dashboard. Stopping recording.")
                try: os.remove(signal_file)
                except Exception: pass
                break
                
            elapsed = int(time.time() - start_time)
            remaining = int((end_dt - datetime.now()).total_seconds())
            print(f"⏳ Recording in progress... Elapsed: {elapsed//60}m {elapsed%60}s | Remaining: {remaining//60}m {remaining%60}s", end="\r")
            time.sleep(10)
    except KeyboardInterrupt:
        print("\nManual stop triggered.")
        
    print("\n⏹️ Class end reached. Finalizing audio...")
    end_time_str = datetime.now().strftime("%H:%M")
    duration_seconds = recorder.stop()
    duration_minutes = round(duration_seconds / 60, 1)
    
    # Convert to MP3
    print(f"Transcoding WAV to MP3: {mp3_filename}...")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", wav_filename, "-acodec", "libmp3lame", "-ab", "128k", mp3_filename],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ MP3 created successfully.")
    except Exception as e:
        print(f"⚠️ FFmpeg error: {e}")
        
    # Log attendance
    attendance_file = os.path.join(OUTPUT_DIR, "attendance_log.csv")
    try:
        with open(attendance_file, "a", encoding="utf-8") as f:
            f.write(f'{datetime.now().strftime("%Y-%m-%d")},{MEETING_NAME},{start_time_str},{end_time_str},{duration_minutes},1,"Mehdid Malik Souhil"\n')
        print("✅ Attendance logged to CSV.")
    except Exception as e:
        print(f"Attendance log note: {e}")

    # Upload to n8n Webhook
    print(f"📤 Uploading to n8n AI webhook ({WEBHOOK_URL})...")
    try:
        data_payload = {
            "meeting_name": MEETING_NAME,
            "video_path": os.path.abspath(mp3_filename),
            "timestamp": timestamp,
            "start_time": start_time_str,
            "end_time": end_time_str,
            "duration_minutes": str(duration_minutes),
            "participants_count": "1",
            "participants_list": "Mehdid Malik Souhil"
        }
        if os.path.exists(mp3_filename):
            with open(mp3_filename, "rb") as f:
                files = {"file": (os.path.basename(mp3_filename), f, "audio/mpeg")}
                resp = requests.post(WEBHOOK_URL, data=data_payload, files=files, timeout=300)
        else:
            resp = requests.post(WEBHOOK_URL, json=data_payload, timeout=60)
            
        if resp.ok:
            resp_json = resp.json()
            if "report_markdown" in resp_json:
                with open(wav_filename.replace(".wav", ".mp4_report.md"), "w", encoding="utf-8") as f:
                    f.write(resp_json["report_markdown"])
                print("🎉 Comprehensive Study Report generated!")
            if "transcript_text" in resp_json:
                with open(wav_filename.replace(".wav", ".mp4_transcript.txt"), "w", encoding="utf-8") as f:
                    f.write(resp_json["transcript_text"])
                print("🎉 Full Transcript saved!")
            if "master_prompt" in resp_json:
                with open(wav_filename.replace(".wav", ".mp4_master_prompt.md"), "w", encoding="utf-8") as f:
                    f.write(resp_json["master_prompt"])
                print("🎉 Master Study Prompt generated!")
        else:
            print(f"Webhook returned status: {resp.status_code}")
    except Exception as e:
        print(f"⚠️ Webhook upload note: {e}")

    print("🏁 Capture and processing complete.")

if __name__ == "__main__":
    main()
