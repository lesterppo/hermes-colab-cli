# AGENTS.md — Hermes Colab CLI + Pony Diffusion V6 XL

Instructions for AI coding assistants working with this repo: operating Google
Colab VMs and deploying image generation models on them.

## What This Is

Two tools in one repo:

1. **Colab CLI** (`colab.py`) — token-efficient wrapper around Google's
   `google-colab-cli`. 33 commands for session management, code execution,
   file ops, VM log streaming, and model pre-flight checks.

2. **Pony Diffusion V6 XL** (`pony.py` + `examples/ponydiff/`) — Full
   deployment of Pony Diffusion V6 XL (SDXL fine-tune, 6.46 GB) on Colab
   T4 GPU. FastAPI server + Cloudflare tunnel + local CLI chatbox.

## File Structure

```
hermes-colab-cli/
├── colab.py              # Colab CLI wrapper (33 commands, v2.2)
├── pony.py               # Pony Diffusion local CLI chatbox (v2.0)
├── install.sh            # One-line installer
├── AGENTS.md             # This file — AI agent onboarding
├── README.md             # Human-readable overview
├── SKILL.md              # Hermes skill format
├── examples/
│   └── ponydiff/
│       ├── server.py     # FastAPI image generation server (runs on Colab VM)
│       └── deploy.py     # Deployment orchestrator (runs on Colab VM)
└── references/
    └── auth_flow.md      # Definitive Colab OAuth2 auth guide
```

## How to Install and Auth

### Install

```bash
pip install google-colab-cli
cp colab.py ~/.hermes/scripts/colab/colab.py
cp pony.py ~/.local/bin/pony && chmod +x ~/.local/bin/pony
# Or: ./install.sh
```

### Auth (one-time)

The Google OAuth2 client is restricted — only ONE flow works reliably.

1. Tell the user to open this URL:
   ```
   https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com&redirect_uri=http://localhost&scope=openid+https://www.googleapis.com/auth/userinfo.email+https://www.googleapis.com/auth/cloud-platform+https://www.googleapis.com/auth/colaboratory+https://www.googleapis.com/auth/drive.file&access_type=offline
   ```

2. User signs in, approves all scopes.

3. Browser tries to redirect to `http://localhost/?code=...` and fails (expected).
   User copies the ENTIRE URL from the address bar.

4. Agent extracts the `code` parameter and exchanges for tokens. See
   `references/auth_flow.md` for the complete exchange script.

5. Verify: `colab sessions`

### Token Refresh

The CLI auto-refreshes expired tokens. If refresh fails, re-run steps 1-5.

## Colab CLI Reference (colab.py v2.2)

### Session management
```
new -s <name> --gpu T4          Create GPU session
sessions                         List all sessions
status -s <name>                 Session status
stop -s <name>                   Stop session
restart -s <name>                Restart kernel
gpu_switch -s <name> --gpu L4    Switch GPU type
```

### Execution
```
exec -s <name> --code "..."      Execute Python code
exec_bg -s <name> --code "..."   Background execution (with --timeout)
exec_bg_poll <job_id>            Poll background job
console -s <name> --cmd "..."    Run shell command
```

### VM file ops
```
upload -s <name> <local> <remote>  Upload file
download -s <name> <remote> <local> Download file
ls -s <name> [path]                List files
logs -s <name> <file> [-n N] [-f]  Read/stream VM file (NEW v2.2)
```

### Deployment helpers
```
check -s <name> --code "..."       Pre-flight model import test (NEW v2.2)
tunnel get -s <name>               Get saved tunnel URL
tunnel set --url <url> -s <name>   Save tunnel URL
```

## Pony Diffusion V6 XL — Full Deployment

### Architecture

```
┌──────────┐   POST /generate    ┌──────────────┐   Cloudflare    ┌──────────┐
│  pony    │ ──────────────────→ │ FastAPI       │ ←─────────────→ │  Colab   │
│  CLI     │ ←──── ZIP download  │ uvicorn :8000 │    tunnel      │  T4 GPU  │
│ (local)  │                     │ server.py     │                │ (cloud)  │
└──────────┘                     └──────────────┘                └──────────┘
```

### Deployment Steps

```bash
# 1. Create GPU session
python3 colab.py new -s ponydiff --gpu T4

# 2. CRITICAL: Downgrade transformers (Colab ships 5.x — breaks SDXL)
python3 colab.py exec -s ponydiff --timeout 120 --code "
import subprocess, sys
subprocess.run([sys.executable, '-m', 'pip', 'install', 'transformers==4.48.0'], check=True)
"

# 3. Install deps on VM
python3 colab.py exec_bg -s ponydiff --timeout 600 --code "
import subprocess, sys
subprocess.run([sys.executable, '-m', 'pip', 'install',
    'diffusers[torch]', 'transformers==4.48.0', 'accelerate', 'xformers',
    'safetensors', 'fastapi', 'uvicorn', 'python-multipart'], check=True)
print('DEPS_OK')
"

# 4. Upload server + deploy
python3 colab.py upload -s ponydiff examples/ponydiff/server.py /content/server.py
python3 colab.py upload -s ponydiff examples/ponydiff/deploy.py /content/deploy.py

# 5. Deploy (model download 5-8 min)
python3 colab.py exec_bg -s ponydiff --timeout 1200 --code "
import subprocess, sys
subprocess.run([sys.executable, '/content/deploy.py'], check=True)
"

# 6. Monitor progress (NEW: log streaming)
python3 colab.py logs -s ponydiff /content/deploy_output.txt -n 30
# Or stream: python3 colab.py logs -s ponydiff /content/deploy_output.txt -f

# 7. Get tunnel URL
python3 colab.py exec -s ponydiff --code "
import os, re
log = open('/content/tunnel.log').read()
urls = re.findall(r'https://[a-zA-Z0-9.-]*\.trycloudflare\.com', log)
print(urls[0] if urls else 'NOT FOUND')
"

# 8. Configure CLI
pony set-url <tunnel-url>
pony test

# 9. Save for persistence
python3 colab.py tunnel set --url <tunnel-url> -s ponydiff
```

### Pre-flight Check (NEW v2.2)

Validate model loading before full deployment (30s vs 8 min):

```bash
python3 colab.py check -s ponydiff --timeout 120 --code "
from diffusers import StableDiffusionXLPipeline
from huggingface_hub import hf_hub_download, list_repo_files
files = list_repo_files('LyliaEngine/Pony_Diffusion_V6_XL')
safetensors = [f for f in files if f.endswith('.safetensors')]
print(f'Repo OK: {safetensors}')
"
```

### CLI Reference (pony.py v2.0)

```bash
pony chat                    # Interactive chatbox
pony generate "prompt" -s 20 # One-shot generation
pony batch "prompt" --num 3  # Parallel seed variants (2 concurrent)
pony view                    # Extract & show last generation
pony test                    # Health check
pony url                     # Show API URL
pony set-url <url>           # Configure endpoint
pony reconnect               # Health check + auto-recover URL
pony watch -i 120            # Background health monitor + auto-reconnect
```

### Chatbox slash commands

```
/set steps N        Inference steps (15-50)
/set cfg N          CFG scale (1.0-15.0)
/set size W H       Output dimensions
/set num N          Images per generation (1-4)
/set negative TEXT  Negative prompt
/batch PROMPT       3 seed variants in parallel
/view               Extract & list last images
/reconnect          Health check + auto-recovery
/params             Show current parameters
/stats              Generation statistics
```

## Output Format

Images saved to `~/pony_output/<timestamp>/`:

```
~/pony_output/
  20260705_234507/
    pony_20260705_234507.zip   # ZIP archive
    pony_001.png               # after extract with pony view
```

ZIP-only from the API. Generic filenames. No inline display.

## Pitfalls

1. **Auth URL must use `redirect_uri=http://localhost`** — no PKCE, no port,
   no `token_usage=remote`. Other combinations fail.

2. **transformers 5.x breaks SDXL from_single_file.** Colab ships 5.12 which
   flattened CLIPTextModel. Pin to `transformers==4.48.0` before anything.

3. **from_single_file needs local path, not repo ID.** Use `hf_hub_download`
   first, then pass the local file path.

4. **Model is 6.46 GB.** Download takes 5-8 min. Stream progress with
   `colab.py logs -s ponydiff /content/deploy_output.txt -f`.

5. **Colab kernel drops during server startup.** deploy.py writes output to
   `/content/deploy_output.txt` so `logs` can read it even if exec response
   is lost.

6. **T4 GPU has 16 GB VRAM.** `enable_model_cpu_offload()` keeps SDXL at ~8 GB.
   Without it, OOM on first generation.

7. **Colab 90 min idle timeout, 24h max.** Run `pony watch -i 120` to keep
   alive and auto-reconnect.

8. **Tunnel URL changes on every session restart.** Save with `colab.py tunnel set`
   and `pony set-url`. `pony watch` does this automatically.
