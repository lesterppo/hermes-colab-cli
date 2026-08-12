# Hermes Colab CLI v3.2

Token-efficient CLI for Google Colab GPU runtimes with automatic auth refresh,
retry logic, and server deployment support.

## What's Included

### Colab CLI (colab.py v3.2)
36 commands for Colab session management with pointer-JSON output.

**v3.1 highlights (carried forward):**
- **exec_detach** — upload script + run detached for long-running servers
- **tunnel_discover** — auto-grep VM for live Cloudflare tunnel URLs
- **Auto-refresh OAuth** — background thread prevents mid-deployment auth death
- **Retry logic** — 2 retries on transient Colab errors (502/503/timeout)
- **exec_file** — upload + execute in one step
- **exec_bg + exec_bg_poll** — proper background job tracking
- **logs -f** — stream VM files in real time

### Qwen2.5-VL-3B-Instruct (examples/qwen-vl/) — v3.2 NEW
Deploy vision-language model on Colab T4:
- FastAPI chat server with multi-turn memory + image support
- Cloudflare tunnel with auto-restart watchdog
- Interactive CLI (`qwen-chat`) with REPL, one-shot, auto-reconnect
- Multi-account support (multiple Colab accounts = multiple endpoints)
- 3-tier model acquisition: cache URL → HF_TOKEN → standard hf_transfer
- See `examples/qwen-vl/README.md` for setup

### Z-Image-Turbo (zimage/)
Deploy Alibaba Tongyi-MAI Z-Image-Turbo (6B) on Colab T4:
- FastAPI server with Cloudflare tunnel
- Local chatbox CLI (`zimage chat`)
- 512x512 generation in ~85s on free T4
- See `zimage/AGENTS.md` for setup

### Pony Diffusion V6 XL (pony.py)
Deploy Pony Diffusion V6 XL on Colab T4:
- FastAPI server with ZIP output
- Local CLI chatbox with interactive mode

## Install

```bash
git clone https://github.com/lesterppo/hermes-colab-cli.git
cd hermes-colab-cli
./install.sh
```

## Quick Start

```bash
# 1. Auth (first time — see references/auth_flow.md)
python3 colab.py whoami

# 2. Quick LLM deploy
python3 colab.py new -s mysession --gpu T4
python3 colab.py exec_detach -s mysession -f deploy_script.py --log /content/deploy.log
python3 colab.py tunnel_discover -s mysession

# 3. Pony Diffusion
python3 examples/ponydiff/deploy.py  # or follow AGENTS.md
pony chat
```

## Commands

```
new -s NAME --gpu T4          Create GPU session
sessions                        List sessions
status -s NAME                  Session status
stop -s NAME                    Stop session
restart -s NAME                 Restart kernel
gpu_switch -s NAME --gpu L4    Switch GPU type

exec -s NAME --code "..."       Execute Python inline
exec_detach -s NAME -f FILE     Upload + run detached (servers!)
exec_file -s NAME -f FILE       Upload + execute in one step
exec_bg -s NAME --code "..."    Background execution
exec_bg_poll JOB_ID [-s NAME]   Poll background job
console -s NAME --cmd "..."     Shell command on VM
check -s NAME --code "..."      Pre-flight model test

upload -s NAME LOCAL REMOTE     Upload file
download -s NAME REMOTE LOCAL   Download file
ls -s NAME [PATH]               List VM files
logs -s NAME FILE [-n N] [-f]   Tail/stream VM file

tunnel_discover -s NAME Auto-discover tunnel URL
tunnel get -s NAME Get saved URL
tunnel set --url URL -s NAME Save URL

recover Rebuild sessions.json from backend (wiped registry fix)
reauth [--account EMAIL] Switch Colab account (per-account GPU quota)
patch Apply 401/404 refresh-retry patch to installed colab_cli

whoami Auth identity
version CLI version
```

All commands output pointer-JSON by default. Use `-o FILE` for file output,
`--json` for inline JSON.

## Reliability notes (from LTX-2.3 Colab deployment, 2026-08)

- **Wiped session registry**: `sessions.json` can be cleared by crashes or the
  upstream CLI's cleanup. `colabctl recover` rebuilds it from the backend —
  a live VM is NOT lost (a wiped registry previously cost 40GB of model
  re-downloads). `exec`/`run` also auto-recover once on "Session not found".
- **Per-account GPU quota**: free-tier Colab rejects new T4 sessions with
  `503 Service Unavailable` / `outcome:2` after ~3-4 GPU sessions/day.
  Fix: `colabctl reauth --account other@gmail.com` (manual OAuth flow) and
  re-create the session. The runtime account and Drive account are
  independent — keep uploads pointed at the original account.
- **401/404 proxy-token expiry**: the runtime proxy token expires hourly; the
  upstream CLI treats 401/404 as fatal and prunes the session (killing the
  keepalive → VM GC). `colabctl patch` applies the refresh-retry fix to the
  installed package (or a shadow copy at `~/colab_cli_patched` when the
  package dir is root-owned). A `*/15` keepalive cron should also re-sync the
  token — see `examples/ltx23/keepalive.py`.
- **Long-running servers**: `colab run` blocks until the kernel cell
  completes — split deploys into short exec steps (provision → download →
  launch) and use `exec_detach` for the server itself.

## File Structure

```
├── colab.py              Colab CLI v3.2 (36 commands, 1139 lines)
├── pony.py               Pony Diffusion local CLI
├── install.sh            One-command installer
├── AGENTS.md             AI agent guide
├── SKILL.md              Hermes skill format
├── zimage/               Z-Image-Turbo deploy + CLI
├── examples/
│   ├── ponydiff/         Pony Diff deployment scripts
│   └── qwen-vl/          Qwen2.5-VL-3B deploy + CLI (v3.2 NEW)
└── references/
    └── auth_flow.md      Colab OAuth2 auth guide
```

## License

MIT
