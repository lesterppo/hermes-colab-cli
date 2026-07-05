#!/usr/bin/env python3
"""Pony Diffusion V6 XL — FastAPI image generation server for Colab.
Endpoints:
  POST /generate  {"prompt": "...", "negative_prompt": "...", "num_images": 1, "steps": 30, "guidance_scale": 7.5, "width": 1024, "height": 1024}
      → returns ZIP of generated images (pony_001.png, pony_002.png, ...)
  GET /health      → {"status": "ok"}
"""

import io, os, shutil, time, uuid, zipfile
from pathlib import Path

import torch
from diffusers import StableDiffusionXLPipeline
from huggingface_hub import hf_hub_download
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="Pony Diffusion V6 XL Server")
OUTPUT_BASE = Path("/tmp/pony_output")
OUTPUT_BASE.mkdir(exist_ok=True)

# ── Model loading ─────────────────────────────────────────────────
MODEL_REPO = "LyliaEngine/Pony_Diffusion_V6_XL"
MODEL_FILE = "ponyDiffusionV6XL_v6StartWithThisOne.safetensors"
pipe = None

def load_model():
    global pipe
    print(f"Downloading {MODEL_REPO}/{MODEL_FILE}...")
    model_path = hf_hub_download(
        repo_id=MODEL_REPO,
        filename=MODEL_FILE,
        local_dir="/content",
    )
    print(f"Model downloaded to {model_path} ({os.path.getsize(model_path)/1e9:.1f} GB)")
    print("Loading into GPU...")
    pipe = StableDiffusionXLPipeline.from_single_file(
        model_path,
        torch_dtype=torch.float16,
    )
    pipe.enable_model_cpu_offload()
    pipe.enable_vae_slicing()
    if hasattr(pipe, "enable_vae_tiling"):
        pipe.enable_vae_tiling()
    print("Model ready.")

# ── Request model ─────────────────────────────────────────────────
class GenerateRequest(BaseModel):
    prompt: str = Field(..., description="Positive prompt for image generation")
    negative_prompt: str = Field(
        default="low quality, blurry, distorted, bad anatomy, watermark, text, signature",
        description="Negative prompt"
    )
    num_images: int = Field(default=1, ge=1, le=4, description="Number of images to generate")
    steps: int = Field(default=30, ge=15, le=50, description="Inference steps")
    guidance_scale: float = Field(default=7.5, ge=1.0, le=15.0, description="CFG scale")
    width: int = Field(default=1024, ge=512, le=1280, description="Image width (multiple of 8)")
    height: int = Field(default=1024, ge=512, le=1280, description="Image height (multiple of 8)")
    seed: int = Field(default=-1, description="Random seed (-1 for random)")

# ── Endpoints ─────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": pipe is not None}


@app.post("/generate")
def generate(req: GenerateRequest):
    if pipe is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet")

    w = (req.width // 8) * 8
    h = (req.height // 8) * 8

    req_id = uuid.uuid4().hex[:8]
    out_dir = OUTPUT_BASE / req_id
    out_dir.mkdir(parents=True, exist_ok=True)

    generator = None
    if req.seed >= 0:
        generator = torch.Generator(device="cpu").manual_seed(req.seed)

    for i in range(req.num_images):
        t0 = time.time()
        result = pipe(
            prompt=req.prompt,
            negative_prompt=req.negative_prompt,
            num_inference_steps=req.steps,
            guidance_scale=req.guidance_scale,
            width=w,
            height=h,
            generator=generator,
        )
        img = result.images[0]
        fname = f"pony_{i+1:03d}.png"
        img.save(str(out_dir / fname), format="PNG")
        elapsed = time.time() - t0
        print(f"  [{i+1}/{req.num_images}] {fname}  ({elapsed:.1f}s)")

    # Zip
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out_dir.iterdir()):
            zf.write(str(p), p.name)
    zip_buf.seek(0)
    zip_data = zip_buf.read()

    shutil.rmtree(out_dir, ignore_errors=True)

    return Response(
        content=zip_data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="pony_{req_id}.zip"',
            "X-Generated-Images": str(req.num_images),
            "X-Request-Id": req_id,
        },
    )


@app.on_event("startup")
def startup():
    load_model()
