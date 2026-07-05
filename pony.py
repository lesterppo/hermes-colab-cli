#!/usr/bin/env python3
"""Pony Diffusion V6 XL — local CLI chatbox for Colab-hosted inference. v2.0

Usage:
  pony set-url <api_url>         Set the Colab tunnel API base URL
  pony chat                      Interactive chatbox mode
  pony generate "<prompt>"       One-shot generation
  pony batch "<prompt>"          Generate multiple variants in parallel
  pony view [session_dir]        Extract and display last generation
  pony watch                     24/7 tunnel health monitor + auto-reconnect
  pony url                       Show current API URL
  pony test                      Health check
  pony reconnect                 Recover from session death

Chatbox commands:
  /set steps N        Inference steps (15-50)
  /set cfg N          CFG scale (1.0-15.0)  
  /set size W H       Output dimensions
  /set num N          Images per generation (1-4)
  /set negative TEXT  Negative prompt
  /batch PROMPT       Generate 3 variants (different seeds)
  /view               Extract and show last generated images
  /reconnect          Check tunnel and attempt recovery
  /params             Show current parameters
  /test               Server health check
  /stats              Generation statistics
  /open               Output directory path
"""

import argparse, io, json, os, subprocess, sys, time, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "ponydiff"
CONFIG_FILE = CONFIG_DIR / "config.json"
OUTPUT_BASE = Path.home() / "pony_output"
WATCHDOG_PID_FILE = CONFIG_DIR / "watchdog.pid"
COLAB_SCRIPT = Path.home() / ".hermes" / "scripts" / "colab" / "colab.py"

DEFAULT_NEGATIVE = "low quality, blurry, distorted, bad anatomy, watermark, text, signature"


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def api_call(api_url, endpoint, method="GET", data=None, timeout=120):
    url = f"{api_url.rstrip('/')}/{endpoint.lstrip('/')}"
    req = urllib.request.Request(url, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode()
        req.data = body
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        content = resp.read()
        return resp, content
    except urllib.error.HTTPError as e:
        return e, e.read()
    except Exception as e:
        return None, str(e).encode()


def health_ok(url):
    resp, _ = api_call(url, "/health", timeout=10)
    return hasattr(resp, "status") and resp.status == 200


def generate_images(api_url, prompt, negative_prompt="", num_images=1,
                    steps=30, guidance_scale=7.5, width=1024, height=1024, seed=-1):
    data = {
        "prompt": prompt,
        "negative_prompt": negative_prompt or DEFAULT_NEGATIVE,
        "num_images": num_images,
        "steps": steps,
        "guidance_scale": guidance_scale,
        "width": width,
        "height": height,
        "seed": seed,
    }
    t0 = time.time()
    resp, content = api_call(api_url, "/generate", method="POST", data=data, timeout=300)
    elapsed = time.time() - t0
    if hasattr(resp, "status") and resp.status == 200:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = OUTPUT_BASE / timestamp
        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / f"pony_{timestamp}.zip"
        zip_path.write_bytes(content)
        filenames = []
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            filenames = sorted(zf.namelist())
        return zip_path, filenames, elapsed, None
    else:
        error_msg = content.decode() if content else "unknown error"
        return None, [], elapsed, error_msg


def extract_zip(zip_path):
    """Extract all images from a zip to its parent directory."""
    out_dir = zip_path.parent
    with zipfile.ZipFile(zip_path) as zf:
        extracted = []
        for name in zf.namelist():
            zf.extract(name, out_dir)
            extracted.append(out_dir / name)
    return extracted


def find_latest_session():
    """Find the most recent output session directory."""
    if not OUTPUT_BASE.exists():
        return None
    dirs = sorted(OUTPUT_BASE.iterdir(), reverse=True)
    for d in dirs:
        if d.is_dir() and (list(d.glob("*.png")) or list(d.glob("*.zip"))):
            return d
    return None


# ─── Commands ──────────────────────────────────────────────────────────────

def cmd_set_url(args):
    cfg = load_config()
    cfg["api_url"] = args.api_url.rstrip("/")
    save_config(cfg)
    print(f"API URL set to: {cfg['api_url']}")


def cmd_url(args):
    cfg = load_config()
    url = cfg.get("api_url")
    if url:
        print(f"API URL: {url}")
    else:
        print("No API URL set. Use: pony set-url <url>")


def cmd_test(args):
    cfg = load_config()
    url = cfg.get("api_url")
    if not url:
        print("ERROR: No API URL set. Run: pony set-url <url>")
        sys.exit(1)
    print(f"Checking {url}/health ...")
    resp, content = api_call(url, "/health")
    if hasattr(resp, "status") and resp.status == 200:
        print(f"OK: {content.decode()}")
    else:
        print(f"FAIL: {content.decode() if content else 'no response'}")


def cmd_generate(args):
    cfg = load_config()
    url = cfg.get("api_url")
    if not url:
        print("ERROR: No API URL set. Run: pony set-url <url>")
        sys.exit(1)
    print(f"Generating: \"{args.prompt}\"")
    zip_path, filenames, elapsed, error = generate_images(
        url, args.prompt,
        negative_prompt=args.negative_prompt or "",
        num_images=args.num, steps=args.steps,
        guidance_scale=args.cfg_scale,
        width=args.width, height=args.height, seed=args.seed,
    )
    if error:
        print(f"ERROR ({elapsed:.1f}s): {error}")
        sys.exit(1)
    print(f"Done ({elapsed:.1f}s)")
    print(f"Saved: {zip_path}")
    for f in filenames:
        print(f"  → {f}")


def cmd_batch(args):
    """Generate multiple variants in parallel by varying seeds/styles."""
    cfg = load_config()
    url = cfg.get("api_url")
    if not url:
        print("ERROR: No API URL set.")
        sys.exit(1)

    n = args.num if args.num else 3
    base_seed = args.seed if args.seed >= 0 else int(time.time())
    
    print(f"Batch: {n} variants of \"{args.prompt}\"")
    print(f"  Steps: {args.steps}, CFG: {args.cfg_scale}, Size: {args.width}x{args.height}")
    
    def gen_one(i):
        seed = base_seed + i * 42
        return generate_images(url, args.prompt,
            negative_prompt=args.negative_prompt or "",
            num_images=1, steps=args.steps,
            guidance_scale=args.cfg_scale,
            width=args.width, height=args.height, seed=seed)

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(gen_one, i): i for i in range(n)}
        for f in as_completed(futures):
            i = futures[f]
            zip_path, filenames, elapsed, error = f.result()
            if error:
                print(f"  [{i+1}/{n}] ERROR: {error}")
            else:
                print(f"  [{i+1}/{n}] {zip_path.name} ({elapsed:.1f}s)")
                results.append((zip_path, filenames))

    total_elapsed = time.time() - t0
    if results:
        # Extract all zips so you can browse
        print(f"\nDone: {len(results)}/{n} generated ({total_elapsed:.1f}s total)")
        for zp, fnames in results:
            extracted = extract_zip(zp)
            for ef in extracted:
                print(f"  → {ef}")
    else:
        print(f"\nAll {n} failed.")


def cmd_view(args):
    """Extract and display the latest generated images."""
    session_dir = None
    if args.session_dir:
        session_dir = Path(args.session_dir)
    else:
        session_dir = find_latest_session()

    if not session_dir or not session_dir.exists():
        print("No generated images found. Run pony generate first.")
        sys.exit(1)

    print(f"Session: {session_dir.name}")

    # Extract any zips
    for zp in sorted(session_dir.glob("*.zip")):
        print(f"  Extracting {zp.name}...")
        extract_zip(zp)

    # List images
    imgs = sorted(session_dir.glob("*.png")) + sorted(session_dir.glob("*.jpg"))
    if not imgs:
        print("  No images found.")
        return

    print(f"  {len(imgs)} images:")
    for img in imgs:
        size_kb = img.stat().st_size / 1024
        print(f"    {img.name}  ({size_kb:.0f} KB)")

    # If xdg-open or wslview is available, offer to open
    opener = None
    for cmd in ["wslview", "xdg-open", "open"]:
        if subprocess.run(["which", cmd], capture_output=True).returncode == 0:
            opener = cmd
            break

    if opener and imgs:
        print(f"\n  Open with: {opener} {imgs[0]}")

    # Save session path to config for quick re-view
    cfg = load_config()
    cfg["last_viewed"] = str(session_dir)
    save_config(cfg)


def cmd_reconnect(args):
    """Check tunnel health and attempt recovery."""
    cfg = load_config()
    url = cfg.get("api_url")
    session = getattr(args, "session", None) or cfg.get("colab_session", "ponydiff")

    if url and health_ok(url):
        print(f"Server is healthy: {url}")
        return

    print("Server unreachable. Attempting recovery...")

    # Check if Colab session exists
    r = subprocess.run(
        [sys.executable, str(COLAB_SCRIPT), "status", "-s", session],
        capture_output=True, text=True, timeout=15
    )
    print(f"Session status: {r.stdout.strip()}")

    if "IDLE" in r.stdout or "RUNNING" in r.stdout:
        # Check tunnel URL
        r = subprocess.run(
            [sys.executable, str(COLAB_SCRIPT), "tunnel", "get", "-s", session],
            capture_output=True, text=True, timeout=15
        )
        tunnel_url = None
        for line in r.stdout.splitlines():
            if "trycloudflare.com" in line:
                tunnel_url = line.strip().split()[-1]
                break

        if tunnel_url and tunnel_url != url:
            print(f"Tunnel URL changed: {tunnel_url}")
            cfg["api_url"] = tunnel_url
            cfg["colab_session"] = session
            save_config(cfg)
            if health_ok(tunnel_url):
                print(f"Reconnected: {tunnel_url}")
                return

        # Server might need restart on the VM
        print("Tunnel OK but server not responding. May need to restart server on VM.")
        print("Run: python3 ~/.hermes/scripts/colab/colab.py exec -s ponydiff --code \"import subprocess, sys; subprocess.Popen([sys.executable, '-m', 'uvicorn', 'server:app', '--host', '0.0.0.0', '--port', '8000'], cwd='/content')\"")
    else:
        print(f"Session '{session}' not active. Recreating...")
        r = subprocess.run(
            [sys.executable, str(COLAB_SCRIPT), "new", "-s", session, "--gpu", "T4"],
            capture_output=True, text=True, timeout=120
        )
        print(r.stdout.strip())
        print("Session created. Run full deploy (see pony-diffusion-colab skill).")


def cmd_watch(args):
    """Background watchdog: monitor tunnel health and auto-reconnect."""
    cfg = load_config()
    url = cfg.get("api_url")
    if not url and not cfg.get("colab_session"):
        print("ERROR: Set API URL first (pony set-url) or colab session (in config)")
        sys.exit(1)

    session = cfg.get("colab_session", "ponydiff")
    interval = getattr(args, "interval", 120) or 120

    # Check if already running
    if WATCHDOG_PID_FILE.exists():
        try:
            old_pid = int(WATCHDOG_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            print(f"Watchdog already running (PID {old_pid})")
            return
        except (OSError, ValueError):
            WATCHDOG_PID_FILE.unlink(missing_ok=True)

    print(f"Starting watchdog (interval={interval}s, session={session})...")
    
    # Fork to background
    pid = os.fork()
    if pid != 0:
        WATCHDOG_PID_FILE.write_text(str(pid))
        print(f"Watchdog PID: {pid}")
        print(f"Log: {OUTPUT_BASE}/watchdog.log")
        return

    # Child: watchdog loop
    log_file = OUTPUT_BASE / "watchdog.log"
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    
    def wlog(msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        with open(log_file, "a") as f:
            f.write(line + "\n")

    wlog(f"Watchdog started. URL: {url}, interval: {interval}s")

    failures = 0
    while True:
        time.sleep(interval)
        
        ok = health_ok(url) if url else False
        
        if ok:
            if failures > 0:
                wlog(f"Server recovered after {failures} failures")
            failures = 0
            continue

        failures += 1
        wlog(f"Health check failed ({failures} consecutive)")

        if failures < 3:
            continue

        # Try reconnection
        wlog("Attempting reconnect...")
        try:
            r = subprocess.run(
                [sys.executable, str(COLAB_SCRIPT), "tunnel", "get", "-s", session],
                capture_output=True, text=True, timeout=15
            )
            for line in r.stdout.splitlines():
                if "trycloudflare.com" in line:
                    new_url = line.strip().split()[-1]
                    if new_url != url:
                        cfg = load_config()
                        cfg["api_url"] = new_url
                        save_config(cfg)
                        url = new_url
                        wlog(f"Tunnel URL updated: {url}")
                        if health_ok(url):
                            wlog("Reconnected successfully")
                            failures = 0
                        break
        except Exception as e:
            wlog(f"Reconnect error: {e}")

        if failures > 10:
            wlog("Max failures reached. Watchdog giving up.")


def cmd_chat(args):
    """Interactive chatbox mode."""
    cfg = load_config()
    url = cfg.get("api_url")
    if not url:
        print("ERROR: No API URL set. Run: pony set-url <url>")
        sys.exit(1)

    if not health_ok(url):
        print(f"ERROR: Server not healthy at {url}")
        print("Try: pony reconnect")
        sys.exit(1)

    print("=" * 60)
    print("  Pony Diffusion V6 XL — Chatbox v2.0")
    print(f"  Server: {url}")
    print("  Type 'quit' to exit, 'help' for commands")
    print("=" * 60)

    params = {
        "steps": 30, "cfg_scale": 7.5, "width": 1024, "height": 1024,
        "num": 1, "seed": -1, "negative": DEFAULT_NEGATIVE,
    }
    generation_count = 0
    total_time = 0.0

    while True:
        try:
            line = input("\n🐴> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not line:
            continue

        if line.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break
        elif line.lower() == "help":
            print("Chat commands:")
            print("  /set steps N        Inference steps (15-50)")
            print("  /set cfg N          CFG scale (1.0-15.0)")
            print("  /set size W H       Output dimensions")
            print("  /set num N          Images per generation (1-4)")
            print("  /set seed N         Seed (-1 = random)")
            print("  /set negative TEXT  Negative prompt")
            print("  /batch PROMPT       Generate 3 seed variants in parallel")
            print("  /view               Extract & show last generation")
            print("  /reconnect          Health check & auto-recovery")
            print("  /params             Show current parameters")
            print("  /test               Server health check")
            print("  /stats              Generation statistics")
            print("  /open               Output directory path")
            print("  quit, exit, q       Exit")
            print("  <anything else>     Generate images from prompt")
            continue

        if line.startswith("/"):
            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()

            if cmd == "/params":
                print("Current parameters:")
                for k, v in params.items():
                    print(f"  {k}: {v}")
            elif cmd == "/set":
                if len(parts) < 2:
                    print("Usage: /set <key> <value>")
                    continue
                key = parts[1].lower()
                val = parts[2] if len(parts) > 2 else ""
                if key == "steps": params["steps"] = max(15, min(50, int(val)))
                elif key == "cfg": params["cfg_scale"] = max(1.0, min(15.0, float(val)))
                elif key == "size":
                    wh = val.split()
                    if len(wh) == 2:
                        params["width"] = (int(wh[0]) // 8) * 8
                        params["height"] = (int(wh[1]) // 8) * 8
                elif key == "num": params["num"] = max(1, min(4, int(val)))
                elif key == "seed": params["seed"] = int(val)
                elif key == "negative": params["negative"] = val
                print(f"  {key} = {params.get(key, val)}")
            elif cmd == "/test":
                if health_ok(url):
                    print(f"Server OK: {url}")
                else:
                    print(f"Server FAIL: {url}\nTry: /reconnect")
            elif cmd == "/reconnect":
                print("Checking server...")
                try:
                    cmd_reconnect(argparse.Namespace(session=cfg.get("colab_session")))
                except Exception as e:
                    print(f"Reconnect failed: {e}")
            elif cmd == "/view":
                try:
                    cmd_view(argparse.Namespace(session_dir=None))
                except SystemExit:
                    pass
            elif cmd == "/batch":
                prompt = parts[2] if len(parts) > 2 else ""
                if not prompt:
                    print("Usage: /batch <prompt>")
                    continue
                print(f"Batch: 3 variants of \"{prompt}\"")
                t0 = time.time()
                results = []
                base_seed = int(time.time())
                with ThreadPoolExecutor(max_workers=2) as ex:
                    futures = {ex.submit(generate_images, url, prompt, params["negative"], 1,
                                        params["steps"], params["cfg_scale"],
                                        params["width"], params["height"],
                                        base_seed + i * 42): i for i in range(3)}
                    for f in as_completed(futures):
                        i = futures[f]
                        zp, fnames, elapsed, error = f.result()
                        if error:
                            print(f"  [{i+1}/3] ERROR: {error}")
                        else:
                            print(f"  [{i+1}/3] {zp.name} ({elapsed:.1f}s)")
                            results.append(zp)
                batch_time = time.time() - t0
                if results:
                    print(f"\nDone: {len(results)}/3 ({batch_time:.1f}s)")
                    for zp in results:
                        for ef in extract_zip(zp):
                            print(f"  → {ef}")
                    generation_count += len(results)
                    total_time += batch_time
            elif cmd == "/stats":
                print(f"Output directory: {OUTPUT_BASE}")
                print(f"This session: {generation_count} gens, {total_time:.0f}s total")
                if OUTPUT_BASE.exists():
                    dirs = sorted(OUTPUT_BASE.iterdir(), reverse=True)
                    total_imgs = sum(len(list(d.glob("*.png"))) for d in dirs if d.is_dir())
                    total_zips = sum(len(list(d.glob("*.zip"))) for d in dirs if d.is_dir())
                    print(f"All time: {len(dirs)} sessions, {total_imgs} images, {total_zips} zips")
            elif cmd == "/open":
                os.makedirs(OUTPUT_BASE, exist_ok=True)
                print(f"Output dir: {OUTPUT_BASE}")
                latest = find_latest_session()
                if latest:
                    print(f"Latest session: {latest}")
            else:
                print(f"Unknown command: {cmd}")
            continue

        # Generate from prompt
        print(f"Generating... ({params['steps']} steps, {params['width']}x{params['height']})")
        zip_path, filenames, elapsed, error = generate_images(
            url, line, negative_prompt=params["negative"],
            num_images=params["num"], steps=params["steps"],
            guidance_scale=params["cfg_scale"],
            width=params["width"], height=params["height"], seed=params["seed"],
        )
        if error:
            print(f"ERROR ({elapsed:.1f}s): {error}")
        else:
            print(f"Done ({elapsed:.1f}s)")
            print(f"Saved: {zip_path}")
            for f in filenames:
                print(f"  → {f}")
            generation_count += 1
            total_time += elapsed
            if params["seed"] >= 0:
                params["seed"] += 1


# ─── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pony Diffusion V6 XL — CLI chatbox v2.0")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("set-url", help="Set API base URL")
    p.add_argument("api_url", help="e.g. https://xxx.trycloudflare.com")

    sub.add_parser("url", help="Show current API URL")
    sub.add_parser("test", help="Health check")
    sub.add_parser("chat", help="Interactive chatbox mode")
    sub.add_parser("reconnect", help="Recover from session death")
    sub.add_parser("watch", help="Start background health monitor").add_argument(
        "--interval", "-i", type=int, default=120, help="Check interval in seconds")

    p = sub.add_parser("generate", help="One-shot image generation")
    p.add_argument("prompt", help="Image prompt")
    p.add_argument("--negative-prompt", "-n", default="", help="Negative prompt")
    p.add_argument("--num", "-N", type=int, default=1, help="Number of images (1-4)")
    p.add_argument("--steps", "-s", type=int, default=30, help="Inference steps")
    p.add_argument("--cfg-scale", "-c", type=float, default=7.5, help="CFG scale")
    p.add_argument("--width", "-W", type=int, default=1024, help="Image width")
    p.add_argument("--height", "-H", type=int, default=1024, help="Image height")
    p.add_argument("--seed", type=int, default=-1, help="Random seed")

    p = sub.add_parser("batch", help="Generate multiple seed variants in parallel")
    p.add_argument("prompt", help="Image prompt")
    p.add_argument("--num", "-N", type=int, default=3, help="Number of variants")
    p.add_argument("--negative-prompt", "-n", default="", help="Negative prompt")
    p.add_argument("--steps", "-s", type=int, default=30)
    p.add_argument("--cfg-scale", "-c", type=float, default=7.5)
    p.add_argument("--width", "-W", type=int, default=1024)
    p.add_argument("--height", "-H", type=int, default=1024)
    p.add_argument("--seed", type=int, default=-1)

    p = sub.add_parser("view", help="Extract and display last generated images")
    p.add_argument("session_dir", nargs="?", help="Specific session directory")

    args = parser.parse_args()

    dispatch = {
        "set-url": cmd_set_url, "url": cmd_url, "test": cmd_test,
        "chat": cmd_chat, "generate": cmd_generate, "batch": cmd_batch,
        "view": cmd_view, "reconnect": cmd_reconnect, "watch": cmd_watch,
    }
    if args.command in dispatch:
        dispatch[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
