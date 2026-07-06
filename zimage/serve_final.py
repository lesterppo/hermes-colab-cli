"""Z-Image-Turbo server for Colab — threading approach.

Accepts --hf-token <token> as first arguments (for colab run passthrough).
"""
import torch, time, os, io, base64, subprocess, re, threading, asyncio, sys

# Parse --hf-token from args
hf_token = ""
for i, arg in enumerate(sys.argv[1:], 1):
    if arg == "--hf-token" and i < len(sys.argv) - 1:
        hf_token = sys.argv[i + 1]
        break
if hf_token:
    from huggingface_hub import login
    login(token=hf_token)
    print(f"[auth] HF logged in")

# Kill old
subprocess.run("pkill -f uvicorn 2>/dev/null; pkill -f cloudflared 2>/dev/null; sleep 2; fuser -k 8081/tcp 2>/dev/null || true", shell=True)
time.sleep(2)

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

print("[1/3] Loading model...")
t0 = time.time()
from diffusers import ZImagePipeline
pipe = ZImagePipeline.from_pretrained(
    "Tongyi-MAI/Z-Image-Turbo", torch_dtype=torch.bfloat16,
    device_map="balanced", low_cpu_mem_usage=True,
)
pipe.enable_attention_slicing()
print(f"Loaded {time.time()-t0:.0f}s VRAM={torch.cuda.max_memory_allocated()/1e9:.1f}GB")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ImgReq(BaseModel):
    prompt: str; size: str = "512x512"; seed: int = -1

@app.get("/health")
async def health():
    return {"status":"ok","model":"Z-Image-Turbo","vram":round(torch.cuda.max_memory_allocated()/1e9,2)}

@app.post("/v1/images/generations")
async def gen(req: ImgReq):
    try: w,h = map(int, req.size.split("x"))
    except: w,h = 512,512
    s = req.seed if req.seed>=0 else int(torch.randint(0,2**31-1,(1,)).item())
    g = torch.Generator("cuda").manual_seed(s)
    t0=time.time()
    img = pipe(prompt=req.prompt,height=h,width=w,num_inference_steps=9,guidance_scale=0.0,generator=g).images[0]
    buf=io.BytesIO(); img.save(buf,format="PNG")
    b64=base64.b64encode(buf.getvalue()).decode()
    dt=time.time()-t0
    print(f"[gen] {w}x{h} seed={s} {dt:.1f}s {req.prompt[:50]}")
    return {"data":[{"b64_json":b64,"seed":s,"size":f"{w}x{h}"}],"elapsed_s":round(dt,2)}

print("[2/3] Starting uvicorn thread...")
import uvicorn
config = uvicorn.Config(app, host="0.0.0.0", port=8081, log_level="warning")
server = uvicorn.Server(config)
def serve():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(server.serve())
threading.Thread(target=serve, daemon=True).start()
time.sleep(2)
print("Server running")

print("[3/3] Starting tunnel...")
subprocess.run("curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared 2>/dev/null; chmod +x /usr/local/bin/cloudflared 2>/dev/null", shell=True, timeout=10)
subprocess.Popen(
    ["/usr/local/bin/cloudflared","tunnel","--url","http://localhost:8081"],
    stdout=open("/content/tunnel_stdout.log","w"), stderr=open("/content/tunnel_stderr.log","w"),
    start_new_session=True,
)
time.sleep(8)
for lf in ["/content/tunnel_stdout.log","/content/tunnel_stderr.log"]:
    try:
        with open(lf) as f:
            m = re.search(r'https://[^\s"]*\.trycloudflare\.com', f.read())
            if m:
                url = m.group(0)
                with open("/content/tunnel_url.txt","w") as f: f.write(url)
                print(f"*** TUNNEL={url} ***")
                break
    except: pass

print("READY. Server alive on :8080")
# Keep process alive
while True:
    time.sleep(60)
