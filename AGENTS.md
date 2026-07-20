# AGENTS.md — Hermes Colab CLI v3.2

Instructions for AI coding assistants using colab.py v3.2: operating Google
Colab VMs and deploying models on free GPU runtimes.

## What This Is

**Colab CLI** (`colab.py`) — token-efficient wrapper around Google's
`google-colab-cli`. 36 commands for session management, code execution,
file ops, VM log streaming, tunnel discovery, and model deployment.

Pony Diffusion, Z-Image-Turbo, and Qwen2.5-VL-3B deployment scripts + local CLIs included.

## File Structure

```
hermes-colab-cli/
├── colab.py              # Colab CLI v3.2 (36 commands, 1139 lines)
├── pony.py               # Pony Diffusion local CLI chatbox
├── zimage/               # Z-Image-Turbo deploy + CLI
├── install.sh            # One-line installer
├── AGENTS.md             # This file
├── README.md             # Human-readable overview
├── SKILL.md              # Hermes skill format
├── examples/
│   ├── ponydiff/         # Pony Diff deployment scripts
│   └── qwen-vl/          # Qwen2.5-VL-3B deployment + CLI (v3.2 NEW)
└── references/
    └── auth_flow.md      # Colab OAuth2 auth guide
```

## Installation & Auth

```bash
pip install google-colab-cli
# Then: ./install.sh
```

**Auth (one-time):** See `references/auth_flow.md`. The only reliable flow uses
`redirect_uri=http://localhost` (no PKCE, no port). v3.1 auto-refreshes tokens
every 5 minutes via background thread.

## Colab CLI Reference (colab.py v3.2)

### Session Management
```
new -s NAME --gpu T4          Create GPU session
sessions                        List all sessions
status -s NAME                  Session status
stop -s NAME                    Stop session (cleans tunnel URL)
restart -s NAME                 Restart kernel
gpu_switch -s NAME --gpu L4    Switch GPU type
```

### Execution
```
exec -s NAME --code "..."       Execute Python inline
exec -s NAME -f FILE            Execute from local file
exec_detach -s NAME -f FILE     Upload + run detached (for servers!)  ← NEW
exec_file -s NAME -f FILE       Upload + execute in one step           ← NEW
exec_bg -s NAME --code "..."    Background execution on VM
exec_bg_poll JOB_ID [-s NAME]   Poll background job
console -s NAME --cmd "..."     Shell command on VM
check -s NAME --code "..."      Pre-flight model test
```

### File Ops
```
upload -s NAME LOCAL REMOTE     Upload file
download -s NAME REMOTE LOCAL   Download file
ls -s NAME [PATH]               List VM files
logs -s NAME FILE [-n N] [-f]   Tail/stream VM file
```

### Tunnel & Auth
```
tunnel_discover -s NAME         Auto-discover live tunnel URL from VM  ← NEW
tunnel get -s NAME              Get saved tunnel URL
tunnel set --url URL -s NAME    Save tunnel URL
```

## v3.1 Key Improvements

1. **exec_detach** — THE way to launch long-running servers. Upload script,
   runs with `start_new_session`, returns PID immediately. No more blocking.

2. **tunnel_discover** — auto-greps VM for `trycloudflare.com` URLs in common
   log locations. Auto-saves found URLs. No more manual tunnel set.

3. **Retry logic** — 2 retries on transient Colab errors (502/503/timeout,
   connection reset). Survives Colab backend flakiness.

4. **Auto-refresh OAuth** — background daemon thread checks token expiry
   every 5 min. Prevents mid-deployment auth death.

5. **exec_file** — upload + execute in one command. Two round-trips collapsed
   into one.

6. **Security fixes** — shell injection in `console` fixed (json.dumps
   escaping). `tunnel_discover` shell=True replaced with list form.

### Output format notes

- `exec` returns output directly. Use for short commands (< 2 min).
- `exec_bg` runs in background. Use `--json` flag to get the job_id for polling:
  ```bash
  python3 colab.py exec_bg -s nbqa --json --timeout 300 --code "..."
  # Returns: {"job_id":"abc123","status":"running","poll":"exec_bg_poll abc123"}
  python3 colab.py exec_bg_poll abc123
  ```
- `upload` has a ~30s timeout. Files >10 MB should be downloaded directly on
  the Colab VM instead (use `exec` with `urllib.request.urlretrieve`).
- `upload` fails with HTTP 500 if parent directories don't exist on the VM.
  Create them first with `exec`:
  ```bash
  python3 colab.py exec -s nbqa --code "import os; os.makedirs('/root/.notebooklm/profiles/default', exist_ok=True)"
  ```

## Deployment Patterns

### Pattern 1: Quick LLM Deploy (exec_detach)

```bash
python3 colab.py new -s mydeploy --gpu T4
python3 colab.py exec_detach -s mydeploy \
    -f deploy_script.py --log /content/deploy.log

# Wait for deployment, then:
python3 colab.py tunnel_discover -s mydeploy
# tunnel_discover auto-saves the URL
python3 colab.py logs -s mydeploy /content/deploy.log -f
```

### Pattern 2: Pony Diffusion (legacy)

See original steps in examples/ponydiff/. Use `exec_bg` for long downloads,
then `logs -f` for progress monitoring.

### Pattern 3: Qwen2.5-VL-3B Deploy (v3.2 NEW)

Vision-language model on Colab T4. Uses exec_detach with deploy_config.json.

```bash
# 1. Create session + deploy
python3 colab.py new -s qwen-vl --gpu T4
# Write deploy config (DRIVE_URL and HF_TOKEN optional)
echo '{"DRIVE_URL":"","HF_TOKEN":""}' > /tmp/deploy_config.json
python3 colab.py upload -s qwen-vl /tmp/deploy_config.json /content/deploy_config.json
# Deploy (detached — runs server + tunnel)
python3 colab.py exec_detach -s qwen-vl \
    -f examples/qwen-vl/deploy.py --log /content/deploy.log

# 2. Wait for tunnel
python3 colab.py tunnel_discover -s qwen-vl
# Or monitor deploy progress:
python3 colab.py logs -s qwen-vl /content/deploy.log -f

# 3. Register with CLI
qwen-chat login <tunnel-url> my-qwen
qwen-chat  # start chatting
```

**Deploy config** (`/content/deploy_config.json` on VM):
```json
{"DRIVE_URL": "", "HF_TOKEN": ""}
```
- `DRIVE_URL`: Direct download URL for pre-built model cache tar.gz
- `HF_TOKEN`: HuggingFace token for authenticated fast download

**Model loading priority:**
1. DRIVE_URL → download + extract cached tar.gz (~30s)
2. HF_TOKEN → authenticated hf_transfer download (~2 min)
3. Standard → public hf_transfer download (~3 min)

**Server endpoints:**
- `POST /chat` — `{"text":"...", "images":[...], "session_id":"...", "max_tokens":512}`
- `POST /reset?session_id=default`
- `GET /health` — VRAM, uptime, active sessions, cache method

**qwen-chat key behaviors:**
- Auto-reconnect on connection failure (creates new session, redeploys, retries)
- Health pre-flight before REPL starts
- Multi-account support for multiple Colab accounts
- Multi-session support for isolated conversations

## Output Format

All commands return pointer-JSON on stdout:
- `-o FILE`: `{"ok":true,"f":"<path>","s":<bytes>}` (~30 tokens)
- `--json`: `{"ok":true,"text":"..."}`
- Default: raw text (verbose)

Error output: `{"ok":false,"err":"<code>","msg":"<message>"}` (~25 tokens)

## Token Budget

| Invocation | Agent tokens |
|---|---|
| `new -s X --gpu T4` | ~40-60 |
| `exec_detach -s X -f Y` | ~50-80 (pointer) |
| `tunnel_discover -s X` | ~30-80 |
| `logs -s X FILE -n 20` | ~100-500 |
| Error response | ~25-35 |

## Pitfalls

1. **Auth: redirect_uri=http://localhost only** — no PKCE, no port.
2. **exec blocks on servers.** Use exec_detach for llama.cpp, FastAPI, tunnels.
3. **Colab 90min idle timeout, 24h max.**
4. **Tunnel URL changes on restart.** Use tunnel_discover.
5. **HF Hub rate-limits unauthenticated downloads.** Always pass HF_TOKEN.
6. **transformers 5.x breaks SDXL.** Pin to transformers==4.48.0.
7. **GPU switch destroys session state.** Re-upload and re-deploy.
8. **gofile.io download pages require JS** — cannot be used as direct download
   URLs for Qwen-VL cache. Use HF_TOKEN instead (~2 min deploy).
9. **exec_detach returns no output** — check /content/deploy_status.json or
   deploy log for progress.
10. **Colab OAuth token at `~/.config/colab-cli/token.json`** — auto-refreshed
    by background thread every 5 min.
11. **exec_bg needs `--json` flag** to output a poll-able job_id. Without it,
    the command returns no usable output for tracking progress.
12. **Cloudflared needs `--metrics 0.0.0.0:0`** when running alongside other
    cloudflared instances — avoids port 20241 conflicts.
13. **Upload to nested paths fails without mkdir.** Always create parent
    directories on the VM before uploading to paths like `/root/.notebooklm/...`.
14. **Large files (>10 MB) time out on upload.** Download directly on the VM
    instead: `urllib.request.urlretrieve(url, '/content/bigfile')`.
