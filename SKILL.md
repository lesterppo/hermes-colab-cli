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

### Colab Python deps & API quirks
11. **Colab 411s empty-body POSTs on /tun/m/assign and /tun/m/unassign** —
    fixed by `patch_colab_cli.py` (fix 2: sends `data="{}"`). Re-run
    `python3 colab.py patch` (or `./install.sh`) after any
    `pip install -U google-colab-cli`.
12. **peft needs torchao>0.16 but Colab ships torchao 0.10** → `ImportError`
    at `get_peft_model`. On the Colab VM run `pip uninstall -y torchao`
    (peft falls back to its default LoRA path) or `pip install -U torchao`.
    Put the uninstall line right after the `pip install` block in any Colab
    driver script that uses peft/LoRA.

## Large-Model QLoRA on Colab T4 (7–8B)

Proven recipe — this is exactly what Peter's `soup-daily-finetune` runs
daily (`Llama-3.1-8B-Instruct`, NF4 QLoRA, finance-alpaca). A free T4
(16 GB) fits 7–8B quantized; this is the training track's graduation path
from 0.5B toys.

**Deps (order matters):**
```bash
pip uninstall -y torchao   # Colab's 0.10 breaks peft
pip install 'transformers>=4.46,<5.0' 'trl>=0.14,<0.29' bitsandbytes \
  accelerate peft datasets safetensors
# Colab ships transformers 5.x — MUST downgrade to <5.0
```

**Load (4-bit NF4):**
```python
bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                         bnb_4bit_use_double_quant=True,
                         bnb_4bit_compute_dtype=torch.bfloat16)
model = AutoModelForCausalLM.from_pretrained(base, quantization_config=bnb,
                                             device_map="auto")
# Do NOT call prepare_model_for_kbit_training() — it casts the frozen bf16
# base to fp32 and crashes the fp16 GradScaler path on T4.
```

**LoRA:** r=16, alpha=32, dropout 0.0, all 7 linears
(`q/k/v/o/gate/up/down_proj`). **Train:** batch 4, seq 512, lr 2e-4,
**bf16** mixed precision — NOT fp16 (fp16 crashes at `clip_grad_norm` on
T4). `trl` `SFTTrainer` works.

**Notes:**
- 8B NF4 ≈ 5 GB weights; full QLoRA (LoRA + optimizer + activations at
  batch 4 / seq 512) fits 16 GB with headroom.
- Gated models (Llama) need `HF_TOKEN`; `Qwen2.5-7B-Instruct` is ungated —
  use it for recipe verification without secrets.
- For long runs, copy `soup-daily-finetune`'s Drive-checkpointing pattern
  (push adapter to Drive every N steps; free sessions recycle in ~2–12h).

## Large Models on a T4: the Playbook

Free Colab gives one T4 (15.36 GB usable). Peter's repos prove 8–30 GB
weight stacks run on it. The levers, in order of impact:

1. **Quantize the weights** — pick per stack:
   - Transformers LLM (train or infer): **NF4** via bitsandbytes — 8B ≈ 5 GB
     (see QLoRA recipe above; `soup-daily-finetune` trains 8B daily).
   - GGUF-compatible (llama.cpp / ComfyUI GGUF nodes): **Q5_K_M** for the big
     weights, **Q3_K_M** where VRAM is tight — `ltx23-deploy` runs a
     15.2 GB Q5 UNet + 5.7 GB Q3 text encoder on T4. Grade is load-bearing:
     fp8 OOMs where Q3 fits.
   - ComfyUI native: **INT8 repack** (`convrot`) — `minimax-music3-colab`
     fits a 22 GB (officially two-GPU) model into 11.9 GB.
2. **Offload what doesn't fit** — diffusers `enable_model_cpu_offload()`
   streams submodules between CPU and VRAM (`qwen-image-2.1-colab` runs
   30 GB of bf16 weights on 15 GB). ComfyUI `--lowvram --cache-none`. Run
   heavy stages sequentially (e.g. upscale only after the sampler frees
   VRAM).
3. **Shrink the working set** — cap resolution (1536px, not native 2K),
   tiled VAE decode, chunked feedforward, attention/VAE slicing. At runtime
   it's usually *activations*, not weights, that OOM.
4. **Precision: bf16 everywhere on T4.** fp16 crashes `clip_grad_norm`;
   fp8 can OOM where GGUF-Q3 fits.
5. **torchao is stack-dependent** — peft training wants it gone
   (`pip uninstall -y torchao`, Colab's 0.10 breaks `get_peft_model`);
   diffusers-main inference wants it *upgraded*. Match it to the stack,
   don't cargo-cult one rule.
6. **Ops for free-tier survival** — Drive checkpointing (sessions recycle
   in ~2–12h), `exec_detach` + Cloudflare tunnel for servers, retry
   watchdogs. HF token optional but avoids anonymous rate limits on big
   downloads — pass via env/file, never hardcoded.

## v3.3 — Training-track housing (Oct 2026)

The daily arXiv training track now runs through one command instead of a
hand-assembled new/upload/exec/monitor chain:

```bash
python3 colab.py train --driver colab_rl_toolselect.py --rotate \
  --results /content/toolselect_metrics.csv --out-dir ./run-out
```

`train` houses: account rotation (`--rotate`, daily pool ppoppo205 →
cyc236es → kyu008008 → kyu009009 → cyc236ha; cyc236de/cyc236hk never
selected), proxy-aware token refresh, session create with a kernel health
check (a session that passes `new` but never answers is a phantom — the
normtune Oct 2026 failure), cross-account retry on gpu-unavailable /
provision failure, driver upload, detached launch, supervised monitoring,
result download, and an honest outcome: DONE / RECLAIMED / DIED / TIMEOUT /
PROVISION_FAILED, written to `<out-dir>/train-run.json`.

Supporting commands:
- `token refresh | refresh-all | status` — proxy-aware refresh; refresh-all
  covers the rotation pool only, never prints secrets.
- `account list | switch <name> | rotate` — rotation state lives in the
  arXiv goal's `hidden_files/colab_account_rotation.txt`.
- `--registry-file <f>` on any command — passes `--config <f>` to the raw
  CLI, so isolated-state work keeps the wrapper's refresh/retry/registry.
  Never call raw `colab_cli.cli` directly: its google-auth transport
  ignores proxy env and falls into interactive OAuth on token expiry, and
  its `sessions` output prints names bracketed (`[name]`), which the old
  parser kept verbatim and once misclassified a live session as RECLAIMED.

Driver contract for `train`: print a done-marker (default `TRAIN_DONE`) to
stdout on completion, flush logs, and write result files to /content.
Reclamation cannot be prevented on free tier — checkpoint to /content
(or Drive) so a re-run resumes instead of restarting.

## v3.3 fix — zombie processes fool PID liveness (Oct 2026)

`ps -p PID` returns 0 for a defunct (zombie) process, so a dead driver
reads as alive and the `train` monitor waits out its whole budget instead
of reporting DIED. The liveness check now reads `ps -o stat=` and treats
state `Z` as dead. Found on the ToolSelect verification run (PID 694
defunct after the RL grad crash; the first monitor would have sat until
TIMEOUT).

## v3.3 fix — done-marker must be grepped, not tailed (Oct 2026)

The `train` monitor once classified a completed run as DIED: the driver
printed `DONE. metrics` and then a ~56-line results JSON, so the marker
fell outside the monitor's 40-line tail window, and the normally-exited
process read as dead. The monitor now greps the whole log for the marker
every poll. Drivers should still print the marker as their very last line.
