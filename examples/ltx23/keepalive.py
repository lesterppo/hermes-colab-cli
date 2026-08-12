#!/usr/bin/env python3
"""Keep the ltx23 Colab session alive AND refresh its proxy token.

Colab session proxy tokens expire after ~1h; the colab-cli does NOT
auto-refresh the stored token, so long sessions die unless something
re-syncs the assignment. This script:
  1. Refreshes ~/.config/colab-cli/sessions.json with fresh url+token from
     the backend's list_assignments() (if the endpoint is still assigned).
  2. Pings the session kernel with a trivial exec (idle watchdog).
Prints nothing on success (silent watchdog); alerts when the session is gone.
"""
import subprocess
import sys
import os
import json

ENV = dict(os.environ)
ENV["PYTHONPATH"] = ""

SESSIONS = os.path.expanduser("~/.config/colab-cli/sessions.json")
PING = "/tmp/ltx23_ping.py"


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, env=ENV, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def refresh_token():
    """Re-fetch runtime_proxy_info from the backend and update the store."""
    code = (
        "import sys; sys.path.insert(0, '/usr/local/lib/python3.12/dist-packages');\n"
        "from colab_cli.common import state\n"
        "import json, os\n"
        "assignments = state.client.list_assignments()\n"
        "store_path = os.path.expanduser('~/.config/colab-cli/sessions.json')\n"
        "store = json.load(open(store_path)) if os.path.exists(store_path) else {}\n"
        "changed = False\n"
        "for a in assignments:\n"
        "    for name, s in list(store.items()):\n"
        "        if s.get('endpoint') == a.endpoint:\n"
        "            s['url'] = a.runtime_proxy_info.url\n"
        "            s['token'] = a.runtime_proxy_info.token\n"
        "            changed = True\n"
        "if changed:\n"
        "    json.dump(store, open(store_path, 'w'), indent=2)\n"
        "    print('TOKEN_REFRESHED')\n"
        "else:\n"
        "    print('NO_MATCH')\n"
    )
    with open("/tmp/ltx23_refresh.py", "w") as f:
        f.write(code)
    rc, out = run(["/usr/bin/python3", "/tmp/ltx23_refresh.py"], timeout=90)
    return rc, out


def main():
    rc, out = refresh_token()
    if rc != 0 or "TOKEN_REFRESHED" not in out:
        # try the CLI path as fallback
        rc2, out2 = run(["/usr/bin/python3", "-m", "colab_cli.cli", "sessions"])
        if "ltx23" not in out2:
            print(f"LTX23 KEEPALIVE: session gone. refresh rc={rc} {out[-400:]}")
            print("Redeploy: cd ~/ltx23_colab && colab run --gpu T4 --keep -s ltx23 --timeout 1500 create_vm.py")
            sys.exit(1)
        print("LTX23 KEEPALIVE: token refresh via CLI fallback")
        sys.exit(0)
    # ping kernel (keeps free-tier idle timer at bay). During an active
    # generation the kernel is busy and the exec queues — treat that as OK
    # (a busy kernel is obviously not idle).
    with open(PING, "w") as f:
        f.write("print('ping')\n")
    rc, out = run(["/usr/bin/python3", "-m", "colab_cli.cli", "exec",
                   "-s", "ltx23", "-f", PING, "--timeout", "60"])
    if rc != 0:
        if "timed out" in out:
            print("LTX23 KEEPALIVE: kernel busy (generating), skipping")
            sys.exit(0)
        print(f"LTX23 KEEPALIVE: exec ping failed rc={rc} {out[-500:]}")
        sys.exit(1)
    # self-heal the gateway: if the gateway/gradio process died on the VM,
    # relaunch it (setup3's launch.sh). Gateway used to die ~6min after
    # startup (daemon-thread bug) — keepalive catches any future deaths.
    with open("/tmp/ltx23_gw_health.py", "w") as f:
        f.write(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "gw_health.py")).read())
    rc, out = run(["/usr/bin/python3", "-m", "colab_cli.cli", "exec",
                   "-s", "ltx23", "-f", "/tmp/ltx23_gw_health.py", "--timeout", "180"])
    if rc != 0:
        print(f"LTX23 KEEPALIVE: gw health failed rc={rc} {out[-400:]}")
    elif "GATEWAY_DOWN" in out:
        print("LTX23 KEEPALIVE: gateway was down, relaunched")
    # silent on success


if __name__ == "__main__":
    main()
