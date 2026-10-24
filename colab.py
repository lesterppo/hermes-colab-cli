#!/usr/bin/env python3
"""colab-cli v3.3.2 — AI-agent-native CLI for Google Colab.

Fixes over v3.0:
- P0: Retry logic for transient Colab errors (502/503/timeout), up to 2 retries
- P0: cmd_console shell injection fixed — json.dumps() escaping
- P0: tunnel_discover shell=True replaced with list-form subprocess
- P1: _remote_exists/_remote_read use json.dumps() for path safety
- P1: Auth pre-check window widened from 120s to 300s
- P1: shlex.split() for pip args (handles quoted arguments)
- P1: Browser sleeps replaced with wait_for_selector / wait_for_timeout
- P2: All =run_colab / =_remote_pexec spacing fixed

v3.3 (Oct 2026, from the failing arXiv training-track review):
- Pre-flight proxy-aware token refresh in run_colab itself, so EVERY
  command refreshes before calling the raw CLI (whose google-auth
  transport ignores proxy env and falls into interactive OAuth on expiry)
- token refresh / refresh-all / status (rotation pool only, never secrets)
- account list / switch / rotate (daily rotation; cyc236de/cyc236hk
  hard-excluded) + --registry-file for isolated session registries
- new: kernel health check after create (kills the phantom-READY class)
- train: one command housing a whole daily training-track run —
  rotate, refresh, create+health-check with cross-account retry,
  upload, detached launch, supervised monitoring, result download,
  honest DONE/RECLAIMED/DIED/TIMEOUT/PROVISION_FAILED classification
- wrapper-side session registry, so a vanished session reads as
  RECLAIMED instead of a bare "not found"

v3.3.2 (Oct 2026, ToolSelect verify3–verify5):
- train monitor greps the WHOLE log for the done-marker every poll
  (a ~56-line results JSON printed after the marker once hid it from a
  40-line tail and misclassified a completed run as DIED); drivers
  should still print the marker last
- train --rotate rewrites the rotation state to the account that
  actually ran when creation falls back (picked ppoppo205, ran on
  cyc236es — otherwise the next day repeats the fallback account)
- train --watch-only prefers the wrapper registry's recorded account
  over the live active token (shared VM: the symlink may have moved)

Wraps the official google-colab-cli with token-efficient output.
"""

import argparse, json, os, subprocess, sys, time, re, shlex, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path
import threading

COLAB_HOME = Path.home() / ".hermes" / "scripts" / "colab"
COLAB_HOME.mkdir(parents=True, exist_ok=True)
TUNNEL_FILE = COLAB_HOME / "tunnel_url.json"
TOKEN_FILE = Path.home() / ".config" / "colab-cli" / "token.json"
TOKEN_DIR = Path.home() / ".config" / "colab-cli"
CACHE_DIR = Path.home() / ".cache" / "colab-cli"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Isolated session-registry file for raw-CLI --config mode. When set (via
# --state-file), every raw-CLI invocation gets `--config <file>` BEFORE the
# subcommand, so wrapper protections (refresh, retry, registry) apply to
# isolated-state work too. Previously agents dropped to raw `colab_cli.cli`
# directly for --config work and lost all of them (Oct 2026: interactive
# OAuth hang on token expiry, wiped state file misread as "not found").
CONFIG_FILE = None

# Training-track account rotation (standing rule, set 2026-10-24): a
# different account every day, never two days in a row on the same one.
# cyc236de / cyc236hk belong to another agent and are NEVER selected.
ROTATION_POOL = ["ppoppo205", "cyc236es", "kyu008008", "kyu009009", "cyc236ha"]
ROTATION_EXCLUDED = {"cyc236de", "cyc236hk"}
ROTATION_STATE_DEFAULT = (Path.home() / "workspace" / "goals" /
                          "arxiv-daily-read-and-test" / "hidden_files" /
                          "colab_account_rotation.txt")

# ── Auth token management ──────────────────────────────────────────────────

_token_lock = threading.Lock()
_token_stop_event = threading.Event()
_refresher_thread = None

def _find_colab():
    import shutil
    for name in ("colab", "google-colab-cli"):
        p = shutil.which(name)
        if p: return p
    for p in [os.path.expanduser("~/.local/bin/colab"), str(Path(sys.prefix)/"bin"/"colab")]:
        if os.path.isfile(p): return p
    return None

COLAB = _find_colab()

# The installed `colab` script may shebang to a broken python (e.g. a venv
# with mismatched pydantic_core). Prefer invoking the package via a working
# system python: `/usr/bin/python3 -m colab_cli.cli`.
COLAB_PY = None
for cand in ("/usr/bin/python3", "/usr/bin/python"):
    if os.path.exists(cand):
        COLAB_PY = cand
        break

# Patched shadow copy (created by `colabctl patch` when the installed package
# is root-owned): prefer it so the 401/404 -> refresh-token fix is active.
PATCHED_SHADOW = os.path.expanduser("~/colab_cli_patched")
if os.path.isdir(PATCHED_SHADOW):
    os.environ["PYTHONPATH"] = PATCHED_SHADOW + os.pathsep + os.environ.get("PYTHONPATH", "")

# The host shell (e.g. inside Hermes) may inject a venv's site-packages into
# PYTHONPATH, which breaks /usr/bin/python3's pydantic (mismatched
# pydantic_core). Strip venv/hermes paths from PYTHONPATH for every subprocess.
def _clean_env():
    env = os.environ.copy()
    pp = env.get("PYTHONPATH", "")
    keep = [p for p in pp.split(os.pathsep)
            if p and "venv" not in p and ".hermes/hermes-agent" not in p]
    cleaned = os.pathsep.join(keep)
    env["PYTHONPATH"] = cleaned
    env.pop("VIRTUAL_ENV", None)
    return env


def _py_for_scripts():
    """System python for helper scripts (venv python has broken pydantic_core)."""
    return COLAB_PY or sys.executable

def die(code, msg):
    print(json.dumps({"ok": False, "err": code, "msg": msg}))
    sys.exit(1)

RETRYABLE_CODES = (502, 503, 504)  # Colab transient errors
MAX_RETRIES = 2
RETRY_DELAY = 3  # seconds

def _is_retryable(stderr, rc):
    """Check if the error is transient and worth retrying."""
    combined = (stderr or "").lower()
    if rc in RETRYABLE_CODES:
        return True
    for phrase in ("connection reset", "connection refused", "service unavailable",
                   "temporarily unavailable", "backend error", "try again"):
        if phrase in combined:
            return True
    return False

def run_colab(args, timeout=120, stdin_data=None, env=None, retries=MAX_RETRIES):
    # Single choke point for every raw-CLI call: refresh the OAuth token
    # BEFORE the call if it expires within 10 minutes, using the
    # proxy-aware urllib path (the raw CLI's google-auth transport ignores
    # proxy env and falls into interactive OAuth on expiry — the Oct 2026
    # hang). Cheap when the token is fresh (one local file read).
    global CONFIG_FILE
    try:
        if _token_expires_soon(600):
            _try_refresh_token()
    except Exception:
        pass
    if COLAB_PY:
        cmd = [COLAB_PY, "-m", "colab_cli.cli"]
        if CONFIG_FILE:
            cmd += ["--config", str(CONFIG_FILE)]
        cmd += args
    else:
        cmd = [COLAB] + args
    merged_env = _clean_env()
    if env: merged_env.update(env)
    last_stdout, last_stderr, last_rc = "", "", -1
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               env=merged_env, input=stdin_data)
            if r.returncode == 0 or not _is_retryable(r.stderr, r.returncode):
                return r.stdout, r.stderr, r.returncode
            last_stdout, last_stderr, last_rc = r.stdout, r.stderr, r.returncode
            # Wiped/expired session registry? Rebuild from the backend once,
            # then retry — this saved 40GB of model re-downloads in the field.
            combined = (r.stderr + r.stdout).lower()
            if attempt == 0 and ("session" in combined and "not found" in combined):
                _auto_recover()
            if attempt < retries:
                time.sleep(RETRY_DELAY * (attempt + 1))
        except subprocess.TimeoutExpired:
            if attempt < retries and retries > 0:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return "", "timeout", -1
        except FileNotFoundError:
            return "", "colab binary not found", -2
    return last_stdout, last_stderr, last_rc

def _auto_recover():
    """Rebuild sessions.json from list_assignments() (same as cmd_recover)."""
    code = (
        "import sys; sys.path.insert(0, '/usr/local/lib/python3.12/dist-packages')\n"
        "from colab_cli.common import state\n"
        "import json, os\n"
        "try:\n"
        "    assignments = state.client.list_assignments()\n"
        "except Exception:\n"
        "    sys.exit(0)\n"
        "store_path = os.path.expanduser('~/.config/colab-cli/sessions.json')\n"
        "store = json.load(open(store_path)) if os.path.exists(store_path) else {}\n"
        "changed = False\n"
        "for a in assignments:\n"
        "    ep = a.endpoint\n"
        "    hit = None\n"
        "    for name, s in store.items():\n"
        "        if s.get('endpoint') == ep:\n"
        "            hit = (name, s); break\n"
        "    if hit:\n"
        "        name, s = hit\n"
        "        s['url'] = a.runtime_proxy_info.url\n"
        "        s['token'] = a.runtime_proxy_info.token\n"
        "        changed = True\n"
        "    else:\n"
        "        tail = ep.split('-')[-1][:10]\n"
        "        store['recovered_' + tail] = {\n"
        "            'name': ep, 'url': a.runtime_proxy_info.url,\n"
        "            'token': a.runtime_proxy_info.token, 'endpoint': ep,\n"
        "            'variant': getattr(a, 'variant', 'GPU'),\n"
        "            'accelerator': getattr(a, 'accelerator', 'T4')}\n"
        "        changed = True\n"
        "if changed:\n"
        "    json.dump(store, open(store_path, 'w'), indent=2)\n"
        "    print('AUTO_RECOVERED')\n"
    )
    try:
        tmp = COLAB_HOME / "auto_recover.py"
        tmp.write_text(code)
        r = subprocess.run([sys.executable, str(tmp)], capture_output=True,
                           text=True, timeout=60)
        if "AUTO_RECOVERED" in (r.stdout or ""):
            print("[colab] session registry was stale — rebuilt from backend", file=sys.stderr)
    except Exception:
        pass

def parse_error(stdout, stderr):
    combined = (stdout + "\n" + stderr).lower()
    if "auth" in combined and ("expired" in combined or "invalid" in combined):
        return "auth-expired", "Token expired."
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
                # If refresh worked, caller can retry; but simplest is just to report
            die(err_code, err_msg or stderr.strip())
        die(fallback_code, stderr.strip() or stdout.strip() or fallback_msg)

def _refresh_token_file(path):
    """Refresh one token file via the OAuth2 refresh-token grant, using
    plain urllib (which honors proxy env vars). Returns (ok, detail).
    Never prints secrets; detail carries only status/expiry/error class.
    The raw CLI's google-auth transport ignores proxy env, so this is the
    only refresh path that works behind the egress proxy."""
    p = Path(path)
    if not p.exists():
        return False, "no-token-file"
    try:
        with _token_lock:
            creds = json.loads(p.read_text())
            if not creds.get("refresh_token"):
                return False, "no-refresh-token"
            data = urllib.parse.urlencode({
                "client_id": creds.get("client_id", ""),
                "client_secret": creds.get("client_secret", ""),
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            }).encode()
            req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            resp = urllib.request.urlopen(req, timeout=30)
            t = json.loads(resp.read())
            if "access_token" not in t:
                return False, "refresh-rejected"
            creds["token"] = t["access_token"]
            if t.get("refresh_token"):
                creds["refresh_token"] = t["refresh_token"]
            creds["expiry"] = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + t.get("expires_in", 3600),
                tz=timezone.utc).isoformat()
            p.write_text(json.dumps(creds, indent=2))
            return True, creds["expiry"]
    except urllib.error.HTTPError as e:
        # invalid_grant = refresh token dead (revoked / 7-day testing-mode
        # expiry) — only a fresh browser OAuth code fixes it, never a retry.
        return False, "invalid-grant" if e.code == 400 else f"http-{e.code}"
    except Exception as e:
        return False, type(e).__name__


def _try_refresh_token():
    """Attempt to refresh the ACTIVE account's OAuth2 token. Returns True
    on success. Backwards-compatible wrapper around _refresh_token_file."""
    if not TOKEN_FILE.exists():
        return False
    ok, _ = _refresh_token_file(TOKEN_FILE)
    return ok

def _token_expires_soon(seconds_before=300):
    """Check if token expires within N seconds. Token expiry is stored as ISO string."""
    if not TOKEN_FILE.exists(): return False
    try:
        creds = json.loads(TOKEN_FILE.read_text())
        expiry_str = creds.get("expiry", "")
        if not expiry_str: return False
        # Handle both float epoch and ISO format
        if isinstance(expiry_str, (int, float)):
            expiry_ts = float(expiry_str)
        else:
            expiry_dt = datetime.fromisoformat(expiry_str.replace("+00:00", "").replace("Z", ""))
            expiry_ts = expiry_dt.timestamp()
        return (expiry_ts - datetime.now(timezone.utc).timestamp()) < seconds_before
    except Exception:
        return False

def _start_token_refresher():
    """Background thread: refresh token every 5 min if expiring soon."""
    global _refresher_thread
    if _refresher_thread and _refresher_thread.is_alive():
        return

    def _refresher_loop():
        while not _token_stop_event.is_set():
            try:
                if _token_expires_soon(300):
                    refreshed = _try_refresh_token()
                    if refreshed:
                        ts = datetime.now().strftime("%H:%M:%S")
                        sys.stderr.write(f"[colab-cli {ts}] Token auto-refreshed\n")
                        sys.stderr.flush()
            except Exception:
                pass
            _token_stop_event.wait(300)  # check every 5 min

    _refresher_thread = threading.Thread(target=_refresher_loop, daemon=True)
    _refresher_thread.start()

def _pre_auth_check():
    """Ensure auth is valid before command runs. Attempt refresh if needed."""
    if _token_expires_soon(300):
        _try_refresh_token()
    _start_token_refresher()

def format_sessions_list(stdout):
    sessions = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("─") or "Session" in line or "No active" in line: continue
        parts = line.split()
        if len(parts) >= 2:
            # Raw CLI prints names bracketed: "[smoke-test] <endpoint> | ...".
            # The brackets must be stripped, or no name ever matches (this
            # misclassified a live session as RECLAIMED in Oct 2026).
            name = parts[0].strip("[]")
            endpoint = parts[1] if parts[1] != "|" else None
            sessions.append({"name": name, "endpoint": endpoint,
                             "status": parts[-1] if len(parts) > 2 else "active"})
    return sessions

def _resolve_session(args):
    """Auto-detect session if not specified."""
    if getattr(args, "session", None): return args.session
    stdout, stderr, rc = run_colab(["sessions"], timeout=15)
    if rc == 0:
        sessions = format_sessions_list(stdout)
        if len(sessions) == 1: return sessions[0]["name"]
    return None

# ─── Core VM helpers ──────────────────────────────────────────────────────

def _remote_exists(session, path):
    """Check if a file exists on the VM."""
    code = "import os; print('YES' if os.path.exists(%s) else 'NO')" % json.dumps(path)
    stdout, stderr, rc = run_colab(["exec", "-s", session, "--timeout", "5"], timeout=10, stdin_data=code)
    return rc == 0 and "YES" in stdout

def _remote_read(session, path):
    """Read a text file from the VM."""
    code = "print(open(%s).read())" % json.dumps(path)
    stdout, stderr, rc = run_colab(["exec", "-s", session, "--timeout", "10"], timeout=15, stdin_data=code)
    return stdout if rc == 0 else None

def _remote_pexec(session, code, timeout=10):
    """Run Python code on VM, return (stdout, stderr, rc)."""
    cargs = ["exec", "-s", session, "--timeout", str(max(timeout, 5))]
    return run_colab(cargs, timeout=timeout + 15, stdin_data=code)

# ─── Commands ──────────────────────────────────────────────────────────────

def cmd_new(args):
    _pre_auth_check()
    cargs = ["new"]
    if args.session: cargs.extend(["-s", args.session])
    if getattr(args, "gpu", None): cargs.extend(["--gpu", args.gpu])
    if getattr(args, "tpu", None): cargs.extend(["--tpu", args.tpu])
    stdout, stderr, rc = run_colab(cargs, timeout=120)
    check_rc(stdout, stderr, rc, "new-failed", "Failed to create session")
    name = args.session or stdout.strip().split()[-1]
    # Phantom-READY guard (normtune Oct 2026): a session that passes `new`
    # but whose kernel never answers is unusable — fail here, loudly,
    # instead of letting a run start on it and die mysteriously.
    if not getattr(args, "no_health_check", False) and not _health_check(name):
        die("provision-failed",
            f"Session '{name}' was created but its kernel never answered "
            "(runtime proxy 404 / phantom session). Stop it and retry, "
            "preferably on another account.")
    _registry_record(name, _active_account(),
                     {"gpu": getattr(args, "gpu", None)})
    write_output(f"Session '{name}' created.\n{stdout.strip()}", args.out, args.json, {"session": name})

def cmd_exec(args):
    _pre_auth_check()
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

# ── P0: exec_detach — upload code + run detached, returns immediately ─────

def cmd_exec_detach(args):
    """Upload code file, run it with nohup/start_new_session, return PID immediately.

    This is THE way to launch long-running servers (llama.cpp, FastAPI, tunnels).
    Unlike exec (which blocks) or exec_bg (which runs Code as exec text),
    this uploads a script and runs it as a proper background process.
    """
    _pre_auth_check()
    s = _resolve_session(args) or args.session
    if not s: die("exec-detach-no-session", "Specify -s SESSION or create one first")

    local_file = getattr(args, "file", None)
    code = getattr(args, "code", None)
    remote_path = getattr(args, "remote", None)
    log_file = getattr(args, "log", None) or "/dev/null"
    work_dir = getattr(args, "dir", None) or "/content"

    # Resolve local file path
    if local_file:
        local_path = Path(local_file)
        if not local_path.is_absolute():
            local_path = Path.cwd() / local_path
        if not local_path.exists():
            die("exec-detach-no-file", f"File not found: {local_file}")
        remote_path = remote_path or f"/content/{local_path.name}"

        # Upload the file
        stdout, stderr, rc = run_colab(["upload", "-s", s, str(local_path), remote_path], timeout=60)
        check_rc(stdout, stderr, rc, "upload-failed", "Upload failed")

    elif code:
        remote_path = remote_path or "/content/detach_script.py"
        # Write code to temp file and upload
        tmp = COLAB_HOME / "detach_code.py"
        tmp.write_text(code)
        stdout, stderr, rc = run_colab(["upload", "-s", s, str(tmp), remote_path], timeout=60)
        check_rc(stdout, stderr, rc, "upload-failed", "Upload failed")
    else:
        die("exec-detach-no-code", "Provide --file LOCAL or --code ...")

    # Launch detached on VM
    launch_code = (
        "import subprocess, sys, os\n"
        "p = subprocess.Popen(\n"
        "    [sys.executable, '%s'],\n"
        "    stdout=open('%s', 'w'), stderr=subprocess.STDOUT,\n"
        "    start_new_session=True, cwd='%s'\n"
        ")\n"
        "print(f'PID:{p.pid}|SESSION:%s|LOG:%s|SCRIPT:%s')"
    ) % (remote_path, log_file, work_dir, s, log_file, remote_path)

    stdout, stderr, rc = _remote_pexec(s, launch_code, timeout=15)
    if rc == 0 and "PID:" in stdout:
        info = {}
        for part in stdout.strip().split("|"):
            if ":" in part:
                k, v = part.split(":", 1)
                info[k] = v
        write_output("", args.out, args.json, {
            "status": "detached",
            "pid": info.get("PID"),
            "log": info.get("LOG"),
            "script": info.get("SCRIPT"),
            "session": s,
            "monitor": f"python3 ~/.hermes/scripts/colab/colab.py logs -s {s} {log_file.replace('/content/', '/content/')} -f"
        })
    else:
        die("exec-detach-failed", stdout.strip())


# ── P1: exec_file — upload + exec in one step ─────────────────────────────

def cmd_exec_file(args):
    """Upload a local script and execute it on the VM in one step.

    Combines upload + exec into a single command.
    """
    _pre_auth_check()
    s = _resolve_session(args) or args.session
    if not s: die("exec-file-no-session", "Specify -s SESSION or create one first")

    local_path = Path(args.file)
    if not local_path.is_absolute():
        local_path = Path.cwd() / local_path
    if not local_path.exists():
        die("exec-file-not-found", f"File not found: {args.file}")

    remote_path = getattr(args, "remote", None) or f"/content/{local_path.name}"

    # Upload
    stdout, stderr, rc = run_colab(["upload", "-s", s, str(local_path), remote_path], timeout=60)
    check_rc(stdout, stderr, rc, "upload-failed", "Upload failed")

    # Execute
    t = getattr(args, "timeout", 30) or 30
    code = f"import subprocess, sys; r = subprocess.run([sys.executable, '{remote_path}'], capture_output=True, text=True, timeout={t}); print(r.stdout); print(r.stderr, file=sys.stderr if r.returncode else sys.stdout)"
    cargs = ["exec", "-s", s, "--timeout", str(t)]
    stdout, stderr, rc = run_colab(cargs, timeout=t + 60, stdin_data=code)
    check_rc(stdout, stderr, rc, "exec-file-failed", "Script execution failed")
    write_output(stdout, args.out, args.json, {"file": str(local_path), "remote": remote_path})


# ── P1: tunnel discover — auto-grep VM for live tunnel URL ────────────────

def cmd_tunnel_discover(args):
    """Search the VM for a live Cloudflare tunnel URL.

    Checks common locations: /content/tunnel.log, /content/api_url.txt,
    /kaggle/working/tunnel.log, /content/*.log patterns.
    """
    _pre_auth_check()
    s = _resolve_session(args) or args.session
    if not s: die("tunnel-discover-no-session", "Specify -s SESSION or create one first")

    search_code = (
        "import os, re, glob\n"
        "urls = []\n"
        "patterns = [\n"
        "    '/content/tunnel.log', '/content/api_url.txt',\n"
        "    '/content/*.log', '/content/*deploy*.log',\n"
        "    '/content/server*.log', '/kaggle/working/tunnel.log',\n"
        "    '/kaggle/working/api_url.txt'\n"
        "]\n"
        "files_to_check = set()\n"
        "for pat in patterns:\n"
        "    files_to_check.update(glob.glob(pat))\n"
        "# Also check explicit paths\n"
        "for f in ['/content/tunnel.log', '/content/api_url.txt']:\n"
        "    if os.path.exists(f): files_to_check.add(f)\n"
        "for f in sorted(files_to_check):\n"
        "    try:\n"
        "        content = open(f).read()\n"
        "        found = re.findall(r'https://[a-zA-Z0-9.-]*\\.trycloudflare\\.com[^\\s\"\\'<>]*', content)\n"
        "        if found:\n"
        "            for u in found:\n"
        "                clean = u.rstrip('/')\n"
        "                if '/v1' not in clean:\n"
        "                    clean += '/v1'\n"
        "                urls.append({'url': clean, 'source': f})\n"
        "    except Exception:\n"
        "        pass\n"
        "# Also try running process check\n"
        "try:\n"
        "    import subprocess as _sp\n"
        "    r = _sp.run(['ps', 'aux'], capture_output=True, text=True)\n"
        "    if 'cloudflared' in r.stdout:\n"
        "        urls.append({'url': 'process_running', 'source': 'ps_aux'})\n"
        "except: pass\n"
        "if urls:\n"
        "    import json\n"
        "    print(json.dumps({'ok': True, 'tunnels': urls}))\n"
        "else:\n"
        "    print(json.dumps({'ok': False, 'err': 'not-found', 'msg': 'No tunnel URLs found on VM'}))\n"
    )
    stdout, stderr, rc = _remote_pexec(s, search_code, timeout=15)
    try:
        result = json.loads(stdout.strip())
    except json.JSONDecodeError:
        result = {"ok": False, "err": "parse-error", "msg": stdout.strip()[:200]}

    if result.get("ok"):
        tunnels = result.get("tunnels", [])
        # Save all found URLs
        for t in tunnels:
            if "trycloudflare" in t.get("url", ""):
                data = json.loads(TUNNEL_FILE.read_text()) if TUNNEL_FILE.exists() else {}
                data[s] = t["url"]
                TUNNEL_FILE.write_text(json.dumps(data, indent=2))
        write_output(json.dumps(tunnels, indent=2), args.out, args.json, {"count": len(tunnels), "tunnels": tunnels})
    else:
        die("tunnel-discover-failed", result.get("msg", "No tunnels found"))


# ── P2: Fixed exec_bg with proper job tracking ────────────────────────────

def cmd_exec_bg(args):
    """Background execution — uploads code, runs it via exec_detach, returns job_id."""
    import uuid
    _pre_auth_check()
    s = _resolve_session(args) or args.session
    if not s: die("exec-bg-no-session", "Specify -s SESSION or create one first")

    job_id = str(uuid.uuid4())[:8]
    timeout = getattr(args, "timeout", 600) or 600

    code = getattr(args, "code", None)
    if not code and not sys.stdin.isatty():
        code = sys.stdin.read().strip()
    if not code:
        die("exec-bg-no-code", "No code provided. Use --code or pipe via stdin.")

    # Write code to temp file
    code_file = COLAB_HOME / f"bg_{job_id}_code.py"
    code_file.write_text(code)
    remote_path = f"/tmp/bg_{job_id}.py"
    log_path = f"/tmp/bg_{job_id}.log"

    # Upload
    stdout, stderr, rc = run_colab(["upload", "-s", s, str(code_file), remote_path], timeout=60)
    check_rc(stdout, stderr, rc, "upload-failed", "Upload failed")

    # Launch detached
    launch_code = (
        "import subprocess, sys, os, json\n"
        "p = subprocess.Popen(\n"
        "    [sys.executable, '%s'],\n"
        "    stdout=open('%s', 'w'), stderr=subprocess.STDOUT,\n"
        "    start_new_session=True\n"
        ")\n"
        "print(json.dumps({'pid': p.pid, 'job_id': '%s', 'session': '%s'}))"
    ) % (remote_path, log_path, job_id, s)

    stdout, stderr, rc = _remote_pexec(s, launch_code, timeout=15)
    try:
        info = json.loads(stdout.strip())
    except json.JSONDecodeError:
        info = {"raw": stdout.strip()}

    write_output("", args.out, args.json, {
        "job_id": job_id,
        "pid": info.get("pid"),
        "status": "running",
        "session": s,
        "log": f"{{'code'}}logs -s {s} {log_path} -f",
        "poll": f"exec_bg_poll {job_id}",
        "code_file": str(code_file),
    })


def cmd_exec_bg_poll(args):
    """Poll a background execution job — checks VM log file."""
    _pre_auth_check()
    job_id = args.job_id
    session = getattr(args, "session", None)

    # Auto-detect session if not specified
    if not session:
        stdout, stderr, rc = run_colab(["sessions"], timeout=10)
        if rc == 0:
            slist = format_sessions_list(stdout)
            if slist:
                session = slist[0]["name"]

    if session:
        log_path = f"/tmp/bg_{job_id}.log"
        code = f"import os; print('EXISTS' if os.path.exists('{log_path}') else 'NO')"
        stdout, stderr, rc = _remote_pexec(session, code, timeout=5)
        if rc == 0 and "EXISTS" in stdout:
            content_text = _remote_read(session, log_path)
            if content_text:
                write_output(content_text, args.out, args.json, {
                    "job_id": job_id,
                    "session": session,
                    "status": "done",
                    "size": len(content_text)
                })
                return

    write_output("", args.out, args.json, {
        "job_id": job_id,
        "status": "running",
        "msg": "Job still running or session not found"
    })
def cmd_run(args):
    _pre_auth_check()
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
    _pre_auth_check()
    stdout, stderr, rc = run_colab(["sessions"], timeout=30)
    check_rc(stdout, stderr, rc, "sessions-failed", stdout.strip())
    sessions_data = format_sessions_list(stdout)
    if args.json: print(json.dumps({"ok": True, "sessions": sessions_data}))
    else: write_output(stdout, args.out, args.json, {"sessions": sessions_data})


def cmd_status(args):
    _pre_auth_check()
    cargs = ["status"]
    s = _resolve_session(args) or args.session
    if s: cargs.extend(["-s", s])
    stdout, stderr, rc = run_colab(cargs, timeout=30)
    check_rc(stdout, stderr, rc, "status-failed", stdout.strip())
    write_output(stdout, args.out, args.json)


def cmd_stop(args):
    _pre_auth_check()
    s = _resolve_session(args) or args.session
    cargs = ["stop"]
    if s: cargs.extend(["-s", s])
    stdout, stderr, rc = run_colab(cargs, timeout=30)
    check_rc(stdout, stderr, rc, "stop-failed", stdout.strip())
    if s:
        _registry_remove(s)
    if TUNNEL_FILE.exists():
        data = json.loads(TUNNEL_FILE.read_text())
        if s in data:
            del data[s]
            TUNNEL_FILE.write_text(json.dumps(data, indent=2))
    write_output(f"Session '{s}' stopped." if s else "Session stopped.", args.out, args.json)


def cmd_gpu_switch(args):
    s = _resolve_session(args) or args.session
    new_gpu = args.gpu
    if not new_gpu: die("gpu-switch-no-gpu", "Specify --gpu target")
    cargs = ["stop", "-s", s]
    run_colab(cargs, timeout=30)
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
    pip_args = getattr(args, "pip_args", None)
    if pip_args:
        code = f"import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'pip', 'install', {json.dumps(' '.join(pkgs))}] + {json.dumps(shlex.split(pip_args))})\n"
        return cmd_exec_custom(args, s, code)
    stdout, stderr, rc = run_colab(cargs, timeout=120)
    check_rc(stdout, stderr, rc, "install-failed", stdout.strip())
    write_output(stdout, args.out, args.json)


def cmd_exec_custom(args, session, code):
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
    code = (
        "import subprocess\n"
        "r = subprocess.run(%s, shell=True, capture_output=True, text=True)\n"
        "if r.stdout: print(r.stdout, end='')\n"
        "if r.stderr: print(r.stderr, end='')"
    ) % json.dumps(cmd_str)
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
    _pre_auth_check()
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
                code = (
                    "import os\ntry:\n"
                    "    size = os.path.getsize('%s')\n"
                    "    if size > %d:\n"
                    "        with open('%s') as f:\n"
                    "            f.seek(%d)\n"
                    "            print(f.read(), end='')\n"
                    "        print('\\n__SIZE__:' + str(size))\n"
                    "except Exception as e:\n"
                    "    print('__ERR__:' + str(e))"
                ) % (filepath, last_size, filepath, last_size)
                stdout, stderr, rc = _remote_pexec(s, code, timeout=10)
                if rc == 0 and stdout.strip():
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
        code = (
            "import subprocess\n"
            "r = subprocess.run(['tail', '-n', str(%d), '%s'], capture_output=True, text=True)\n"
            "print(r.stdout, end='')\n"
            "if r.stderr: print(r.stderr, end='')"
        ) % (lines, filepath)
        stdout, stderr, rc = _remote_pexec(s, code, timeout=15)
        check_rc(stdout, "", rc, "logs-failed", stdout.strip())
        write_output(stdout, args.out, args.json)


def cmd_check(args):
    """Pre-flight model check: test imports before full deployment."""
    s = _resolve_session(args) or args.session
    code = args.code
    if not code:
        die("check-no-code", "Provide --code with Python to test")
    print(f"Running pre-flight check on {s or 'auto'}...")
    stdout, stderr, rc = _remote_pexec(s, code, timeout=getattr(args, "timeout", 60) or 60)
    if rc == 0:
        write_output(f"PASS:\n{stdout}", args.out, args.json, {"status": "pass"})
    else:
        write_output(f"FAIL:\n{stdout}", args.out, args.json, {"status": "fail"})


def cmd_notebook(args):
    s = _resolve_session(args) or args.session
    action = getattr(args, "nb_action", "export")
    if action == "export":
        out = getattr(args, "output", None) or f"/tmp/{s or 'colab'}_export.ipynb"
        cargs = ["log", "-o", out]
        if s: cargs.extend(["-s", s])
        stdout, stderr, rc = run_colab(cargs, timeout=30)
        check_rc(stdout, stderr, rc, "notebook-failed", stdout.strip())
        write_output(f"Notebook exported to: {out}\n{stdout.strip()}", args.out, args.json, {"notebook_path": out})
    elif action == "execute":
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
    write_output(f"colab-cli v3.3\n{stdout.strip()}", args.out, args.json)


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


# ─── Browser-based ─────────────────────────────────────────────────────────

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
        page.wait_for_selector('button:has-text("Secrets")', timeout=15000)
        secrets_btn = page.locator('button:has-text("Secrets")')
        if secrets_btn.count() == 0: die("secrets-not-found", "Secrets panel missing.")
        secrets_btn.first.click()
        page.wait_for_timeout(500)
        if action == "list":
            result["secrets"] = _parse_secrets_list(page.locator("body").inner_text())
        elif action == "set":
            add_btn = page.locator('button:has-text("Add new secret")')
            if add_btn.count() > 0:
                add_btn.first.click()
                page.wait_for_selector('input[placeholder*="Name"]', timeout=5000)
                ni = page.locator('input[placeholder*="Name"]')
                if ni.count() > 0: ni.first.fill(key)
                vi = page.locator('input[placeholder*="Value"]')
                if vi.count() > 0: vi.first.fill(value)
                page.keyboard.press("Enter")
                page.wait_for_timeout(500)
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
                page.wait_for_timeout(1000)
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


# ─── P1: recover — rebuild sessions.json from the backend ──────────────────
# The colab-cli registry (sessions.json) can be wiped by crashes or the
# upstream CLI's destructive cleanup. This rebuilds it from the server's
# list_assignments() so a live VM is NOT lost (a wiped registry previously
# caused 40GB of models to be re-downloaded).

def cmd_recover(args):
    code = (
        "import sys; sys.path.insert(0, '/usr/local/lib/python3.12/dist-packages')\n"
        "from colab_cli.common import state\n"
        "import json, os\n"
        "try:\n"
        "    assignments = state.client.list_assignments()\n"
        "except Exception as e:\n"
        "    print('LIST_FAIL ' + str(e)[:200]); sys.exit(1)\n"
        "store_path = os.path.expanduser('~/.config/colab-cli/sessions.json')\n"
        "store = json.load(open(store_path)) if os.path.exists(store_path) else {}\n"
        "changed = False\n"
        "for a in assignments:\n"
        "    ep = a.endpoint\n"
        "    hit = None\n"
        "    for name, s in store.items():\n"
        "        if s.get('endpoint') == ep:\n"
        "            hit = (name, s); break\n"
        "    if hit:\n"
        "        name, s = hit\n"
        "        s['url'] = a.runtime_proxy_info.url\n"
        "        s['token'] = a.runtime_proxy_info.token\n"
        "        changed = True\n"
        "    else:\n"
        "        tail = ep.split('-')[-1][:10]\n"
        "        store['recovered_' + tail] = {\n"
        "            'name': ep, 'url': a.runtime_proxy_info.url,\n"
        "            'token': a.runtime_proxy_info.token, 'endpoint': ep,\n"
        "            'variant': getattr(a, 'variant', 'GPU'),\n"
        "            'accelerator': getattr(a, 'accelerator', 'T4')}\n"
        "        changed = True\n"
        "if changed:\n"
        "    json.dump(store, open(store_path, 'w'), indent=2)\n"
        "    print('RECOVERED ' + json.dumps({k: v.get('endpoint') for k, v in store.items()}))\n"
        "else:\n"
        "    print('NO_ASSIGNMENTS')\n"
    )
    tmp = COLAB_HOME / "recover.py"
    tmp.write_text(code)
    try:
        r = subprocess.run([_py_for_scripts(), str(tmp)], capture_output=True,
                           text=True, timeout=120, env=_clean_env())
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        out = "timeout"
    if "RECOVERED" in out:
        write_output(out, args.out, args.json, {"status": "recovered"})
    elif "NO_ASSIGNMENTS" in out:
        write_output("No live assignments found. Is the VM still running?",
                     args.out, args.json, {"status": "no-assignments"})
    else:
        die("recover-failed", out.strip()[-400:])


# ─── P2: reauth — switch the Colab account (manual OAuth flow) ─────────────
# Free-tier GPU quota is per-account; when `new` returns 503 outcome:2 the
# account is exhausted for the day. Re-auth as another Google account and
# keep the same session name. The flow prints an AUTH_URL; open it in a
# browser, approve, and the token is saved.

def cmd_reauth(args):
    hint = getattr(args, "account", None)
    code = f'''
import sys, os, json, threading, time
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, "/usr/local/lib/python3.12/dist-packages")
os.environ.setdefault("PYTHONPATH", "")
from colab_cli.auth import PUBLIC_SCOPES
from google_auth_oauthlib.flow import InstalledAppFlow
PORT = 8765
cfg = None
for c in [os.path.expanduser("~/.colab-cli-oauth-config.json"),
          "/usr/local/lib/python3.12/dist-packages/colab_cli/oauth_config.json"]:
    if os.path.exists(c):
        cfg = json.load(open(c)); break
if not cfg:
    print("NO_CLIENT_CONFIG"); sys.exit(1)
flow = InstalledAppFlow.from_client_config(cfg, PUBLIC_SCOPES)
flow.redirect_uri = f"http://localhost:{{PORT}}"
kwargs = dict(access_type="offline", prompt="consent",
              include_granted_scopes="true")
{"if hint: kwargs['login_hint'] = %r" % hint}
auth_url, _ = flow.authorization_url(**kwargs)
print("AUTH_URL: " + auth_url, flush=True)
holder = {{}}
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if "code" in q:
            holder["code"] = q["code"][0]
            self.send_response(200); self.end_headers()
            self.wfile.write(b"OK - close this tab")
        else:
            self.send_response(400); self.end_headers(); self.wfile.write(b"no code")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
deadline = time.time() + 900
while "code" not in holder and time.time() < deadline:
    time.sleep(1)
if "code" not in holder:
    print("TIMEOUT"); sys.exit(1)
flow.fetch_token(code=holder["code"])
open(os.path.expanduser("~/.config/colab-cli/token.json"), "w").write(
    flow.credentials.to_json())
print("SAVED " + ",".join(flow.credentials.scopes or []), flush=True)
'''
    tmp = COLAB_HOME / "reauth_flow.py"
    tmp.write_text(code)
    print("Open the AUTH_URL below in a browser, approve, and the CLI account "
          "switches. (See references/auth_flow.md for the manual flow.)")
    r = subprocess.run([_py_for_scripts(), str(tmp)], capture_output=True,
                       text=True, timeout=960, env=_clean_env())
    out = (r.stdout or "") + (r.stderr or "")
    if "SAVED" in out:
        write_output("Token saved; scopes: " + out.split("SAVED ", 1)[1],
                     args.out, args.json, {"status": "reauth-ok"})
    else:
        die("reauth-failed", out.strip()[-400:])


# ─── P3: patch — apply the 401/404-refresh reliability patch ──────────────
def cmd_patch(args):
    here = Path(__file__).resolve().parent
    script = here / "patch_colab_cli.py"
    if not script.exists():
        die("patch-no-script", f"{script} not found in repo")
    r = subprocess.run([_py_for_scripts(), str(script)], capture_output=True,
                       text=True, timeout=120, env=_clean_env())
    out = (r.stdout or "") + (r.stderr or "")
    if "patched" in out or "already patched" in out:
        write_output(out.strip(), args.out, args.json, {"status": "patched"})
    else:
        die("patch-failed", out.strip()[-400:])


# ─── Training-track housing (v3.3) ─────────────────────────────────────────
# Failure modes harvested from every failing arXiv training-track run
# (Oct 2026: elitereplay, tf-opd, normtune, lorasuper, toolselect, SRD):
#  1. token refresh behind proxy (raw CLI's google-auth ignores proxy env)
#  2. phantom READY: session "created" but runtime proxy 404s, VM unusable
#  3. gpu-unavailable / quota with no cross-account retry
#  4. wiped session registry: isolated --config file back as {} after death,
#     so "session not found" was indistinguishable from "reclaimed"
#  5. silent driver deaths: no heartbeat, no death classification, no
#     result download on death
#  6. wrapper/raw interface mismatch pushing agents onto the raw CLI
# Reclamation itself cannot be prevented (free tier); everything here is
# about detecting it fast, classifying it honestly, and resuming cheaply.

REGISTRY_FILE = COLAB_HOME / "train-registry.json"


def _active_account():
    """Name of the currently active Colab account (token.json symlink)."""
    try:
        target = os.readlink(TOKEN_FILE)
        base = os.path.basename(target)
        if base.startswith("token.") and base.endswith(".json"):
            return base[len("token."):-len(".json")]
    except OSError:
        pass
    return None


def _registry_load():
    try:
        if REGISTRY_FILE.exists():
            return json.loads(REGISTRY_FILE.read_text())
    except Exception:
        pass
    return {}


def _registry_save(d):
    try:
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_FILE.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


def _registry_record(name, account, extra=None):
    d = _registry_load()
    rec = {"account": account,
           "created_at": datetime.now(timezone.utc).isoformat()}
    if extra:
        rec.update(extra)
    d[name] = rec
    _registry_save(d)


def _registry_remove(name):
    d = _registry_load()
    if name in d:
        del d[name]
        _registry_save(d)


def _server_sessions():
    """Names of sessions the server currently reports (best effort)."""
    stdout, stderr, rc = run_colab(["sessions"], timeout=30)
    if rc != 0:
        return None
    return {s["name"] for s in format_sessions_list(stdout)}


def _server_session_map():
    """{session name: endpoint} from the server list. None if the list
    call itself failed (distinct from an empty list, which is {})."""
    stdout, stderr, rc = run_colab(["sessions"], timeout=30)
    if rc != 0:
        return None
    return {s["name"]: s.get("endpoint") for s in format_sessions_list(stdout)
            if s.get("endpoint")}


def _classify_missing(name):
    """A session absent from the server list is RECLAIMED if the wrapper
    registry knows it (it existed and Colab took it), UNKNOWN otherwise
    (never created, or created outside this wrapper)."""
    if name in _registry_load():
        return "reclaimed"
    return "unknown"


def _token_expiry(path):
    """ISO expiry of a token file, or None if unreadable."""
    try:
        creds = json.loads(Path(path).read_text())
        return creds.get("expiry")
    except Exception:
        return None


def _token_expires_within(path, seconds):
    exp = _token_expiry(path)
    if not exp:
        return True
    try:
        if isinstance(exp, (int, float)):
            ts = float(exp)
        else:
            ts = datetime.fromisoformat(
                str(exp).replace("+00:00", "").replace("Z", "")).timestamp()
        return (ts - datetime.now(timezone.utc).timestamp()) < seconds
    except Exception:
        return True


def cmd_token(args):
    """Token ops: refresh the active account, refresh the whole rotation
    pool, or show expiries. Never prints secret values."""
    action = getattr(args, "token_action", "status")
    if action == "refresh":
        ok, detail = _refresh_token_file(TOKEN_FILE)
        if ok:
            write_output(f"Token refreshed for {_active_account()}; expiry {detail}",
                         args.out, args.json,
                         {"account": _active_account(), "expiry": detail})
        else:
            die("token-refresh-failed",
                f"Refresh failed for {_active_account()}: {detail}. "
                "invalid-grant means the refresh token is dead — re-do the "
                "browser OAuth code flow (references/auth_flow.md).")
    elif action == "refresh-all":
        results = {}
        for name in ROTATION_POOL:
            if name in ROTATION_EXCLUDED:
                continue
            p = TOKEN_DIR / f"token.{name}.json"
            if not p.exists():
                results[name] = "no-token-file"
                continue
            if not getattr(args, "force", False) and not _token_expires_within(p, 3600):
                results[name] = "fresh-skip"
                continue
            ok, detail = _refresh_token_file(p)
            results[name] = "ok" if ok else f"fail:{detail}"
        write_output(json.dumps(results, indent=2), args.out, args.json,
                     {"results": results})
    else:  # status
        rows = {}
        for name in ROTATION_POOL:
            if name in ROTATION_EXCLUDED:
                continue
            p = TOKEN_DIR / f"token.{name}.json"
            rows[name] = _token_expiry(p) if p.exists() else None
        rows["_active"] = _active_account()
        write_output(json.dumps(rows, indent=2), args.out, args.json, rows)


def _switch_account(name):
    """Point token.json at token.<name>.json. Refuses excluded accounts."""
    if name in ROTATION_EXCLUDED:
        die("account-excluded",
            f"{name} belongs to another agent and must not be touched. "
            "Use colab-use-account directly if you truly mean it.")
    p = TOKEN_DIR / f"token.{name}.json"
    if not p.exists():
        die("account-unknown", f"No token file for account {name}")
    try:
        if TOKEN_FILE.exists() or TOKEN_FILE.is_symlink():
            TOKEN_FILE.unlink()
        TOKEN_FILE.symlink_to(f"token.{name}.json")
    except OSError as e:
        die("account-switch-failed", str(e))
    return name


def _read_rotation_state(state_path):
    """Last-used account from the rotation state file, or None."""
    try:
        text = Path(state_path).read_text().strip()
        if text:
            return text.split()[-1]
    except Exception:
        pass
    return None


def _write_rotation_state(state_path, account):
    try:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text(
            f"{datetime.now().strftime('%Y-%m-%d')} {account}\n")
    except Exception:
        pass


def _next_rotation_account(state_path):
    """Next account after the last-used one; wraps around the pool."""
    last = _read_rotation_state(state_path)
    if last in ROTATION_POOL:
        return ROTATION_POOL[(ROTATION_POOL.index(last) + 1) % len(ROTATION_POOL)]
    return ROTATION_POOL[0]


def cmd_account(args):
    """Account ops: list, switch, or rotate to the next training-track
    account. Rotation never selects an excluded account."""
    action = getattr(args, "account_action", "list")
    if action == "list":
        rows = {}
        for p in sorted(TOKEN_DIR.glob("token.*.json")):
            name = p.name[len("token."):-len(".json")]
            rows[name] = {"active": name == _active_account(),
                          "expiry": _token_expiry(p)}
        write_output(json.dumps(rows, indent=2), args.out, args.json, rows)
    elif action == "switch":
        name = getattr(args, "name", None)
        if not name:
            die("account-no-name", "Provide an account name")
        _switch_account(name)
        ok, detail = _refresh_token_file(TOKEN_FILE)
        write_output(f"Active account: {name} (token refresh: "
                     f"{'ok' if ok else 'fail:' + detail})",
                     args.out, args.json,
                     {"account": name, "refresh": ok})
    elif action == "rotate":
        state_path = getattr(args, "rotation_file", None) or str(ROTATION_STATE_DEFAULT)
        nxt = _next_rotation_account(state_path)
        _switch_account(nxt)
        ok, detail = _refresh_token_file(TOKEN_FILE)
        _write_rotation_state(state_path, nxt)
        write_output(f"Rotated to {nxt} (token refresh: "
                     f"{'ok' if ok else 'fail:' + detail})",
                     args.out, args.json,
                     {"account": nxt, "refresh": ok,
                      "state_file": state_path})


def _health_check(session, attempts=3, gap=10):
    """True if the session's kernel answers a trivial exec. A session
    that passes `new` but fails this is a phantom (normtune Oct 2026:
    runtime proxy 404s, VM never usable) — never let a run start on it."""
    for i in range(attempts):
        stdout, stderr, rc = run_colab(
            ["exec", "-s", session, "--timeout", "30"], timeout=45,
            stdin_data="print('HEALTH_OK')")
        if rc == 0 and "HEALTH_OK" in stdout:
            return True
        if i < attempts - 1:
            time.sleep(gap)
    return False


def _launch_detached(session, remote_script, log_file, env_map):
    """Launch a script on the VM detached (Popen, start_new_session).
    Returns (pid, log_file) or dies."""
    env_code = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}"
                         for k, v in env_map.items())
    launch_code = (
        "import subprocess, sys, os\n"
        "p = subprocess.Popen(\n"
        "    [sys.executable, %s],\n"
        "    stdout=open(%s, 'w'), stderr=subprocess.STDOUT,\n"
        "    start_new_session=True, cwd='/content',\n"
        "    env={**os.environ, %s})\n"
        "print(f'PID:{p.pid}|LOG:%s')"
    ) % (json.dumps(remote_script), json.dumps(log_file), env_code, log_file)
    stdout, stderr, rc = _remote_pexec(session, launch_code, timeout=20)
    if rc == 0 and "PID:" in stdout:
        pid = stdout.strip().split("PID:")[1].split("|")[0].strip()
        return pid, log_file
    die("train-launch-failed", (stdout or "").strip()[-300:])


def _vm_log_has_marker(session, path, marker):
    """True if the done-marker appears ANYWHERE in the log. A tail-window
    check misses it when the driver prints a long block after the marker
    (verify3, Oct 2026: a ~56-line results JSON after DONE. metrics fell
    outside the 40-line tail, so a completed run read as DIED). Grep the
    whole file; print only FOUND/MISS, never the content."""
    code = ("try:\n"
            "    data = open(%s, errors='ignore').read()\n"
            "    print('FOUND' if %s in data else 'MISS')\n"
            "except Exception:\n"
            "    print('MISS')") % (json.dumps(path), json.dumps(marker))
    stdout, stderr, rc = _remote_pexec(session, code, timeout=30)
    return rc == 0 and "FOUND" in stdout


def _vm_tail(session, path, lines=30):
    code = ("import subprocess\n"
            "r = subprocess.run(['tail', '-n', str(%d), %s], capture_output=True, text=True)\n"
            "print(r.stdout, end='')") % (lines, json.dumps(path))
    stdout, stderr, rc = _remote_pexec(session, code, timeout=20)
    return stdout if rc == 0 else ""


def _vm_pid_alive(session, pid):
    """True only if the PID is a LIVE process. A zombie (defunct) process
    still answers `ps -p` with returncode 0, so the state must be checked
    explicitly — otherwise a dead driver reads as alive and the monitor
    waits out its whole budget (toolselect-verify, Oct 2026: PID 694
    defunct, monitor would have sat until TIMEOUT instead of DIED)."""
    code = ("import subprocess\n"
            "r = subprocess.run(['ps', '-o', 'stat=', '-p', %s], capture_output=True, text=True)\n"
            "st = (r.stdout or '').strip()\n"
            "print('ALIVE' if (r.returncode == 0 and st and 'Z' not in st) else 'GONE')") % json.dumps(str(pid))
    stdout, stderr, rc = _remote_pexec(session, code, timeout=20)
    return rc == 0 and "ALIVE" in stdout


def _vm_file_exists(session, path):
    code = "import os; print('YES' if os.path.exists(%s) else 'NO')" % json.dumps(path)
    stdout, stderr, rc = _remote_pexec(session, code, timeout=20)
    return rc == 0 and "YES" in stdout


def cmd_train(args):
    """House one daily training-track run end to end: account rotation,
    token refresh, session create (with health check + cross-account retry
    on gpu-unavailable/provision failure), driver upload, detached launch,
    supervised monitoring, result download, honest outcome classification.

    Outcome vocabulary (mirrors the arXiv verdicts, but for the RUN):
      DONE      — done-marker seen in the log (or done-file exists)
      RECLAIMED — session vanished from the server mid-run (free tier)
      DIED      — driver process gone with no marker (silent death; check
                  the downloaded log tail for the last lines)
      TIMEOUT   — monitor budget exhausted, run still going (session left
                  up unless --no-keep is passed; re-run with --watch-only)
    A DONE run still needs its metrics judged by the paper's own criteria;
    this command classifies the run, not the paper."""
    _pre_auth_check()
    gpu = getattr(args, "gpu", "T4") or "T4"
    session = getattr(args, "session", None)
    if not session:
        session = f"train-{datetime.now().strftime('%m%d-%H%M')}"
    log_file = getattr(args, "log", None) or "/content/train_run.log"
    marker = getattr(args, "done_marker", None) or "TRAIN_DONE"
    poll = max(15, getattr(args, "poll", 60) or 60)
    budget = max(60, getattr(args, "monitor_timeout", 7200) or 7200)
    out_dir = Path(getattr(args, "out_dir", None) or
                   (COLAB_HOME / "train-runs" / session))
    out_dir.mkdir(parents=True, exist_ok=True)

    # -- Resolve account -------------------------------------------------
    account = _active_account()
    if getattr(args, "rotate", False):
        state_path = getattr(args, "rotation_file", None) or str(ROTATION_STATE_DEFAULT)
        account = _next_rotation_account(state_path)
        _switch_account(account)
        _write_rotation_state(state_path, account)
    elif getattr(args, "account", None):
        account = _switch_account(args.account)
    ok, detail = _refresh_token_file(TOKEN_FILE)
    if not ok:
        die("train-auth-failed",
            f"Token refresh failed for {account}: {detail}. "
            "invalid-grant means the refresh token is dead — re-do the "
            "browser OAuth code flow (references/auth_flow.md).")

    summary = {"session": session, "account": account, "gpu": gpu,
               "log": log_file, "outcome": None, "downloaded": []}

    if not getattr(args, "watch_only", False):
        # -- Create with health check + cross-account retry --------------
        tried = []
        created = False
        candidates = [account] + [a for a in ROTATION_POOL if a != account]
        for cand in candidates[:max(1, getattr(args, "max_accounts", 5) or 5)]:
            if cand != account:
                _switch_account(cand)
                account = cand
                ok, detail = _refresh_token_file(TOKEN_FILE)
                if not ok:
                    tried.append({"account": cand, "error": f"auth:{detail}"})
                    continue
            stdout, stderr, rc = run_colab(
                ["new", "-s", session, "--gpu", gpu], timeout=180)
            if rc != 0:
                code, _ = parse_error(stdout, stderr)
                tried.append({"account": cand, "error": code or "new-failed"})
                if code not in ("gpu-unavailable", "capacity", "quota-exceeded"):
                    break
                continue
            if getattr(args, "no_health_check", False) or _health_check(session):
                created = True
                break
            tried.append({"account": cand, "error": "provision-failed"})
            # Phantom session: stop it so it does not linger, then try next.
            run_colab(["stop", "-s", session], timeout=60)
        summary["create_attempts"] = tried
        summary["account"] = account
        if getattr(args, "rotate", False):
            # Fallback may have moved to a different account than the one
            # rotation picked (verify5, Oct 2026: picked ppoppo205, ran on
            # cyc236es). The state file must name the account that actually
            # ran, or the next day repeats it.
            _write_rotation_state(state_path, account)
        if not created:
            summary["outcome"] = "PROVISION_FAILED"
            (out_dir / "train-run.json").write_text(json.dumps(summary, indent=2))
            write_output(json.dumps(summary, indent=2), args.out, args.json, summary)
            return
        _registry_record(session, account, {"gpu": gpu, "log": log_file})
        # Record the endpoint too: a session created under an isolated
        # registry shows up as "[?]" in the global list, so the monitor
        # must be able to match by endpoint as well as by name.
        smap = _server_session_map()
        if smap and session in smap:
            summary["endpoint"] = smap[session]
            _registry_record(session, account,
                             {"gpu": gpu, "log": log_file,
                              "endpoint": smap[session]})

        # -- Upload + launch ----------------------------------------------
        driver = Path(args.driver)
        if not driver.exists():
            die("train-no-driver", f"Driver not found: {args.driver}")
        remote_driver = f"/content/{driver.name}"
        stdout, stderr, rc = run_colab(
            ["upload", "-s", session, str(driver), remote_driver], timeout=120)
        check_rc(stdout, stderr, rc, "upload-failed", "Driver upload failed")
        for extra in (getattr(args, "upload", None) or []):
            local, _, remote = extra.partition(":")
            remote = remote or f"/content/{Path(local).name}"
            stdout, stderr, rc = run_colab(
                ["upload", "-s", session, local, remote], timeout=120)
            check_rc(stdout, stderr, rc, "upload-failed", f"Upload failed: {local}")
        env_map = {}
        for e in (getattr(args, "env", None) or []):
            k, _, v = e.partition("=")
            if k:
                env_map[k] = v
        pid, _ = _launch_detached(session, remote_driver, log_file, env_map)
        summary["pid"] = pid
        (out_dir / "train-run.json").write_text(json.dumps(summary, indent=2))
    else:
        pid = getattr(args, "pid", None)
        summary["pid"] = pid
        summary["watch_only"] = True
        reg = _registry_load().get(session, {})
        if reg.get("endpoint"):
            summary["endpoint"] = reg["endpoint"]
        if reg.get("account"):
            # The registry names the account that created the session;
            # the active token may have moved since (shared VM).
            summary["account"] = reg["account"]

    # -- Monitor ----------------------------------------------------------
    deadline = time.time() + budget
    last_tail = ""
    outcome = "TIMEOUT"
    while time.time() < deadline:
        tail = _vm_tail(session, log_file, lines=40)
        if tail:
            last_tail = tail
        if _vm_log_has_marker(session, log_file, marker):
            outcome = "DONE"
            break
        if getattr(args, "done_file", None) and _vm_file_exists(session, args.done_file):
            outcome = "DONE"
            break
        server = _server_session_map()
        if server is not None:
            ep = summary.get("endpoint")
            if session not in server and not (ep and ep in set(server.values())):
                outcome = "RECLAIMED"
                break
        pid = summary.get("pid")
        if pid and not _vm_pid_alive(session, pid):
            # Process gone: re-check once after a short gap (log flush race).
            time.sleep(10)
            tail2 = _vm_tail(session, log_file, lines=40)
            if tail2:
                last_tail = tail2
            if (not _vm_log_has_marker(session, log_file, marker)
                    and not _vm_pid_alive(session, pid)):
                outcome = "DIED"
                break
        time.sleep(poll)
    summary["outcome"] = outcome
    summary["log_tail"] = last_tail[-2000:] if last_tail else ""

    # -- Download results + teardown --------------------------------------
    if outcome in ("DONE", "DIED", "TIMEOUT"):
        for remote in (getattr(args, "results", None) or []):
            local = out_dir / Path(remote).name
            stdout, stderr, rc = run_colab(
                ["download", "-s", session, remote, str(local)], timeout=120)
            if rc == 0 and local.exists():
                summary["downloaded"].append(str(local))
    if outcome in ("DONE", "DIED", "RECLAIMED"):
        _registry_remove(session)
    if not getattr(args, "keep", False) and outcome in ("DONE", "DIED"):
        run_colab(["stop", "-s", session], timeout=60)
    if outcome == "RECLAIMED":
        # Server already took it; drop the stale registry entry only.
        _registry_remove(session)
    (out_dir / "train-run.json").write_text(json.dumps(summary, indent=2))
    write_output(json.dumps(summary, indent=2), args.out, args.json, summary)


# ─── CLI ───────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(description="colab-cli v3.3 — AI-agent-native CLI for Google Colab",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    shared_output = argparse.ArgumentParser(add_help=False)
    shared_output.add_argument("-o", "--out", help="Write output to FILE, stdout gets JSON pointer")
    shared_output.add_argument("--json", action="store_true", help="Structured JSON output")
    shared_output.add_argument("--registry-file", help="Isolated session-registry file "
        "(passed as --config to the raw CLI; keeps this run's sessions safe "
        "from another agent's global account switches)")

    sub = p.add_subparsers(dest="command")

    # Session management
    pn = sub.add_parser("new", parents=[shared_output], help="Create session")
    pn.add_argument("-s", "--session"); pn.add_argument("--gpu", choices=["T4","L4","G4","H100","A100"]); pn.add_argument("--tpu", choices=["v5e1","v6e1"])
    pn.add_argument("--no-health-check", action="store_true", help="Skip the kernel health check after create")
    sub.add_parser("sessions", parents=[shared_output], help="List sessions")
    ps = sub.add_parser("status", parents=[shared_output], help="Session status"); ps.add_argument("-s", "--session")
    ps2 = sub.add_parser("stop", parents=[shared_output], help="Stop session"); ps2.add_argument("-s", "--session")
    pr = sub.add_parser("restart", parents=[shared_output], help="Restart kernel"); pr.add_argument("-s", "--session")
    pg = sub.add_parser("gpu_switch", parents=[shared_output], help="Switch GPU on session"); pg.add_argument("-s", "--session"); pg.add_argument("--gpu", choices=["T4","L4","G4","H100","A100"], required=True)

    # Execution
    pe = sub.add_parser("exec", parents=[shared_output], help="Execute code on VM")
    pe.add_argument("-s", "--session"); pe.add_argument("-f", "--file"); pe.add_argument("--code"); pe.add_argument("--output-image"); pe.add_argument("--timeout", type=float, default=30.0)

    # NEW: exec_detach — upload + run server code detached
    ped = sub.add_parser("exec_detach", parents=[shared_output], help="Upload script + run detached (for servers)")
    ped.add_argument("-s", "--session", required=True, help="Colab session name")
    ped.add_argument("-f", "--file", help="Local Python script to upload and run")
    ped.add_argument("--code", help="Python code to upload and run (if no -f)")
    ped.add_argument("--remote", help="Remote path (default: /content/<filename>)")
    ped.add_argument("--log", help="Log file path on VM (default: /dev/null)")
    ped.add_argument("--dir", default="/content", help="Working directory on VM (default: /content)")

    # NEW: exec_file — upload + exec in one step
    pef = sub.add_parser("exec_file", parents=[shared_output], help="Upload and execute a script in one step")
    pef.add_argument("-s", "--session", required=True, help="Colab session name")
    pef.add_argument("-f", "--file", required=True, help="Local Python script")
    pef.add_argument("--remote", help="Remote path (default: /content/<filename>)")
    pef.add_argument("--timeout", type=float, default=120.0, help="Script timeout in seconds")

    # Fixed exec_bg
    peb = sub.add_parser("exec_bg", parents=[shared_output], help="Background execution on VM")
    peb.add_argument("-s", "--session"); peb.add_argument("--code"); peb.add_argument("--timeout", type=float, default=600.0)
    pep = sub.add_parser("exec_bg_poll", parents=[shared_output], help="Poll background job"); pep.add_argument("job_id"); pep.add_argument("-s", "--session")

    # Other execution
    pr2 = sub.add_parser("run", parents=[shared_output], help="Ephemeral job"); pr2.add_argument("-f", "--file"); pr2.add_argument("--code"); pr2.add_argument("--gpu", choices=["T4","L4","G4","H100","A100"]); pr2.add_argument("--tpu", choices=["v5e1","v6e1"]); pr2.add_argument("--keep", action="store_true"); pr2.add_argument("args", nargs="*")
    pr3 = sub.add_parser("repl", parents=[shared_output], help="Python REPL"); pr3.add_argument("-s", "--session"); pr3.add_argument("--code"); pr3.add_argument("--output-image"); pr3.add_argument("--timeout", type=float, default=60.0)
    pc = sub.add_parser("console", parents=[shared_output], help="Shell command on VM"); pc.add_argument("-s", "--session"); pc.add_argument("--cmd", help="Shell command to run"); pc.add_argument("--timeout", type=float, default=60.0)

    # NEW: tunnel discover
    ptd = sub.add_parser("tunnel_discover", parents=[shared_output], help="Auto-discover live tunnel URL from VM")
    ptd.add_argument("-s", "--session", required=True, help="Colab session name")

    # VM file ops
    plogs = sub.add_parser("logs", parents=[shared_output], help="Tail VM file")
    plogs.add_argument("-s", "--session"); plogs.add_argument("filepath", help="Path on VM (e.g. /content/server.log)")
    plogs.add_argument("-n", "--lines", type=int, default=50, help="Lines to show")
    plogs.add_argument("-f", "--follow", action="store_true", help="Stream output (Ctrl+C to stop)")

    # File ops
    pl = sub.add_parser("ls", parents=[shared_output], help="List files on VM"); pl.add_argument("-s", "--session"); pl.add_argument("path", nargs="?")
    pu = sub.add_parser("upload", parents=[shared_output], help="Upload file to VM"); pu.add_argument("-s", "--session"); pu.add_argument("local"); pu.add_argument("remote")
    pd = sub.add_parser("download", parents=[shared_output], help="Download file from VM"); pd.add_argument("-s", "--session"); pd.add_argument("remote"); pd.add_argument("local")
    prm = sub.add_parser("rm", parents=[shared_output], help="Delete file on VM"); prm.add_argument("-s", "--session"); prm.add_argument("path")
    pe2 = sub.add_parser("edit", parents=[shared_output], help="Edit remote file"); pe2.add_argument("-s", "--session"); pe2.add_argument("remote_path")

    # Automation
    pi = sub.add_parser("install", parents=[shared_output], help="Install packages on VM"); pi.add_argument("-s", "--session"); pi.add_argument("-r", "--requirements"); pi.add_argument("--pip-args", help="Extra pip flags"); pi.add_argument("packages", nargs="*")
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
    prc = sub.add_parser("recover", parents=[shared_output], help="Rebuild sessions.json from backend (fixes wiped registry)")
    pra = sub.add_parser("reauth", parents=[shared_output], help="Re-auth / switch Colab account (per-account GPU quota)"); pra.add_argument("--account", help="login_hint email")
    ppp = sub.add_parser("patch", parents=[shared_output], help="Patch installed colab_cli: 401/404 -> refresh token + retry")

    # Training-track housing (v3.3)
    ptok = sub.add_parser("token", parents=[shared_output], help="OAuth token ops (never prints secrets)")
    ptok.add_argument("token_action", nargs="?", default="status", choices=["status", "refresh", "refresh-all"])
    ptok.add_argument("--force", action="store_true", help="Refresh-all: refresh even fresh tokens")
    pacc = sub.add_parser("account", parents=[shared_output], help="Colab account ops + daily rotation")
    pacc.add_argument("account_action", nargs="?", default="list", choices=["list", "switch", "rotate"])
    pacc.add_argument("name", nargs="?", help="Account for switch")
    pacc.add_argument("--rotation-file", help="Rotation state file (default: arxiv goal hidden_files)")
    ptr = sub.add_parser("train", parents=[shared_output], help="House one daily training-track run end to end")
    ptr.add_argument("--driver", required=True, help="Local driver script to upload + launch")
    ptr.add_argument("-s", "--session", help="Session name (default train-MMDD-HHMM)")
    ptr.add_argument("--gpu", choices=["T4", "L4", "G4", "H100", "A100"], default="T4")
    ptr.add_argument("--account", help="Switch to this account first (refuses excluded accounts)")
    ptr.add_argument("--rotate", action="store_true", help="Rotate to the next daily account first")
    ptr.add_argument("--rotation-file", help="Rotation state file (default: arxiv goal hidden_files)")
    ptr.add_argument("--max-accounts", type=int, default=5, help="Max accounts to try on provision failure")
    ptr.add_argument("--log", default="/content/train_run.log", help="Remote driver log path")
    ptr.add_argument("--done-marker", default="TRAIN_DONE", help="Log marker meaning the run finished")
    ptr.add_argument("--done-file", help="Remote file whose existence means the run finished")
    ptr.add_argument("--results", nargs="*", default=[], help="Remote result files to download on DONE/DIED/TIMEOUT")
    ptr.add_argument("--upload", nargs="*", default=[], help="Extra local files to upload (LOCAL or LOCAL:REMOTE)")
    ptr.add_argument("--env", nargs="*", default=[], help="Driver env vars (KEY=VALUE)")
    ptr.add_argument("--out-dir", help="Local dir for downloads + train-run.json")
    ptr.add_argument("--poll", type=int, default=60, help="Monitor poll interval seconds")
    ptr.add_argument("--monitor-timeout", type=int, default=7200, help="Monitor budget seconds")
    ptr.add_argument("--no-health-check", action="store_true", help="Skip kernel health check after create")
    ptr.add_argument("--keep", action="store_true", help="Do not stop the session on DONE/DIED")
    ptr.add_argument("--watch-only", action="store_true", help="Only monitor an existing session (no create/upload/launch)")
    ptr.add_argument("--pid", help="Driver PID for watch-only process-liveness checks")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command: parser.print_help(); sys.exit(1)
    global CONFIG_FILE
    CONFIG_FILE = getattr(args, "registry_file", None)
    if not COLAB or not os.path.isfile(COLAB):
        die("no-colab-cli", "google-colab-cli not found. Install: pip install google-colab-cli")

    dispatcher = {
        "new": cmd_new, "exec": cmd_exec, "exec_bg": cmd_exec_bg, "exec_bg_poll": cmd_exec_bg_poll,
        "exec_detach": cmd_exec_detach, "exec_file": cmd_exec_file,
        "run": cmd_run, "sessions": cmd_sessions, "status": cmd_status, "stop": cmd_stop,
        "gpu_switch": cmd_gpu_switch, "restart": cmd_restart,
        "ls": cmd_ls, "upload": cmd_upload, "download": cmd_download, "rm": cmd_rm,
        "install": cmd_install, "log": cmd_log, "notebook": cmd_notebook,
        "url": cmd_url, "drivemount": cmd_drivemount, "auth": cmd_auth,
        "console": cmd_console, "edit": cmd_edit, "repl": cmd_repl,
        "logs": cmd_logs, "check": cmd_check,
        "tunnel": cmd_tunnel, "tunnel_discover": cmd_tunnel_discover,
        "pay": cmd_pay, "version": cmd_version, "update": cmd_update, "whoami": cmd_whoami,
        "secrets": cmd_secrets, "resources": cmd_resources, "share": cmd_share,
        "recover": cmd_recover, "reauth": cmd_reauth, "patch": cmd_patch,
        "token": cmd_token, "account": cmd_account, "train": cmd_train,
    }
    try:
        dispatcher[args.command](args)
    except KeyboardInterrupt: die("interrupted", "Cancelled")
    except Exception as e: die("exception", str(e))


if __name__ == "__main__":
    main()
