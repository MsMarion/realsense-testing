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
./setup.sh                   # create .venv, install pinned deps, list cameras
./run.sh check --together    # verify every camera delivers good data
./run.sh launch --open       # broker + one node per camera + board tracker + dashboard
```

`run.sh` runs scripts with the venv's Python, so you don't need to activate anything. Its shortcuts are `check`, `launch`, `info` (device list) and `board` (print the calibration board), and it also accepts any other script path. The scripts are also executable directly (`./launch.py`) once you've run `source .venv/bin/activate`.

The dashboard runs at http://localhost:8000.

## Setup

Requires Python ≥ 3.8 and the cameras on **USB 3** ports. Tested on a Jetson (L4T R36.4 / JetPack 6, aarch64, Python 3.10) with a D435i and a D405, firmware 5.15.1.55.

`setup.sh` creates `.venv/` with the system Python and installs `pyrealsense2` (librealsense) from PyPI, so you don't need to build the SDK.

| Command | What it does |
|---|---|
| `./setup.sh` | Installs the exact versions in `requirements.lock` (the versions verified on this hardware) |
| `./setup.sh --latest` | Installs the newest versions allowed by `requirements.txt` |
| `./setup.sh --recreate` | Deletes `.venv/` and starts over |
| `PYTHON=python3.11 ./setup.sh` | Uses a specific interpreter |

If an upgrade with `--latest` passes `tests/check_cameras.py`, refresh the lock file with `.venv/bin/pip freeze > requirements.lock` and keep the two header comment lines.

Run everything inside the venv (`source .venv/bin/activate`), or call `.venv/bin/python` directly.

## Testing the cameras

`tests/check_cameras.py` streams each camera with the hub's own stream profiles (from `rs_hub/profiles.py`) and checks that the data is correct, not just that the camera shows up.

```bash
python tests/check_cameras.py                   # each camera on its own, any scene
python tests/check_cameras.py --together        # all cameras at once, as the hub runs them
python tests/check_cameras.py --serial 263122074692 --seconds 10
```

| Check | Passes when |
|---|---|
| `usb` | Connected at USB 3.x. USB 2 gives a WARN because only reduced modes are available. |
| `start streams` | The richest stream set the hub would use actually delivers frames |
| `<stream> rate` | Measured fps (from device timestamps) is ≥ 90% of requested. WARN if > 1% of frames dropped. |
| `<stream> content` | The image is not frozen (FAIL). It is also not black or saturated (WARN: check the lens and lighting). |
| `depth centre` | Info only: median distance, fill rate, and frame-to-frame noise in the central 40% of the image |
| `accel` / `gyro` | IMU models only: \|accel\| = 9.81 ± 0.5 m/s² and \|gyro\| < 0.05 rad/s, with the camera at rest |

The script exits with code 1 if any check FAILs, so you can use it in scripts.

### Depth accuracy (flat-wall test)

To check that the depth values are accurate, not just present:

1. Point the camera squarely at a flat, matte, textured wall. Plain white walls are harder for the D405, which has no IR projector.
2. Measure the distance from the camera's front glass to the wall with a tape measure.
3. Pass that distance in metres:

```bash
python tests/check_cameras.py --distance 1.00                                     # same for all cameras
python tests/check_cameras.py --distance 263122074692=1.0 --distance 260522271385=0.3   # per camera
```

| Check | Passes when |
|---|---|
| `wall fill` | ≥ 90% of the central pixels have depth |
| `wall distance` | Median depth is within ±2% of the true distance (Intel's spec within the ideal range) |
| `wall flatness` | The plane-fit RMS is ≤ 2% of the distance |
| `wall angle` | WARN if the camera is more than 5° off perpendicular. The distance check assumes it faces the wall squarely. |

Use a distance inside each camera's good range:

| Camera | Minimum Z | Good test distance | Depth unit |
|---|---|---|---|
| D435i | ~0.28 m | 0.5 – 2 m | 1 mm |
| D405 | ~0.07 m | 0.1 – 0.5 m | 0.1 mm |

Note that depth is measured from the camera's internal reference plane, not the front glass, so expect a few mm of offset at close range.

If the distance or flatness check fails at a good range, run the On-Chip Self-Calibration in Intel's `realsense-viewer`, or update the firmware.

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

`launch.py` listens on **all network interfaces** by default, so other machines can open the dashboard and join the bus. On startup it prints the address for each interface.

| Port | Service | From another machine |
|---|---|---|
| 8000 | Dashboard (HTTP + WebSocket) | `http://<jetson-ip>:8000` |
| 5551 | Bus, subscribe (ZeroMQ XPUB) | `RS_HUB_HOST=<jetson-ip> python your_script.py` using `bus.make_sub(...)` |
| 5550 | Bus, publish (ZeroMQ XSUB) | the same, using `bus.make_pub()`; `fx/...` topics show up on the dashboard |

On the other machine, copy `rs_hub/bus.py` (it needs `pyzmq`, `numpy` and `opencv-python-headless`), or clone this repo, and set `RS_HUB_HOST`:

```python
# RS_HUB_HOST=192.168.2.38 python remote.py
from rs_hub import bus
sub = bus.make_sub(["cam/"])
while True:
    topic, header, payload = bus.recv(sub)
```

| Option | Effect |
|---|---|
| `./run.sh launch --local` | This machine only (`127.0.0.1`) |
| `./run.sh launch --bind 192.168.2.38` | One interface only, for example the LAN but not Docker bridges or a VPN |
| `RS_HUB_BIND=...` | Sets the default for `--bind` |

Running `python -m rs_hub.broker` on its own still defaults to `127.0.0.1`.

**There is no authentication or encryption.** Anyone who can reach these ports can watch every camera feed and depth stream, and can publish onto the bus. The dashboard can also trigger calibration saves. Only run open on networks you trust. Use `--bind` to keep the hub off interfaces you don't need, such as a VPN (`tun0`), or firewall the ports. Each camera at full rate sends roughly 10–20 MB/s to every subscriber, so use wired Ethernet or 5 GHz Wi-Fi for remote viewers.

Recordings, scans, and calibration files capture your space and your devices, so they are git-ignored by default.

## Troubleshooting

**`python3` is 3.6 / `No matching distribution for pyrealsense2`.** pyenv's global Python is too old. `setup.sh` uses `/usr/bin/python3` instead, and `.python-version` (`system`) does the same for pyenv inside this folder.

**D435i: `IMU failed: Couldn't resolve requests`, or the check prints `imu WARN`.** The pip wheel reads the IMU through the kernel's HID sensor (IIO) drivers, and the stock Jetson kernel doesn't include them (`/sys/bus/iio/devices` is empty). Depth, color, and IR are unaffected. To get the IMU, do one of these:
- build librealsense from source with `-DFORCE_RSUSB_BACKEND=ON -DBUILD_PYTHON_BINDINGS=ON`, then use that `pyrealsense2` in the venv, or
- build and load the `hid-sensor-*` kernel modules for your L4T release.

**No devices found / permission denied.** Your user needs access to the camera's `/dev/video*` and USB nodes. You can get it through the `video` and `plugdev` groups, or by installing Intel's udev rules ([`99-realsense-libusb.rules`](https://github.com/IntelRealSense/librealsense/blob/master/config/99-realsense-libusb.rules) in `/etc/udev/rules.d/`, then `sudo udevadm control --reload && sudo udevadm trigger`). Replug the camera afterwards.

**USB 2 reported, low fps, or drops with `--together`.** Use a USB 3 port and a short USB 3 cable. `lsusb -t` should show `5000M` for each camera. Both cameras share the same root hub, so a few dropped frames when they run together is normal; failing rates are not.

**Low depth fill.** Common causes: the target is closer than the minimum Z, the surface is shiny or black, or there is direct sunlight. On the D405, a textureless surface also gives low fill.

## Layout

```
setup.sh               create .venv and install dependencies
requirements.txt       dependency list (loose); requirements.lock: verified exact versions
launch.py              start everything
rs_hub/                bus, broker, camera node, board detection + node, stream profiles
dashboard/             FastAPI server + static web UI
calib/                 board generator and board definition
demos/device_info.py   list devices, stream profiles, and options
tests/check_cameras.py stream each camera and check rates, image content, depth accuracy, IMU
```
