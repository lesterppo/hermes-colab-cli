"""Generate image on Colab and save to disk (no tunnel needed)."""
import torch, time, sys, os

# Parse --hf-token
hf_token = ""
for i, arg in enumerate(sys.argv[1:], 1):
    if arg == "--hf-token" and i < len(sys.argv) - 1:
        hf_token = sys.argv[i+1]; break
if hf_token:
    from huggingface_hub import login; login(token=hf_token)

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

print("[1/2] Loading model...")
t0 = time.time()
from diffusers import ZImagePipeline
pipe = ZImagePipeline.from_pretrained(
    "Tongyi-MAI/Z-Image-Turbo", torch_dtype=torch.bfloat16,
    device_map="balanced", low_cpu_mem_usage=True,
)
pipe.enable_attention_slicing()
print(f"Loaded {time.time()-t0:.0f}s VRAM={torch.cuda.max_memory_allocated()/1e9:.1f}GB")

prompt = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "A cute orange tabby cat on a windowsill, warm sunlight, photorealistic"
size = 512
print(f"[2/2] Generating {size}x{size}: {prompt[:60]}")
t0 = time.time()
img = pipe(prompt=prompt, height=size, width=size, num_inference_steps=9,
           guidance_scale=0.0, generator=torch.Generator("cuda").manual_seed(42)).images[0]
img.save("/content/zimage_output.png")
print(f"SAVED /content/zimage_output.png in {time.time()-t0:.0f}s")
