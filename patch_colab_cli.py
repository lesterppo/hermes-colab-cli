#!/usr/bin/env python3
"""Idempotently patch the installed google-colab-cli package with reliability
fixes discovered during Colab deployments:

1. execution.py — on 404/401 (expired runtime proxy token, or a wiped
   sessions.json), REFRESH the proxy token from the backend and reconnect
   instead of prune_session. Without this, a transient 401/404 kills the
   session registry, the keepalive dies, and the VM gets GC'd server-side —
   losing 40GB of downloaded models. (LTX-2.3 deployment, 2026-08)

2. client.py — Colab's /tun/m/assign and /tun/m/unassign endpoints started
   rejecting POSTs with an empty/missing body (HTTP 411 Length Required).
   Send a minimal `data="{}"` body on those POSTs. (found 2026-10-03,
   extended to unassign 2026-10-04)

Safe to re-run (checks for patch markers). Backups written to .bak.

Usage:  python3 patch_colab_cli.py
"""
import importlib.util
import os
import shutil
import sys

def find_package():
    spec = importlib.util.find_spec("colab_cli")
    if spec is None or not spec.submodule_search_locations:
        print("colab_cli package not found — install: pip install google-colab-cli")
        sys.exit(1)
    return next(iter(spec.submodule_search_locations))

PKG = find_package()
SHADOW = os.path.expanduser("~/colab_cli_patched")

HELPER = '''
def _refresh_session_token(name):
    """On 404/401: re-fetch runtime_proxy_info from the backend and reconnect."""
    from colab_cli.common import state
    try:
        assignments = state.client.list_assignments()
    except Exception:
        assignments = []
    s = state.store.get(name)
    if s is None:
        return None
    for a in assignments:
        if a.endpoint == s.endpoint:
            s.url = a.runtime_proxy_info.url
            s.token = a.runtime_proxy_info.token
            state.store.add(s)
            return ColabRuntime(s.url, s.token, kernel_id=s.kernel_id,
                                session_id=s.session_id)
    return None

'''

OLD = '''        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up."
            )
            state.prune_session(name)
            raise typer.Exit(1)
        raise e'''

NEW = '''        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears lost (404/401) - refreshing token and retrying..."
            )
            try:
                rt2 = _refresh_session_token(name)
            except Exception:
                rt2 = None
            if rt2 is not None:
                typer.echo("[colab] Token refreshed, reconnected.")
                runtime = rt2
            else:
                typer.echo(
                    f"[colab] Session '{name}' no longer assigned on server. Cleaning up."
                )
                state.prune_session(name)
                raise typer.Exit(1)
        raise e'''


def apply_patch(target, backup_suffix):
    src = open(target).read()
    if "def _refresh_session_token" in src:
        print(f"already patched: {target}")
        return "already"
    n = src.count(OLD)
    if n != 3:
        print(f"WARNING: expected 3 prune blocks, found {n}; aborting (version drift?)")
        return "drift"
    shutil.copy2(target, target + backup_suffix)
    src = src.replace("_console = Console()", HELPER + "_console = Console()", 1)
    src = src.replace(OLD, NEW)
    open(target, "w").write(src)
    print(f"patched {target} (backup: {target}{backup_suffix})")
    return "ok"


CLIENT = os.path.join(PKG, "client.py")
SHADOW_CLIENT = os.path.join(SHADOW, "client.py")
MARKER_411 = "HERMES-411-FIX"
# Methods whose empty-body POSTs Colab now 411s:
PATCH_METHODS_411 = ("def unassign(", "def _post_assignment(")


def _add_data_to_post_calls(body):
    """Insert data="{}" into POST _issue_request calls that lack a data kwarg.

    Returns (new_body, n_patched). Handles multi-line calls via paren depth.
    """
    lines = body.split("\n")
    out, i, n, found = [], 0, 0, 0
    while i < len(lines):
        line = lines[i]
        if "_issue_request(" in line:
            call = [line]
            depth = line.count("(") - line.count(")")
            j = i + 1
            while j < len(lines) and depth > 0:
                call.append(lines[j])
                depth += lines[j].count("(") - lines[j].count(")")
                j += 1
            if depth > 0:
                print("WARNING: unterminated _issue_request call; aborting 411 patch")
                return body, -1, found
            call_text = "\n".join(call)
            is_post = 'method="POST"' in call_text or "method='POST'" in call_text
            if is_post:
                found += 1
            if is_post and "data=" not in call_text:
                closing = call[-1]
                indent = closing[: len(closing) - len(closing.lstrip())]
                prev = list(call[:-1])
                if not prev[-1].rstrip().endswith(","):
                    prev[-1] = prev[-1] + ","
                out.extend(prev)
                out.append(f'{indent}    data="{{}}",  # {MARKER_411}: Colab 411s empty-body POSTs')
                out.append(closing)
                n += 1
            else:
                out.extend(call)
            i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out), n, found


def apply_411_patch(target):
    """Patch client.py so /tun/m/assign and /tun/m/unassign POSTs carry a
    minimal body (fixes HTTP 411). Idempotent via MARKER_411; aborts with
    'drift' if the expected methods are not found."""
    src = open(target).read()
    if MARKER_411 in src:
        print(f"already patched (411): {target}")
        return "already"
    new_src, total = src, 0
    for method_sig in PATCH_METHODS_411:
        start = new_src.find(method_sig)
        if start == -1:
            print(f"WARNING: {method_sig} not found in {target}; aborting (version drift?)")
            return "drift"
        # method body: up to the next same-indent def/class or EOF
        end = new_src.find("\n    def ", start + 1)
        end2 = new_src.find("\nclass ", start + 1)
        cut = min(e for e in (end, end2) if e != -1) if (end != -1 or end2 != -1) else -1
        body = new_src[start:cut] if cut != -1 else new_src[start:]
        patched_body, n, found = _add_data_to_post_calls(body)
        if n == -1:
            return "drift"
        if found == 0:
            print(f"WARNING: no POST _issue_request in {method_sig}; aborting (version drift?)")
            return "drift"
        if n == 0:
            print(f"note: {method_sig} POST already carries data= in {target}")
        total += n
        new_src = new_src[:start] + patched_body + (new_src[cut:] if cut != -1 else "")
    if total == 0:
        print(f"nothing to patch (411): {target}")
        return "already"
    shutil.copy2(target, target + ".bak")
    open(target, "w").write(new_src)
    print(f"patched (411) {target}: {total} POST call(s) (backup: {target}.bak)")
    return "ok"


def main():
    # Prefer patching in place (root installs on WSL/colab images); fall back
    # to a writable shadow copy at ~/colab_cli_patched when the package dir
    # is not writable.
    pkg_dir, shadow = PKG, False
    if not os.access(os.path.join(PKG, "client.py"), os.W_OK):
        import shutil as _sh
        if not os.path.isdir(SHADOW):
            _sh.copytree(PKG, SHADOW, symlinks=True,
                         ignore=_sh.ignore_patterns("__pycache__", "*.pyc"))
            print(f"created shadow copy at {SHADOW}")
        pkg_dir, shadow = SHADOW, True

    exec_target = os.path.join(pkg_dir, "commands", "execution.py")
    client_target = os.path.join(pkg_dir, "client.py")

    r1 = apply_patch(exec_target, ".bak")
    if r1 == "drift":
        sys.exit(1)
    r2 = apply_411_patch(client_target)
    if r2 == "drift":
        sys.exit(1)
    via = ("PYTHONPATH=$HOME/colab_cli_patched python3 -m colab_cli.cli"
           if shadow else "python3 -m colab_cli.cli")
    print(f"RUN_VIA: {via}")


if __name__ == "__main__":
    main()
