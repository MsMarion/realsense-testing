# RealSense Hub

A pub/sub hub and live web dashboard for Intel RealSense cameras (tested with a **D435i** and a **D405**).
Every camera stream goes onto a ZeroMQ bus, and any process can subscribe to it or publish its own results back.

- Live color, depth, and IR tiles for every connected camera, with rates and latency
- Click-to-measure real 3D distances on the color or depth image
- 3D point cloud viewer (three.js), per camera or both fused into one frame
- IMU panel (D435i): orientation model, bubble level, accelerometer and gyro charts
- ChArUco board detection and camera-to-camera calibration
- Hot-plug: cameras that connect or reconnect are picked up automatically, with a USB 2 fallback mode

## Quick start

```bash
pip install -r requirements.txt
python demos/device_info.py      # check both cameras are found, on USB 3
python launch.py --open          # broker + one node per camera + board tracker + dashboard
```

The dashboard runs at http://localhost:8000.

## Architecture

```
camera_node (per camera) ─┐                        ┌─> dashboard/server.py ── WebSocket ──> browser
board_node               ─┼─ PUB ─> broker ── SUB ─┼─> board_node
your plugin (fx/*)       ─┘   (XSUB :5550 / XPUB :5551)  └─> your subscriber
```

Each message is `[topic, header_json, payload]`.

| Topic | Payload |
|---|---|
| `cam/<serial>/color` | JPEG |
| `cam/<serial>/depth` | zlib-compressed uint16 depth, aligned to color (header has scale and intrinsics) |
| `cam/<serial>/depth_vis` | colorized depth (JPEG) |
| `cam/<serial>/ir_left`, `ir_right` | JPEG |
| `cam/<serial>/points` | float16 XYZ + uint8 RGB, about 10 Hz |
| `cam/<serial>/imu` | header only: accel (m/s²) and gyro (rad/s), about 200 Hz |
| `cam/<serial>/info` | header only, 1 Hz: model, firmware, USB, fps, temperatures, exposure |
| `calib/<serial>/board`, `calib/pair` | ChArUco board pose per camera, and the relative pose between cameras |
| `fx/<name>/...` | your own outputs; JPEG topics show up as dashboard tiles automatically |

Subscribing from your own script:

```python
from rs_hub import bus
sub = bus.make_sub(["cam/"])
while True:
    topic, header, payload = bus.recv(sub)
```

## Calibration

1. Print the board with `python calib/make_board.py`, or use the link in the dashboard. Print at **100% / actual size**, then check the 100 mm line and one 30 mm square with a ruler. Mount the board perfectly flat.
2. If a square doesn't measure exactly 30 mm, put the measured size in `calib/board.json`.
3. Hold the board still where both cameras see it. Capture 10 or more samples from different angles, then click **Solve & save**. This writes `calib/extrinsics.json`, which is git-ignored because it contains your camera serials.

## Network and privacy

The broker listens on **localhost only**. To stream from another machine (for example a Raspberry Pi camera head), start it with `RS_HUB_BIND=0.0.0.0`. That exposes every camera feed to your network, so only do it on a network you trust.

Recordings, scans, and calibration files capture your space and your devices, so they are git-ignored by default.

## Layout

```
launch.py              start everything
rs_hub/                bus, broker, camera node, board detection + node, stream profiles
dashboard/             FastAPI server + static web UI
calib/                 board generator and board definition
demos/device_info.py   list devices, stream profiles, and options
```
