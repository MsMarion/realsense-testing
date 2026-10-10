#!/usr/bin/env python3
"""Hardware check: stream every connected RealSense with the hub's own profiles and
verify the data is sane: frame rate, dropped frames, frozen or black images, depth
fill and noise, IMU gravity, and (optionally) depth accuracy against a flat wall.

Usage:
  python tests/check_cameras.py                       # health check, any scene
  python tests/check_cameras.py --distance 0.50       # accuracy: flat wall 0.50 m away
  python tests/check_cameras.py --distance 263122074692=1.0 --distance 260522271385=0.3
  python tests/check_cameras.py --serial 263122074692 --seconds 10
  python tests/check_cameras.py --together            # all cameras at once, like the hub

Exits 1 if any check FAILs, so it can gate scripts. WARN means "probably the scene or
setup, have a look"; FAIL means the camera is not delivering correct data.
"""
import argparse
import select
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyrealsense2 as rs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rs_hub.profiles import STREAM_SETS, USB2_TIERS, profile_for  # noqa: E402

WARMUP_S = 2.0        # let auto-exposure settle before measuring
ROI_FRAC = 0.4        # central fraction of the depth image used for depth stats
MIN_FPS_RATIO = 0.9   # measured / requested
MAX_DROP_RATIO = 0.01
MIN_FILL = 0.90       # valid depth pixels in the ROI (flat-wall test)
MAX_DEPTH_ERR = 0.02  # |median - true| / true   (Intel spec: <=2% within ideal range)
MAX_PLANE_RMS = 0.02  # plane-fit RMS / distance (Intel spec: <=2%)
GRAVITY = 9.81

STREAM_KEYS = {
    "color": (rs.stream.color, 0), "depth": (rs.stream.depth, 0),
    "ir_left": (rs.stream.infrared, 1), "ir_right": (rs.stream.infrared, 2),
}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, cam, status, check, detail):
        self.rows.append((cam, status, check, detail))
        print(f"  [{status:4s}] {check:22s} {detail}", flush=True)

    @property
    def failed(self):
        return any(r[1] == "FAIL" for r in self.rows)


def info(dev, key):
    return dev.get_info(key) if dev.supports(key) else ""


def start_pipeline(serial, prof, usb2):
    """Same tier / stream-set fallback as rs_hub.camera_node. Returns (pipe, profile, tier, streams)."""
    tiers, stream_sets = (USB2_TIERS, [("color", "depth")]) if usb2 else (prof["tiers"], STREAM_SETS)
    for tier in tiers:
        for streams in stream_sets:
            cfg = rs.config()
            cfg.enable_device(serial)
            cfg.enable_stream(rs.stream.color, *tier["color"][:2], rs.format.bgr8, tier["color"][2])
            cfg.enable_stream(rs.stream.depth, *tier["depth"][:2], rs.format.z16, tier["depth"][2])
            w, h, f = tier["ir"]
            if "ir_left" in streams:
                cfg.enable_stream(rs.stream.infrared, 1, w, h, rs.format.y8, f)
            if "ir_right" in streams:
                cfg.enable_stream(rs.stream.infrared, 2, w, h, rs.format.y8, f)
            pipe = rs.pipeline()
            try:
                profile = pipe.start(cfg)
                pipe.wait_for_frames(5000)
                return pipe, profile, tier, streams
            except RuntimeError:
                try:
                    pipe.stop()
                except RuntimeError:
                    pass
    return None, None, None, None


def plane_fit_rms(z, intr, roi):
    """Fit a plane to the ROI's valid 3D points; return (rms_m, tilt_deg from the optical axis)."""
    (y0, y1), (x0, x1) = roi
    v, u = np.nonzero(z > 0)
    if len(u) < 500:
        return None, None
    zz = z[v, u]
    u, v = u + x0, v + y0
    pts = np.column_stack([(u - intr.ppx) / intr.fx * zz, (v - intr.ppy) / intr.fy * zz, zz])
    centered = pts - pts.mean(axis=0)
    _, _, vt = np.linalg.svd(centered[:: max(1, len(pts) // 20000)], full_matrices=False)
    normal = vt[2]
    rms = float(np.sqrt(np.mean((centered @ normal) ** 2)))
    tilt = float(np.degrees(np.arccos(min(1.0, abs(normal[2])))))
    return rms, tilt


def check_video(dev, serial, prof, args, truth, rep):
    usb = info(dev, rs.camera_info.usb_type_descriptor)
    usb2 = usb.startswith("2")
    pipe, profile, tier, streams = start_pipeline(serial, prof, usb2)
    if pipe is None:
        rep.add(serial, "FAIL", "start streams", "no stream set could be started")
        return
    rep.add(serial, "PASS", "start streams",
            f"{', '.join(streams)}  depth {tier['depth']}  color {tier['color']}")

    depth_sensor = dev.first_depth_sensor()
    scale = depth_sensor.get_depth_scale()
    d_intr = profile.get_stream(rs.stream.depth).as_video_stream_profile().get_intrinsics()

    if args.barrier:
        # --together: wait until every camera is streaming, since starting one camera
        # briefly stalls the others; keep draining frames meanwhile.
        print("READY", flush=True)
        while not select.select([sys.stdin], [], [], 0)[0]:
            pipe.poll_for_frames()
            time.sleep(0.005)
        sys.stdin.readline()
    t_end = time.time() + WARMUP_S
    while time.time() < t_end:
        pipe.wait_for_frames(5000)

    numbers = defaultdict(list)    # stream -> frame numbers
    stamps = defaultdict(list)     # stream -> device timestamps (ms)
    means = defaultdict(list)      # stream -> mean pixel value per frame
    depth_stack = []
    h, w = d_intr.height, d_intr.width
    y0, y1 = int(h * (1 - ROI_FRAC) / 2), int(h * (1 + ROI_FRAC) / 2)
    x0, x1 = int(w * (1 - ROI_FRAC) / 2), int(w * (1 + ROI_FRAC) / 2)
    timeouts = 0
    t_end = time.time() + args.seconds
    try:
        while time.time() < t_end:
            try:
                fs = pipe.wait_for_frames(2000)
            except RuntimeError:
                timeouts += 1
                continue
            for key in streams:
                st, idx = STREAM_KEYS[key]
                f = fs.first_or_default(st) if idx == 0 else fs.get_infrared_frame(idx)
                if not f:
                    continue
                numbers[key].append(f.get_frame_number())
                stamps[key].append(f.get_timestamp())
                img = np.asanyarray(f.get_data())
                if key == "depth":
                    if len(depth_stack) < 60:
                        depth_stack.append(img[y0:y1, x0:x1].astype(np.float32) * scale)
                    means[key].append(float((img > 0).mean()))
                else:
                    means[key].append(float(img[::8, ::8].mean()))
    finally:
        pipe.stop()

    if timeouts:
        rep.add(serial, "FAIL", "frame timeouts", f"{timeouts} waits of 2 s got no frames")

    # Rate, drops, and image content per stream.
    for key in streams:
        target = tier["ir" if key.startswith("ir") else key][2]
        n, ts = numbers[key], stamps[key]
        if len(n) < 2:
            rep.add(serial, "FAIL", f"{key} frames", f"only {len(n)} frames received")
            continue
        span = (ts[-1] - ts[0]) / 1000.0
        fps = (len(ts) - 1) / span if span > 0 else 0.0
        status = "PASS" if fps >= target * MIN_FPS_RATIO else "FAIL"
        gaps = np.diff(n)
        dropped = int(np.sum(gaps[gaps > 1] - 1))
        drop_ratio = dropped / (n[-1] - n[0]) if n[-1] > n[0] else 0.0
        detail = f"{fps:5.1f} fps (target {target}), {dropped} dropped ({drop_ratio:.1%})"
        if status == "PASS" and drop_ratio > MAX_DROP_RATIO:
            status = "WARN"
        rep.add(serial, status, f"{key} rate", detail)

        m = np.array(means[key])
        if key == "depth":
            continue
        if m.mean() < 5:
            rep.add(serial, "WARN", f"{key} content", f"image nearly black (mean {m.mean():.0f}): covered lens or dark room?")
        elif m.mean() > 250:
            rep.add(serial, "WARN", f"{key} content", f"image saturated (mean {m.mean():.0f})")
        elif m.std() == 0 and len(m) > 10:
            rep.add(serial, "FAIL", f"{key} content", "every frame identical: stream frozen")
        else:
            rep.add(serial, "PASS", f"{key} content", f"mean level {m.mean():.0f}/255")

    # Depth quality over the central ROI.
    if not depth_stack:
        return
    stack = np.stack(depth_stack)
    valid = stack > 0
    fill = float(valid.mean())
    last = stack[-1]
    if not valid[-1].any():
        rep.add(serial, "FAIL", "depth fill", "no valid depth in the image centre")
        return
    median = float(np.median(last[last > 0]))
    both = valid.all(axis=0)
    noise = float(np.median(stack[:, both].std(axis=0))) if both.sum() > 100 else float("nan")
    rep.add(serial, "INFO", "depth scale", f"{scale * 1000:g} mm per unit, max range {prof['max_range']} m")
    rep.add(serial, "INFO", "depth centre", f"median {median:.3f} m, fill {fill:.0%}, "
            f"temporal noise {noise * 1000:.2f} mm ({noise / median:.2%})")
    if not 0.05 < median < 20:
        rep.add(serial, "FAIL", "depth range", f"median {median:.3f} m is implausible: depth scale wrong?")

    if truth is None:
        return
    rep.add(serial, "PASS" if fill >= MIN_FILL else "FAIL", "wall fill",
            f"{fill:.1%} valid (need {MIN_FILL:.0%})")
    err = (median - truth) / truth
    rep.add(serial, "PASS" if abs(err) <= MAX_DEPTH_ERR else "FAIL", "wall distance",
            f"measured {median:.4f} m vs true {truth:.4f} m: {err * 100:+.2f}% (limit ±{MAX_DEPTH_ERR:.0%})")
    rms, tilt = plane_fit_rms(last, d_intr, ((y0, y1), (x0, x1)))
    if rms is None:
        rep.add(serial, "FAIL", "wall flatness", "too few valid points to fit a plane")
    else:
        rel = rms / truth
        rep.add(serial, "PASS" if rel <= MAX_PLANE_RMS else "FAIL", "wall flatness",
                f"plane-fit RMS {rms * 1000:.2f} mm ({rel:.2%}, limit {MAX_PLANE_RMS:.0%})")
        if tilt > 5:
            rep.add(serial, "WARN", "wall angle",
                    f"camera is {tilt:.1f}° off perpendicular: distance check assumes it faces the wall squarely")


def check_imu(dev, serial, prof, args, rep):
    has_motion = any(s.is_motion_sensor() for s in dev.query_sensors())
    if not prof["imu"] and not has_motion:
        return
    if not has_motion:
        rep.add(serial, "WARN", "imu", "model has an IMU but librealsense can't see it "
                "(Jetson kernel has no hid-sensor drivers; see README > Troubleshooting)")
        return
    cfg = rs.config()
    cfg.enable_device(serial)
    cfg.enable_stream(rs.stream.accel)
    cfg.enable_stream(rs.stream.gyro)
    samples = {"accel": [], "gyro": []}

    def on_frame(frame):
        m = frame.as_motion_frame()
        d = m.get_motion_data()
        key = "accel" if m.get_profile().stream_type() == rs.stream.accel else "gyro"
        samples[key].append((m.get_timestamp(), d.x, d.y, d.z))

    pipe = rs.pipeline()
    try:
        pipe.start(cfg, on_frame)
    except RuntimeError as e:
        rep.add(serial, "FAIL", "imu start", str(e))
        return
    time.sleep(min(args.seconds, 3.0) + 0.5)
    pipe.stop()
    for key in samples:
        s = np.array(samples[key][20:])  # skip start-up samples
        if len(s) < 10:
            rep.add(serial, "FAIL", f"{key} rate", f"only {len(s)} samples")
            return
        hz = (len(s) - 1) / ((s[-1, 0] - s[0, 0]) / 1000.0)
        rep.add(serial, "PASS", f"{key} rate", f"{hz:.0f} Hz")
    a = np.linalg.norm(np.array(samples["accel"][20:])[:, 1:], axis=1).mean()
    g = np.linalg.norm(np.array(samples["gyro"][20:])[:, 1:], axis=1).mean()
    rep.add(serial, "PASS" if abs(a - GRAVITY) < 0.5 else "FAIL", "accel gravity",
            f"|a| = {a:.2f} m/s² (expect {GRAVITY} ±0.5 at rest)")
    rep.add(serial, "PASS" if g < 0.05 else "WARN", "gyro at rest",
            f"|ω| = {g:.4f} rad/s (expect < 0.05 when still)")


def parse_distances(values):
    default, per = None, {}
    for v in values or []:
        if "=" in v:
            s, d = v.split("=", 1)
            per[s] = float(d)
        else:
            default = float(v)
    return default, per


def run_together(devices, args):
    """One child process per camera, all streaming at once; print each output in turn."""
    serials = [info(d, rs.camera_info.serial_number) for d in devices]
    serials = [s for s in serials if not args.serial or s in args.serial]
    passthrough = [a for a in sys.argv[1:] if a not in ("--together",)]
    passthrough = [a for i, a in enumerate(passthrough)
                   if a != "--serial" and (i == 0 or passthrough[i - 1] != "--serial")]
    children = [subprocess.Popen([sys.executable, __file__, "--serial", s, "--barrier", *passthrough],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True)
                for s in serials]
    outputs = []
    for p in children:  # collect output up to READY (or exit, if the camera failed to start)
        lines = []
        for line in p.stdout:
            if line.strip() == "READY":
                break
            lines.append(line)
        outputs.append(lines)
    for p in children:
        if p.poll() is None:
            p.stdin.write("go\n")
            p.stdin.flush()
    code = 0
    for p, lines in zip(children, outputs):
        out, _ = p.communicate()
        print("".join(lines[1:]) + out, end="")  # drop each child's version line
        code |= p.returncode
    print("\nall cameras streamed concurrently: " + ("OK" if code == 0 else "FAILURES above"))
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", action="append", help="only check this camera (repeatable)")
    ap.add_argument("--seconds", type=float, default=5.0, help="measurement time per camera")
    ap.add_argument("--distance", action="append", metavar="[SERIAL=]METRES",
                    help="true distance to a flat wall, enables the accuracy checks")
    ap.add_argument("--together", action="store_true",
                    help="stream all cameras at the same time (tests shared USB bandwidth)")
    ap.add_argument("--barrier", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    default_d, per_d = parse_distances(args.distance)

    rep = Report()
    print(f"librealsense {rs.__version__}")
    devices = list(rs.context().query_devices())
    if not devices:
        print("FAIL: no RealSense devices found (cable, USB port, permissions? see README)")
        sys.exit(1)
    if args.together:
        sys.exit(run_together(devices, args))
    for dev in devices:
        serial = info(dev, rs.camera_info.serial_number)
        if args.serial and serial not in args.serial:
            continue
        name = info(dev, rs.camera_info.name)
        model, prof = profile_for(name)
        print(f"\n{name}  serial {serial}  fw {info(dev, rs.camera_info.firmware_version)}")
        usb = info(dev, rs.camera_info.usb_type_descriptor)
        rep.add(serial, "PASS" if usb.startswith("3") else "WARN", "usb",
                f"USB {usb}" + ("" if usb.startswith("3") else ": reduced modes only, use a USB 3 port/cable"))
        if model == "UNKNOWN":
            rep.add(serial, "WARN", "profile", "model not in rs_hub/profiles.py, using defaults")
        check_video(dev, serial, prof, args, per_d.get(serial, default_d), rep)
        check_imu(dev, serial, prof, args, rep)

    if args.serial and not rep.rows:
        print(f"FAIL: none of {args.serial} is connected")
        sys.exit(1)
    counts = defaultdict(int)
    for r in rep.rows:
        counts[r[1]] += 1
    print("\n" + "  ".join(f"{k}: {counts[k]}" for k in ("PASS", "WARN", "FAIL") if counts[k]))
    sys.exit(1 if rep.failed else 0)


if __name__ == "__main__":
    main()
