#!/usr/bin/env python3
"""Idempotently patch the installed google-colab-cli package with reliability
fixes discovered during the LTX-2.3 Colab deployment (2026-08):

1. execution.py — on 404/401 (expired runtime proxy token, or a wiped
   sessions.json), REFRESH the proxy token from the backend and reconnect
   instead of prune_session. Without this, a transient 401/404 kills the
   session registry, the keepalive dies, and the VM gets GC'd server-side —
   losing 40GB of downloaded models.

Safe to re-run (checks for the patch marker). Backup written to .bak.

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
EXEC = os.path.join(PKG, "commands", "execution.py")
SHADOW = os.path.expanduser("~/colab_cli_patched")
SHADOW_EXEC = os.path.join(SHADOW, "commands", "execution.py")

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


def main():
    # 1) Prefer patching in place (root installs on WSL/colab images).
    try:
        if os.path.exists(EXEC):
            res = apply_patch(EXEC, ".bak")
            if res == "ok":
                print("RUN_VIA: python3 -m colab_cli.cli")
                return
            if res == "already":
                return
            # drift: fall through to shadow attempt? no — report and exit
            sys.exit(1)
    except PermissionError:
        pass

    # 2) Root-owned package + no sudo: build a writable shadow copy at
    #    ~/colab_cli_patched and patch THAT. Use with
    #    PYTHONPATH=$HOME/colab_cli_patched (the colabctl wrapper does this
    #    automatically when the shadow exists).
    import shutil as _sh
    if not os.path.isdir(SHADOW):
        _sh.copytree(PKG, SHADOW, symlinks=True,
                     ignore=_sh.ignore_patterns("__pycache__", "*.pyc"))
        print(f"created shadow copy at {SHADOW}")
    if os.path.exists(SHADOW_EXEC):
        res = apply_patch(SHADOW_EXEC, ".bak")
        if res == "ok":
            print("RUN_VIA: PYTHONPATH=$HOME/colab_cli_patched python3 -m colab_cli.cli")
        elif res == "already":
            print("RUN_VIA: PYTHONPATH=$HOME/colab_cli_patched python3 -m colab_cli.cli")
        else:
            sys.exit(1)
    else:
        print(f"shadow execution.py missing at {SHADOW_EXEC}")
        sys.exit(1)


if __name__ == "__main__":
    main()
