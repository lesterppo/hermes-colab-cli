# AGENTS.md — Hermes Colab CLI

Instructions for AI coding assistants working with this repo or using
the colab CLI to operate Google Colab.

## What This Is

A token-efficient CLI wrapper around Google's official `google-colab-cli`.
31 commands that map 1:1 to the official CLI plus extensions (background exec,
GPU switching, notebook export, tunnel persistence). Every command supports
`-o FILE` for pointer-JSON output — the agent reads the full content from the
file when needed.

Two integration surfaces:
- **Standalone CLI** (`colab.py`) — run directly with `python3 colab.py <cmd>`
- **Hermes native tools** (`tools/colab_tool.py`) — 24 tools registered via
  `registry.register()` with `check_fn` gating

## File Structure

```
hermes-colab-cli/
├── colab.py              # Main CLI (31 commands)
├── install.sh            # One-line installer
├── SKILL.md              # Skill definition for Hermes agents (usage + deploy workflow)
├── AGENTS.md             # This file — for AI coding assistants
├── README.md             # Human-readable docs
└── references/
    └── auth_flow.md      # Definitive auth guide (what works + what failed)
```

## How to Install and Auth (for agents)

### Install

```bash
pip install google-colab-cli
cp colab.py ~/.hermes/scripts/colab/colab.py
```

### Auth (one-time)

The Google OAuth2 client is restricted — only ONE flow works reliably.

1. Tell the user to open this URL:
   ```
   https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com&redirect_uri=http://localhost&scope=openid+https://www.googleapis.com/auth/userinfo.email+https://www.googleapis.com/auth/cloud-platform+https://www.googleapis.com/auth/colaboratory+https://www.googleapis.com/auth/drive.file&access_type=offline
   ```

2. User signs in, approves all scopes.

3. Browser tries to redirect to `http://localhost/?code=...` and fails (expected).
   User copies the ENTIRE URL from the address bar.

4. Agent extracts the `code` parameter from the URL and runs:
   ```python
   import json, os, urllib.request, urllib.parse
   from datetime import datetime, timezone

   code = "4/0AdkVLP..."  # extracted from URL
   CLIENT_ID = "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com"
   CLIENT_SECRET = "d-FL95Q19q7MQmFpd7hHD0Ty"
   SCOPES = ["openid","https://www.googleapis.com/auth/userinfo.email",
             "https://www.googleapis.com/auth/cloud-platform",
             "https://www.googleapis.com/auth/colaboratory",
             "https://www.googleapis.com/auth/drive.file"]

   data = urllib.parse.urlencode({
       "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
       "redirect_uri": "http://localhost", "grant_type": "authorization_code",
   }).encode()
   req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
   req.add_header("Content-Type", "application/x-www-form-urlencoded")
   t = json.loads(urllib.request.urlopen(req, timeout=30).read())

   expiry = datetime.fromtimestamp(
       datetime.now(timezone.utc).timestamp() + t.get("expires_in", 3600),
       tz=timezone.utc)
   token = {"token":t["access_token"],"refresh_token":t.get("refresh_token",""),
            "token_uri":"https://oauth2.googleapis.com/token",
            "client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,
            "scopes":SCOPES,"expiry":expiry.isoformat()}
   os.makedirs(os.path.expanduser("~/.config/colab-cli"), exist_ok=True)
   with open(os.path.expanduser("~/.config/colab-cli/token.json"),"w") as f:
       json.dump(token, f, indent=2)
   os.makedirs(os.path.expanduser("~/.config/gcloud"), exist_ok=True)
   with open(os.path.expanduser("~/.config/gcloud/application_default_credentials.json"),"w") as f:
       json.dump({"client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,
                  "refresh_token":t.get("refresh_token",""),"type":"authorized_user"}, f)
   ```

5. Verify: `colab --auth=adc sessions`

### Token Refresh

The CLI auto-refreshes expired tokens. If refresh fails, re-run steps 1-5.

## Key Patterns for Agents

### Create session + execute code

```bash
python3 colab.py new -s demo --gpu T4 -o /tmp/sess.json
echo "print('hello')" | python3 colab.py exec -s demo -o /tmp/out.json
cat /tmp/out.json  # read full output
python3 colab.py stop -s demo
```

### Shell command on VM

```bash
python3 colab.py console -s demo --cmd "nvidia-smi" -o /tmp/gpu.json
```

### Background execution with polling

```bash
echo "long_running_code..." | python3 colab.py exec_bg -s demo -o /tmp/job.json
# stdout: {"ok":true,"job_id":"abc12345","status":"running"}
python3 colab.py exec_bg_poll abc12345 -o /tmp/result.json
```

### Deploy LLM

Use the full workflow from SKILL.md — provision, install deps, download model,
start server, tunnel. See `SKILL.md` → "Deploy LLM to Colab" section.

### Tunnel persistence

```bash
python3 colab.py tunnel set --url "https://xxx.trycloudflare.com/v1" -s session
python3 colab.py tunnel get -s session
```

## Pitfalls

1. **Auth URL must use `redirect_uri=http://localhost`** — no PKCE, no
   `token_usage=remote`, no port. Other combinations fail with
   `redirect_uri_mismatch`.
2. **Token exchange uses `client_secret`, not PKCE** — the OAuth client is
   a web application type, not a native app.
3. **Console uses `--cmd` not `--command`** — `--command` conflicts with
   the subcommand name in argparse.
4. **Background exec uses `os.fork()`** — child survives parent exit.
   Poll with `exec_bg_poll <job_id>`.
5. **Tunnel URLs are Cloudflare temp domains** — save with `tunnel set`
   and retrieve with `tunnel get`. They change on session restart.
6. **GPU switch destroys session** — stops and recreates. State lost.
