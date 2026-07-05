# Colab OAuth2 Authentication

## What Works (the only reliable method)

**Copy-paste code from browser URL bar.** The trick: use `redirect_uri=http://localhost`
(no port, no PKCE, no `token_usage=remote`). The browser redirects to localhost
and fails — copy the URL with the code from the address bar.

### Complete one-time setup

**Step 1:** Open this URL in your Windows browser:

```
https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com&redirect_uri=http://localhost&scope=openid+https://www.googleapis.com/auth/userinfo.email+https://www.googleapis.com/auth/cloud-platform+https://www.googleapis.com/auth/colaboratory+https://www.googleapis.com/auth/drive.file&access_type=offline
```

**Step 2:** Sign in with your Google account, approve all scopes.

**Step 3:** Browser tries to load `http://localhost/?code=...&scope=...` and fails.
Copy the **entire URL from the address bar**. It looks like:
```
http://localhost/?iss=https://accounts.google.com&code=4/0AdkVLP...&scope=email%20https://www.googleapis.com/auth/colaboratory%20...&authuser=1&prompt=consent
```

**Step 4:** Paste the URL to your AI agent. It extracts the `code` parameter and
runs the token exchange script below.

**Step 5:** The exchange script saves tokens to:
- `~/.config/colab-cli/token.json` (Colab CLI format)
- `~/.config/gcloud/application_default_credentials.json` (ADC format)

After this, the token auto-refreshes on expiry.

### Token exchange script

The AI agent runs this with your code:

```python
import json, os, urllib.request, urllib.parse
from datetime import datetime, timezone

code = "4/0AdkVLP..."  # extracted from the URL you pasted
CLIENT_ID = "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com"
CLIENT_SECRET = "<your-oauth-client-secret>"
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
token = {
    "token": t["access_token"],
    "refresh_token": t.get("refresh_token", ""),
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    "scopes": SCOPES,
    "expiry": expiry.isoformat(),
}
os.makedirs(os.path.expanduser("~/.config/colab-cli"), exist_ok=True)
with open(os.path.expanduser("~/.config/colab-cli/token.json"), "w") as f:
    json.dump(token, f, indent=2)
os.makedirs(os.path.expanduser("~/.config/gcloud"), exist_ok=True)
with open(os.path.expanduser("~/.config/gcloud/application_default_credentials.json"), "w") as f:
    json.dump({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
               "refresh_token": t.get("refresh_token", ""), "type": "authorized_user"}, f)
print("Token saved. CLI ready.")
```

### Token refresh

The CLI wrapper auto-refreshes expired tokens. If refresh fails, re-run steps 1-5.

### Test auth

```bash
colab --auth=adc sessions
# Expected: "No active sessions found on server." or list of sessions
```

---

## What Failed (do NOT repeat)

| Approach | Failure | Root Cause |
|---|---|---|
| OAuth2 with `redirect_uri=https://sdk.cloud.google.com/applicationdefaultauthcode.html` | `redirect_uri_mismatch` error 400 | OAuth client `764086051850-...` is Google-internal; not authorized for this Gmail account with that redirect |
| Same URL but with `token_usage=remote` | Same `redirect_uri_mismatch` | Same root cause |
| Device code flow (`oauth2.googleapis.com/device/code`) | HTTP 401 Unauthorized | Client not configured for device flow |
| WSL2 localhost forwarding (local server on port) | Browser "keeps loading" or times out | WSL2 localhost forwarding broken; Windows browser can't reach WSL ports |
| `gcloud auth login` on Windows | gcloud install failed (Python path, module errors) | SDK extraction incomplete |
| gcloud `--no-launch-browser` | Same `redirect_uri_mismatch` | Same OAuth client restriction |
| gcloud `--no-browser` | Remote bootstrap requires gcloud on both machines | No gcloud on Windows |
| Firefox cookie extraction + SAPISIDHASH | HTTP 403 on all Colab API endpoints | Colab requires OAuth2 bearer tokens, not cookie auth |
| `google_auth_oauthlib` PKCE flow | `code_verifier is not needed` in token exchange | This OAuth client uses `client_secret`, not PKCE |
| Playwright browser automation for OAuth2 | Couldn't maintain CDP connection | Browser disconnected between sessions |

### Key insight

The `redirect_uri=http://localhost` (no port, no PKCE, no `token_usage`) is the
ONLY combination that Google accepts for this specific OAuth client +
`<your-google-account>` account. The "copy URL from failed redirect" pattern
works because we don't need the browser to actually reach localhost — we just
need the `code` parameter from the redirect URL.
