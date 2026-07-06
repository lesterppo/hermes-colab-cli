# AGENTS.md — Qwen2.5-VL-3B Colab Deployment

Instructions for AI coding assistants deploying and operating Qwen2.5-VL-3B-Instruct
on Google Colab T4 GPUs.

## What This Is

Deploy Qwen2.5-VL-3B-Instruct (vision-language model, 3B params, 4-bit quantized)
on a free Colab T4 GPU. FastAPI chat server exposed via Cloudflare tunnel with an
interactive CLI that supports multi-turn conversation and image input.

## Architecture

```
Local Machine                    Colab VM (T4, 16GB VRAM)
┌──────────┐                    ┌─────────────────────────┐
│ qwen-chat │─── HTTP/JSON ───▶│ cloudflared tunnel       │
│  (CLI)   │                    │   ↓                     │
└──────────┘                    │ FastAPI :8000            │
                                │   ↓                     │
                                │ Qwen2.5-VL-3B (4-bit)   │
                                │ ~2.4GB VRAM             │
                                └─────────────────────────┘
```

## Quick Deploy

```bash
# Install the CLI
cp examples/qwen-vl/qwen-chat ~/.local/bin/qwen-chat
chmod +x ~/.local/bin/qwen-chat

# Prerequisites: colab-cli authenticated, colab.py available
# See references/auth_flow.md for Colab OAuth setup

# Deploy (creates session, uploads deploy script, waits for tunnel)
python3 ~/.local/bin/qwen-chat reconnect

# Chat
python3 ~/.local/bin/qwen-chat
```

## File Roles

| File | Purpose |
|---|---|
| `qwen-chat` | Local CLI — REPL, one-shot, auto-reconnect, multi-account |
| `deploy.py` | Colab VM script — install deps, download model, start server + tunnel |
| `upload_cache.py` | VM utility — upload model cache tar.gz to free file hosts |

## Deploy Script (deploy.py)

The deploy script runs on the Colab VM. It handles:

1. **Package install**: transformers, bitsandbytes, accelerate, fastapi, uvicorn, pillow, qwen-vl-utils, hf_transfer
2. **Model acquisition** (3-tier):
   - DRIVE_URL → curl/gdown download + extract cached tar.gz (~30s)
   - HF_TOKEN → authenticated snapshot_download with hf_transfer (~2 min)
   - Standard → public snapshot_download with hf_transfer (~3 min)
3. **Model loading**: 4-bit NF4 quantization via bitsandbytes
4. **FastAPI server**: `/chat`, `/reset`, `/health` endpoints with multi-session conversation memory
5. **Cloudflare tunnel**: auto-download cloudflared, start tunnel, watchdog loop

Config read from `/content/deploy_config.json` (uploaded before deploy).

### Server API

```python
POST /chat
{
    "text": "prompt",
    "images": ["data:image/jpeg;base64,..."],  # optional
    "session_id": "default",                    # multi-session support
    "max_tokens": 512,
    "temperature": 0.7
}
→ {"response": "...", "session_id": "default"}

POST /reset?session_id=default
→ {"status": "ok", "session_id": "default"}

GET /health
→ {"status": "ok", "model": "...", "vram_gb": 2.4, "uptime_s": 3600, ...}
```

## CLI (qwen-chat)

### Config
- `~/.qwen-cli/accounts.json` — account URLs, HF tokens, cache URLs
- `~/.qwen-cli/session_<name>.txt` — per-account active session ID

### Key Behaviors

1. **Auto-reconnect**: On connection failure in REPL or one-shot mode, automatically:
   - Creates new Colab session
   - Uploads deploy config + deploy script
   - Polls for tunnel URL (up to 15 min)
   - Retries the failed request

2. **Health pre-flight**: Before REPL starts, checks `/health`. If unreachable,
   attempts auto-reconnect.

3. **Image encoding**: Base64-encodes images with MIME type prefix. Supports
   jpg, jpeg, png, webp, gif, bmp.

### Adding to PATH

```bash
ln -sf $(pwd)/examples/qwen-vl/qwen-chat ~/.local/bin/qwen-chat
```

## Pitfalls

1. **Colab sessions expire after ~90 min idle, 24h max runtime.**
2. **Tunnel URL changes on VM restart** — tunnel watchdog handles this.
3. **gofile.io download pages require JS** — can't be used as cache URLs.
   Use direct-download URLs only, or HF_TOKEN for reliable fast deploys.
4. **Free Colab quota** — typically 1 GPU session at a time. Multiple accounts
   can bypass this.
5. **hf_transfer requires hf_transfer package** — always installed by deploy.py.
6. **4-bit quantization uses ~2.4GB VRAM** — leaves ~13.6GB free on T4.
