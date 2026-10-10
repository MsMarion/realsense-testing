"""One process per camera: grabs every stream and publishes it on the bus.

Usage: python -m rs_hub.camera_node --serial <serial>
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import pyrealsense2 as rs

from rs_hub import bus
from rs_hub.profiles import STREAM_SETS, USB2_TIERS, profile_for

POINTS_HZ = 10
POINT_STEP = 4  # subsample every Nth pixel for the point cloud


def cam_info(dev, key):
    return dev.get_info(key) if dev.supports(key) else ""


def find_device(serial):
    for d in rs.context().query_devices():
        if d.get_info(rs.camera_info.serial_number) == serial:
            return d
    sys.exit(f"[node] device {serial} not found")


class CameraNode:
    def __init__(self, serial):
        self.serial = serial
        self.dev = find_device(serial)
        self.name = cam_info(self.dev, rs.camera_info.name)
        self.model, self.prof = profile_for(self.name)
        self.prefix = f"cam/{serial}/"
        self.log(f"{self.name} ({self.model})")

        self.pub = bus.make_pub()
        self.pool = ThreadPoolExecutor(4)
        self.align = rs.align(rs.stream.color)
        self.temporal = rs.temporal_filter()
        self.colorizer = rs.colorizer()
        self.depth_sensor = self.dev.first_depth_sensor()
        self.depth_scale = self.depth_sensor.get_depth_scale()

        self.pipe = None
        self.imu_pipe = None
        self.streams = ()
        self.fps = 0.0
        self.imu_count = 0
        self.imu_hz = 0.0
        self.accel = [0.0, 0.0, 0.0]

    def log(self, msg):
        print(f"[node {self.serial}] {msg}", flush=True)

    # ---------- startup ----------

    def start_video(self):
        tiers, stream_sets = self.prof["tiers"], STREAM_SETS
        if cam_info(self.dev, rs.camera_info.usb_type_descriptor).startswith("2"):
            self.log("USB 2 connection: using reduced modes, color + depth only (use a USB 3 port for everything)")
            tiers, stream_sets = USB2_TIERS, [("color", "depth")]
        for tier in tiers:
            for streams in stream_sets:
                cfg = rs.config()
                cfg.enable_device(self.serial)
                w, h, f = tier["color"]
                cfg.enable_stream(rs.stream.color, w, h, rs.format.bgr8, f)
                w, h, f = tier["depth"]
                cfg.enable_stream(rs.stream.depth, w, h, rs.format.z16, f)
                w, h, f = tier["ir"]
                if "ir_left" in streams:
                    cfg.enable_stream(rs.stream.infrared, 1, w, h, rs.format.y8, f)
                if "ir_right" in streams:
                    cfg.enable_stream(rs.stream.infrared, 2, w, h, rs.format.y8, f)
                pipe = rs.pipeline()
                try:
                    profile = pipe.start(cfg)
                    pipe.wait_for_frames(5000)  # a start can "succeed" yet never deliver frames
                except RuntimeError as e:
                    self.log(f"{tier['depth']} {streams} failed: {e}")
                    try:
                        pipe.stop()
                    except RuntimeError:
                        pass
                    continue
                self.pipe, self.streams, self.tier = pipe, streams, tier
                i = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
                self.intr = dict(fx=i.fx, fy=i.fy, ppx=i.ppx, ppy=i.ppy, w=i.width, h=i.height,
                                 model=str(i.model).split(".")[-1], coeffs=list(i.coeffs))
                self.log(f"streaming {', '.join(streams)} (depth {tier['depth']}, color {tier['color']})")
                return
        sys.exit(f"[node {self.serial}] could not start any stream set")

    def start_imu(self):
        if not self.prof["imu"]:
            return
        cfg = rs.config()
        cfg.enable_device(self.serial)
        cfg.enable_stream(rs.stream.accel)
        cfg.enable_stream(rs.stream.gyro)
        self.imu_pub = bus.make_pub()  # used only from the librealsense callback thread
        pipe = rs.pipeline()
        try:
            pipe.start(cfg, self.on_imu)
            self.imu_pipe = pipe
            self.log("IMU streaming")
        except RuntimeError as e:
            self.log(f"IMU failed: {e}")

    def on_imu(self, frame):
        frames = frame.as_frameset() if frame.is_frameset() else [frame]
        for f in frames:
            m = f.as_motion_frame()
            d = m.get_motion_data()
            v = [d.x, d.y, d.z]
            if m.get_profile().stream_type() == rs.stream.accel:
                self.accel = v
            else:
                self.imu_count += 1
                bus.publish(self.imu_pub, self.prefix + "imu",
                            {"accel": self.accel, "gyro": v, "cam_ts": m.get_timestamp()})

    # ---------- per-frame work (runs in the thread pool) ----------

    @staticmethod
    def jpeg(img, quality=80):
        return bus.encode_jpeg(img, quality), {}

    def depth_raw(self, depth):
        return bus.encode_depth(depth), {"scale": self.depth_scale, "intr": self.intr}

    def depth_vis(self, vis_rgb):
        return bus.encode_jpeg(cv2.cvtColor(vis_rgb, cv2.COLOR_RGB2BGR)), {}

    def points(self, depth, color):
        s = POINT_STEP
        z = depth[::s, ::s].astype(np.float32) * self.depth_scale
        h, w = z.shape
        u = (np.arange(w) * s - self.intr["ppx"]) / self.intr["fx"]
        v = (np.arange(h) * s - self.intr["ppy"]) / self.intr["fy"]
        mask = (z > 0) & (z < self.prof["max_range"])
        x = (z * u[None, :])[mask]
        y = (z * v[:, None])[mask]
        xyz = np.stack([x, y, z[mask]], axis=1)
        rgb = color[::s, ::s][mask][:, ::-1]  # BGR -> RGB
        return bus.encode_points(xyz, rgb), {"n": len(xyz)}

    # ---------- info ----------

    def info_header(self, color, depth):
        h, w = depth.shape
        c = depth[h // 2 - 5:h // 2 + 5, w // 2 - 5:w // 2 + 5]
        c = c[c > 0]
        hdr = dict(
            serial=self.serial, name=self.name, model=self.model,
            firmware=cam_info(self.dev, rs.camera_info.firmware_version),
            usb=cam_info(self.dev, rs.camera_info.usb_type_descriptor),
            depth_scale=self.depth_scale, intr=self.intr, streams=list(self.streams),
            depth_profile=list(self.tier["depth"]), color_profile=list(self.tier["color"]),
            fps=round(self.fps, 1), imu=self.imu_pipe is not None, imu_hz=round(self.imu_hz, 1),
            center_m=float(np.median(c)) * self.depth_scale if c.size else None,
            max_range=self.prof["max_range"],
        )
        for key, opt in (("asic_temp", rs.option.asic_temperature),
                         ("projector_temp", rs.option.projector_temperature),
                         ("laser_power", rs.option.laser_power),
                         ("emitter", rs.option.emitter_enabled)):
            if self.depth_sensor.supports(opt):
                try:
                    hdr[key] = self.depth_sensor.get_option(opt)
                except RuntimeError:
                    pass
        for key, md in (("exposure", rs.frame_metadata_value.actual_exposure),
                        ("gain", rs.frame_metadata_value.gain_level)):
            if color.supports_frame_metadata(md):
                hdr[key] = color.get_frame_metadata(md)
        return hdr

    # ---------- main loop ----------

    def run(self):
        self.start_video()
        self.start_imu()
        last_points = last_info = time.time()
        frames_since = 0
        imu_since = 0
        try:
            while True:
                frames = self.pipe.wait_for_frames(5000)
                ir_l = frames.get_infrared_frame(1) if "ir_left" in self.streams else None
                ir_r = frames.get_infrared_frame(2) if "ir_right" in self.streams else None
                aligned = self.align.process(frames)
                color = aligned.get_color_frame()
                depth_f = aligned.get_depth_frame()
                if not color or not depth_f:
                    continue
                depth_f = self.temporal.process(depth_f)

                color_img = np.asanyarray(color.get_data())
                depth_img = np.asanyarray(depth_f.get_data())
                vis = np.asanyarray(self.colorizer.colorize(depth_f).get_data())

                jobs = {
                    "color": self.pool.submit(self.jpeg, color_img),
                    "depth_vis": self.pool.submit(self.depth_vis, vis),
                    "depth": self.pool.submit(self.depth_raw, depth_img),
                }
                if ir_l:
                    jobs["ir_left"] = self.pool.submit(self.jpeg, np.asanyarray(ir_l.get_data()), 70)
                if ir_r:
                    jobs["ir_right"] = self.pool.submit(self.jpeg, np.asanyarray(ir_r.get_data()), 70)
                now = time.time()
                if now - last_points >= 1 / POINTS_HZ:
                    last_points = now
                    jobs["points"] = self.pool.submit(self.points, depth_img, color_img)

                h, w = color_img.shape[:2]
                base = {"frame": color.get_frame_number(), "cam_ts": color.get_timestamp(),
                        "grab_ts": now, "w": w, "h": h}
                for name, fut in jobs.items():
                    payload, extra = fut.result()
                    bus.publish(self.pub, self.prefix + name, {**base, **extra}, payload)

                frames_since += 1
                if now - last_info >= 1.0:
                    dt = now - last_info
                    self.fps = frames_since / dt
                    self.imu_hz = (self.imu_count - imu_since) / dt
                    imu_since = self.imu_count
                    frames_since = 0
                    last_info = now
                    bus.publish(self.pub, self.prefix + "info", self.info_header(color, depth_img))
        except RuntimeError as e:
            self.log(f"camera error, exiting: {e}")
            sys.exit(2)
        finally:
            for p in (self.pipe, self.imu_pipe):
                if p:
                    try:
                        p.stop()
                    except RuntimeError:
                        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True)
    args = ap.parse_args()
    try:
        CameraNode(args.serial).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
