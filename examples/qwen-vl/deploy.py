#!/usr/bin/env python3 -u
"""Qwen2.5-VL-3B Colab deployment v3 — Drive cache, HF_TOKEN, watchdog.

Model loading priority:
  1. DRIVE_URL env → gdown download cached tar.gz (~30s)
  2. HF_TOKEN env → authenticated fast download (~2 min)
  3. Standard download (~5 min)
"""
import subprocess, sys, os, time, re, json, io, base64, tarfile

PORT = 8000
MODEL_ID = "Qwen/Qwen2.5-VL-3B-Instruct"
STATUS_FILE = "/content/deploy_status.json"
TUNNEL_LOG = "/content/tunnel.log"
API_URL_FILE = "/content/api_url.txt"
CACHE_DIR = "/content/model_cache"
CONFIG_FILE = "/content/deploy_config.json"

# Read config from uploaded file
DRIVE_URL = ""
HF_TOKEN = ""
try:
    if os.path.exists(CONFIG_FILE):
        cfg = json.load(open(CONFIG_FILE))
        DRIVE_URL = cfg.get("DRIVE_URL", "")
        HF_TOKEN = cfg.get("HF_TOKEN", "")
        if DRIVE_URL:
            print(f"  Config: DRIVE_URL set", flush=True)
        if HF_TOKEN:
            print(f"  Config: HF_TOKEN set", flush=True)
except Exception as e:
    print(f"  Config read failed: {e}", flush=True)

def write_status(stage, detail="", ok=True):
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump({"stage": stage, "detail": detail, "ok": ok, "ts": time.time()}, f)
    except: pass
    sys.stdout.flush()

def die(msg):
    write_status("fatal", msg, ok=False)
    print(f"FATAL: {msg}", flush=True)
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        print(f"PROCESSES:\n{r.stdout[-2000:]}", flush=True)
    except: pass
    sys.exit(1)

write_status("init", "v3 deployment starting")

# ── Step 1: Install deps ──────────────────────────────────────────
try:
    write_status("install", "Installing packages")
    print("[1/5] Installing packages...", flush=True)
    pkgs = [
        "transformers>=4.49.0", "bitsandbytes>=0.45.0", "accelerate>=1.0.0",
        "fastapi>=0.115.0", "uvicorn[standard]>=0.32.0",
        "pillow>=11.0", "qwen-vl-utils>=0.0.10",
    ]
    # Always install hf_transfer for faster HF downloads
    pkgs.append("hf_transfer>=0.1")
    # Add gdown if DRIVE_URL is Google Drive
    if DRIVE_URL and "drive.google.com" in DRIVE_URL:
        pkgs.append("gdown>=5.0")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + pkgs, timeout=300, check=True)
    print("  Done.", flush=True)
except Exception as e:
    die(f"Package install failed: {e}")

# ── Step 2: Acquire model files ────────────────────────────────────
try:
    write_status("acquire_model", "Getting model files")
    print(f"\n[2/5] Acquiring model files...", flush=True)

    model_ready = False

    # Path A: Cache download from URL
    if DRIVE_URL:
        print(f"  Cache URL configured. Downloading...", flush=True)
        try:
            tarball = "/content/qwen-vl-cache.tar.gz"

            # Detect URL type: Google Drive vs direct HTTP
            if "drive.google.com" in DRIVE_URL:
                subprocess.run([
                    sys.executable, "-m", "gdown", DRIVE_URL, "-O", tarball, "--quiet"
                ], timeout=600, check=True)
            else:
                # Direct HTTP download via curl (faster and handles large files)
                subprocess.run([
                    "curl", "-sL", "-o", tarball,
                    "--connect-timeout", "30", "--max-time", "1800",
                    DRIVE_URL
                ], timeout=1900, check=True)
            size_mb = os.path.getsize(tarball) / 1e6
            print(f"  Downloaded: {size_mb:.0f} MB. Extracting...", flush=True)
            os.makedirs(CACHE_DIR, exist_ok=True)
            with tarfile.open(tarball, "r:gz") as tar:
                tar.extractall(path=CACHE_DIR)
            # The tar has 'qwen-vl-cache/' prefix — move contents up if needed
            inner = os.path.join(CACHE_DIR, "qwen-vl-cache")
            if os.path.isdir(inner):
                import shutil
                for item in os.listdir(inner):
                    src = os.path.join(inner, item)
                    dst = os.path.join(CACHE_DIR, item)
                    if not os.path.exists(dst):
                        shutil.move(src, dst)
                os.rmdir(inner)
            os.remove(tarball)  # free space
            # Set HF cache to use our local copy
            os.environ["HF_HOME"] = CACHE_DIR
            # Symlink into HF cache structure
            hf_dst = "/root/.cache/huggingface/hub/models--Qwen--Qwen2.5-VL-3B-Instruct"
            os.makedirs(os.path.dirname(hf_dst), exist_ok=True)
            if not os.path.exists(hf_dst):
                os.symlink(CACHE_DIR, hf_dst)
            model_ready = True
            print(f"  Cache ready: {CACHE_DIR}", flush=True)
        except Exception as e:
            print(f"  Drive download failed: {e}. Falling back...", flush=True)

    # Path B: HF_TOKEN authenticated download
    if not model_ready and HF_TOKEN:
        print(f"  HF_TOKEN set. Using authenticated download...", flush=True)
        try:
            os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id=MODEL_ID,
                local_dir=CACHE_DIR,
                local_dir_use_symlinks=False,
                resume_download=True,
                token=HF_TOKEN,
            )
            os.environ["HF_HOME"] = CACHE_DIR
            hf_dst = "/root/.cache/huggingface/hub/models--Qwen--Qwen2.5-VL-3B-Instruct"
            os.makedirs(os.path.dirname(hf_dst), exist_ok=True)
            if not os.path.exists(hf_dst):
                os.symlink(CACHE_DIR, hf_dst)
            model_ready = True
            print(f"  Downloaded to {CACHE_DIR}", flush=True)
        except Exception as e:
            print(f"  HF download failed: {e}. Falling back...", flush=True)

    # Path C: Standard download (use hf_transfer for speed even without token)
    if not model_ready:
        print(f"  Standard download with hf_transfer...", flush=True)
        try:
            os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id=MODEL_ID,
                local_dir=CACHE_DIR,
                local_dir_use_symlinks=False,
                resume_download=True,
            )
            os.environ["HF_HOME"] = CACHE_DIR
            hf_dst = "/root/.cache/huggingface/hub/models--Qwen--Qwen2.5-VL-3B-Instruct"
            os.makedirs(os.path.dirname(hf_dst), exist_ok=True)
            if not os.path.exists(hf_dst):
                os.symlink(CACHE_DIR, hf_dst)
            model_ready = True
            print(f"  Downloaded to {CACHE_DIR}", flush=True)
        except Exception as e:
            print(f"  hf_transfer download failed: {e}. Will load directly.", flush=True)
            # Model will be loaded from_pretrained directly in Step 3
            write_status("acquire_model", "Will load from HF directly", ok=True)
    else:
        write_status("acquire_model", "Model cached locally", ok=True)

except Exception as e:
    die(f"Model acquisition failed: {e}")

# ── Step 3: Load model (4-bit) ─────────────────────────────────────
try:
    write_status("model_load", "Loading model into GPU")
    print(f"\n[3/5] Loading {MODEL_ID} (4-bit)...", flush=True)
    import torch
    from transformers import BitsAndBytesConfig
    from PIL import Image

    try:
        from transformers import Qwen2_5_VLForConditionalGeneration as ModelCls
        from transformers import AutoProcessor
        print("  Using Qwen2_5_VLForConditionalGeneration", flush=True)
    except ImportError:
        from transformers import Qwen2VLForConditionalGeneration as ModelCls
        from transformers import AutoProcessor
        print("  Using Qwen2VLForConditionalGeneration", flush=True)

    quantization_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
    )

    model = ModelCls.from_pretrained(
        MODEL_ID,
        torch_dtype=torch.float16,
        quantization_config=quantization_config,
        device_map="auto",
    )
    processor = AutoProcessor.from_pretrained(MODEL_ID)

    vram_gb = torch.cuda.memory_allocated() / 1e9
    total_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
    print(f"  VRAM: {vram_gb:.1f}/{total_gb:.1f} GB", flush=True)
except Exception as e:
    die(f"Model load failed: {e}")

# ── Step 4: FastAPI server ─────────────────────────────────────────
try:
    write_status("server_start", "Starting FastAPI")
    print(f"\n[4/5] Starting FastAPI on port {PORT}...", flush=True)

    from fastapi import FastAPI
    from pydantic import BaseModel
    from typing import List, Optional
    from contextlib import asynccontextmanager
    from threading import Thread

    conversations = {}

    def b64_to_pil(b64_str):
        if "," in b64_str:
            b64_str = b64_str.split(",", 1)[1]
        return Image.open(io.BytesIO(base64.b64decode(b64_str))).convert("RGB")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        write_status("server_ready", "Accepting requests")
        yield

    app = FastAPI(title="Qwen2.5-VL-3B", lifespan=lifespan)

    class ChatRequest(BaseModel):
        text: str
        images: Optional[List[str]] = None
        session_id: str = "default"
        max_tokens: int = 512
        temperature: float = 0.7

    class ChatResponse(BaseModel):
        response: str
        session_id: str

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest):
        sid = req.session_id
        history = conversations.get(sid, [])
        content = []
        if req.images:
            for b64 in req.images:
                content.append({"type": "image", "image": b64_to_pil(b64)})
        content.append({"type": "text", "text": req.text})
        messages = history + [{"role": "user", "content": content}]
        text_prompt = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        image_inputs = []
        if isinstance(messages[-1]["content"], list):
            for block in messages[-1]["content"]:
                if block["type"] == "image":
                    image_inputs.append(block["image"])
        if image_inputs:
            inputs = processor(
                text=[text_prompt], images=image_inputs,
                return_tensors="pt", padding=True
            ).to(model.device)
        else:
            inputs = processor(
                text=[text_prompt], return_tensors="pt", padding=True
            ).to(model.device)
        with torch.no_grad():
            generated_ids = model.generate(
                **inputs, max_new_tokens=req.max_tokens,
                temperature=req.temperature, do_sample=req.temperature > 0,
            )
        generated_ids_trimmed = generated_ids[:, inputs.input_ids.shape[1]:]
        response_text = processor.batch_decode(
            generated_ids_trimmed, skip_special_tokens=True,
            clean_up_tokenization_spaces=False
        )[0]
        conversations[sid] = history + [
            messages[-1], {"role": "assistant", "content": response_text}
        ]
        return ChatResponse(response=response_text, session_id=sid)

    @app.post("/reset")
    async def reset(session_id: str = "default"):
        conversations.pop(session_id, None)
        return {"status": "ok", "session_id": session_id}

    @app.get("/health")
    async def health():
        vram = torch.cuda.memory_allocated() / 1e9
        uptime = time.time() - _start_time
        method = "drive_cache" if DRIVE_URL else ("hf_token" if HF_TOKEN else "hf_transfer")
        return {"status": "ok", "model": MODEL_ID, "vram_gb": round(vram, 1),
                "uptime_s": round(uptime), "active_sessions": len(conversations),
                "cache": method}

    _start_time = time.time()

    import uvicorn
    Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning"),
           daemon=True).start()
    time.sleep(5)
    print("  Server running.", flush=True)
except Exception as e:
    die(f"Server start failed: {e}")

# ── Step 5: Cloudflare tunnel with watchdog ─────────────────────────
try:
    write_status("tunnel_start", "Starting tunnel")
    print(f"\n[5/5] Starting Cloudflare tunnel...", flush=True)

    cf_bin = "/content/cloudflared"
    if not os.path.exists(cf_bin):
        subprocess.run(
            f"curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o {cf_bin} && chmod +x {cf_bin}",
            shell=True, check=True, timeout=30
        )

    subprocess.run("pkill -f cloudflared 2>/dev/null", shell=True)
    time.sleep(1)

    def start_tunnel():
        return subprocess.Popen(
            [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{PORT}"],
            stdout=open(TUNNEL_LOG, "w"), stderr=subprocess.STDOUT,
        )

    def extract_url():
        try:
            log_text = open(TUNNEL_LOG).read()
            found = re.findall(r'https://[^ ]*trycloudflare\.com', log_text)
            return found[0] if found else None
        except: return None

    tp = start_tunnel()
    url = None
    for _ in range(30):
        time.sleep(1)
        url = extract_url()
        if url: break

    if not url:
        die(f"Tunnel URL not found in {TUNNEL_LOG}")

    with open(API_URL_FILE, "w") as f:
        f.write(url)
    write_status("ready", url, ok=True)
    print(f"\n{'=' * 60}", flush=True)
    print(f"DEPLOYED: {url}", flush=True)
    print(f"Cache: {'drive' if DRIVE_URL else ('hf_token' if HF_TOKEN else 'hf_transfer')}", flush=True)
    print(f"{'=' * 60}", flush=True)

    # Watchdog loop
    tunnel_restarts = 0
    while True:
        time.sleep(30)

        if tp.poll() is not None:
            tunnel_restarts += 1
            print(f"[WATCHDOG] Tunnel died (restart #{tunnel_restarts}). Restarting...", flush=True)
            tp = start_tunnel()
            time.sleep(10)
            new_url = extract_url()
            if new_url and new_url != url:
                url = new_url
                with open(API_URL_FILE, "w") as f:
                    f.write(url)
                write_status("ready", url, ok=True)
                print(f"[WATCHDOG] New URL: {url}", flush=True)

        write_status("running", url, ok=True)

except Exception as e:
    die(f"Tunnel setup failed: {e}")
