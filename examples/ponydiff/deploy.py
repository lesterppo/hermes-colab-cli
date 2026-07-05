import os, time, subprocess, sys, urllib.request, re

PORT = 8000
OUTPUT_FILE = "/content/deploy_output.txt"

def log(msg):
    print(msg, flush=True)
    with open(OUTPUT_FILE, "a") as f:
        f.write(msg + "\n")

# Start FastAPI server
server_log = open("/content/server.log", "w")
p = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", str(PORT)],
    cwd="/content",
    stdout=server_log, stderr=server_log
)
log(f"Server PID: {p.pid}")

# Wait for model to load (can take 3-8 min)
log("Waiting for model to load (up to 8 min)...")
for i in range(96):
    time.sleep(5)
    if p.poll() is not None:
        server_log.seek(0)
        log(f"SERVER_DIED: {server_log.read()[-500:]}")
        sys.exit(1)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/health")
        resp = urllib.request.urlopen(req, timeout=3)
        data = resp.read().decode()
        if '"status":"ok"' in data:
            log("SERVER_READY")
            break
    except Exception:
        if i % 6 == 0:
            log(f"  still waiting... ({i*5}s)")
else:
    log("TIMEOUT: model failed to load in 8 min")
    sys.exit(1)

# Start Cloudflare tunnel
urllib.request.urlretrieve(
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    "/content/cf")
os.chmod("/content/cf", 0o755)
tlog = open("/content/tunnel.log", "w")
subprocess.Popen(["/content/cf", "tunnel", "--url", f"http://0.0.0.0:{PORT}"], stdout=tlog, stderr=tlog)
time.sleep(10)
log_text = open("/content/tunnel.log").read()
urls = re.findall(r'https://[a-zA-Z0-9.-]*\.trycloudflare\.com', log_text)
if urls:
    log(f"TUNNEL_URL: {urls[0]}")
else:
    log("TUNNEL_FAILED")
    log(log_text[-300:])
