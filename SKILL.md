---
name: hermes-colab-cli
description: Operate Colab VMs with colab-cli v3.2 — auto-auth, retries, exec_detach, tunnel_discover, Qwen-VL deployment.
version: 3.2.0
author: Peter (lesterppo)
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [colab, cloud, gpu, diffusion, image-generation, vision-language, qwen]
    category: devops
    related_skills: [deploy-llm-to-colab, pony-diffusion-colab, qwen-vl-colab]
---

# Hermes Colab CLI v3.2 — Colab Management + Model Deployments

Operate Google Colab VMs, deploy Pony Diffusion V6 XL, and deploy Qwen2.5-VL-3B-Instruct on free T4 GPUs.

## When to Use

- Managing Colab GPU sessions from terminal
- Deploying SDXL-based image models to free Colab T4
- Need a public endpoint for image generation via Cloudflare tunnel
- Streaming VM logs during long deployments

## Prerequisites

- `google-colab-cli` installed
- Colab OAuth2 authenticated (see `references/auth_flow.md`)
- For Pony Diffusion: `transformers==4.48.0` (not 5.x)

## Colab CLI Commands (v3.2)

```bash
# Session
python3 colab.py new -s <name> --gpu T4
python3 colab.py sessions
python3 colab.py status -s <name>
python3 colab.py stop -s <name>

# Execution
python3 colab.py exec -s <name> --code "..." --timeout 120
python3 colab.py exec_detach -s <name> -f script.py --log /content/deploy.log
python3 colab.py exec_file -s <name> -f script.py
python3 colab.py exec_bg -s <name> --code "..." --timeout 600
python3 colab.py exec_bg_poll <job_id>
python3 colab.py console -s <name> --cmd "nvidia-smi"

# VM log streaming
python3 colab.py logs -s <name> <file> -n 50     # read last 50 lines
python3 colab.py logs -s <name> <file> -f         # stream (Ctrl+C to stop)

# Pre-flight model check
python3 colab.py check -s <name> --code "..." --timeout 120

# Files
python3 colab.py upload -s <name> <local> <remote>
python3 colab.py download -s <name> <remote> <local>

# Tunnel (v3.1: tunnel_discover auto-finds URLs)
python3 colab.py tunnel_discover -s <name>
python3 colab.py tunnel get -s <name>
python3 colab.py tunnel set --url <url> -s <name>
```

## Pony Diffusion V6 XL Deployment

### Full Deploy Workflow

```bash
# 1. Create session
python3 colab.py new -s ponydiff --gpu T4

# 2. Fix transformers (Colab ships 5.x)
python3 colab.py exec -s ponydiff --timeout 120 --code "
import subprocess, sys
subprocess.run([sys.executable, '-m', 'pip', 'install', 'transformers==4.48.0'], check=True)
"

# 3. Install deps
python3 colab.py exec_bg -s ponydiff --timeout 600 --code "
import subprocess, sys
subprocess.run([sys.executable, '-m', 'pip', 'install',
    'diffusers[torch]', 'transformers==4.48.0', 'accelerate', 'xformers',
    'safetensors', 'fastapi', 'uvicorn', 'python-multipart'], check=True)
print('DEPS_OK')
"

# 4. Upload + deploy
python3 colab.py upload -s ponydiff examples/ponydiff/server.py /content/server.py
python3 colab.py upload -s ponydiff examples/ponydiff/deploy.py /content/deploy.py
python3 colab.py exec_bg -s ponydiff --timeout 1200 --code "
import subprocess, sys
subprocess.run([sys.executable, '/content/deploy.py'], check=True)
"

# 5. Monitor (NEW)
python3 colab.py logs -s ponydiff /content/deploy_output.txt -f

# 6. Configure CLI
pony set-url <tunnel-url>
pony test
```

### Pony CLI (v2.0)

```bash
pony chat                    # Interactive chatbox
pony generate "prompt"       # One-shot
pony batch "prompt" --num 3  # Parallel variants
pony view                    # Extract last images
pony watch -i 120            # Auto-reconnect watchdog
pony reconnect               # Manual recovery
```

### Pre-flight Check (NEW)

```bash
python3 colab.py check -s ponydiff --timeout 120 --code "
from diffusers import StableDiffusionXLPipeline
from huggingface_hub import hf_hub_download, list_repo_files
files = list_repo_files('LyliaEngine/Pony_Diffusion_V6_XL')
print('Repo accessible:', len([f for f in files if 'safetensors' in f]), 'files')
print('Imports OK')
"
```

## Qwen2.5-VL-3B-Instruct Deployment (v3.2 NEW)

Vision-language model (3B params, 4-bit quantized, ~2.4GB VRAM) on Colab T4.

### Quick Deploy

```bash
# Install CLI
cp examples/qwen-vl/qwen-chat ~/.local/bin/qwen-chat
chmod +x ~/.local/bin/qwen-chat

# Deploy
qwen-chat reconnect

# Chat (interactive REPL with multi-turn memory)
qwen-chat

# One-shot with image
qwen-chat image photo.jpg "Describe this image"
```

### Deploy Times

| Method | Time | Setup |
|---|---|---|
| HF_TOKEN | ~2 min | `qwen-chat hf-token <token>` |
| Standard (hf_transfer) | ~3 min | None |
| Cache URL (direct download) | ~30s | Host tar.gz on HTTP server |

### Qwen Chat CLI Commands

```bash
qwen-chat                          # Interactive REPL (default)
qwen-chat reconnect                # Force redeploy + reconnect
qwen-chat login <url> [name]       # Register endpoint (multi-account)
qwen-chat list                     # List accounts
qwen-chat switch <name>            # Set active account
qwen-chat chat <prompt>            # One-shot text
qwen-chat image <path> <prompt>    # One-shot image + text
qwen-chat status                   # Server info + health
qwen-chat hf-token [token]         # Set/view HF token for fast deploys
qwen-chat drive-link [url]         # Set/view direct cache URL
```

### REPL Commands

- `/image <path>` — attach image to next message
- `/reconnect` — force redeploy Colab session
- `/reset` — clear conversation history
- `/status` — show server info
- `/session <id>` — switch conversation session
- `/help`, `/quit`

### Auto-Reconnect

If Colab runtime expires (90 min idle), qwen-chat auto-detects connection failure,
creates new session, redeploys, and retries the failed request. Transparent to user.

### Server Watchdog

The deploy script includes a cloudflared watchdog that auto-restarts the tunnel
if it dies and extracts the new URL.

## Pitfalls

### Colab
1. **Auth: redirect_uri=http://localhost only** — no PKCE, no port
2. **Colab 90min idle timeout, 24h max runtime.**
3. **Tunnel URL changes on restart** — tunnel_discover or watchdog handles this.

### Pony Diffusion
4. **transformers 5.x breaks SDXL** — pin to 4.48.0
5. **from_single_file needs local path** — use hf_hub_download first
6. **Model 6.46 GB** — 5-8 min download, stream with `logs -f`
7. **Kernel drops during deploy** — output saved to deploy_output.txt

### Qwen-VL
8. **gofile.io download pages require JS** — cannot be used as cache URLs.
   Use direct-download URLs or HF_TOKEN for reliable fast deploys.
9. **hf_transfer package required** — deploy script auto-installs it.
10. **4-bit quantization uses ~2.4GB VRAM** — leaves headroom on T4.
