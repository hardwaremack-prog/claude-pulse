"""Claude Pulse launcher for Windows (the desktop / Start-menu icon runs this).

Starts the background server if it isn't running, then opens the dashboard
in an Edge app window (or your default browser).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

APP = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(APP, "server.py")
CONFIG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ClaudePulse", "config.json")


def port():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return int(json.load(f).get("port", 8787))
    except Exception:
        return 8787


def running(p):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/health", timeout=1.5) as r:
            return b"ok" in r.read()
    except Exception:
        return False


def main():
    p = port()
    if not running(p):
        exe = sys.executable
        if exe.lower().endswith("python.exe"):  # prefer the no-console interpreter
            alt = exe[:-10] + "pythonw.exe"
            exe = alt if os.path.exists(alt) else exe
        subprocess.Popen([exe, SERVER], creationflags=0x00000008 | 0x00000200, close_fds=True)
        for _ in range(30):
            time.sleep(0.5)
            if running(p):
                break
    url = f"http://localhost:{p}"
    for edge in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(edge):
            subprocess.Popen([edge, f"--app={url}", "--window-size=1400,950"])
            return
    webbrowser.open(url)


if __name__ == "__main__":
    main()
