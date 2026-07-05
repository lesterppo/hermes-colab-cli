# Hermes Colab CLI + Pony Diffusion V6 XL

Token-efficient CLI for Google Colab GPU runtimes, plus a full deployment
of Pony Diffusion V6 XL on free T4 GPUs.

## What's Included

### Colab CLI (colab.py v2.2)
33 commands for Colab session management with pointer-JSON output:
- Session: new, sessions, status, stop, restart, gpu_switch
- Execution: exec, exec_bg, exec_bg_poll, console, repl
- Files: upload, download, ls, rm
- **NEW** logs, check — VM file streaming and pre-flight model tests
- Tunnel persistence, notebook export, secrets management

### Pony Diffusion V6 XL (pony.py v2.0)
Deploy Pony Diffusion V6 XL on Colab T4:
- FastAPI server with ZIP output (no inline images)
- Local CLI chatbox with interactive mode
- Parallel batch generation (2 concurrent workers)
- Auto-reconnect watchdog
- Image viewer

## Install

```bash
git clone https://github.com/lesterppo/hermes-colab-cli.git
cd hermes-colab-cli
./install.sh
```

## Quick Start

```bash
# 1. Auth (first time — see references/auth_flow.md)
colab sessions

# 2. Deploy Pony Diffusion
#    Full workflow in AGENTS.md or SKILL.md

# 3. Use the CLI
pony chat
```

## File Structure

```
├── colab.py              Colab CLI wrapper (33 commands)
├── pony.py               Pony Diffusion local CLI
├── install.sh            One-command installer
├── AGENTS.md             AI agent onboarding (read this first)
├── SKILL.md              Hermes skill format
├── examples/ponydiff/    Pony Diff deployment scripts
│   ├── server.py         FastAPI image gen server (runs on Colab VM)
│   └── deploy.py         Deployment orchestrator (runs on Colab VM)
└── references/
    └── auth_flow.md      Colab OAuth2 setup
```

## License

MIT
