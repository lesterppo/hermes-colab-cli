# AGENTS.md — Hermes Colab CLI v3.1

Instructions for AI coding assistants using colab.py v3.1: operating Google
Colab VMs and deploying models on free GPU runtimes.

## What This Is

**Colab CLI** (`colab.py`) — token-efficient wrapper around Google's
`google-colab-cli`. 36 commands for session management, code execution,
file ops, VM log streaming, tunnel discovery, and model deployment.

Pony Diffusion and Z-Image-Turbo deployment scripts + local CLIs also included.

## File Structure

```
hermes-colab-cli/
├── colab.py              # Colab CLI v3.1 (36 commands, 1139 lines)
├── pony.py               # Pony Diffusion local CLI chatbox
├── zimage/               # Z-Image-Turbo deploy + CLI
├── install.sh            # One-line installer
├── AGENTS.md             # This file
├── README.md             # Human-readable overview
├── SKILL.md              # Hermes skill format
├── examples/ponydiff/    # Pony Diff deployment scripts
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

## Colab CLI Reference (colab.py v3.1)

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
