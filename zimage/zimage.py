#!/usr/bin/env python3
"""
zimage — Chatbox CLI for Z-Image-Turbo (Colab deployment).

Usage:
    zimage set-url <tunnel-url>     Configure API endpoint
    zimage "prompt"                 One-shot generation (default 512x512)
    zimage -s 768x768 "prompt"      Custom resolution
    zimage chat                     Interactive chatbox mode
    zimage view                     View last generated image
    zimage health                   Check server status

Output images saved to ~/.hermes/zimage/output/.
"""

import json, os, sys, time, base64, readline
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    print("Missing 'requests'. Install: pip install requests")
    sys.exit(1)

# HOME is resolved at runtime
HOME = Path(os.path.expanduser("~/.hermes/zimage"))
HOME.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR = HOME / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = HOME / "config.json"
HISTORY_FILE = HOME / "history.json"

DEFAULT_SIZE = "512x512"  # T4-safe; use 384x384 for faster generation over Cloudflare tunnel


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def save_history(entries):
    HISTORY_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False))


def get_api_base():
    cfg = load_config()
    url = cfg.get("api_base", "")
    if not url:
        print("ERROR: No API URL configured.", file=sys.stderr)
        print("  Set it: zimage set-url <tunnel-url>", file=sys.stderr)
        sys.exit(1)
    return url.rstrip("/")


def health_check(url):
    try:
        r = requests.get(f"{url}/health", timeout=15)
        if r.status_code == 200:
            return r.json()
        return {"error": f"HTTP {r.status_code}"}
    except requests.exceptions.Timeout:
        return {"error": "Health check timed out — server may be busy generating"}
    except requests.exceptions.ConnectionError:
        return {"error": "Connection refused — server may not be running"}
    except Exception as e:
        return {"error": str(e)}


def generate(url, prompt, size=DEFAULT_SIZE, seed=-1, retries=2):
    """Call the Z-Image-Turbo API. Returns (image_path, metadata)."""
    payload = {"prompt": prompt, "size": size}
    if seed >= 0:
        payload["seed"] = seed

    for attempt in range(retries + 1):
        if attempt > 0:
            wait = attempt * 5
            print(f"  Retrying in {wait}s (attempt {attempt+1}/{retries+1})...")
            time.sleep(wait)

        t0 = time.time()
        try:
            r = requests.post(
                f"{url}/v1/images/generations",
                json=payload,
                timeout=180,  # Cloudflare free tunnel has ~100s limit; generation is ~80s
            )
            r.raise_for_status()
            data = r.json()
            break  # success
        except requests.exceptions.Timeout:
            if attempt < retries:
                print("  Request timed out (Cloudflare tunnel limit). Retrying...")
                continue
            print("ERROR: Request timed out after retries. Try a smaller resolution (384x384).", file=sys.stderr)
            return None, None
        except requests.exceptions.ConnectionError as e:
            if attempt < retries:
                print(f"  Connection dropped ({e}). Retrying...")
                continue
            print(f"ERROR: Connection failed — {e}", file=sys.stderr)
            return None, None
        except Exception as e:
            msg = str(e)[:200]
            print(f"ERROR: {msg}", file=sys.stderr)
            return None, None

    elapsed = time.time() - t0
    img_data = data.get("data", [{}])[0]
    b64 = img_data.get("b64_json", "")
    used_seed = img_data.get("seed", seed)
    gen_size = img_data.get("size", size)

    if not b64:
        print("ERROR: No image data in response", file=sys.stderr)
        print(f"  Response keys: {list(data.keys())}", file=sys.stderr)
        return None, None

    # Save image
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prompt = "".join(c if c.isalnum() else "_" for c in prompt[:40])
    fname = f"{timestamp}_{safe_prompt}_{gen_size}_s{used_seed}.png"
    fpath = OUTPUT_DIR / fname
    fpath.write_bytes(base64.b64decode(b64))

    meta = {
        "prompt": prompt,
        "size": gen_size,
        "seed": used_seed,
        "elapsed_s": round(elapsed, 2),
        "file": str(fpath),
    }

    print(f"  Generated in {elapsed:.1f}s | seed={used_seed} | {gen_size}")
    print(f"  Saved: {fpath}")

    return str(fpath), meta


def interactive_chat(url):
    """Interactive chatbox mode with /commands."""
    print("  Z-Image-Turbo Chatbox CLI")
    print("  /size WxH  /seed N  /health  /view  /history  /help  /quit")

    h = health_check(url)
    if "error" in h:
        print(f"  Server: {h['error']}")
    else:
        vram = h.get('vram', h.get('vram_gb', '?'))
        print(f"  Server OK — VRAM: {vram}GB | Default: {DEFAULT_SIZE}")

    size = DEFAULT_SIZE
    seed = -1
    history = load_history()

    while True:
        try:
            raw = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not raw:
            continue

        if raw.startswith("/"):
            parts = raw[1:].strip().split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("quit", "q", "exit"):
                print("Bye!")
                break
            elif cmd in ("help", "h", "?"):
                print("  /size WxH  /seed N  /health  /view  /history  /quit")
                print(f"  Default size: {DEFAULT_SIZE}")
            elif cmd == "size":
                if arg and "x" in arg:
                    try:
                        w, h = map(int, arg.split("x"))
                        size = arg
                        print(f"  Size: {size}")
                    except Exception:
                        print(f"  Bad size. Use WxH (e.g., 512x512)")
                else:
                    print(f"  Current: {size}. Usage: /size WxH")
            elif cmd == "seed":
                if arg:
                    try:
                        seed = int(arg)
                        print(f"  Seed: {seed}")
                    except Exception:
                        print(f"  Bad seed")
                else:
                    seed = -1
                    print("  Seed: random")
            elif cmd == "health":
                h = health_check(url)
                print(json.dumps(h, indent=2))
            elif cmd == "view":
                imgs = sorted(OUTPUT_DIR.glob("*.png"), key=os.path.getmtime, reverse=True)
                if imgs:
                    for i, p in enumerate(imgs[:3]):
                        print(f"  {i+1}. {p.name} ({p.stat().st_size//1024}KB)")
                else:
                    print("  No images yet.")
            elif cmd == "history":
                if history:
                    for i, h in enumerate(reversed(history[-10:])):
                        print(f"  {i+1}. [{h.get('size','?')}] {h.get('prompt','')[:80]}")
                else:
                    print("  No history.")
            else:
                print(f"  Unknown: {cmd}")
            continue

        # Generate
        print(f"  Generating ({size})...")
        fpath, meta = generate(url, raw, size=size, seed=seed)
        if meta:
            history.append(meta)
            save_history(history[-200:])


def main():
    args = sys.argv[1:]

    if not args:
        url = get_api_base()
        interactive_chat(url)
        return

    first = args[0]

    if first == "set-url":
        if len(args) < 2:
            print("Usage: zimage set-url <tunnel-url>", file=sys.stderr)
            sys.exit(1)
        url = args[1].rstrip("/")
        cfg = load_config()
        cfg["api_base"] = url
        save_config(cfg)
        print(f"API URL set: {url}")
        h = health_check(url)
        if "error" in h:
            print(f"  Server: {h['error']}")
        else:
            vram = h.get('vram', h.get('vram_gb', '?'))
            print(f"  Server online — VRAM: {vram}GB")
        return

    if first == "health":
        url = get_api_base()
        h = health_check(url)
        print(json.dumps(h, indent=2))
        return

    if first == "view":
        imgs = sorted(OUTPUT_DIR.glob("*.png"), key=os.path.getmtime, reverse=True)
        if not imgs:
            print("No images generated yet.")
        else:
            for i, p in enumerate(imgs[:10]):
                print(f"  {i+1}. {p.name} ({p.stat().st_size//1024}KB)")
            print(f"\nDirectory: {OUTPUT_DIR}")
        return

    if first == "chat":
        url = get_api_base()
        interactive_chat(url)
        return

    # Parse options before prompt
    size = DEFAULT_SIZE
    seed = -1
    prompt_parts = []
    i = 0
    while i < len(args):
        if args[i] == "-s" and i + 1 < len(args):
            size = args[i + 1]
            i += 2
        elif args[i] == "--seed" and i + 1 < len(args):
            seed = int(args[i + 1])
            i += 2
        elif args[i] in ("-h", "--help"):
            print(__doc__)
            return
        else:
            prompt_parts.append(args[i])
            i += 1

    prompt = " ".join(prompt_parts)
    if not prompt:
        url = get_api_base()
        interactive_chat(url)
        return

    url = get_api_base()
    print(f"Generating: {prompt[:80]}...")
    generate(url, prompt, size=size, seed=seed)


if __name__ == "__main__":
    main()
