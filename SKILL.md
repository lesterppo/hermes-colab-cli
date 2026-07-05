---
name: hermes-colab-cli
description: Operate Colab VMs and deploy SDXL models on free GPUs.
version: 2.2.0
author: Peter (lesterppo)
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [colab, cloud, gpu, diffusion, image-generation]
    category: devops
    related_skills: [deploy-llm-to-colab, pony-diffusion-colab]
---

# Hermes Colab CLI + Pony Diffusion Skill

Operate Google Colab VMs and deploy Pony Diffusion V6 XL on free T4 GPUs.

## When to Use

- Managing Colab GPU sessions from terminal
- Deploying SDXL-based image models to free Colab T4
- Need a public endpoint for image generation via Cloudflare tunnel
- Streaming VM logs during long deployments

## Prerequisites

- `google-colab-cli` installed
- Colab OAuth2 authenticated (see `references/auth_flow.md`)
- For Pony Diffusion: `transformers==4.48.0` (not 5.x)

## Colab CLI Commands (v2.2)

```bash
# Session
python3 colab.py new -s <name> --gpu T4
python3 colab.py sessions
python3 colab.py status -s <name>
python3 colab.py stop -s <name>

# Execution
python3 colab.py exec -s <name> --code "..." --timeout 120
python3 colab.py exec_bg -s <name> --code "..." --timeout 600
python3 colab.py exec_bg_poll <job_id>
python3 colab.py console -s <name> --cmd "nvidia-smi"

# NEW: VM log streaming
python3 colab.py logs -s <name> <file> -n 50     # read last 50 lines
python3 colab.py logs -s <name> <file> -f         # stream (Ctrl+C to stop)

# NEW: Pre-flight model check
python3 colab.py check -s <name> --code "..." --timeout 120

# Files
python3 colab.py upload -s <name> <local> <remote>
python3 colab.py download -s <name> <remote> <local>

# Tunnel
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

## Pitfalls

1. **Auth: redirect_uri=http://localhost only** — no PKCE, no port
2. **transformers 5.x breaks SDXL** — pin to 4.48.0
3. **from_single_file needs local path** — use hf_hub_download first
4. **Model 6.46 GB** — 5-8 min download, stream with `logs -f`
5. **Kernel drops during deploy** — output saved to deploy_output.txt
6. **Colab 90min idle timeout** — run `pony watch`
7. **Tunnel URL changes on restart** — `pony watch` auto-updates
