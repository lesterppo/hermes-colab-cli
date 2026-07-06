# Z-Image-Turbo for Colab T4

Deploy Alibaba Tongyi-MAI Z-Image-Turbo (6B) on Colab T4 with a local chatbox CLI.

## Quick Start

```bash
# 1. Install
cp zimage/zimage.py ~/.local/bin/zimage
chmod +x ~/.local/bin/zimage

# 2. Set HF_TOKEN (free from huggingface.co/settings/tokens)
export HF_TOKEN="hf_xxx"

# 3. Deploy server (~10 min first run, ~3 min cached)
colab run --gpu T4 --keep --session zimage --timeout 900 \
  zimage/serve_final.py --hf-token "$HF_TOKEN"

# 4. Get tunnel URL (wait 3 min for model load)
sleep 200
colab download -s zimage /content/tunnel_url.txt tunnel_url.txt
cat tunnel_url.txt

# 5. Configure CLI and chat
zimage set-url <TUNNEL_URL>
zimage chat
```

## T4 Constraints

- Max resolution: 512×512 (768×768 OOMs at VAE decode)
- Generation speed: ~50s at 256×256, ~85s at 512×512
- Cloudflare free tunnel has ~100s request timeout — use 256×256 for reliability
- Model VRAM: 12.5GB out of 15GB available

## Files

| File | Purpose |
|------|---------|
| `zimage.py` | Local chatbox CLI (install to ~/.local/bin/zimage) |
| `serve_final.py` | Colab server with FastAPI + Cloudflare tunnel |
| `gen_simple.py` | One-shot generation (no server, outputs to disk) |

## CLI Reference

```
zimage chat              Interactive mode
zimage "prompt"          One-shot generation
zimage -s 512x512 "p"    Custom resolution
zimage health            Server status
zimage view              Recent images
zimage set-url <url>     Configure server
```
