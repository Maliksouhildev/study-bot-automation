import os
import json
import re
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse
import urllib.request

PORT = 5050
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STUDY_BOT_DIR = os.path.dirname(BASE_DIR)
RECORDINGS_DIR = os.path.join(STUDY_BOT_DIR, "recordings")
CONFIG_PATH = os.path.join(STUDY_BOT_DIR, "config.json")
N8N_PROMPT_WEBHOOK = "http://localhost:5678/webhook/generate-study-prompt"

class StudyHubHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Enable CORS for local interactions
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/status":
            self.handle_api_status()
        elif parsed.path == "/api/data":
            self.handle_api_data()
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/generate-prompt":
            self.handle_generate_prompt()
        else:
            self.send_error(404, "Not Found")

    def handle_api_status(self):
        """Returns live active sessions and current date/time."""
        now = datetime.now()
        config = self._load_config()
        meetings = config.get("meetings", [])
        
        # Check currently active or upcoming classes
        active_meeting = None
        current_day = now.strftime("%A")
        
        for m in meetings:
            if m.get("day_of_week", "").lower() == current_day.lower():
                try:
                    start_t = datetime.strptime(m["time"], "%H:%M").time()
                    start_dt = datetime.combine(now.date(), start_t)
                    end_dt = start_dt + timedelta(minutes=m.get("duration_minutes", 90))
                    if start_dt <= now <= end_dt:
                        active_meeting = m
                        break
                except Exception:
                    pass

        data = {
            "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "current_day": current_day,
            "active_meeting": active_meeting,
            "total_meetings_configured": len(meetings)
        }
        self._send_json(data)

    def handle_api_data(self):
        """Returns the full timetable, config, and all parsed report files from recordings."""
        config = self._load_config()
        reports = self._scan_reports()
        
        data = {
            "config": config,
            "reports": reports,
            "timetable": self._get_timetable_structure()
        }
        self._send_json(data)

    def handle_generate_prompt(self):
        """Proxies prompt generation to n8n webhook and returns master prompt."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            payload = json.loads(body)
            
            # Send to n8n
            req = urllib.request.Request(
                N8N_PROMPT_WEBHOOK,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                res_data = json.loads(resp.read().decode('utf-8'))
                self._send_json(res_data)
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, status=500)

    def _load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"meetings": []}

    def handle_api_data(self):
        """Returns the full timetable, config, parsed reports, transcripts, and course materials."""
        config = self._load_config()
        reports = self._scan_reports()
        materials = self._scan_materials()
        
        data = {
            "config": config,
            "reports": reports,
            "materials": materials,
            "timetable": self._get_timetable_structure()
        }
        self._send_json(data)

    def _scan_materials(self):
        mat_dir = os.path.join(BASE_DIR, "course_materials")
        materials = []
        if os.path.exists(mat_dir):
            for f in os.listdir(mat_dir):
                fpath = os.path.join(mat_dir, f)
                if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                    materials.append({
                        "filename": f,
                        "url": f"/course_materials/{urllib.parse.quote(f)}",
                        "size_kb": round(os.path.getsize(fpath) / 1024, 1)
                    })
        return materials

    def _scan_reports(self):
        """Scans all *.mp4_report.md files in recordings and returns structured history."""
        reports = []
        if not os.path.exists(RECORDINGS_DIR):
            return reports

        for fname in os.listdir(RECORDINGS_DIR):
            if fname.endswith("_report.md"):
                fpath = os.path.join(RECORDINGS_DIR, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    # Extract session details
                    date_match = re.search(r'(\d{8})_(\d{6})', fname)
                    date_str = ""
                    base_prefix = fname.replace("_report.md", "")
                    if date_match:
                        raw_d = date_match.group(1)
                        date_str = f"{raw_d[:4]}-{raw_d[4:6]}-{raw_d[6:8]}"

                    # Check for matching raw transcript
                    raw_transcript = ""
                    transcript_file = os.path.join(RECORDINGS_DIR, base_prefix + "_transcript.txt")
                    if os.path.exists(transcript_file):
                        with open(transcript_file, "r", encoding="utf-8", errors="ignore") as tf:
                            raw_transcript = tf.read()

                    # Check for auto master prompt
                    master_prompt = ""
                    prompt_file = os.path.join(RECORDINGS_DIR, base_prefix + "_master_prompt.md")
                    if os.path.exists(prompt_file):
                        with open(prompt_file, "r", encoding="utf-8", errors="ignore") as pf:
                            master_prompt = pf.read()

                    # Module name inference from file or content
                    mod_match = re.search(r'Course Name:\*?\*?\s*([^\n\r]+)', content)
                    mod_name = mod_match.group(1).strip() if mod_match else fname.replace("_report.md", "")
                    
                    reports.append({
                        "filename": fname,
                        "date": date_str or "Unknown",
                        "course_name": mod_name,
                        "content": content,
                        "transcript": raw_transcript,
                        "master_prompt": master_prompt,
                        "size": os.path.getsize(fpath),
                        "mtime": os.path.getmtime(fpath)
                    })
                except Exception:
                    pass

        reports.sort(key=lambda x: x["mtime"], reverse=True)
        return reports

    def _get_timetable_structure(self):
        # Program slots based on university schedule EAD3
        return {
            "days": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Saturday"],
            "slots": [
                {"label": "08:30 - 10:00", "time": "08:30"},
                {"label": "10:00 - 11:30", "time": "10:00"},
                {"label": "11:30 - 13:00", "time": "11:30"},
                {"label": "14:00 - 15:30", "time": "14:00"},
                {"label": "15:30 - 17:00", "time": "15:30"},
                {"label": "17:00 - 18:30", "time": "17:00"}
            ]
        }

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        out = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

if __name__ == "__main__":
    os.chdir(BASE_DIR)
    print(f"🚀 Study Bot Hub Web Server listening at http://localhost:{PORT}")
    server = HTTPServer(("0.0.0.0", PORT), StudyHubHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
