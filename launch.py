#!/usr/bin/env python3
"""Start the whole hub: broker, one camera node per connected RealSense, and the dashboard.

Cameras plugged in later are picked up automatically, and a node whose camera
drops off is restarted when the camera comes back.

By default the broker and dashboard listen on every network interface, so other
machines can open the dashboard and join the bus. Pass --local to keep everything
on this machine.

Usage: python launch.py [--port 8000] [--open] [--local | --bind ADDR]
"""
import argparse
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import pyrealsense2 as rs

ROOT = Path(__file__).parent
RESPAWN_DELAY = 5.0


def spawn(*args, env=None):
    return subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env)


def lan_addresses():
    """Best-effort list of this machine's IPv4 addresses (for printing URLs)."""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout
        return [a for a in out.split() if "." in a and not a.startswith("127.")]
    except (OSError, subprocess.TimeoutExpired):
        return [socket.gethostbyname(socket.gethostname())]


def connected_serials(ctx):
    try:
        return {d.get_info(rs.camera_info.serial_number) for d in ctx.query_devices()}
    except RuntimeError:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open the dashboard in your default browser")
    ap.add_argument("--bind", default=os.environ.get("RS_HUB_BIND", "0.0.0.0"),
                    help="address the broker and dashboard listen on (default: all interfaces)")
    ap.add_argument("--local", action="store_const", const="127.0.0.1", dest="bind",
                    help="listen on this machine only (same as --bind 127.0.0.1)")
    args = ap.parse_args()

    env = {**os.environ, "RS_HUB_BIND": args.bind}
    core = {"broker": spawn("-m", "rs_hub.broker", env=env)}
    time.sleep(0.5)
    core["dashboard"] = spawn("-m", "uvicorn", "dashboard.server:app", "--host", args.bind,
                              "--port", str(args.port), "--log-level", "warning")
    core["board"] = spawn("-m", "rs_hub.board_node")
    print(f"[launch] dashboard at http://localhost:{args.port}", flush=True)
    if args.bind != "127.0.0.1":
        for ip in lan_addresses() if args.bind == "0.0.0.0" else [args.bind]:
            print(f"[launch]   network: http://{ip}:{args.port}   bus: RS_HUB_HOST={ip}", flush=True)
        print("[launch]   no authentication: anyone who can reach this machine can view and publish",
              flush=True)
    if args.open:
        webbrowser.open(f"http://localhost:{args.port}")

    ctx = rs.context()
    nodes = {}   # serial -> (Popen, start_time)
    try:
        while True:
            serials = connected_serials(ctx)
            for s in serials:
                p, started = nodes.get(s, (None, 0))
                if (p is None or p.poll() is not None) and time.time() - started > RESPAWN_DELAY:
                    print(f"[launch] starting node for {s}", flush=True)
                    nodes[s] = (spawn("-m", "rs_hub.camera_node", "--serial", s), time.time())
            for s, (p, _) in list(nodes.items()):
                if s not in serials and p.poll() is not None:
                    print(f"[launch] camera {s} disconnected", flush=True)
                    del nodes[s]
            for name, p in core.items():
                if p.poll() is not None:
                    raise SystemExit(f"[launch] {name} exited ({p.returncode})")
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        procs = [p for p, _ in nodes.values()] + list(core.values())
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("[launch] stopped", flush=True)


if __name__ == "__main__":
    main()
