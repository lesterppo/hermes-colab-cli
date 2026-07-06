# Qwen2.5-VL-3B on Colab T4

Deploy Qwen2.5-VL-3B-Instruct (vision-language model) on free Google Colab T4 GPU
with a FastAPI chat server, Cloudflare tunnel, and interactive CLI.

## Quick Start

```bash
# 1. Deploy (creates Colab session, uploads script, waits for tunnel)
qwen-chat reconnect

# 2. Start chatting
qwen-chat

# 3. With image
qwen-chat image photo.jpg "Describe this image"
```

## Deploy Times

| Method | Time | Setup |
|---|---|---|
| HF_TOKEN | ~2 min | `qwen-chat hf-token <token>` |
| Standard (hf_transfer) | ~3 min | None |
| Cache URL (direct download) | ~30s | Host tar.gz on HTTP server |

## CLI Commands

```
qwen-chat                        Interactive REPL (default)
qwen-chat login <url> [name]     Register endpoint
qwen-chat reconnect [name]       Force redeploy + reconnect
qwen-chat list                   List accounts
qwen-chat switch <name>          Set active account
qwen-chat chat <prompt>          One-shot text
qwen-chat image <path> <prompt>  One-shot image + text
qwen-chat status                 Server info
qwen-chat remove <name>          Delete account
qwen-chat reset                  Clear history
qwen-chat session <name>         Switch session
qwen-chat drive-link [url]       Set/view cache URL (direct download)
qwen-chat hf-token [token]       Set/view HuggingFace token
```

## REPL Commands

| Command | Action |
|---|---|
| `/image <path>` | Attach image to next message |
| `/reconnect` | Force redeploy Colab session |
| `/reset` | Clear conversation history |
| `/status` | Show server info |
| `/session <id>` | Switch conversation session |
| `/help` | Show commands |
| `/quit` | Exit |

## File Structure

```
examples/qwen-vl/
├── qwen-chat          CLI client (599 lines)
├── deploy.py          Colab deploy script (383 lines)
├── upload_cache.py    Cache upload utility (140 lines)
└── README.md          This file
```

## Model Details

- **Model**: Qwen/Qwen2.5-VL-3B-Instruct
- **Architecture**: Vision-Language, 3B params
- **Quantization**: 4-bit (bitsandbytes NF4) — ~2.4GB VRAM
- **GPU**: T4 (16GB VRAM, free Colab tier)
- **Image support**: Base64-encoded, any format Pillow supports

## Cache Caching

The deploy script (`deploy.py`) uses a 3-tier model acquisition strategy:

1. **DRIVE_URL** — If a direct download URL is configured, downloads and extracts
   a pre-built tar.gz of the HuggingFace model cache. Must be a direct HTTP URL
   (not a file-host download page). Supports Google Drive share links via gdown.
2. **HF_TOKEN** — Authenticated download via HuggingFace Hub with hf_transfer
   (~2 min). Get a free token at https://huggingface.co/settings/tokens.
3. **Standard** — Public download via hf_transfer (~3 min). No setup needed.

## Auto-Reconnect

If the Colab runtime expires (90 min idle limit), qwen-chat automatically:
1. Detects connection failure
2. Creates new Colab session
3. Uploads and runs deploy script
4. Waits for tunnel URL
5. Retries the failed request

The tunnel watchdog on the server side auto-restarts cloudflared if it dies.

## Multi-Account

```bash
qwen-chat login <url1> account1
qwen-chat login <url2> account2
qwen-chat switch account1
qwen-chat list
```

## License

MIT
