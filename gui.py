#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
TRANSLATE_SCRIPT = SCRIPT_DIR / "translate.py"
CONFIG_PATH = SCRIPT_DIR / "config.json"
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = SCRIPT_DIR / ".venv" / "bin" / "python3"


class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.reader_thread = None
        self.logs = []
        self.next_log_id = 1
        self.status = "Ready"
        self.last_exit_code = None

    def add_log(self, level, message):
        with self.lock:
            item = {
                "id": self.next_log_id,
                "level": level,
                "message": message.rstrip("\n"),
                "time": time.strftime("%H:%M:%S"),
            }
            self.next_log_id += 1
            self.logs.append(item)
            if len(self.logs) > 3000:
                self.logs = self.logs[-2500:]
            return item

    def snapshot_logs(self, after_id):
        with self.lock:
            return [item for item in self.logs if item["id"] > after_id]

    def clear_logs(self):
        with self.lock:
            self.logs = []
            self.next_log_id = 1

    def set_status(self, status, exit_code=None):
        with self.lock:
            self.status = status
            self.last_exit_code = exit_code

    def get_status(self):
        with self.lock:
            return {
                "status": self.status,
                "running": self.process is not None,
                "exit_code": self.last_exit_code,
            }


STATE = AppState()


def read_config():
    if not CONFIG_PATH.exists():
        return {"source_language": "en", "target_languages": []}

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    source_language = str(config.get("source_language") or "en").strip() or "en"
    target_languages = config.get("target_languages") or []
    if not isinstance(target_languages, list):
        raise ValueError("config.json target_languages must be a list")

    return {
        "source_language": source_language,
        "target_languages": [str(language).strip() for language in target_languages if str(language).strip()],
    }


def parse_target_languages(raw_text):
    languages = []
    seen = set()
    for line in raw_text.splitlines():
        for value in line.split(","):
            language = value.strip()
            if language and language not in seen:
                languages.append(language)
                seen.add(language)
    return languages


def save_config(source_language, target_languages):
    source_language = source_language.strip()
    if not source_language:
        raise ValueError("Source language must not be empty")
    if not target_languages:
        raise ValueError("Target languages must not be empty")

    config = {
        "source_language": source_language,
        "target_languages": target_languages,
    }
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")


def build_command(payload):
    project_root = Path(str(payload.get("project_root") or "")).expanduser()
    resource_paths = payload.get("resource_paths") or []
    if isinstance(resource_paths, str):
        resource_paths = [resource_paths]
    resource_paths = [str(value).strip() for value in resource_paths if str(value).strip()]
    workers = str(payload.get("workers") or "").strip()
    ids = str(payload.get("ids") or "").strip()
    engine = str(payload.get("engine") or "google").strip().lower()

    if not project_root.is_dir():
        raise ValueError(f"Project root does not exist: {project_root}")
    if not resource_paths:
        raise ValueError("Select at least one scanned resource file")
    for resource_path in resource_paths:
        candidate = (project_root / resource_path).resolve()
        try:
            candidate.relative_to(project_root.resolve())
        except ValueError as error:
            raise ValueError(f"Resource file is outside project root: {resource_path}") from error
        if not candidate.is_file() or candidate.suffix.lower() != ".xml":
            raise ValueError(f"Resource file does not exist: {resource_path}")
    try:
        workers_int = int(workers)
    except ValueError as error:
        raise ValueError("Workers must be a number") from error
    if workers_int < 1:
        raise ValueError("Workers must be at least 1")
    if engine not in {"google", "argos", "nllb"}:
        raise ValueError(f"Unsupported translation engine: {engine}")

    python_executable = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

    command = [
        python_executable,
        "-u",
        str(TRANSLATE_SCRIPT),
        "--project-root",
        str(project_root.resolve()),
        "--resource-paths",
        *resource_paths,
        "--workers",
        str(workers_int),
        "--engine",
        engine,
    ]

    if payload.get("skip_translated"):
        command.append("--skip-translated")
    if payload.get("word_by_word"):
        command.append("--word-by-word")
    if ids:
        command.extend(["--ids", ids])

    return command


def run_doctor(payload):
    project_root = Path(str(payload.get("project_root") or "")).expanduser().resolve()
    engine = str(payload.get("engine") or "google").strip().lower()
    resource_paths = payload.get("resource_paths") or []
    if isinstance(resource_paths, str):
        resource_paths = [resource_paths]

    checks = []
    def add(name, status, detail=""):
        checks.append({"name": name, "status": status, "detail": detail})

    project_ok = project_root.is_dir()
    add("Project", "ok" if project_ok else "error", str(project_root) if project_ok else f"Not found: {project_root}")
    if project_ok:
        writable = os.access(project_root, os.W_OK)
        add("Project writable", "ok" if writable else "error", "Writable" if writable else "No write permission")

    valid_resources = 0
    for resource_path in resource_paths:
        candidate = (project_root / str(resource_path)).resolve()
        try:
            candidate.relative_to(project_root)
            if candidate.is_file():
                valid_resources += 1
        except ValueError:
            pass
    add("Resources", "ok" if valid_resources else "warn", f"{valid_resources}/{len(resource_paths)} selected file(s) valid")

    python_executable = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    add("Python runtime", "ok", python_executable)
    add("Virtual environment", "ok" if VENV_PYTHON.exists() else "warn", "Ready" if VENV_PYTHON.exists() else "Missing .venv; run bash setup_local_engines.sh for local engines")

    probe = '''
import importlib.util, json, sys
result = {"python": sys.version.split()[0], "deps": {}}
for name in ["requests", "lxml", "argostranslate", "torch", "transformers", "sentencepiece"]:
    result["deps"][name] = importlib.util.find_spec(name) is not None
print(json.dumps(result))
'''
    try:
        completed = subprocess.run([python_executable, "-c", probe], capture_output=True, text=True, timeout=20)
        runtime = json.loads(completed.stdout.strip()) if completed.returncode == 0 else {"deps": {}}
    except Exception:
        runtime = {"deps": {}}

    deps = runtime.get("deps", {})
    required = ["requests", "lxml"]
    if engine == "argos":
        required.append("argostranslate")
    elif engine == "nllb":
        required.extend(["torch", "transformers", "sentencepiece"])
    missing = [name for name in required if not deps.get(name)]
    add("Dependencies", "ok" if not missing else "error", "Ready" if not missing else f"Missing: {', '.join(missing)}. Run bash setup_local_engines.sh")

    if engine == "nllb" and deps.get("transformers"):
        nllb_probe = '''
import json
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
name = "facebook/nllb-200-distilled-600M"
ready = True
try:
    AutoTokenizer.from_pretrained(name, local_files_only=True)
    AutoModelForSeq2SeqLM.from_pretrained(name, local_files_only=True)
except Exception:
    ready = False
print(json.dumps({"ready": ready}))
'''
        try:
            completed = subprocess.run([python_executable, "-c", nllb_probe], capture_output=True, text=True, timeout=30)
            ready = json.loads(completed.stdout.strip()).get("ready", False) if completed.returncode == 0 else False
            add("NLLB model", "ok" if ready else "warn", "Cached locally" if ready else "Not cached; it will auto-download on Start")
        except Exception as error:
            add("NLLB model", "warn", f"Could not inspect cache: {error}")

    return {"ok": not any(item["status"] == "error" for item in checks), "checks": checks}


def start_translation(payload):
    with STATE.lock:
        if STATE.process is not None:
            raise ValueError("Translator is already running")

    target_languages = parse_target_languages(str(payload.get("target_languages") or ""))
    save_config(str(payload.get("source_language") or ""), target_languages)
    command = build_command(payload)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    with STATE.lock:
        STATE.process = process
        STATE.status = "Running translation..."
        STATE.last_exit_code = None

    STATE.add_log("SECTION", "$ " + shlex.join(command))

    thread = threading.Thread(target=read_process_output, args=(process,), daemon=True)
    with STATE.lock:
        STATE.reader_thread = thread
    thread.start()


def read_process_output(process):
    assert process.stdout is not None
    for line in process.stdout:
        STATE.add_log(level_for_line(line), line)

    exit_code = process.wait()
    with STATE.lock:
        if STATE.process is process:
            STATE.process = None
            STATE.reader_thread = None
    if exit_code == 0:
        STATE.set_status("Finished successfully", exit_code)
        STATE.add_log("OK", f"Process finished with exit code {exit_code}")
    else:
        STATE.set_status(f"Finished with exit code {exit_code}", exit_code)
        STATE.add_log("ERROR", f"Process finished with exit code {exit_code}")


def stop_translation():
    with STATE.lock:
        process = STATE.process
    if process is None:
        return
    STATE.set_status("Stopping translator process...")
    STATE.add_log("WARN", "Stop requested. Terminating translator process...")
    process.terminate()


def scan_resource_files(project_root):
    project_root = Path(str(project_root or "")).expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError(f"Project root does not exist: {project_root}")

    resources = []
    for xml_file in sorted(project_root.rglob("src/main/res/values/*.xml")):
        try:
            root = ET.parse(xml_file).getroot()
        except Exception:
            continue

        count = 0
        kinds = set()
        for child in list(root):
            tag = child.tag.rsplit("}", 1)[-1]
            if tag not in {"string", "plurals", "string-array"}:
                continue
            if str(child.attrib.get("translatable", "true")).lower() == "false":
                continue
            count += 1
            kinds.add(tag)

        if count == 0:
            continue

        resources.append({
            "name": xml_file.name,
            "path": str(xml_file.relative_to(project_root)),
            "module": xml_file.parents[4].name if len(xml_file.parents) > 4 else "?",
            "count": count,
            "kinds": sorted(kinds),
        })

    return resources


def choose_folder():
    if sys.platform != "darwin":
        raise ValueError("Choose Folder is only available on macOS. Paste the project path manually.")

    script = 'POSIX path of (choose folder with prompt "Select Android project root")'
    result = subprocess.run(
        ["osascript", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def level_for_line(line):
    if line.startswith("ERROR"):
        return "ERROR"
    if line.startswith("WARN"):
        return "WARN"
    if line.startswith("OK"):
        return "OK"
    if line.startswith("INFO"):
        return "INFO"
    if line.startswith("=="):
        return "SECTION"
    return "LOG"


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GuiHandler(BaseHTTPRequestHandler):
    server_version = "AndroidStringTranslatorGui/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return
        if parsed.path == "/api/config":
            self.send_json({
                "config": read_config(),
                "default_project_root": str(DEFAULT_PROJECT_ROOT),
                "config_path": str(CONFIG_PATH),
            })
            return
        if parsed.path == "/api/status":
            self.send_json(STATE.get_status())
            return
        if parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0] or 0)
            self.send_json({"logs": STATE.snapshot_logs(after), **STATE.get_status()})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/save-config":
                payload = self.read_json()
                target_languages = parse_target_languages(str(payload.get("target_languages") or ""))
                save_config(str(payload.get("source_language") or ""), target_languages)
                STATE.add_log("OK", f"Saved {len(target_languages)} target language(s) to {CONFIG_PATH}")
                self.send_json({"ok": True, "target_count": len(target_languages)})
                return
            if parsed.path == "/api/scan-resources":
                payload = self.read_json()
                resources = scan_resource_files(payload.get("project_root"))
                STATE.add_log("INFO", f"Scanned {len(resources)} resource file name(s) containing translatable text.")
                self.send_json({"ok": True, "resources": resources})
                return
            if parsed.path == "/api/doctor":
                payload = self.read_json()
                self.send_json(run_doctor(payload))
                return
            if parsed.path == "/api/start":
                payload = self.read_json()
                start_translation(payload)
                self.send_json({"ok": True})
                return
            if parsed.path == "/api/stop":
                stop_translation()
                self.send_json({"ok": True})
                return
            if parsed.path == "/api/clear-logs":
                STATE.clear_logs()
                self.send_json({"ok": True})
                return
            if parsed.path == "/api/choose-folder":
                self.send_json({"path": choose_folder()})
                return
        except Exception as error:
            STATE.add_log("ERROR", str(error))
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Android String Translator</title>
  <style>
    :root {
      --bg: #f4f6f8;
      --panel: #ffffff;
      --text: #17202b;
      --muted: #5c6673;
      --border: #d8dee6;
      --primary: #1769e0;
      --primary-strong: #0f57bd;
      --secondary: #e8edf3;
      --log-bg: #101419;
      --ok: #15803d;
      --warn: #a16207;
      --error: #b91c1c;
      --info: #1d4ed8;
      --danger: #dc2626;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      max-width: 1240px;
      margin: 0 auto;
      padding: 24px;
    }
    .topbar {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 18px;
    }
    h1 {
      margin: 0;
      font-size: 28px;
      line-height: 1.2;
    }
    .subtitle {
      margin: 6px 0 18px;
      color: var(--muted);
      font-size: 14px;
    }
    .status {
      background: var(--primary);
      color: #fff;
      border-radius: 999px;
      padding: 9px 14px;
      font-size: 13px;
      font-weight: 800;
      white-space: nowrap;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 16px;
    }
    .panel-title {
      margin: 0 0 14px;
      font-size: 15px;
      font-weight: 700;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .summary-card {
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      min-height: 84px;
      padding: 13px;
    }
    .summary-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .04em;
      margin-bottom: 7px;
    }
    .summary-value {
      min-width: 0;
      overflow-wrap: anywhere;
      font-size: 14px;
      line-height: 1.35;
    }
    .target-strip {
      background: #eef6ff;
      border: 1px solid #c7dcfb;
      border-radius: 8px;
      padding: 12px 14px;
    }
    .target-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 4px;
    }
    .target-chip {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      border-radius: 999px;
      background: #ffffff;
      border: 1px solid #b8d2f5;
      color: #174a85;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
      font-weight: 700;
      padding: 4px 10px;
    }
    .target-empty {
      color: var(--muted);
      font-size: 14px;
    }
    .layout-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(360px, .9fr);
      gap: 16px;
    }
    .form-grid {
      display: grid;
      grid-template-columns: 130px minmax(0, 1fr);
      gap: 12px;
      align-items: center;
    }
    .project-row {
      display: grid;
      grid-template-columns: 130px minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
    }
    .language-grid {
      display: grid;
      grid-template-columns: 130px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }
    label {
      color: #263241;
      font-size: 14px;
      font-weight: 600;
    }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      background: #fff;
      font: inherit;
      padding: 10px 11px;
      outline: none;
      box-sizing: border-box;
    }
    textarea {
      min-height: 190px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      resize: vertical;
    }
    input:focus, textarea:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(23, 105, 224, .14);
    }
    .resource-list {
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #fff;
      max-height: 190px;
      overflow: auto;
      padding: 6px;
    }
    .resource-option {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      gap: 9px;
      align-items: center;
      padding: 8px 7px;
      border-radius: 5px;
      font-weight: 500;
    }
    .resource-option:hover { background: #f7f9fb; }
    .resource-option input { width: auto; }
    .resource-detail { color: var(--muted); font-size: 12px; }
    .resource-empty { color: var(--muted); padding: 10px; font-size: 13px; }
    .checks {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 16px;
    }
    .check {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 600;
      background: #f7f9fb;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 8px 12px;
    }
    .check input {
      width: auto;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 20px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
    }
    button {
      border: 0;
      border-radius: 6px;
      padding: 11px 18px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      background: var(--secondary);
      color: #263241;
    }
    button.primary {
      background: var(--primary);
      color: #fff;
      min-width: 120px;
    }
    button.danger {
      background: #fee2e2;
      color: var(--danger);
    }
    button.primary:hover { background: var(--primary-strong); }
    button:disabled {
      cursor: not-allowed;
      opacity: .55;
    }
    .meta {
      color: var(--muted);
      font-size: 13px;
    }
    .doctor-results { margin-top: 14px; display: grid; gap: 8px; }
    .doctor-row { display: grid; grid-template-columns: 140px 70px minmax(0, 1fr); gap: 10px; align-items: start; font-size: 13px; }
    .doctor-status { font-weight: 800; text-transform: uppercase; }
    .doctor-status.ok { color: var(--ok); }
    .doctor-status.warn { color: var(--warn); }
    .doctor-status.error { color: var(--error); }
    .log {
      height: 360px;
      overflow: auto;
      background: var(--log-bg);
      color: #d7dde5;
      border-radius: 8px;
      padding: 14px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .line { margin: 0; }
    .INFO { color: #7db7ff; }
    .OK { color: #65d487; }
    .WARN { color: #f2c15f; }
    .ERROR { color: #ff6b7a; }
    .SECTION { color: #f0f3f8; font-weight: 700; }
    @media (max-width: 800px) {
      main { padding: 14px; }
      .topbar { display: block; }
      .status { display: inline-block; margin-top: 12px; }
      .summary-grid, .layout-grid, .form-grid, .language-grid, .project-row {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <header class="topbar">
      <div>
        <h1>Android String Translator</h1>
        <p class="subtitle">Configure translation, edit languages, and monitor realtime output.</p>
      </div>
      <div id="status" class="status">Ready</div>
    </header>

    <section class="summary-grid">
      <div class="summary-card">
        <div class="summary-label">Project</div>
        <div id="sum-project" class="summary-value">-</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Resource</div>
        <div id="sum-resource" class="summary-value">-</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Mode</div>
        <div id="sum-mode" class="summary-value">-</div>
      </div>
      <div class="summary-card">
        <div class="summary-label">Languages</div>
        <div id="sum-languages" class="summary-value">-</div>
      </div>
    </section>
    <section class="target-strip">
      <div class="summary-label">Targets</div>
      <div id="sum-targets" class="summary-value">-</div>
      <div class="summary-label" style="margin-top:10px">IDs</div>
      <div id="sum-ids" class="summary-value">-</div>
    </section>

    <div class="layout-grid" style="margin-top:16px">
      <section class="panel">
        <h2 class="panel-title">Project</h2>
        <div class="project-row">
          <label for="projectRoot">Project root</label>
          <input id="projectRoot" type="text">
          <button id="chooseFolder" type="button">Browse</button>
        </div>

        <h2 class="panel-title" style="margin-top:22px">Options</h2>
        <div class="form-grid">
          <label>Resource files</label>
          <div>
            <div id="resourceFiles" class="resource-list">
              <div class="resource-empty">Choose a project to scan resource XML files.</div>
            </div>
            <div id="resourceScanMeta" class="meta" style="margin-top:6px">Not scanned</div>
          </div>
          <label for="engine">Engine</label>
          <select id="engine">
            <option value="argos" selected>Argos (local)</option>
            <option value="nllb">NLLB-200 (local)</option>
            <option value="google">Google GTX</option>
          </select>
          <label for="workers">Workers</label>
          <input id="workers" type="number" min="1" max="64" value="8">
          <label for="ids">IDs</label>
          <input id="ids" type="text" placeholder="All resource IDs">
        </div>
        <div class="checks">
          <label class="check"><input id="skipTranslated" type="checkbox" checked> Skip translated</label>
          <label class="check"><input id="wordByWord" type="checkbox"> Word by word</label>
        </div>
        <div class="actions">
          <button id="runDoctor" type="button">Run Doctor</button>
          <button id="runToggle" class="primary" type="button">Start</button>
        </div>
        <div id="doctorResults" class="doctor-results"></div>
      </section>

      <section class="panel">
        <h2 class="panel-title">Languages</h2>
        <div class="language-grid">
          <label for="sourceLanguage">Source language</label>
          <input id="sourceLanguage" type="text" value="en">
          <label>Target count</label>
          <div id="targetCount" class="meta">0 target languages</div>
          <label for="targetLanguages">Target languages</label>
          <textarea id="targetLanguages" placeholder="vi&#10;fr&#10;de"></textarea>
          <div></div>
          <div class="meta">One language per line, or comma separated.</div>
        </div>
        <div class="actions">
          <button id="saveConfig" type="button">Save Config</button>
          <button id="reloadConfig" type="button">Reload Config</button>
        </div>
      </section>
    </div>

    <section class="panel">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px">
        <h2 class="panel-title" style="margin:0">Terminal Log</h2>
        <button id="clearLog" type="button">Clear Log</button>
      </div>
      <div id="log" class="log"></div>
    </section>
  </main>

  <script>
    const fields = {
      projectRoot: document.getElementById("projectRoot"),
      engine: document.getElementById("engine"),
      workers: document.getElementById("workers"),
      ids: document.getElementById("ids"),
      skipTranslated: document.getElementById("skipTranslated"),
      wordByWord: document.getElementById("wordByWord"),
      sourceLanguage: document.getElementById("sourceLanguage"),
      targetLanguages: document.getElementById("targetLanguages"),
    };
    const buttons = {
      chooseFolder: document.getElementById("chooseFolder"),
      reloadConfig: document.getElementById("reloadConfig"),
      runDoctor: document.getElementById("runDoctor"),
      runToggle: document.getElementById("runToggle"),
      saveConfig: document.getElementById("saveConfig"),
      clearLog: document.getElementById("clearLog"),
    };
    const summary = {
      status: document.getElementById("status"),
      project: document.getElementById("sum-project"),
      resource: document.getElementById("sum-resource"),
      mode: document.getElementById("sum-mode"),
      languages: document.getElementById("sum-languages"),
      ids: document.getElementById("sum-ids"),
      targets: document.getElementById("sum-targets"),
      targetCount: document.getElementById("targetCount"),
      log: document.getElementById("log"),
    };
    const resourceFilesView = document.getElementById("resourceFiles");
    const doctorResults = document.getElementById("doctorResults");
    const resourceScanMeta = document.getElementById("resourceScanMeta");
    let scannedResources = [];
    let selectedResourceFiles = new Set();
    let lastLogId = 0;
    let isRunning = false;

    function parseTargets() {
      const seen = new Set();
      const result = [];
      for (const line of fields.targetLanguages.value.split(/\r?\n/)) {
        for (const part of line.split(",")) {
          const value = part.trim();
          if (value && !seen.has(value)) {
            seen.add(value);
            result.push(value);
          }
        }
      }
      return result;
    }

    function selectedResources() {
      return scannedResources
        .map(item => item.path)
        .filter(path => selectedResourceFiles.has(path));
    }

    function renderResourceFiles() {
      resourceFilesView.textContent = "";
      if (!scannedResources.length) {
        const empty = document.createElement("div");
        empty.className = "resource-empty";
        empty.textContent = "No translatable resource XML files found.";
        resourceFilesView.appendChild(empty);
        return;
      }
      for (const item of scannedResources) {
        const label = document.createElement("label");
        label.className = "resource-option";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = selectedResourceFiles.has(item.path);
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) selectedResourceFiles.add(item.path);
          else selectedResourceFiles.delete(item.path);
          resourceScanMeta.textContent = `${scannedResources.length} file(s) found · ${selectedResourceFiles.size} selected`;
          updateSummary();
        });
        const name = document.createElement("div");
        const title = document.createElement("div");
        title.textContent = item.name;
        const detail = document.createElement("div");
        detail.className = "resource-detail";
        detail.textContent = `${item.module} · ${item.path} · ${item.kinds.join(", ")}`;
        name.append(title, detail);
        const count = document.createElement("div");
        count.className = "meta";
        count.textContent = `${item.count} item${item.count === 1 ? "" : "s"}`;
        label.append(checkbox, name, count);
        resourceFilesView.appendChild(label);
      }
    }

    async function scanResources(autoSelect = true) {
      const projectRoot = fields.projectRoot.value.trim();
      if (!projectRoot) return;
      resourceScanMeta.textContent = "Scanning...";
      const data = await api("/api/scan-resources", {
        method: "POST",
        body: JSON.stringify({project_root: projectRoot}),
      });
      scannedResources = data.resources || [];
      const available = new Set(scannedResources.map(item => item.path));
      selectedResourceFiles = new Set([...selectedResourceFiles].filter(path => available.has(path)));
      if (autoSelect && selectedResourceFiles.size === 0) {
        selectedResourceFiles = new Set(scannedResources.map(item => item.path));
      }
      renderResourceFiles();
      resourceScanMeta.textContent = `${scannedResources.length} file(s) found · ${selectedResourceFiles.size} selected`;
      updateSummary();
    }

    function payload() {
      return {
        project_root: fields.projectRoot.value.trim(),
        resource_paths: selectedResources(),
        engine: fields.engine.value,
        workers: fields.workers.value.trim(),
        ids: fields.ids.value.trim(),
        skip_translated: fields.skipTranslated.checked,
        word_by_word: fields.wordByWord.checked,
        source_language: fields.sourceLanguage.value.trim(),
        target_languages: fields.targetLanguages.value,
      };
    }

    function updateSummary(statusText) {
      const targets = parseTargets();
      summary.project.textContent = fields.projectRoot.value.trim() || "-";
      const resources = selectedResources();
      summary.resource.textContent = `${resources.length ? resources.join(", ") : "No resource selected"} | ${fields.engine.value} | workers ${fields.workers.value.trim() || "-"}`;
      summary.mode.textContent = `Selected files | skip translated ${fields.skipTranslated.checked ? "On" : "Off"} | ${fields.wordByWord.checked ? "Word by word" : "Sentence"}`;
      summary.languages.textContent = `${fields.sourceLanguage.value.trim() || "-"} -> ${targets.length} target(s)`;
      summary.ids.textContent = fields.ids.value.trim() || "All resource IDs";
      renderTargets(targets);
      summary.targetCount.textContent = `${targets.length} target language${targets.length === 1 ? "" : "s"}`;
      if (statusText) summary.status.textContent = statusText;
    }

    function renderTargets(targets) {
      summary.targets.textContent = "";
      if (!targets.length) {
        const empty = document.createElement("div");
        empty.className = "target-empty";
        empty.textContent = "No target languages configured.";
        summary.targets.appendChild(empty);
        return;
      }

      const list = document.createElement("div");
      list.className = "target-list";
      for (const target of targets) {
        const chip = document.createElement("span");
        chip.className = "target-chip";
        chip.textContent = target;
        list.appendChild(chip);
      }
      summary.targets.appendChild(list);
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: {"Content-Type": "application/json"},
        ...options,
      });
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `Request failed: ${path}`);
      }
      return data;
    }

    async function loadConfig() {
      const data = await api("/api/config");
      fields.projectRoot.value = data.default_project_root;
      fields.sourceLanguage.value = data.config.source_language || "en";
      fields.targetLanguages.value = (data.config.target_languages || []).join("\n");
      updateSummary();
    }

    async function saveConfig() {
      const data = await api("/api/save-config", {
        method: "POST",
        body: JSON.stringify({
          source_language: fields.sourceLanguage.value,
          target_languages: fields.targetLanguages.value,
        }),
      });
      updateSummary(`Saved config (${data.target_count} target languages)`);
    }

    function renderDoctor(checks) {
      doctorResults.textContent = "";
      for (const check of checks || []) {
        const row = document.createElement("div");
        row.className = "doctor-row";
        const name = document.createElement("div");
        name.textContent = check.name;
        const status = document.createElement("div");
        status.className = `doctor-status ${check.status}`;
        status.textContent = check.status;
        const detail = document.createElement("div");
        detail.className = "meta";
        detail.textContent = check.detail || "";
        row.append(name, status, detail);
        doctorResults.appendChild(row);
      }
    }

    async function runDoctor() {
      buttons.runDoctor.disabled = true;
      doctorResults.textContent = "Checking environment...";
      try {
        const data = await api("/api/doctor", {method: "POST", body: JSON.stringify(payload())});
        renderDoctor(data.checks);
        updateSummary(data.ok ? "Doctor: ready" : "Doctor: issues found");
        return data.ok;
      } finally {
        buttons.runDoctor.disabled = false;
      }
    }

    async function start() {
      buttons.runToggle.disabled = true;
      await api("/api/start", {method: "POST", body: JSON.stringify(payload())});
      updateSummary("Running translation...");
      syncRunButton(true);
    }

    async function stop() {
      buttons.runToggle.disabled = true;
      await api("/api/stop", {method: "POST", body: "{}"});
      updateSummary("Stopping translator process...");
      syncRunButton(true);
    }

    function syncRunButton(running) {
      isRunning = running;
      buttons.runToggle.disabled = false;
      buttons.runToggle.textContent = running ? "Stop" : "Start";
      buttons.runToggle.classList.toggle("primary", !running);
      buttons.runToggle.classList.toggle("danger", running);
    }

    async function toggleRun() {
      if (isRunning) {
        await stop();
      } else {
        await start();
      }
    }

    async function chooseFolder() {
      const data = await api("/api/choose-folder", {method: "POST", body: "{}"});
      fields.projectRoot.value = data.path;
      await scanResources(true);
      await runDoctor();
      updateSummary();
      await scanResources(true);
    }

    async function clearLog() {
      await api("/api/clear-logs", {method: "POST", body: "{}"});
      lastLogId = 0;
      summary.log.textContent = "";
    }

    function addLog(item) {
      const line = document.createElement("div");
      line.className = `line ${item.level}`;
      line.textContent = `[${item.time}] ${item.message}`;
      summary.log.appendChild(line);
      summary.log.scrollTop = summary.log.scrollHeight;
      lastLogId = Math.max(lastLogId, item.id);
    }

    async function pollLogs() {
      try {
        const data = await api(`/api/logs?after=${lastLogId}`);
        for (const item of data.logs) addLog(item);
        summary.status.textContent = data.status;
        syncRunButton(data.running);
      } catch (error) {
        summary.status.textContent = error.message;
        buttons.runToggle.disabled = false;
      } finally {
        setTimeout(pollLogs, 700);
      }
    }

    for (const field of Object.values(fields)) {
      field.addEventListener("input", () => updateSummary());
      field.addEventListener("change", () => updateSummary());
    }
    fields.projectRoot.addEventListener("change", () => scanResources(true).catch(error => alert(error.message)));
    buttons.reloadConfig.addEventListener("click", () => loadConfig().catch(error => alert(error.message)));
    buttons.runDoctor.addEventListener("click", () => runDoctor().catch(error => alert(error.message)));
    buttons.saveConfig.addEventListener("click", () => saveConfig().catch(error => alert(error.message)));
    buttons.runToggle.addEventListener("click", () => toggleRun().catch(error => {
      buttons.runToggle.disabled = false;
      alert(error.message);
    }));
    buttons.chooseFolder.addEventListener("click", () => chooseFolder().catch(error => alert(error.message)));
    buttons.clearLog.addEventListener("click", () => clearLog().catch(error => alert(error.message)));
    fields.engine.addEventListener("change", () => runDoctor().catch(() => {}));

    loadConfig().catch(error => alert(error.message));
    pollLogs();
  </script>
</body>
</html>
"""


def parse_args():
    parser = argparse.ArgumentParser(description="Android string translator GUI")
    parser.add_argument("--port", type=int, default=0, help="Local web UI port. Default: random free port")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    return parser.parse_args()


def main():
    if not TRANSLATE_SCRIPT.exists():
        raise FileNotFoundError(f"Cannot find {TRANSLATE_SCRIPT}")

    args = parse_args()
    port = args.port or find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), GuiHandler)
    url = f"http://127.0.0.1:{port}"

    print(f"Android String Translator GUI: {url}")
    print("Press Ctrl+C to stop the local UI server.")
    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_translation()
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
