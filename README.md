# Hermes Colab CLI

AI-agent-native CLI for Google Colab. Wraps the official
[google-colab-cli](https://github.com/googlecolab/google-colab-cli) with
token-efficient output for AI agent consumption.

31 CLI commands, 24 Hermes tools. Provision GPU/TPU VMs, execute Python code,
run shell commands, manage files, export notebooks, deploy LLMs, and tunnel
APIs — all from the terminal.

## Quick Start

```bash
# Install
git clone https://github.com/lesterppo/hermes-colab-cli.git
cd hermes-colab-cli
./install.sh

# One-time auth (see references/auth_flow.md)
# 1. Open auth URL in browser
# 2. Sign in, approve scopes
# 3. Copy the redirect URL from the address bar
# 4. Agent extracts the code and exchanges for token

# Create a T4 GPU session
python3 colab.py new -s my-gpu --gpu T4 -o /tmp/session.json

# Execute code
echo "print('Hello from Colab GPU!')" | python3 colab.py exec -s my-gpu -o /tmp/out.json

# Stop session
python3 colab.py stop -s my-gpu
```

## Token-Efficient Output

Every command supports `-o FILE` — writes full output to file, returns a
compact JSON pointer (~60 chars) on stdout for agents:

```json
{"ok":true,"f":"/tmp/output.txt","s":1234,"b":2}
```

## Commands

| Category | Commands |
|---|---|
| Session | new, sessions, status, stop, restart, gpu_switch |
| Execution | exec, exec_bg, exec_bg_poll, run, repl, console |
| Files | ls, upload, download, rm, edit |
| Automation | install, log, notebook, url, tunnel, drivemount, auth |
| Info | pay, version, update, whoami |
| Browser | secrets, resources, share |

## Deploy an LLM

See `SKILL.md` → deploy-llm-to-colab for the complete workflow.
Example deploys Ornith 1.0 9B to a T4 GPU with public API endpoint in ~10 minutes.

## Credits

- [google-colab-cli](https://github.com/googlecolab/google-colab-cli) — official Colab CLI by Google
- Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent)

## License

MIT
