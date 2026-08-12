# LTX-2.3 Colab keepalive example

Keep a free-tier Colab session + a Gradio/ComfyUI gateway alive:

- `keepalive.py` — re-sync the proxy token into sessions.json from
  `list_assignments()` and ping the kernel (idle watchdog). Treats a busy
  kernel (active generation) as OK. Run every 15 min via cron:
  `*/15 * * * * /usr/bin/python3 /path/to/keepalive.py`
- `gw_health.py` — VM-side: if the gateway process or Gradio (7860) or
  ComfyUI (8188) died, relaunch via `/content/ltx23/launch.sh`.

Requires the patched CLI (`colabctl patch`) so 401/404 refreshes instead of
pruning the session.
