#!/usr/bin/env python3
"""colab-cli v2.1 — AI-agent-native CLI for Google Colab.

Fixes over v2.0:
- console: fixed stdin piping (uses exec+subprocess for shell commands)
- install: added --pip-args for custom pip flags
- auth: auto-refresh expired tokens
- exec_bg: background execution with progress polling
- gpu_switch: change GPU on running session
- notebook: create/save .ipynb files
- tunnel: persistent Cloudflare tunnel URL tracking

Wraps the official google-colab-cli with token-efficient output.
"""

import argparse, json, os, subprocess, sys, time, re
from pathlib import Path

COLAB_HOME = Path.home() / ".hermes" / "scripts" / "colab"
COLAB_HOME.mkdir(parents=True, exist_ok=True)
TUNNEL_FILE = COLAB_HOME / "tunnel_url.json"
TOKEN_FILE = Path.home() / ".config" / "colab-cli" / "token.json"

def _find_colab():
    import shutil
    for name in ("colab", "google-colab-cli"):
        p = shutil.which(name)
        if p: return p
    for p in [os.path.expanduser("~/.local/bin/colab"), str(Path(sys.prefix)/"bin"/"colab")]:
        if os.path.isfile(p): return p
    return None

COLAB = _find_colab()

def die(code, msg):
    print(json.dumps({"ok": False, "err": code, "msg": msg}))
    sys.exit(1)

def run_colab(args, timeout=120, stdin_data=None, env=None):
    cmd = [COLAB] + args
    merged_env = os.environ.copy()
    if env: merged_env.update(env)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=merged_env, input=stdin_data)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except FileNotFoundError:
        return "", "colab binary not found", -2

def parse_error(stdout, stderr):
    combined = (stdout + "\n" + stderr).lower()
    if "auth" in combined and ("expired" in combined or "invalid" in combined):
        return "auth-expired", "Token expired. See references/auth_flow.md to re-authenticate."
    if "unauthorized" in combined or "authentication" in combined:
        return "auth-expired", "Auth required."
    if "rate" in combined and "limit" in combined:
        return "rate-limit", "Rate limited."
    if "quota" in combined:
        return "quota-exceeded", "Quota exceeded."
    if "not found" in combined or "does not exist" in combined:
        return "not-found", "Not found."
    if "timeout" in combined or "timed out" in combined:
        return "timeout", "Timed out."
    if "gpu" in combined and ("unavailable" in combined or "not available" in combined):
        return "gpu-unavailable", "GPU unavailable."
    if "busy" in combined or "capacity" in combined:
        return "capacity", "Backend busy."
    if "network" in combined or "connection" in combined:
        return "network", "Network error."
    return None, None

def write_output(text, out_file=None, json_mode=False, extra=None):
    if out_file:
        path = Path(out_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        pointer = {"ok": True, "f": str(path.resolve()), "s": len(text.encode("utf-8")), "b": text.count("```") // 2}
        if extra: pointer.update(extra)
        print(json.dumps(pointer))
    elif json_mode:
        d = {"ok": True, "text": text}
        if extra: d.update(extra)
        print(json.dumps(d, ensure_ascii=False))
    else:
        print(text)

def check_rc(stdout, stderr, rc, fallback_code, fallback_msg):
    if rc != 0:
        err_code, err_msg = parse_error(stdout, stderr)
        if err_code:
            if err_code == "auth-expired":
                _try_refresh_token()
                die(err_code, err_msg)
            die(err_code, err_msg or stderr.strip())
        die(fallback_code, stderr.strip() or stdout.strip() or fallback_msg)

def _try_refresh_token():
    """Attempt to refresh OAuth2 token."""
    if not TOKEN_FILE.exists(): return False
    try:
        creds = json.loads(TOKEN_FILE.read_text())
        if not creds.get("refresh_token"): return False
        import urllib.request as ur
        data = urllib.parse.urlencode({
            "client_id": creds.get("client_id", ""),
            "client_secret": creds.get("client_secret", ""),
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = ur.Request("https://oauth2.googleapis.com/token", data=data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        resp = ur.urlopen(req, timeout=15)
        t = json.loads(resp.read())
        creds["token"] = t["access_token"]
        creds["expiry"] = (__import__("datetime").datetime.now(__import__("datetime").timezone.utc).timestamp() + t.get("expires_in", 3600))
        TOKEN_FILE.write_text(json.dumps(creds, indent=2))
        return True
    except Exception:
        return False

def format_sessions_list(stdout):
    sessions = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("─") or "Session" in line or "No active" in line: continue
        parts = line.split()
        if len(parts) >= 2: sessions.append({"name": parts[0], "status": parts[-1] if len(parts) > 2 else "active"})
    return sessions

def _resolve_session(args):
    """Auto-detect session if not specified."""
    if getattr(args, "session", None): return args.session
    stdout, stderr, rc = run_colab(["sessions"], timeout=15)
    if rc == 0:
        sessions = format_sessions_list(stdout)
        if len(sessions) == 1: return sessions[0]["name"]
    return None

# ─── Commands ──────────────────────────────────────────────────────────────────

def cmd_new(args):
    cargs = ["new"]
    if args.session: cargs.extend(["-s", args.session])
    if getattr(args, "gpu", None): cargs.extend(["--gpu", args.gpu])
    if getattr(args, "tpu", None): cargs.extend(["--tpu", args.tpu])
    stdout, stderr, rc = run_colab(cargs, timeout=120)
    check_rc(stdout, stderr, rc, "new-failed", "Failed to create session")
    name = args.session or stdout.strip().split()[-1]
    write_output(f"Session '{name}' created.\n{stdout.strip()}", args.out, args.json, {"session": name})

def cmd_exec(args):
    cargs = ["exec"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    if getattr(args, "file", None): cargs.extend(["-f", args.file])
    if getattr(args, "output_image", None): cargs.extend(["--output-image", args.output_image])
    t = getattr(args, "timeout", 30) or 30
    cargs.extend(["--timeout", str(t)])
    stdin_data = args.code if getattr(args, "code", None) else None
    stdout, stderr, rc = run_colab(cargs, timeout=t + 60, stdin_data=stdin_data)
    check_rc(stdout, stderr, rc, "exec-failed", "Execution failed")
    write_output(stdout, args.out, args.json)

def cmd_exec_bg(args):
    """Background execution — spawns a separate process, returns job ID for polling."""
    import uuid
    s = _resolve_session(args) or args.session
    job_id = str(uuid.uuid4())[:8]
    out_file = str(COLAB_HOME / f"bg_{job_id}.txt")
    timeout = getattr(args, "timeout", 600) or 600

    code = getattr(args, "code", None)
    if not code and not sys.stdin.isatty():
        code = sys.stdin.read().strip()
    if not code:
        die("exec-bg-no-code", "No code provided. Use --code or pipe via stdin.")

    # Write code to temp file and spawn process
    code_file = COLAB_HOME / f"bg_{job_id}_code.py"
    code_file.write_text(code)

    runner = COLAB_HOME / "bg_runner.py"
    if not runner.exists():
        runner.write_text("""#!/usr/bin/env python3
import subprocess, sys, json, os
job_id, session, code_file, out_file, timeout = sys.argv[1:7]
code = open(code_file).read()
cargs = ["exec", "-s", session, "--timeout", timeout]
r = subprocess.run([sys.executable.replace('python3','colab').replace('/bin/colab', 'colab')], 
    capture_output=True, text=True, timeout=int(timeout)+60, input=code)
# Actually just use subprocess with colab directly
""")

    # Spawn directly with subprocess.Popen
    cargs = ["exec"]
    if s: cargs.extend(["-s", s])
    cargs.extend(["--timeout", str(timeout)])
    
    pid = os.fork()
    if pid == 0:
        # Child: run exec and write output
        stdout, stderr, rc = run_colab(cargs, timeout=timeout + 60, stdin_data=code)
        Path(out_file).write_text(stdout if rc == 0 else (stderr or stdout or "BG exec failed"))
        os._exit(0)

    write_output("", args.out, args.json, {"job_id": job_id, "status": "running", "poll": f"exec_bg_poll {job_id}"})

def cmd_exec_bg_poll(args):
    """Poll a background execution job."""
    job_id = args.job_id
    out_file = COLAB_HOME / f"bg_{job_id}.txt"
    if not out_file.exists():
        write_output("", args.out, args.json, {"job_id": job_id, "status": "running"})
        return
    content = out_file.read_text()
    if content.startswith("{"):
        try:
            data = json.loads(content)
            write_output(content, args.out, args.json, {"job_id": job_id, "status": data.get("err", "done")})
        except: pass
    write_output(content, args.out, args.json, {"job_id": job_id, "status": "done"})

def cmd_run(args):
    cargs = ["run"]
    if getattr(args, "gpu", None): cargs.extend(["--gpu", args.gpu])
    if getattr(args, "tpu", None): cargs.extend(["--tpu", args.tpu])
    if getattr(args, "keep", None): cargs.append("--keep")
    if getattr(args, "file", None): cargs.append(args.file)
    if getattr(args, "args", None): cargs.extend(args.args)
    elif getattr(args, "code", None):
        import tempfile
        tmp = Path(tempfile.mktemp(suffix=".py"))
        tmp.write_text(args.code)
        cargs.append(str(tmp))
    stdout, stderr, rc = run_colab(cargs, timeout=300)
    check_rc(stdout, stderr, rc, "run-failed", "Job failed")
    write_output(stdout, args.out, args.json)

def cmd_sessions(args):
    stdout, stderr, rc = run_colab(["sessions"], timeout=30)
    check_rc(stdout, stderr, rc, "sessions-failed", stdout.strip())
    sessions_data = format_sessions_list(stdout)
    if args.json: print(json.dumps({"ok": True, "sessions": sessions_data}))
    else: write_output(stdout, args.out, args.json, {"sessions": sessions_data})

def cmd_status(args):
    cargs = ["status"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    stdout, stderr, rc = run_colab(cargs, timeout=30)
    check_rc(stdout, stderr, rc, "status-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_stop(args):
    s = _resolve_session(args) or args.session
    cargs = ["stop"]
    if s: cargs.extend(["-s", s])
    stdout, stderr, rc = run_colab(cargs, timeout=30)
    check_rc(stdout, stderr, rc, "stop-failed", stdout.strip())
    # Clear tunnel URL
    if TUNNEL_FILE.exists(): TUNNEL_FILE.unlink()
    write_output(f"Session '{s}' stopped." if s else "Session stopped.", args.out, args.json)

def cmd_gpu_switch(args):
    """Switch GPU type on a running session by stopping and recreating."""
    s = _resolve_session(args) or args.session
    new_gpu = args.gpu
    if not new_gpu: die("gpu-switch-no-gpu", "Specify --gpu target")
    # Get current status
    cargs = ["status"]
    if s: cargs.extend(["-s", s])
    stdout, _, _ = run_colab(cargs, timeout=15)

    # Stop current
    cargs = ["stop"]
    if s: cargs.extend(["-s", s])
    stdout2, stderr2, rc2 = run_colab(cargs, timeout=30)
    # Allow failure (session may already be stopping)

    # Create new with different GPU
    cargs = ["new", "-s", s, "--gpu", new_gpu]
    stdout3, stderr3, rc3 = run_colab(cargs, timeout=120)
    check_rc(stdout3, stderr3, rc3, "gpu-switch-failed", f"Failed to recreate with {new_gpu}")
    write_output(f"GPU switched to {new_gpu} on session '{s}'.\n{stdout3.strip()}", args.out, args.json, {"session": s, "gpu": new_gpu})

def cmd_ls(args):
    cargs = ["ls"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    if getattr(args, "path", None): cargs.append(args.path)
    stdout, stderr, rc = run_colab(cargs, timeout=30)
    check_rc(stdout, stderr, rc, "ls-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_upload(args):
    cargs = ["upload"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    cargs.extend([args.local, args.remote])
    stdout, stderr, rc = run_colab(cargs, timeout=60)
    check_rc(stdout, stderr, rc, "upload-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_download(args):
    cargs = ["download"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    cargs.extend([args.remote, args.local])
    stdout, stderr, rc = run_colab(cargs, timeout=60)
    check_rc(stdout, stderr, rc, "download-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_rm(args):
    cargs = ["rm"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    cargs.append(args.path)
    stdout, stderr, rc = run_colab(cargs, timeout=30)
    check_rc(stdout, stderr, rc, "rm-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_install(args):
    cargs = ["install"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    if getattr(args, "requirements", None): cargs.extend(["-r", args.requirements])
    pkgs = getattr(args, "packages", None)
    if pkgs: cargs.extend(pkgs)
    # pip-args: inject via exec if needed (official install doesn't support it)
    pip_args = getattr(args, "pip_args", None)
    if pip_args:
        # Fall through to exec-based install
        code = f"import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'pip', 'install', {json.dumps(' '.join(pkgs))}] + {json.dumps(pip_args.split())})"
        return cmd_exec_custom(args, s, code)
    stdout, stderr, rc = run_colab(cargs, timeout=120)
    check_rc(stdout, stderr, rc, "install-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_exec_custom(args, session, code):
    """Internal: execute custom Python code via exec."""
    cargs = ["exec"]
    if session: cargs.extend(["-s", session])
    t = getattr(args, "timeout", 120) or 120
    cargs.extend(["--timeout", str(t)])
    stdout, stderr, rc = run_colab(cargs, timeout=t + 60, stdin_data=code)
    check_rc(stdout, stderr, rc, "exec-failed", "Execution failed")
    write_output(stdout, args.out, args.json)

def cmd_console(args):
    """Shell commands via exec+subprocess (avoids tmux garbling)."""
    s = _resolve_session(args) or args.session
    cmd_str = getattr(args, "cmd", None)
    if not cmd_str:
        if not sys.stdin.isatty():
            cmd_str = sys.stdin.read().strip()
    if not cmd_str:
        die("console-no-input", "No command provided. Use --command 'CMD' or pipe via stdin.")
    # Use exec to run the shell command via subprocess
    code = f"import subprocess, json\nr=subprocess.run({json.dumps(cmd_str)}, shell=True, capture_output=True, text=True)\nif r.stdout: print(r.stdout, end='')\nif r.stderr: print(r.stderr, end='')"
    cargs = ["exec"]
    if s: cargs.extend(["-s", s])
    t = getattr(args, "timeout", 60) or 60
    cargs.extend(["--timeout", str(t)])
    stdout, stderr, rc = run_colab(cargs, timeout=t + 30, stdin_data=code)
    check_rc(stdout, stderr, rc, "console-failed", stdout.strip() or stderr.strip())
    write_output(stdout, args.out, args.json)

def cmd_edit(args):
    cargs = ["edit"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    cargs.append(args.remote_path)
    stdout, stderr, rc = run_colab(cargs, timeout=60)
    check_rc(stdout, stderr, rc, "edit-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_repl(args):
    cargs = ["repl"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    if getattr(args, "output_image", None): cargs.extend(["--output-image", args.output_image])
    stdin_data = args.code if getattr(args, "code", None) else None
    stdout, stderr, rc = run_colab(cargs, timeout=getattr(args, "timeout", 60), stdin_data=stdin_data)
    check_rc(stdout, stderr, rc, "repl-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_log(args):
    cargs = ["log"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    if getattr(args, "lines", None): cargs.extend(["-n", str(args.lines)])
    if getattr(args, "event_type", None): cargs.extend(["-t", args.event_type])
    if getattr(args, "output", None): cargs.extend(["-o", args.output])
    stdout, stderr, rc = run_colab(cargs, timeout=30)
    check_rc(stdout, stderr, rc, "log-failed", stdout.strip())
    write_output(stdout, args.out, args.json)


def cmd_logs(args):
    """Tail a file on the Colab VM, with optional --follow streaming."""
    s = _resolve_session(args) or args.session
    filepath = args.filepath
    lines = getattr(args, "lines", 50) or 50
    follow = getattr(args, "follow", False)
    
    if follow:
        import signal
        stop = [False]
        def on_sigint(sig, frame):
            stop[0] = True
        prev = signal.signal(signal.SIGINT, on_sigint)
        try:
            print(f"Streaming {filepath} (Ctrl+C to stop)...")
            last_size = 0
            while not stop[0]:
                code = f"import os\ntry:\n    size = os.path.getsize({json.dumps(filepath)})\n    if size > {last_size}:\n        with open({json.dumps(filepath)}) as f:\n            f.seek({last_size})\n            print(f.read(), end='')\n        print('\\n__SIZE__:' + str(size))\nexcept Exception as e:\n    print('__ERR__:' + str(e))"
                cargs = ["exec"]
                if s: cargs.extend(["-s", s])
                cargs.extend(["--timeout", "12"])
                stdout, stderr, rc = run_colab(cargs, timeout=15, stdin_data=code)
                if rc == 0 and stdout.strip():
                    # Split size marker from content
                    if '__SIZE__:' in stdout:
                        content, marker = stdout.rsplit('__SIZE__:', 1)
                        if content.strip():
                            print(content, end='', flush=True)
                        try:
                            last_size = int(marker.strip().split('\n')[0])
                        except ValueError:
                            pass
                time.sleep(1.0)
        finally:
            signal.signal(signal.SIGINT, prev)
            if stop[0]:
                print("\nStopped.")
    else:
        code = f"import subprocess, sys\nr = subprocess.run(['tail', '-n', str({lines}), {json.dumps(filepath)}], capture_output=True, text=True)\nprint(r.stdout, end='')\nif r.stderr: print(r.stderr, end='')"
        cargs = ["exec"]
        if s: cargs.extend(["-s", s])
        cargs.extend(["--timeout", "15"])
        stdout, stderr, rc = run_colab(cargs, timeout=20, stdin_data=code)
        check_rc(stdout, stderr, rc, "logs-failed", stdout.strip() or stderr.strip())
        write_output(stdout, args.out, args.json)


def cmd_check(args):
    """Pre-flight model check: test imports before full deployment."""
    s = _resolve_session(args) or args.session
    code = args.code
    if not code:
        die("check-no-code", "Provide --code with Python to test")
    
    cargs = ["exec"]
    if s: cargs.extend(["-s", s])
    t = getattr(args, "timeout", 60) or 60
    cargs.extend(["--timeout", str(t)])
    
    print(f"Running pre-flight check on {s or 'auto'}...")
    stdout, stderr, rc = run_colab(cargs, timeout=t + 30, stdin_data=code)
    
    if rc == 0:
        write_output(f"PASS:\n{stdout}", args.out, args.json, {"status": "pass"})
    else:
        write_output(f"FAIL:\n{stdout}\n{stderr}", args.out, args.json, {"status": "fail"})

def cmd_notebook(args):
    """Export session or execute notebook file."""
    s = _resolve_session(args) or args.session
    action = getattr(args, "nb_action", "export")
    if action == "export":
        # Export session as .ipynb
        out = getattr(args, "output", None) or f"/tmp/{s or 'colab'}_export.ipynb"
        cargs = ["log", "-o", out]
        if s: cargs.extend(["-s", s])
        stdout, stderr, rc = run_colab(cargs, timeout=30)
        check_rc(stdout, stderr, rc, "notebook-failed", stdout.strip())
        write_output(f"Notebook exported to: {out}\n{stdout.strip()}", args.out, args.json, {"notebook_path": out})
    elif action == "execute":
        # Execute an .ipynb file
        nb_file = getattr(args, "file", None)
        if not nb_file: die("notebook-no-file", "Specify --file NOTEBOOK.ipynb")
        cargs = ["exec", "-f", nb_file]
        if s: cargs.extend(["-s", s])
        stdout, stderr, rc = run_colab(cargs, timeout=120)
        check_rc(stdout, stderr, rc, "notebook-exec-failed", stdout.strip())
        write_output(stdout, args.out, args.json)

def cmd_url(args):
    cargs = ["url"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    if getattr(args, "open_browser", None): cargs.append("--open")
    stdout, stderr, rc = run_colab(cargs, timeout=30)
    check_rc(stdout, stderr, rc, "url-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_drivemount(args):
    cargs = ["drivemount"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    if getattr(args, "path", None): cargs.append(args.path)
    stdout, stderr, rc = run_colab(cargs, timeout=60)
    check_rc(stdout, stderr, rc, "drivemount-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_auth(args):
    cargs = ["auth"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    stdout, stderr, rc = run_colab(cargs, timeout=60)
    check_rc(stdout, stderr, rc, "auth-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_restart(args):
    cargs = ["restart-kernel"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    stdout, stderr, rc = run_colab(cargs, timeout=30)
    check_rc(stdout, stderr, rc, "restart-failed", stdout.strip())
    write_output(stdout, args.out, args.json)

def cmd_pay(args):
    stdout, stderr, rc = run_colab(["pay"], timeout=10)
    check_rc(stdout, stderr, rc, "pay-failed", stdout.strip())
    write_output(stdout or "https://colab.research.google.com/signup", args.out, args.json)

def cmd_version(args):
    stdout, stderr, rc = run_colab(["version"], timeout=5)
    check_rc(stdout, stderr, rc, "version-failed", stdout.strip())
    write_output(stdout.strip(), args.out, args.json)

def cmd_update(args):
    cargs = ["update"]
    if getattr(args, "install", None): cargs.append("--install")
    stdout, stderr, rc = run_colab(cargs, timeout=15)
    check_rc(stdout, stderr, rc, "update-failed", stdout.strip())
    write_output(stdout.strip(), args.out, args.json)

def cmd_whoami(args):
    stdout, stderr, rc = run_colab(["whoami"], timeout=10)
    if rc != 0:
        die("whoami-failed", "whoami not available. Upgrade: pip install --upgrade google-colab-cli")
    write_output(stdout.strip(), args.out, args.json)

# ─── Browser-based ─────────────────────────────────────────────────────────────

def _get_browser_colab_url(session=None):
    cargs = ["url"]
    if session: cargs.extend(["-s", session])
    stdout, stderr, rc = run_colab(cargs, timeout=15)
    if rc != 0 or not stdout.strip(): return None
    for line in stdout.strip().splitlines():
        if line.strip().startswith("http"): return line.strip()
    return None

def cmd_secrets(args):
    action = getattr(args, "secrets_action", "list")
    key = getattr(args, "key", None)
    value = getattr(args, "value", None)
    s = _resolve_session(args) or args.session
    if action in ("set", "get", "delete") and not key:
        die("secrets-missing-key", f"Action '{action}' requires --key")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        die("secrets-no-playwright", "Install: pip install playwright && playwright install chromium")

    url = _get_browser_colab_url(s)
    if not url: die("secrets-no-url", "No browser URL. Session running?")

    result = {"ok": True, "action": action}
    pw = sync_playwright().start()
    browser = ctx = page = None
    try:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        secrets_btn = page.locator('button:has-text("Secrets")')
        if secrets_btn.count() == 0: die("secrets-not-found", "Secrets panel missing.")
        secrets_btn.first.click(); time.sleep(2)
        if action == "list":
            result["secrets"] = _parse_secrets_list(page.locator("body").inner_text())
        elif action == "set":
            add_btn = page.locator('button:has-text("Add new secret")')
            if add_btn.count() > 0:
                add_btn.first.click(); time.sleep(1)
                ni = page.locator('input[placeholder*="Name"]')
                if ni.count() > 0: ni.first.fill(key)
                vi = page.locator('input[placeholder*="Value"]')
                if vi.count() > 0: vi.first.fill(value)
                page.keyboard.press("Enter"); time.sleep(1)
                result["msg"] = f"Secret '{key}' set."
        elif action == "delete":
            result["msg"] = "Delete requires DOM interaction — use list first to identify."
    except Exception as e:
        die("secrets-error", str(e))
    finally:
        for obj in [page, ctx, browser]:
            try: obj.close()
            except: pass
        try: pw.stop()
        except: pass
    write_output(json.dumps(result, ensure_ascii=False), args.out, args.json)

def _parse_secrets_list(page_text):
    secrets = []
    in_secrets = False
    for line in page_text.split("\n"):
        s = line.strip()
        if "Secrets" in s and "Close" in s: in_secrets = True; continue
        if in_secrets and s == "Close": break
        if in_secrets and s and not s.startswith("Add") and not s.startswith("Secrets"): secrets.append(s)
    return secrets

def cmd_resources(args):
    s = _resolve_session(args) or args.session
    try:
        from playwright.sync_api import sync_playwright
        url = _get_browser_colab_url(s)
        if url:
            pw = sync_playwright().start()
            browser = ctx = page = None
            try:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
                ctx = browser.new_context(viewport={"width": 1280, "height": 800})
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                body = page.locator("body").inner_text()
                resources = {}
                for m in re.finditer(r'RAM:\s*([\d.]+)\s*GB\s*/\s*([\d.]+)\s*GB', body):
                    u, t = float(m.group(1)), float(m.group(2))
                    resources["ram_used_gb"] = u; resources["ram_total_gb"] = t
                    resources["ram_percent"] = round(u / t * 100, 1) if t else 0
                for m in re.finditer(r'Disk:\s*([\d.]+)\s*GB\s*/\s*([\d.]+)\s*GB', body):
                    u, t = float(m.group(1)), float(m.group(2))
                    resources["disk_used_gb"] = u; resources["disk_total_gb"] = t
                    resources["disk_percent"] = round(u / t * 100, 1) if t else 0
                if resources:
                    write_output(json.dumps({"ok": True, "resources": resources}, ensure_ascii=False), args.out, args.json)
                    return
            finally:
                for obj in [page, ctx, browser]: 
                    try: obj.close()
                    except: pass
                try: pw.stop()
                except: pass
    except Exception: pass
    cmd_status(args)

def cmd_share(args):
    s = _resolve_session(args) or args.session
    url = _get_browser_colab_url(s)
    if not url: die("share-no-url", "No browser URL.")
    result = {"ok": True, "url": url, "msg": "Share this URL."}
    write_output(json.dumps(result, ensure_ascii=False), args.out, args.json)

def cmd_tunnel(args):
    """Get or save Cloudflare tunnel URL for a session."""
    s = _resolve_session(args) or args.session
    action = getattr(args, "action", "get")
    if action == "get":
        if TUNNEL_FILE.exists():
            data = json.loads(TUNNEL_FILE.read_text())
            url = data.get(s) or data.get("default")
            if url:
                write_output(url, args.out, args.json, {"tunnel_url": url, "session": s})
                return
        die("tunnel-not-found", "No tunnel URL saved. Run: colab.py tunnel set --url URL -s SESSION")
    elif action == "set":
        url = getattr(args, "url", None)
        if not url: die("tunnel-no-url", "Provide --url")
        data = {}
        if TUNNEL_FILE.exists(): data = json.loads(TUNNEL_FILE.read_text())
        data[s or "default"] = url
        TUNNEL_FILE.write_text(json.dumps(data, indent=2))
        write_output(f"Tunnel URL saved for {s or 'default'}: {url}", args.out, args.json)

# ─── CLI ───────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(description="colab-cli v2.1 — AI-agent-native CLI for Google Colab",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    shared_output = argparse.ArgumentParser(add_help=False)
    shared_output.add_argument("-o", "--out", help="Write output to FILE, stdout gets JSON pointer")
    shared_output.add_argument("--json", action="store_true", help="Structured JSON output")

    sub = p.add_subparsers(dest="command")

    # Session management
    pn = sub.add_parser("new", parents=[shared_output], help="Create session")
    pn.add_argument("-s", "--session"); pn.add_argument("--gpu", choices=["T4","L4","G4","H100","A100"]); pn.add_argument("--tpu", choices=["v5e1","v6e1"])
    sub.add_parser("sessions", parents=[shared_output], help="List sessions")
    ps = sub.add_parser("status", parents=[shared_output], help="Session status"); ps.add_argument("-s", "--session")
    ps2 = sub.add_parser("stop", parents=[shared_output], help="Stop session"); ps2.add_argument("-s", "--session")
    pr = sub.add_parser("restart", parents=[shared_output], help="Restart kernel"); pr.add_argument("-s", "--session")
    pg = sub.add_parser("gpu_switch", parents=[shared_output], help="Switch GPU on session"); pg.add_argument("-s", "--session"); pg.add_argument("--gpu", choices=["T4","L4","G4","H100","A100"], required=True)

    # Execution
    pe = sub.add_parser("exec", parents=[shared_output], help="Execute code")
    pe.add_argument("-s", "--session"); pe.add_argument("-f", "--file"); pe.add_argument("--code"); pe.add_argument("--output-image"); pe.add_argument("--timeout", type=float, default=30.0)
    peb = sub.add_parser("exec_bg", parents=[shared_output], help="Background execution")
    peb.add_argument("-s", "--session"); peb.add_argument("--code"); peb.add_argument("--timeout", type=float, default=600.0)
    pep = sub.add_parser("exec_bg_poll", parents=[shared_output], help="Poll background job"); pep.add_argument("job_id")
    pr2 = sub.add_parser("run", parents=[shared_output], help="Ephemeral job"); pr2.add_argument("-f", "--file"); pr2.add_argument("--code"); pr2.add_argument("--gpu", choices=["T4","L4","G4","H100","A100"]); pr2.add_argument("--tpu", choices=["v5e1","v6e1"]); pr2.add_argument("--keep", action="store_true"); pr2.add_argument("args", nargs="*")
    pr3 = sub.add_parser("repl", parents=[shared_output], help="Python REPL"); pr3.add_argument("-s", "--session"); pr3.add_argument("--code"); pr3.add_argument("--output-image"); pr3.add_argument("--timeout", type=float, default=60.0)
    pc = sub.add_parser("console", parents=[shared_output], help="Shell command"); pc.add_argument("-s", "--session"); pc.add_argument("--cmd", help="Shell command to run"); pc.add_argument("--timeout", type=float, default=60.0)

    # VM file ops
    plogs = sub.add_parser("logs", parents=[shared_output], help="Tail VM file")
    plogs.add_argument("-s", "--session"); plogs.add_argument("filepath", help="Path on VM (e.g. /content/server.log)")
    plogs.add_argument("-n", "--lines", type=int, default=50, help="Lines to show")
    plogs.add_argument("-f", "--follow", action="store_true", help="Stream output (Ctrl+C to stop)")

    # File ops
    pl = sub.add_parser("ls", parents=[shared_output], help="List files"); pl.add_argument("-s", "--session"); pl.add_argument("path", nargs="?")
    pu = sub.add_parser("upload", parents=[shared_output], help="Upload file"); pu.add_argument("-s", "--session"); pu.add_argument("local"); pu.add_argument("remote")
    pd = sub.add_parser("download", parents=[shared_output], help="Download file"); pd.add_argument("-s", "--session"); pd.add_argument("remote"); pd.add_argument("local")
    prm = sub.add_parser("rm", parents=[shared_output], help="Delete file"); prm.add_argument("-s", "--session"); prm.add_argument("path")
    pe2 = sub.add_parser("edit", parents=[shared_output], help="Edit remote file"); pe2.add_argument("-s", "--session"); pe2.add_argument("remote_path")

    # Automation
    pi = sub.add_parser("install", parents=[shared_output], help="Install packages"); pi.add_argument("-s", "--session"); pi.add_argument("-r", "--requirements"); pi.add_argument("--pip-args", help="Extra pip flags"); pi.add_argument("packages", nargs="*")
    pchk = sub.add_parser("check", parents=[shared_output], help="Pre-flight model test"); pchk.add_argument("-s", "--session"); pchk.add_argument("--code", required=True, help="Python to test"); pchk.add_argument("--timeout", type=float, default=60.0)
    pl2 = sub.add_parser("log", parents=[shared_output], help="View/export history"); pl2.add_argument("-s", "--session"); pl2.add_argument("-n", "--lines", type=int); pl2.add_argument("-t", "--event-type"); pl2.add_argument("--output")
    pn2 = sub.add_parser("notebook", parents=[shared_output], help="Notebook export/execute"); pn2.add_argument("-s", "--session"); pn2.add_argument("action", nargs="?", choices=["export","execute"], default="export", help="export or execute"); pn2.add_argument("--output", help="Export path"); pn2.add_argument("--file", help="Notebook file to execute")
    purl = sub.add_parser("url", parents=[shared_output], help="Get browser URL"); purl.add_argument("-s", "--session"); purl.add_argument("--open", dest="open_browser", action="store_true")
    pdm = sub.add_parser("drivemount", parents=[shared_output], help="Mount Drive"); pdm.add_argument("-s", "--session"); pdm.add_argument("path", nargs="?")
    pa = sub.add_parser("auth", parents=[shared_output], help="Auth VM for GCP"); pa.add_argument("-s", "--session")

    # Info
    sub.add_parser("pay", parents=[shared_output], help="Subscription page")
    sub.add_parser("version", parents=[shared_output], help="CLI version")
    pu2 = sub.add_parser("update", parents=[shared_output], help="Check updates"); pu2.add_argument("--install", action="store_true")
    sub.add_parser("whoami", parents=[shared_output], help="Auth identity")

    # Browser
    ps3 = sub.add_parser("secrets", parents=[shared_output], help="Manage secrets"); ps3.add_argument("-s", "--session"); ps3.add_argument("action", nargs="?", default="list", choices=["list","set","get","delete"]); ps3.add_argument("--key"); ps3.add_argument("--value")
    pr4 = sub.add_parser("resources", parents=[shared_output], help="VM resources"); pr4.add_argument("-s", "--session")
    psh = sub.add_parser("share", parents=[shared_output], help="Share URL"); psh.add_argument("-s", "--session")
    pt = sub.add_parser("tunnel", parents=[shared_output], help="Tunnel URL mgmt"); pt.add_argument("-s", "--session"); pt.add_argument("action", nargs="?", default="get", choices=["get","set"]); pt.add_argument("--url")

    return p

def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command: parser.print_help(); sys.exit(1)
    if not COLAB or not os.path.isfile(COLAB):
        die("no-colab-cli", "google-colab-cli not found. Install: pip install google-colab-cli")

    dispatcher = {
        "new": cmd_new, "exec": cmd_exec, "exec_bg": cmd_exec_bg, "exec_bg_poll": cmd_exec_bg_poll,
        "run": cmd_run, "sessions": cmd_sessions, "status": cmd_status, "stop": cmd_stop,
        "gpu_switch": cmd_gpu_switch, "restart": cmd_restart,
        "ls": cmd_ls, "upload": cmd_upload, "download": cmd_download, "rm": cmd_rm,
        "install": cmd_install, "log": cmd_log, "notebook": cmd_notebook,
        "url": cmd_url, "drivemount": cmd_drivemount, "auth": cmd_auth,
        "console": cmd_console, "edit": cmd_edit, "repl": cmd_repl,
        "logs": cmd_logs, "check": cmd_check,
        "pay": cmd_pay, "version": cmd_version, "update": cmd_update, "whoami": cmd_whoami,
        "secrets": cmd_secrets, "resources": cmd_resources, "share": cmd_share, "tunnel": cmd_tunnel,
    }
    try:
        dispatcher[args.command](args)
    except KeyboardInterrupt: die("interrupted", "Cancelled")
    except Exception as e: die("exception", str(e))

if __name__ == "__main__": main()
