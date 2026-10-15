import os
import json
import re
import subprocess
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse
import urllib.request

PORT = 5050
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STUDY_BOT_DIR = os.path.dirname(BASE_DIR)
RECORDINGS_DIR = os.path.join(STUDY_BOT_DIR, "recordings")
CONFIG_PATH = os.path.join(STUDY_BOT_DIR, "config.json")
TASKS_DATA_PATH = os.path.join(BASE_DIR, "tasks_data.json")
N8N_PROMPT_WEBHOOK = "http://localhost:5678/webhook/generate-study-prompt"

class StudyHubHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Enable CORS for local interactions
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
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
        elif parsed.path == "/api/tasks":
            self.handle_api_tasks()
        elif parsed.path == "/api/professor-intel":
            self.handle_api_professor_intel()
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/generate-prompt":
            self.handle_generate_prompt()
        elif parsed.path == "/api/tasks":
            self.handle_api_save_tasks()
        elif parsed.path == "/api/jarvis/chat":
            self.handle_api_jarvis_chat()
        elif parsed.path == "/api/meeting/exit":
            self.handle_api_exit_meeting()
        elif parsed.path == "/api/meeting/focus":
            self.handle_api_focus_meeting()
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

    def _load_tasks_data(self):
        if os.path.exists(TASKS_DATA_PATH):
            try:
                with open(TASKS_DATA_PATH, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"tasks": [], "modules": [], "projects": []}

    def _save_tasks_data(self, data):
        try:
            with open(TASKS_DATA_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False

    def handle_api_tasks(self):
        """Loads tasks, runs auto-rollover for overdue incomplete items, and returns state."""
        data = self._load_tasks_data()
        today_str = datetime.now().strftime("%Y-%m-%d")
        changed = False

        for t in data.get("tasks", []):
            if t.get("status") != "completed":
                due_date = t.get("due_date", "")
                if due_date and due_date < today_str:
                    if not t.get("rolled_over"):
                        t["original_date"] = due_date
                    t["due_date"] = today_str
                    t["rolled_over"] = True
                    changed = True

        if changed:
            self._save_tasks_data(data)

        self._send_json(data)

    def handle_api_save_tasks(self):
        """Saves updated tasks, modules progress sliders, or projects state."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            payload = json.loads(body)
            
            current = self._load_tasks_data()
            if "tasks" in payload:
                current["tasks"] = payload["tasks"]
            if "modules" in payload:
                current["modules"] = payload["modules"]
            if "projects" in payload:
                current["projects"] = payload["projects"]
            
            self._save_tasks_data(current)
            self._send_json({"success": True, "data": current})
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, status=500)

    @staticmethod
    def _is_actionable_intel(text, intel_type):
        if not text:
            return False
        cleaned = text.strip().strip("-*# \t\r\n")
        lower = cleaned.lower()
        if len(cleaned) < 15 or cleaned == "---":
            return False

        # Strong filter for boilerplate / non-announcement negative phrasing
        negatives = [
            "none", "none mentioned", "n/a", "no homework", "no specific", 
            "no formal", "no explicit", "not explicitly", "were not explicitly", 
            "was not explicitly", "was not announced", "were not announced", 
            "not announced", "not mentioned", "no exam", "no quiz", "no midterm",
            "no deliverables", "no standard university", "no specific tp", 
            "no specific td", "none stated", "none announced", "aside from following",
            "malik_called", "exam_details", "total duration recorded",
            "no direct call-out", "only preparation for upcoming", "start time:", "end time:"
        ]
        for neg in negatives:
            if neg in lower:
                if len(lower) < 130 or lower.startswith("no ") or lower.startswith("*no ") or lower.startswith("- no ") or lower.startswith("- *no "):
                    return False
                if "no specific" in lower or "not explicitly" in lower or "none mentioned" in lower or "no formal" in lower or "no explicit" in lower:
                    return False

        # Filter out misparsed mathematical equations or circuit parameters
        if intel_type in ["exam", "tp"]:
            if lower.startswith("given parameters") or lower.startswith("$") or lower.startswith("v_{o") or ("bits = " in lower and "byte =" in lower):
                return False

        # Filter out non-academic test sandboxes
        if "odm gear" in lower or "ackerman" in lower or "titan" in lower or "sandbox testing" in lower:
            return False

        return True

    def handle_api_professor_intel(self):
        """Scans recordings reports to extract only genuine, actionable homework, exams, and TP guidelines."""
        intel_items = []
        if os.path.exists(RECORDINGS_DIR):
            for fname in os.listdir(RECORDINGS_DIR):
                if not fname.endswith("_report.md") or fname.lower().startswith("test_") or "test_immediate" in fname.lower():
                    continue
                fpath = os.path.join(RECORDINGS_DIR, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    date_match = re.search(r'(\d{8})_(\d{6})', fname)
                    date_str = ""
                    if date_match:
                        raw_d = date_match.group(1)
                        date_str = f"{raw_d[:4]}-{raw_d[4:6]}-{raw_d[6:8]}"

                    mod_match = re.search(r'Course Name:\*?\*?\s*([^\n\r]+)', content)
                    mod_name = mod_match.group(1).strip() if mod_match else fname.replace("_report.md", "").replace("__", " ")

                    # 1. Homework Extraction
                    hw_match = re.search(r'(?:Homework Mentioned|Devoir|Homework)[^:\n]*:?\s*([^\n\r]+(?:\n[^\n\r#]+)?)', content, re.IGNORECASE)
                    if hw_match:
                        hw_text = hw_match.group(1).strip()
                        if self._is_actionable_intel(hw_text, "homework"):
                            clean_hw = re.sub(r'^\s*[-*#]+\s*', '', hw_text)
                            intel_items.append({
                                "id": f"intel_hw_{fname}",
                                "type": "homework",
                                "title": f"Homework: {mod_name}",
                                "description": clean_hw,
                                "category": mod_name,
                                "date": date_str,
                                "source": fname
                            })

                    # 2. TP / Practical Work changes
                    tp_match = re.search(r'(?:Practical Work Changes|Travaux Pratiques|TP)[^:\n]*:?\s*([^\n\r]+(?:\n[^\n\r#]+)?)', content, re.IGNORECASE)
                    if tp_match:
                        tp_text = tp_match.group(1).strip()
                        if self._is_actionable_intel(tp_text, "tp"):
                            clean_tp = re.sub(r'^\s*[-*#]+\s*', '', tp_text)
                            intel_items.append({
                                "id": f"intel_tp_{fname}",
                                "type": "tp",
                                "title": f"TP / Lab Guideline: {mod_name}",
                                "description": clean_tp,
                                "category": mod_name,
                                "date": date_str,
                                "source": fname
                            })

                    # 3. Exam / Quiz / Interro extraction
                    exam_match = re.search(r'(?:Exam|Interro|Quiz|Test|Contrôle)[^:\n]*:?\s*([^\n\r]+(?:\n[^\n\r#]+)?)', content, re.IGNORECASE)
                    if exam_match:
                        exam_text = exam_match.group(1).strip()
                        if self._is_actionable_intel(exam_text, "exam"):
                            clean_exam = re.sub(r'^\s*[-*#]+\s*', '', exam_text)
                            intel_items.append({
                                "id": f"intel_exam_{fname}",
                                "type": "exam",
                                "title": f"Exam Alert: {mod_name}",
                                "description": clean_exam,
                                "category": mod_name,
                                "date": date_str,
                                "source": fname
                            })
                except Exception:
                    pass
        self._send_json({"intel": intel_items})

    def handle_api_jarvis_chat(self):
        """Conversational AI task strategist and Jarvis advisor."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            payload = json.loads(body)
            user_msg = payload.get("message", "").strip()

            tasks_data = self._load_tasks_data()
            tasks = tasks_data.get("tasks", [])
            modules = tasks_data.get("modules", [])
            projects = tasks_data.get("projects", [])

            pending_tasks = [t for t in tasks if t.get("status") != "completed"]
            today_str = datetime.now().strftime("%Y-%m-%d")
            
            api_key = os.environ.get("GEMINI_API_KEY", "")
            ai_reply = None
            action_command = None

            system_instruction = (
                "You are JARVIS, an omnipotent, sharp, tactical academic and project advisor for Malik, an L3 Automatique student at Univ Annaba. "
                "You have complete visibility over his 8 modules, his ongoing n8n automation and web projects, his pending checklist, and overdue items. "
                "Your objective: Advise him on optimal priority (e.g. what to study or build first), keep him disciplined, and manage his tasks. "
                "Keep answers direct, high-impact, and motivating like Tony Stark's JARVIS. "
                "If the user asks to create or schedule a task, output a structured JSON block at the end:\n"
                "```json\n"
                "{\"action\": \"create_task\", \"title\": \"...\", \"description\": \"...\", \"category\": \"...\", \"priority\": \"high|medium|low\", \"due_date\": \"YYYY-MM-DD\"}\n"
                "```"
            )

            context_blob = (
                f"Current Date: {today_str}\n"
                f"Active Pending Tasks ({len(pending_tasks)}):\n"
                + "\n".join([f"- [{t.get('priority', 'normal').upper()}] {t.get('title')} (Due: {t.get('due_date')}, Category: {t.get('category')}, Rolled Over: {t.get('rolled_over', False)})" for t in pending_tasks[:10]])
                + f"\n\nModule Progress:\n"
                + "\n".join([f"- {m.get('name')}: {m.get('progress', 0)}%" for m in modules])
                + f"\n\nProjects:\n"
                + "\n".join([f"- {p.get('name')}: {p.get('progress', 0)}%" for p in projects])
                + f"\n\nUser Message: {user_msg}"
            )

            if api_key:
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
                    req_payload = {
                        "contents": [
                            {"parts": [{"text": system_instruction + "\n\n" + context_blob}]}
                        ]
                    }
                    req = urllib.request.Request(
                        url,
                        data=json.dumps(req_payload).encode('utf-8'),
                        headers={'Content-Type': 'application/json'},
                        method='POST'
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        res_json = json.loads(resp.read().decode('utf-8'))
                        ai_reply = res_json['candidates'][0]['content']['parts'][0]['text']
                except Exception:
                    ai_reply = None

            if not ai_reply:
                low_modules = [m['name'] for m in modules if m.get('progress', 0) == 0]
                rolled_tasks = [t['title'] for t in pending_tasks if t.get('rolled_over')]
                
                reply_parts = []
                reply_parts.append("Sir, here is your tactical priority breakdown:")
                if rolled_tasks:
                    reply_parts.append(f"⚠️ **Immediate Action Needed:** You have rolled-over tasks from yesterday: *{rolled_tasks[0]}*. I recommend completing this before starting new items.")
                if low_modules:
                    reply_parts.append(f"📚 **Curriculum Alert:** You currently have 0% recorded progress in *{low_modules[0]}*. Allocate 45 minutes today to review the intro TD notes.")
                reply_parts.append("⚡ **Workflow Sync:** Your n8n automations are online. Keep momentum steady.")
                
                if any(w in user_msg.lower() for w in ["add task", "new task", "remind me to", "schedule"]):
                    clean_title = re.sub(r'(?i)^(add task|new task|remind me to|schedule)\s*:?\s*', '', user_msg)
                    action_command = {
                        "action": "create_task",
                        "title": clean_title or "New Study Task",
                        "description": "Task created via Jarvis tactical advisor",
                        "category": "General",
                        "priority": "high",
                        "due_date": today_str
                    }
                    reply_parts.append(f"\n✅ I have prepared the task *'{clean_title}'* for today's checklist.")

                ai_reply = "\n\n".join(reply_parts)

            if not action_command and "```json" in ai_reply:
                try:
                    json_str = ai_reply.split("```json")[1].split("```")[0].strip()
                    parsed_action = json.loads(json_str)
                    if isinstance(parsed_action, dict) and "action" in parsed_action:
                        action_command = parsed_action
                except Exception:
                    pass

            self._send_json({
                "reply": ai_reply,
                "action": action_command
            })
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

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

    def handle_api_exit_meeting(self):
        """Signals the bot to exit the active meeting cleanly and immediately."""
        try:
            signal_path = os.path.join(STUDY_BOT_DIR, "exit_meeting.signal")
            with open(signal_path, "w") as f:
                f.write("exit")

            # Forcefully close bot Chrome instance if active to ensure immediate exit
            cmd = 'Get-CimInstance Win32_Process -Filter "Name = \'chrome.exe\'" | Where-Object { $_.CommandLine -like "*chrome_bot_profile*" } | Stop-Process -Force -ErrorAction SilentlyContinue'
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd], timeout=5)
            self._send_json({"success": True, "message": "Exit signal sent. Meeting left."})
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, status=500)

    def handle_api_focus_meeting(self):
        """Brings the Chrome Google Meet window to the front."""
        try:
            import ctypes
            u32 = ctypes.windll.user32
            h_default = u32.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_default:
                u32.SetThreadDesktop(h_default)

            SW_RESTORE = 9
            activated = False
            EnumWindows = u32.EnumWindows
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)

            def cb(hwnd, lparam):
                nonlocal activated
                if u32.IsWindowVisible(hwnd):
                    length = u32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        u32.GetWindowTextW(hwnd, buf, length + 1)
                        title = buf.value.lower()
                        if "meet" in title or "chrome" in title:
                            u32.ShowWindowAsync(hwnd, SW_RESTORE)
                            u32.SetForegroundWindow(hwnd)
                            activated = True
                            return False
                return True

            EnumWindows(EnumWindowsProc(cb), 0)
            self._send_json({"success": True, "activated": activated, "message": "Brought Google Meet window to front."})
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
        # Program slots based on updated university schedule EAD3 (Oct 12, 2026)
        return {
            "days": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Saturday"],
            "slots": [
                {"label": "08:00 - 09:30", "time": "08:00"},
                {"label": "09:30 - 11:00", "time": "09:30"},
                {"label": "11:00 - 12:30", "time": "11:00"},
                {"label": "13:00 - 14:30", "time": "13:00"},
                {"label": "14:30 - 16:00", "time": "14:30"},
                {"label": "16:00 - 17:30", "time": "16:00"}
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
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    os.chdir(BASE_DIR)
    print(f"[OK] Study Bot Hub Web Server listening at http://localhost:{PORT}")
    server = HTTPServer(("0.0.0.0", PORT), StudyHubHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
