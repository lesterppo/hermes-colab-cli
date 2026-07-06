#!/usr/bin/env python3 -u
"""Upload model cache tar.gz to multiple free services.
Tries: gofile.io → bashupload.com → file.io (split)
"""
import subprocess, sys, os, json, time, re

TARBALL = "/content/qwen-vl-cache.tar.gz"
DONE_FILE = "/content/drive_setup_done.txt"
CONFIG_FILE = "/content/deploy_config.json"
LOG_FILE = "/content/upload_result.txt"

def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def try_upload(service_name, curl_args, extract_url_fn):
    """Try uploading with given curl args. Returns URL or None."""
    log(f"Trying {service_name}...")
    try:
        r = subprocess.run(
            ["curl", "-s"] + curl_args + ["-w", "\n%{http_code}"],
            capture_output=True, text=True, timeout=3700
        )
        output = r.stdout.strip()
        lines = output.split("\n")
        http_code = lines[-1] if lines else "000"
        body = "\n".join(lines[:-1]) if len(lines) > 1 else output

        log(f"  HTTP {http_code}, response: {body[:200]}")

        if http_code in ("200", "201", "302"):
            url = extract_url_fn(body, http_code)
            if url:
                log(f"  SUCCESS: {url}")
                return url
        return None
    except subprocess.TimeoutExpired:
        log(f"  TIMEOUT")
        return None
    except Exception as e:
        log(f"  ERROR: {e}")
        return None

def finish(direct_url):
    """Write success files and print instructions."""
    log(f"FINAL URL: {direct_url}")
    with open(DONE_FILE, "w") as f:
        f.write(direct_url)
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        cfg = json.load(open(CONFIG_FILE))
    cfg["DRIVE_URL"] = direct_url
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)

    print(f"\n{'=' * 60}", flush=True)
    print(f"CACHE SNAPSHOT READY", flush=True)
    print(f"  {direct_url}", flush=True)
    print(f"{'=' * 60}", flush=True)
    print(f"\nRun locally:", flush=True)
    print(f"  qwen-chat drive-link {direct_url}", flush=True)
    return True

# ── Main ──────────────────────────────────────────────────────────
if not os.path.exists(TARBALL):
    log(f"ERROR: Tarball not found at {TARBALL}")
    sys.exit(1)

size_gb = os.path.getsize(TARBALL) / 1e9
log(f"Tarball ready: {size_gb:.1f} GB. Starting upload attempts...")

# Attempt 1: gofile.io (free, no auth, large files OK)
gofile_server = None
try:
    r = subprocess.run(["curl", "-s", "https://api.gofile.io/servers"],
                       capture_output=True, text=True, timeout=10)
    servers = json.loads(r.stdout)["data"]["servers"]
    gofile_server = servers[0]["name"]
    log(f"Gofile server: {gofile_server}")
except: pass

if gofile_server:
    url = try_upload(
        "gofile.io",
        ["-X", "POST", "-F", f"file=@{TARBALL}",
         "--connect-timeout", "30", "--max-time", "3600",
         f"https://{gofile_server}.gofile.io/uploadFile"],
        lambda body, code: (
            json.loads(body).get("data", {}).get("directLink", "") or
            json.loads(body).get("data", {}).get("downloadPage", "")
        ) if body else None
    )
    if url:
        finish(url)
        sys.exit(0)

# Attempt 2: bashupload.com (free, 50GB limit)
url = try_upload(
    "bashupload.com",
    ["-F", f"file=@{TARBALL}",
     "--connect-timeout", "30", "--max-time", "3600",
     "https://bashupload.com/"],
    lambda body, code: (
        re.search(r'(https?://[^\s"]+bashupload[^\s"]+)', body)
    ).group(1) if re.search(r'(https?://[^\s"]+bashupload[^\s"]+)', body) else None
)
if url:
    finish(url)
    sys.exit(0)

# Attempt 3: tmpfiles.org (10GB, free)
url = try_upload(
    "tmpfiles.org",
    ["-F", f"file=@{TARBALL}",
     "--connect-timeout", "30", "--max-time", "3600",
     "https://tmpfiles.org/api/v1/upload"],
    lambda body, code: (
        json.loads(body).get("data", {}).get("url", "")
        if body else None
    )
)
if url:
    # tmpfiles returns a relative URL like /dl/12345/filename
    if url.startswith("/"):
        url = "https://tmpfiles.org" + url
    finish(url)
    sys.exit(0)

# All failed
log("All upload services failed. Tarball remains at /content/qwen-vl-cache.tar.gz")
log("")
log("MANUAL RECOVERY OPTIONS:")
log("  1. Set HF_TOKEN: qwen-chat hf-token <token>")
log("     Get token: https://huggingface.co/settings/tokens")
log("  2. Download tarball locally, host it, and: qwen-chat drive-link <url>")
log("  3. Run: qwen-chat reconnect (uses standard download)")
sys.exit(1)
