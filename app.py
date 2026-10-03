import ast
import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

BASE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = Path(os.getenv("SCRIPT_PATH", str(BASE_DIR / "script.py"))).expanduser()
SCRIPT_CWD = Path(os.getenv("SCRIPT_CWD", str(SCRIPT_PATH.parent))).expanduser()
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "scripts"))).expanduser()
ACTIVE_STATE_FILE = Path(os.getenv("ACTIVE_STATE_FILE", str(BASE_DIR / ".active_script"))).expanduser()
PYTHON_BIN = os.getenv("PYTHON_BIN", sys.executable)
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))
MAX_OUTPUT_CHARS = int(os.getenv("MAX_OUTPUT_CHARS", "120000"))
AUTO_INSTALL_DEPS = os.getenv("AUTO_INSTALL_DEPS", "1").lower() not in {"0", "false", "no"}
PIP_INSTALL_TIMEOUT = int(os.getenv("PIP_INSTALL_TIMEOUT", "180"))
KEEP_SCRIPT_ALIVE = os.getenv("KEEP_SCRIPT_ALIVE", "0").lower() not in {"0", "false", "no"}
RESTART_DELAY = int(os.getenv("RESTART_DELAY", "5"))
PACKAGE_ALIASES = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "fitz": "PyMuPDF",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
}

state_lock = threading.RLock()
process = None
run_state = "idle"
started_at = None
finished_at = None
current_run_id = None
exit_code = None
last_script_path = None
last_script_cwd = None
stdout_buffer = deque(maxlen=MAX_OUTPUT_CHARS)
stderr_buffer = deque(maxlen=MAX_OUTPUT_CHARS)
history = deque(maxlen=MAX_HISTORY)
active_script_path = SCRIPT_PATH
try:
    saved_script = Path(ACTIVE_STATE_FILE.read_text(encoding="utf-8").strip())
    if saved_script.is_file():
        active_script_path = saved_script
except (OSError, ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def active_script_cwd():
    return active_script_path.parent if active_script_path.parent == UPLOAD_DIR else SCRIPT_CWD


def available_scripts():
    files = []
    if SCRIPT_PATH.is_file():
        files.append(str(SCRIPT_PATH))
    if UPLOAD_DIR.is_dir():
        files.extend(str(path) for path in sorted(UPLOAD_DIR.glob("*.py")))
    return list(dict.fromkeys(files))


def missing_packages(script_path):
    try:
        tree = ast.parse(script_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise RuntimeError(f"Cannot inspect imports: {exc}") from exc
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    stdlib = getattr(sys, "stdlib_module_names", set())
    missing = []
    for module in sorted(imported):
        if module in stdlib or (script_path.parent / f"{module}.py").is_file():
            continue
        if importlib.util.find_spec(module) is None:
            missing.append(PACKAGE_ALIASES.get(module, module))
    return list(dict.fromkeys(missing))


def install_missing_dependencies(script_path):
    packages = missing_packages(script_path)
    if not packages:
        stdout_buffer.extend("[deps] All imported packages are already installed.\n")
        return
    stdout_buffer.extend(f"[deps] Installing: {', '.join(packages)}\n")
    for package in packages:
        result = subprocess.run(
            [PYTHON_BIN, "-m", "pip", "install", "--disable-pip-version-check", package],
            capture_output=True,
            text=True,
            timeout=PIP_INSTALL_TIMEOUT,
            check=False,
        )
        if result.stdout:
            stdout_buffer.extend(f"[pip:{package}] {result.stdout}\n")
        if result.stderr:
            stderr_buffer.extend(f"[pip:{package}] {result.stderr}\n")
        if result.returncode != 0:
            raise RuntimeError(f"pip install failed for {package} (exit {result.returncode})")
    stdout_buffer.extend("[deps] Dependency setup complete.\n")


def spawn_script_child(script_path, script_cwd, run_id, started):
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    child = subprocess.Popen(
        [PYTHON_BIN, "-u", str(script_path)],
        cwd=str(script_cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
        env=child_env,
    )
    stdout_buffer.extend(f"[script] Running uploaded file: {script_path.name}\n")
    threading.Thread(target=drain_stream, args=(child.stdout, stdout_buffer), daemon=True).start()
    threading.Thread(target=drain_stream, args=(child.stderr, stderr_buffer), daemon=True).start()
    threading.Thread(target=process_watcher, args=(child, run_id, started), daemon=True).start()
    return child


def drain_stream(stream, buffer):
    try:
        for line in iter(stream.readline, ""):
            with state_lock:
                buffer.extend(line)
    finally:
        stream.close()


def process_watcher(child, run_id, started):
    global process, run_state, finished_at, exit_code
    code = child.wait()
    with state_lock:
        exit_code = code
        finished_at = utc_now()
        if run_state == "stopping":
            final_state = "stopped"
        elif code == 0:
            final_state = "completed"
        else:
            final_state = "failed"
        run_state = final_state
        history.appendleft(
            {
                "id": run_id,
                "started_at": started,
                "finished_at": finished_at,
                "status": final_state,
                "exit_code": code,
            }
        )
        process = None
        if KEEP_SCRIPT_ALIVE and final_state in {"completed", "failed"}:
            run_state = "restarting"
            threading.Thread(target=restart_after_exit, daemon=True).start()


def restart_after_exit():
    global process, run_state, started_at, finished_at, current_run_id, exit_code
    time.sleep(RESTART_DELAY)
    with state_lock:
        if run_state != "restarting" or last_script_path is None or last_script_cwd is None:
            return
        started_at = utc_now()
        finished_at = None
        exit_code = None
        current_run_id = f"run-{int(time.time() * 1000)}"
        stdout_buffer.clear()
        stderr_buffer.clear()
        try:
            process = spawn_script_child(last_script_path, last_script_cwd, current_run_id, started_at)
            run_state = "running"
        except OSError as exc:
            process = None
            run_state = "failed"
            stderr_buffer.extend(f"[restart] {exc}\n")


def snapshot():
    with state_lock:
        is_running = (process is not None and process.poll() is None) or run_state in {"installing", "restarting"}
        return {
            "status": run_state,
            "running": is_running,
            "script_path": str(active_script_path),
            "script_cwd": str(active_script_cwd()),
            "available_scripts": available_scripts(),
            "python_bin": PYTHON_BIN,
            "auto_install_deps": AUTO_INSTALL_DEPS,
            "keep_script_alive": KEEP_SCRIPT_ALIVE,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "run_id": current_run_id,
            "stdout": "".join(stdout_buffer),
            "stderr": "".join(stderr_buffer),
            "history": list(history),
        }


@app.get("/")
def index():
    return render_template_string(PAGE_HTML)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "pybutton", "status": run_state})


@app.get("/manus-routes.json")
def routes_manifest():
    return jsonify({"routes": [{"path": "/", "title": "Python Button Runner"}]})


@app.get("/api/status")
def api_status():
    return jsonify(snapshot())


@app.post("/api/upload")
def api_upload():
    global active_script_path
    with state_lock:
        if process is not None and process.poll() is None:
            return jsonify({"ok": False, "error": "Stop the active run before uploading a new script."}), 409
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"ok": False, "error": "Choose a .py file first."}), 400
        if not uploaded.filename.lower().endswith(".py"):
            return jsonify({"ok": False, "error": "Only Python .py files are accepted."}), 400
        filename = secure_filename(uploaded.filename)
        if not filename or filename == ".py":
            return jsonify({"ok": False, "error": "Invalid Python filename."}), 400
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        destination = UPLOAD_DIR / filename
        uploaded.save(destination)
        active_script_path = destination
        ACTIVE_STATE_FILE.write_text(str(destination), encoding="utf-8")
        return jsonify({"ok": True, "script_path": str(active_script_path)})


@app.post("/api/run")
def api_run():
    global process, run_state, started_at, finished_at, current_run_id, exit_code, last_script_path, last_script_cwd
    with state_lock:
        if (process is not None and process.poll() is None) or run_state in {"installing", "stopping", "restarting"}:
            return jsonify({"ok": False, "error": "A run is already active."}), 409
        script_path = active_script_path
        script_cwd = active_script_cwd()
        last_script_path = script_path
        last_script_cwd = script_cwd
        if not script_path.is_file():
            return jsonify({"ok": False, "error": f"Script not found: {script_path}"}), 400
        if not script_cwd.is_dir():
            return jsonify({"ok": False, "error": f"Working directory not found: {script_cwd}"}), 400

        stdout_buffer.clear()
        stderr_buffer.clear()
        started_at = utc_now()
        finished_at = None
        exit_code = None
        current_run_id = f"run-{int(time.time() * 1000)}"
        run_state = "installing" if AUTO_INSTALL_DEPS else "running"
        try:
            if AUTO_INSTALL_DEPS:
                install_missing_dependencies(script_path)
            run_state = "running"
            process = spawn_script_child(script_path, script_cwd, current_run_id, started_at)
        except OSError as exc:
            process = None
            run_state = "failed"
            finished_at = utc_now()
            history.appendleft({"id": current_run_id, "started_at": started_at, "finished_at": finished_at, "status": "failed", "exit_code": 1})
            return jsonify({"ok": False, "error": str(exc)}), 500
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            process = None
            run_state = "failed"
            finished_at = utc_now()
            stderr_buffer.extend(f"[deps] {exc}\n")
            history.appendleft({"id": current_run_id, "started_at": started_at, "finished_at": finished_at, "status": "failed", "exit_code": 1})
            return jsonify({"ok": False, "error": str(exc)}), 500

        run_id = current_run_id
        started = started_at
        threading.Thread(target=drain_stream, args=(process.stdout, stdout_buffer), daemon=True).start()
        threading.Thread(target=drain_stream, args=(process.stderr, stderr_buffer), daemon=True).start()
        threading.Thread(target=process_watcher, args=(process, run_id, started), daemon=True).start()
        return jsonify({"ok": True, "run_id": run_id, "status": run_state})


@app.post("/api/stop")
def api_stop():
    global run_state
    with state_lock:
        if run_state == "restarting":
            run_state = "stopping"
            return jsonify({"ok": True, "status": "stopping"})
        if process is None or process.poll() is not None:
            return jsonify({"ok": False, "error": "No active run."}), 409
        run_state = "stopping"
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        return jsonify({"ok": True, "status": "stopping"})


PAGE_HTML = r"""
<!doctype html>
<html lang="my">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PYBUTTON · Python Runner</title>
  <style>
    :root{--bg:#071116;--panel:#0c1a20;--panel2:#10232a;--line:#1c3942;--text:#e7f4f4;--muted:#85a6a8;--cyan:#5eead4;--lime:#b7f36b;--amber:#f5bf5b;--coral:#ff8066;--shadow:0 20px 60px rgba(0,0,0,.24)}
    *{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at 75% 0%,#12343a 0,#071116 42%);color:var(--text);font-family:system-ui,-apple-system,"Noto Sans Myanmar",sans-serif;min-height:100vh}
    .shell{display:grid;grid-template-columns:260px 1fr;min-height:100vh}.rail{border-right:1px solid var(--line);padding:30px 22px;display:flex;flex-direction:column;gap:30px;background:rgba(5,14,18,.4)}
    .brand{display:flex;align-items:center;gap:11px}.mark{height:38px;width:38px;border:1px solid var(--cyan);color:var(--cyan);display:grid;place-items:center;border-radius:11px;font:700 17px ui-monospace,monospace;box-shadow:0 0 24px rgba(94,234,212,.16)}.brand b{letter-spacing:.16em;font-size:13px}.brand small{display:block;color:var(--muted);font-size:10px;letter-spacing:.08em;margin-top:3px}
    .rail-note{margin-top:auto;border:1px solid var(--line);border-radius:14px;padding:14px;color:var(--muted);font-size:12px;line-height:1.7;background:rgba(16,35,42,.5)}.rail-note strong{color:var(--text);display:block;margin-bottom:4px}
    main{max-width:1400px;width:100%;padding:42px 48px 50px;margin:0 auto}.eyebrow{color:var(--cyan);font:600 11px ui-monospace,monospace;letter-spacing:.18em;text-transform:uppercase}.heading{display:flex;justify-content:space-between;gap:24px;align-items:end;margin-bottom:28px}.heading h1{font-size:clamp(30px,4vw,52px);line-height:1.05;margin:9px 0 0;letter-spacing:-.04em}.heading p{color:var(--muted);margin:12px 0 0;max-width:630px}.signal{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:12px;white-space:nowrap}.dot{width:9px;height:9px;border-radius:50%;background:var(--lime);box-shadow:0 0 16px var(--lime)}.dot.active{background:var(--cyan);box-shadow:0 0 16px var(--cyan);animation:pulse 1.2s infinite}.dot.warn{background:var(--amber);box-shadow:0 0 16px var(--amber)}.dot.error{background:var(--coral);box-shadow:0 0 16px var(--coral)}@keyframes pulse{50%{opacity:.4;transform:scale(.75)}}
    .grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(280px,.65fr);gap:18px}.card{background:linear-gradient(145deg,rgba(16,35,42,.95),rgba(9,24,29,.95));border:1px solid var(--line);border-radius:19px;box-shadow:var(--shadow)}.card-head{padding:20px 22px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:14px}.card-title{font-weight:700;font-size:15px}.card-kicker{color:var(--muted);font-size:11px;margin-top:4px}.run-card{min-height:220px}.run-body{padding:24px 22px}.status-line{display:flex;align-items:center;gap:14px}.status-word{font-size:34px;font-weight:750;letter-spacing:-.04em}.status-copy{color:var(--muted);font-size:13px}.actions{display:flex;gap:10px;margin-top:24px}.btn{border:0;border-radius:11px;padding:12px 18px;color:#06211e;background:var(--cyan);font-weight:750;cursor:pointer;transition:.18s transform,.18s opacity,.18s background}.btn:hover{transform:translateY(-1px)}.btn:disabled{opacity:.38;cursor:not-allowed;transform:none}.btn.stop{background:transparent;color:var(--coral);border:1px solid rgba(255,128,102,.5)}.btn.stop:hover{background:rgba(255,128,102,.08)}.config{display:grid;gap:12px}.kv{display:flex;justify-content:space-between;gap:20px;border-bottom:1px dashed rgba(133,166,168,.22);padding-bottom:10px}.kv:last-child{border:0;padding:0}.kv span:first-child{color:var(--muted);font-size:12px}.kv code{font:12px ui-monospace,monospace;color:var(--text);text-align:right;overflow-wrap:anywhere}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}.metric{padding:13px;border:1px solid var(--line);border-radius:12px;background:rgba(7,17,22,.35)}.metric label{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.1em}.metric strong{display:block;margin-top:7px;font:600 12px ui-monospace,monospace;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.console{margin-top:18px}.stream{padding:18px 22px}.stream + .stream{border-top:1px solid var(--line)}.stream-head{display:flex;justify-content:space-between;color:var(--muted);font:11px ui-monospace,monospace;margin-bottom:9px}.stream-head b{color:var(--cyan);font-weight:500}.stream.stderr .stream-head b{color:var(--coral)}pre{margin:0;min-height:100px;max-height:270px;overflow:auto;white-space:pre-wrap;word-break:break-word;color:#b9d6d2;font:12px/1.65 ui-monospace,SFMono-Regular,monospace;background:#061014;border:1px solid rgba(133,166,168,.13);border-radius:12px;padding:14px}.stderr pre{color:#ffc1b5}.history{margin-top:18px}.history-list{padding:8px 22px 16px}.history-row{display:grid;grid-template-columns:1.2fr .8fr .5fr;gap:12px;align-items:center;padding:14px 0;border-bottom:1px solid rgba(133,166,168,.13);font-size:12px}.history-row:last-child{border:0}.history-row code{font:11px ui-monospace,monospace;color:var(--muted)}.badge{justify-self:start;border-radius:99px;padding:5px 9px;font:600 10px ui-monospace,monospace;text-transform:uppercase}.badge.completed{color:var(--lime);background:rgba(183,243,107,.1)}.badge.failed{color:var(--coral);background:rgba(255,128,102,.1)}.badge.stopped{color:var(--amber);background:rgba(245,191,91,.1)}.empty{padding:28px 0;color:var(--muted);font-size:13px;text-align:center}.toast{position:fixed;right:22px;bottom:22px;max-width:360px;background:#102a31;border:1px solid var(--cyan);color:var(--text);padding:12px 15px;border-radius:12px;box-shadow:var(--shadow);font-size:12px;opacity:0;transform:translateY(8px);pointer-events:none;transition:.2s}.toast.show{opacity:1;transform:none}@media(max-width:900px){.shell{grid-template-columns:1fr}.rail{border-right:0;border-bottom:1px solid var(--line);padding:20px}.rail-note{display:none}main{padding:30px 18px}.heading{display:block}.signal{margin-top:18px}.grid{grid-template-columns:1fr}.metrics{grid-template-columns:1fr 1fr 1fr}}@media(max-width:500px){.status-word{font-size:27px}.metrics{grid-template-columns:1fr}.history-row{grid-template-columns:1fr}.badge{justify-self:start}}
    .upload-card{margin-bottom:18px}.upload-form{display:grid;gap:11px}.upload-form input[type=file]{width:100%;color:var(--muted);font-size:12px;border:1px dashed var(--line);border-radius:11px;padding:10px;background:rgba(7,17,22,.35)}.upload-form input[type=file]::file-selector-button{border:0;border-radius:8px;padding:8px 10px;margin-right:8px;background:var(--panel2);color:var(--cyan);cursor:pointer}.upload-form .btn{width:100%}.upload-help{color:var(--muted);font-size:11px;line-height:1.6}
  </style>
</head>
<body>
  <div class="shell">
    <aside class="rail">
      <div class="brand"><div class="mark">&gt;_</div><div><b>PYBUTTON</b><small>VPS SCRIPT CONTROL</small></div></div>
      <div class="rail-note"><strong>One run at a time.</strong>ဒီ panel က server ပေါ်မှာ သတ်မှတ်ထားတဲ့ Python script တစ်ခုကိုသာ run ပေးပါတယ်။ Browser ကနေ command အသစ်ထည့်လို့မရပါ။</div>
    </aside>
    <main>
      <div class="heading"><div><div class="eyebrow">Private VPS utility · ready mode</div><h1>Python runner,<br><span style="color:var(--cyan)">one click away.</span></h1><p>Script ကို run/stop လုပ်ပြီး output နဲ့ နောက်ဆုံး run မှတ်တမ်းကို တစ်နေရာတည်းမှာ ကြည့်ပါ။</p></div><div class="signal"><i id="signalDot" class="dot"></i><span id="signalText">Connecting…</span></div></div>
      <section class="grid">
        <div>
          <div class="card run-card"><div class="card-head"><div><div class="card-title">Current run</div><div class="card-kicker">လက်ရှိ process အခြေအနေ</div></div><div id="runId" class="card-kicker">—</div></div><div class="run-body"><div class="status-line"><i id="statusDot" class="dot"></i><div><div id="statusWord" class="status-word">IDLE</div><div id="statusCopy" class="status-copy">Ready when you are.</div></div></div><div class="actions"><button id="runBtn" class="btn" onclick="runScript()">▶ Run script</button><button id="stopBtn" class="btn stop" onclick="stopScript()" disabled>■ Stop</button></div><div class="metrics"><div class="metric"><label>Started</label><strong id="started">—</strong></div><div class="metric"><label>Finished</label><strong id="finished">—</strong></div><div class="metric"><label>Exit code</label><strong id="exitCode">—</strong></div></div></div></div>
          <div class="card console"><div class="card-head"><div><div class="card-title">Process output</div><div class="card-kicker">stdout / stderr · live refresh</div></div><div id="outputCount" class="card-kicker">0 chars</div></div><div class="stream"><div class="stream-head"><b>STDOUT</b><span>normal output</span></div><pre id="stdout">No output yet.</pre></div><div class="stream stderr"><div class="stream-head"><b>STDERR</b><span>errors & diagnostics</span></div><pre id="stderr">No errors.</pre></div></div>
        </div>
        <div>
          <div class="card upload-card"><div class="card-head"><div><div class="card-title">Add Python file</div><div class="card-kicker">ကိုယ့် .py ဖိုင်ကို ထည့်ပြီး Run လုပ်ပါ</div></div></div><div class="run-body"><form id="uploadForm" class="upload-form"><input id="scriptFile" type="file" name="file" accept=".py,text/x-python" required><button class="btn" type="submit">＋ Upload & use this file</button></form><div class="upload-help">Upload လုပ်လိုက်တဲ့ file ကို active script အဖြစ် အလိုအလျောက်ရွေးပေးပါမယ်။ 2 MB အောက် .py ဖိုင်သာလက်ခံပါတယ်။</div></div></div>
          <div class="card"><div class="card-head"><div><div class="card-title">Runner config</div><div class="card-kicker">server-side configuration</div></div></div><div class="run-body config"><div class="kv"><span>Active script</span><code id="scriptPath">—</code></div><div class="kv"><span>Working dir</span><code id="scriptCwd">—</code></div><div class="kv"><span>Python</span><code id="pythonBin">—</code></div><div class="kv"><span>Files</span><code id="fileCount">—</code></div><div class="kv"><span>Auto pip</span><code id="autoDeps">—</code></div><div class="kv"><span>Keep alive</span><code id="keepAlive">—</code></div><div class="kv"><span>Mode</span><code>single instance</code></div></div></div>
          <div class="card history"><div class="card-head"><div><div class="card-title">Recent runs</div><div class="card-kicker">နောက်ဆုံး run မှတ်တမ်း</div></div></div><div id="history" class="history-list"><div class="empty">No runs yet.</div></div></div>
        </div>
      </section>
    </main>
  </div><div id="toast" class="toast"></div>
  <script>
    const $ = id => document.getElementById(id);
    const fmt = value => value ? new Date(value).toLocaleString([], {month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
    function esc(value){return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));}
    function toast(message){$('toast').textContent=message;$('toast').classList.add('show');setTimeout(()=>$('toast').classList.remove('show'),3000)}
    function render(data){
      const active = data.running; const state = data.status || 'idle';
      $('statusWord').textContent = state.toUpperCase(); $('runId').textContent = data.run_id || '—'; $('started').textContent=fmt(data.started_at); $('finished').textContent=fmt(data.finished_at); $('exitCode').textContent=data.exit_code ?? '—';
      $('stdout').textContent=data.stdout || 'No output yet.'; $('stderr').textContent=data.stderr || 'No errors.'; $('outputCount').textContent=((data.stdout||'').length+(data.stderr||'').length)+' chars';
      $('scriptPath').textContent=data.script_path; $('scriptCwd').textContent=data.script_cwd; $('pythonBin').textContent=data.python_bin; $('fileCount').textContent=(data.available_scripts||[]).length+' file(s)'; $('autoDeps').textContent=data.auto_install_deps?'enabled':'disabled'; $('keepAlive').textContent=data.keep_script_alive?'enabled':'disabled';
      $('runBtn').disabled=active; $('stopBtn').disabled=!active;
      const dot=$('statusDot'); dot.className='dot '+(active?'active':(state==='failed'?'error':(state==='stopping'?'warn':'')));
      $('statusCopy').textContent=state==='installing'?'Installing missing packages…':(state==='restarting'?'Restarting after exit…':(active?'Process is running…':(state==='completed'?'Completed cleanly.':(state==='failed'?'Exited with an error.':(state==='stopped'?'Stopped by you.':'Ready when you are.')))));
      $('signalText').textContent='Live · '+state; $('signalDot').className='dot '+(active?'active':(state==='failed'?'error':''));
      const rows=data.history||[]; $('history').innerHTML=rows.length?rows.map(r=>`<div class="history-row"><div><code>${esc(fmt(r.started_at))}</code><br><span style="color:var(--muted)">${esc(fmt(r.finished_at))}</span></div><span class="badge ${esc(r.status)}">${esc(r.status)}</span><code>exit ${esc(r.exit_code)}</code></div>`).join(''):'<div class="empty">No runs yet.</div>';
    }
    async function refresh(){try{const response=await fetch('/api/status',{cache:'no-store'});render(await response.json())}catch(e){$('signalText').textContent='Offline';$('signalDot').className='dot error'}}
    async function runScript(){const r=await fetch('/api/run',{method:'POST'});const d=await r.json();if(!r.ok)toast(d.error||'Could not start script.');else toast('Script started.');refresh()}
    async function stopScript(){const r=await fetch('/api/stop',{method:'POST'});const d=await r.json();if(!r.ok)toast(d.error||'Could not stop script.');else toast('Stop requested.');refresh()}
    $('uploadForm').addEventListener('submit',async event=>{event.preventDefault();const file=$('scriptFile').files[0];if(!file)return;const body=new FormData();body.append('file',file);const r=await fetch('/api/upload',{method:'POST',body});const d=await r.json();if(!r.ok)toast(d.error||'Upload failed.');else{toast('Python file uploaded and selected.');$('scriptFile').value='';}refresh()});
    refresh();setInterval(refresh,1500);
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "3000"))
    app.run(host=host, port=port, debug=False, threaded=True)
