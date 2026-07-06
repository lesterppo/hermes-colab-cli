# Hermes Colab CLI v3.1

Token-efficient CLI for Google Colab GPU runtimes with automatic auth refresh,
retry logic, and server deployment support.

## What's Included

### Colab CLI (colab.py v3.1)
36 commands for Colab session management with pointer-JSON output.

**v3.1 highlights:**
- **exec_detach** — upload script + run detached for long-running servers
- **tunnel_discover** — auto-grep VM for live Cloudflare tunnel URLs
- **Auto-refresh OAuth** — background thread prevents mid-deployment auth death
- **Retry logic** — 2 retries on transient Colab errors (502/503/timeout)
- **exec_file** — upload + execute in one step
- **exec_bg + exec_bg_poll** — proper background job tracking
- **logs -f** — stream VM files in real time

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

tunnel_discover -s NAME         Auto-discover tunnel URL
tunnel get -s NAME              Get saved URL
tunnel set --url URL -s NAME    Save URL

whoami                          Auth identity
version                         CLI version
```

All commands output pointer-JSON by default. Use `-o FILE` for file output,
`--json` for inline JSON.

## File Structure

```
├── colab.py              Colab CLI v3.1 (36 commands, 1139 lines)
├── pony.py               Pony Diffusion local CLI
├── install.sh            One-command installer
├── AGENTS.md             AI agent guide
├── SKILL.md              Hermes skill format
├── zimage/               Z-Image-Turbo deploy + CLI
├── examples/ponydiff/    Pony Diff deployment scripts
└── references/
    └── auth_flow.md      Colab OAuth2 auth guide
```

## License

MIT
