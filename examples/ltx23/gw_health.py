import subprocess, os, time
def sh(cmd, t=40):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return str(e)

gw = sh("ps aux | grep '[l]tx_gateway' | wc -l").strip()
port = sh("ss -tln 2>/dev/null | grep -c ':7860 '").strip()
cport = sh("ss -tln 2>/dev/null | grep -c ':8188 '").strip()
print("gateway_procs:", gw, "port7860:", port, "port8188:", cport, flush=True)
if gw == "0" or port == "0" or cport == "0":
    print("GATEWAY_DOWN relaunching...", flush=True)
    sh("pkill -f '[l]tx_gateway.py' || true", 20)
    sh("pkill -f '[m]ain.py --listen' || true", 20)
    time.sleep(3)
    r = subprocess.run("bash /content/ltx23/launch.sh", shell=True,
                       capture_output=True, text=True, timeout=90)
    print("relaunch rc:", r.returncode, flush=True)
    time.sleep(60)
    gw2 = sh("ps aux | grep '[l]tx_gateway' | wc -l").strip()
    port2 = sh("ss -tln 2>/dev/null | grep -c ':7860 '").strip()
    cport2 = sh("ss -tln 2>/dev/null | grep -c ':8188 '").strip()
    print("after_relaunch gw:", gw2, "port:", port2, "comfy:", cport2, flush=True)
    if gw2 != "0" and port2 != "0" and cport2 != "0":
        print("GATEWAY_RECOVERED", flush=True)
    else:
        print("GATEWAY_RECOVER_FAILED", flush=True)
else:
    print("GATEWAY_OK", flush=True)
