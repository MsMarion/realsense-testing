"""Dashboard: subscribes to the whole bus and fans it out to browsers over a WebSocket.

Run: python -m uvicorn dashboard.server:app --port 8000
"""
import asyncio
import collections
import json
import struct
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from rs_hub import bus
from rs_hub.board import CALIB_DIR, EXTRINSICS_FILE, average_poses, euler_deg, load_board_config, pose_spread

STATIC = Path(__file__).parent / "static"


class Hub:
    """Keeps the latest message per topic; browsers pull whatever is newest (slow clients just skip frames)."""

    def __init__(self):
        self.latest = {}                 # topic -> (seq, header, payload)
        self.depth = {}                  # serial -> (header, payload)  raw depth, used for measuring
        self.imu = collections.deque(maxlen=4000)  # (idx, serial, header)
        self.imu_idx = 0
        self.seq = 0
        self.lock = threading.Lock()
        self.counters = {}               # topic -> [count, bytes, latency_sum]
        self.rates = {}                  # topic -> {hz, kbps, lat_ms}
        self.rates_seq = 0

    def run(self):
        sock = bus.make_sub(hwm=200)
        last_stats = time.time()
        while True:
            msg = bus.recv(sock, 500)
            now = time.time()
            if msg:
                topic, hdr, payload = msg
                c = self.counters.setdefault(topic, [0, 0, 0.0])
                c[0] += 1
                c[1] += len(payload)
                c[2] += now - hdr.get("grab_ts", hdr.get("ts", now))
                parts = topic.split("/")
                kind = parts[-1]
                if parts[0] == "cam" and kind == "imu":
                    with self.lock:
                        self.imu_idx += 1
                        self.imu.append((self.imu_idx, parts[1], hdr))
                elif parts[0] == "cam" and kind == "depth":
                    self.depth[parts[1]] = (hdr, payload)
                else:
                    self.seq += 1
                    self.latest[topic] = (self.seq, hdr, payload)
            if now - last_stats >= 1.0:
                dt = now - last_stats
                self.rates = {t: {"hz": round(c[0] / dt, 1), "kbps": round(c[1] / dt / 1024),
                                  "lat_ms": round(c[2] / c[0] * 1000, 1) if c[0] else None}
                              for t, c in self.counters.items()}
                self.counters = {t: [0, 0, 0.0] for t in self.counters}
                self.rates_seq += 1
                last_stats = now

    def imu_since(self, idx):
        with self.lock:
            return self.imu_idx, [(s, h) for i, s, h in self.imu if i > idx]


hub = Hub()


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=hub.run, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/probe")
def probe(req: dict):
    """req: {serial, points: [[x, y], ...]} with x, y normalized 0..1 on the color image.
    Returns the 3D point (meters, camera frame) under each pixel and the distance between the first two."""
    entry = hub.depth.get(req.get("serial"))
    if not entry:
        return {"error": "no depth for that camera"}
    hdr, payload = entry
    depth = bus.decode_depth(payload, hdr)
    intr, scale = hdr["intr"], hdr["scale"]
    h, w = depth.shape
    out = []
    for x, y in req.get("points", []):
        u = min(max(int(x * w), 0), w - 1)
        v = min(max(int(y * h), 0), h - 1)
        win = depth[max(v - 2, 0):v + 3, max(u - 2, 0):u + 3]
        win = win[win > 0]
        if not win.size:
            out.append(None)
            continue
        z = float(np.median(win)) * scale
        out.append([(u - intr["ppx"]) / intr["fx"] * z, (v - intr["ppy"]) / intr["fy"] * z, z])
    res = {"points": out}
    if len(out) >= 2 and out[0] and out[1]:
        res["distance"] = float(np.linalg.norm(np.subtract(out[0], out[1])))
    return res


# ---------------------------------------------------------------- camera-to-camera calibration

calib_samples = []   # list of (ref, other, T_ref_other)


def load_extrinsics():
    return json.loads(EXTRINSICS_FILE.read_text()) if EXTRINSICS_FILE.exists() else None


@app.get("/calib/board")
def calib_board():
    page = CALIB_DIR / "charuco_board.html"
    if not page.exists():  # generated file, not checked in
        subprocess.run([sys.executable, str(CALIB_DIR / "make_board.py")], check=True)
    return FileResponse(page)


@app.get("/api/calib/state")
def calib_state():
    return {"samples": len(calib_samples), "saved": load_extrinsics(), "board": load_board_config()}


@app.post("/api/calib/capture")
def calib_capture():
    entry = hub.latest.get("calib/pair")
    if not entry or time.time() - entry[1]["ts"] > 0.5:
        return {"error": "both cameras must see the board right now", "samples": len(calib_samples)}
    hdr = entry[1]
    if calib_samples and calib_samples[0][:2] != (hdr["ref"], hdr["other"]):
        calib_samples.clear()
    calib_samples.append((hdr["ref"], hdr["other"], np.array(hdr["T"]).reshape(4, 4)))
    return {"samples": len(calib_samples)}


@app.post("/api/calib/reset")
def calib_reset():
    calib_samples.clear()
    return {"samples": 0}


@app.post("/api/calib/solve")
def calib_solve():
    if len(calib_samples) < 3:
        return {"error": "capture at least 3 samples (10+ from different board positions is better)"}
    Ts = [s[2] for s in calib_samples]
    T = average_poses(Ts)
    dt, dr = pose_spread(Ts, T)
    result = {
        "ref": calib_samples[0][0], "other": calib_samples[0][1],
        "T_ref_other": T.ravel().tolist(),
        "translation_mm": (T[:3, 3] * 1000).round(2).tolist(),
        "rotation_deg": np.round(euler_deg(T[:3, :3]), 3).tolist(),
        "samples": len(Ts),
        "spread_mm": {"mean": round(float(dt.mean()), 2), "max": round(float(dt.max()), 2)},
        "spread_deg": {"mean": round(float(dr.mean()), 3), "max": round(float(dr.max()), 3)},
        "board": load_board_config(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    EXTRINSICS_FILE.write_text(json.dumps(result, indent=2))
    return result


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    sent = {}
    imu_idx = hub.imu_idx
    rates_seq = 0
    state = {"points": None}

    async def reader():
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "points":
                state["points"] = msg.get("serial")

    rt = asyncio.create_task(reader())
    try:
        while True:
            for topic, (seq, hdr, payload) in list(hub.latest.items()):
                if sent.get(topic) == seq:
                    continue
                sent[topic] = seq
                if topic.endswith("/points") and state["points"] != "all" and f"/{state['points']}/" not in topic:
                    continue
                if payload:
                    h = json.dumps({"topic": topic, **hdr}).encode()
                    await ws.send_bytes(struct.pack("<I", len(h)) + h + payload)
                else:
                    await ws.send_text(json.dumps({"type": "msg", "topic": topic, "header": hdr}))

            imu_idx, samples = hub.imu_since(imu_idx)
            if samples:
                await ws.send_text(json.dumps({"type": "imu", "samples": [
                    {"serial": s, "accel": h["accel"], "gyro": h["gyro"], "cam_ts": h["cam_ts"]} for s, h in samples]}))

            if hub.rates_seq != rates_seq:
                rates_seq = hub.rates_seq
                await ws.send_text(json.dumps({"type": "stats", "rates": hub.rates}))

            await asyncio.sleep(0.01)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        rt.cancel()
