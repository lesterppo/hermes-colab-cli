---
name: colab-cli
description: Operate Google Colab cloud VMs via official CLI wrapper.
version: 2.1.0
author: Peter (lesterppo)
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [colab, cloud, gpu, tpu, python, notebook]
    category: devops
    related_skills: [cloud-llm-chat, deploy-llm-to-colab]
---

# Colab CLI Skill

Operate Google Colab cloud VMs from the terminal. 31 CLI commands, 24 Hermes tools.
Wraps the official [google-colab-cli](https://github.com/googlecolab/google-colab-cli).

## When to Use

- User needs cloud GPU/TPU compute for ML training, data processing, or batch jobs
- User wants to execute Python code on a remote Colab VM without leaving the terminal
- User asks to "run this on Colab", "use a GPU notebook", or "provision a cloud runtime"
- User wants to deploy an LLM to Colab with a public API endpoint
- User needs to export notebook history as .ipynb

## Prerequisites

Install:
```bash
pip install google-colab-cli
```

Auth: see `references/auth_flow.md`. One-time setup — open URL in browser,
copy redirect URL, agent exchanges code for token.

## Quick Reference

**Session management:**
```
new -s NAME [--gpu T4|L4|G4|H100|A100] [--tpu v5e1|v6e1]   Create VM
sessions                                                      List active
status [-s NAME]                                              Hardware/status
stop [-s NAME]                                                Terminate VM
restart [-s NAME]                                             Restart kernel
gpu_switch -s NAME --gpu TYPE                                 Switch GPU (stop+recreate)
```

**Execution:**
```
exec -s NAME [--code CODE | -f FILE] [--timeout SEC]          Run Python code
exec_bg -s NAME --code CODE [--timeout SEC]                   Background run → job_id
exec_bg_poll JOB_ID                                           Poll background job
run [--gpu TYPE] [-f FILE | --code CODE]                      Ephemeral: provision→run→teardown
console -s NAME --cmd "SHELL_CMD"                             Run shell command
```

**Files + Automation:**
```
ls [-s NAME] [PATH]                                           List remote files
upload -s NAME LOCAL REMOTE                                   Upload file
download -s NAME REMOTE LOCAL                                 Download file
install -s NAME PKG... [--pip-args FLAGS]                     Install packages
log -s NAME [-n N] [-t TYPE] [--output FILE]                  View/export history
notebook export -s NAME --output FILE.ipynb                   Export as notebook
tunnel set --url URL -s NAME                                  Save tunnel URL
tunnel get -s NAME                                            Get saved tunnel URL
```

**Auth:**
```
whoami                                                        Print auth identity
```

All commands support `-o FILE` (token-efficient pointer JSON on stdout) and `--json` (structured output).

## Deploy LLM to Colab

Full workflow to deploy any GGUF model to a Colab GPU with public API endpoint.

### 1. Provision GPU session

```bash
python3 colab.py new -s llm --gpu T4 -o /tmp/session.json
```

### 2. Install dependencies

```bash
python3 colab.py exec -s llm --code "
import subprocess, sys
subprocess.run([sys.executable, '-m', 'pip', 'install', 'llama-cpp-python[server]',
    '--extra-index-url', 'https://abetlen.github.io/llama-cpp-python/whl/cu121'], check=True)
subprocess.run([sys.executable, '-m', 'pip', 'install', 'huggingface_hub'], check=True)
print('DEPS_OK')
" --timeout 300 -o /tmp/deps.json
```

### 3. Download model + start server + tunnel

```bash
python3 colab.py exec_bg -s llm --timeout 900 --code "
import os, time, subprocess, sys, urllib.request
from huggingface_hub import hf_hub_download

MODEL_REPO = 'REPO_NAME'
MODEL_FILE = 'model.gguf'
N_GPU_LAYERS = 99
N_CTX = 8192
CHAT_FORMAT = 'chatml'

model_path = hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE, local_dir='/content')
print(f'Model: {os.path.getsize(model_path)/1e9:.1f} GB')

os.system('fuser -k 8000/tcp 2>/dev/null')
log = open('/content/server.log', 'w')
p = subprocess.Popen([sys.executable, '-m', 'llama_cpp.server',
    '--model', model_path, '--n_gpu_layers', str(N_GPU_LAYERS),
    '--n_ctx', str(N_CTX), '--chat_format', CHAT_FORMAT,
    '--host', '0.0.0.0', '--port', '8000'], stdout=log, stderr=log)
time.sleep(15)
print('SERVER_OK' if p.poll() is None else f'SERVER_FAIL')

urllib.request.urlretrieve(
    'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64',
    '/content/cf')
os.chmod('/content/cf', 0o755)
tlog = open('/content/tunnel.log', 'w')
subprocess.Popen(['/content/cf', 'tunnel', '--url', 'http://0.0.0.0:8000'], stdout=tlog, stderr=tlog)
time.sleep(10)
import re
urls = re.findall(r'https://[a-zA-Z0-9.-]*\.trycloudflare\.com', open('/content/tunnel.log').read())
if urls: print(f'TUNNEL_URL: {urls[0]}/v1')
" -o /tmp/deploy.json
```

### 4. Poll + save tunnel

```bash
python3 colab.py exec_bg_poll <job_id_from_step_3> -o /tmp/result.json
python3 colab.py tunnel set --url "<TUNNEL_URL>/v1" -s llm
```

## Model Compatibility

| Model | Quant | VRAM | chat_format |
|---|---|---|---|
| Ornith 1.0 9B | Q4_K_M | ~5.8 GB | chatml |
| Llama 3 8B | Q4_K_M | ~5.5 GB | llama-3 |
| Qwen 2.5 7B | Q4_K_M | ~5.3 GB | chatml |
| Mistral 7B | Q4_K_M | ~5.0 GB | mistral |

T4 GPU has 16 GB VRAM — all above fit with room for 8192 context.

## Pitfalls

1. **Auth URL must use `redirect_uri=http://localhost`** — no PKCE, no port.
   Other combinations fail with `redirect_uri_mismatch`. See `references/auth_flow.md`.
2. Token exchange uses `client_secret`, not PKCE.
3. CLIENT_ID and CLIENT_SECRET are Google's cloud SDK OAuth2 credentials —
   these are public and hardcoded in the official CLI.
4. Token auto-refreshes. If refresh fails, re-do auth.
5. Console uses `--cmd` not `--command` (argparse name collision).
6. Background exec forks child process — survives parent exit.
7. Cloudflare tunnel URL changes on session restart — use `tunnel set/get`.
8. Colab sessions auto-terminate after ~90min idle or 24h max.
