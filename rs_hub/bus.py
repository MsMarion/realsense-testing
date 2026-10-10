"""Tiny pub/sub layer over ZeroMQ.

Every message is a 3-part multipart frame: [topic, header_json, payload].
Publishers connect to the broker's XSUB side, subscribers to its XPUB side,
so any process can join or leave at any time.

Topic layout:
    cam/<serial>/color      JPEG                         (header: w, h)
    cam/<serial>/depth      zlib(uint16 depth, aligned)  (header: w, h, scale, intr)
    cam/<serial>/depth_vis  JPEG colorized depth
    cam/<serial>/ir_left    JPEG
    cam/<serial>/ir_right   JPEG
    cam/<serial>/points     float16 xyz (n*6 bytes) + uint8 rgb (n*3 bytes)  (header: n)
    cam/<serial>/imu        empty payload, header: accel [x,y,z] m/s^2, gyro [x,y,z] rad/s
    cam/<serial>/info       empty payload, header: model, firmware, fps, temps, ...
    fx/<name>/...           anything published by demo/plugin nodes
"""
import json
import os
import time
import zlib

import cv2
import numpy as np
import zmq

# Where the broker runs. On another machine, set RS_HUB_HOST=<jetson-ip> to join this hub.
HOST = os.environ.get("RS_HUB_HOST", "127.0.0.1")
PUB_PORT = 5550   # publishers connect here (broker XSUB)
SUB_PORT = 5551   # subscribers connect here (broker XPUB)
PUB_ADDR = f"tcp://{HOST}:{PUB_PORT}"
SUB_ADDR = f"tcp://{HOST}:{SUB_PORT}"


def make_pub(ctx=None):
    ctx = ctx or zmq.Context.instance()
    sock = ctx.socket(zmq.PUB)
    sock.setsockopt(zmq.SNDHWM, 20)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(PUB_ADDR)
    return sock


def make_sub(topics=("",), ctx=None, hwm=20):
    ctx = ctx or zmq.Context.instance()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.RCVHWM, hwm)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(SUB_ADDR)
    for t in topics:
        sock.setsockopt(zmq.SUBSCRIBE, t.encode())
    return sock


def publish(sock, topic, header=None, payload=b""):
    header = dict(header or {})
    header.setdefault("ts", time.time())
    try:
        sock.send_multipart([topic.encode(), json.dumps(header).encode(), payload], flags=zmq.NOBLOCK)
    except zmq.Again:
        pass  # nobody keeping up -> drop, never block the camera loop


def recv(sock, timeout_ms=None):
    """Returns (topic, header, payload) or None on timeout."""
    if timeout_ms is not None and not sock.poll(timeout_ms):
        return None
    topic, header, payload = sock.recv_multipart()
    return topic.decode(), json.loads(header), payload


# ---------- payload codecs ----------

def encode_jpeg(img, quality=80):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


def decode_jpeg(payload):
    return cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_UNCHANGED)


def encode_depth(depth_u16):
    return zlib.compress(np.ascontiguousarray(depth_u16).tobytes(), 1)


def decode_depth(payload, header):
    """Returns depth in raw units (multiply by header['scale'] for meters)."""
    return np.frombuffer(zlib.decompress(payload), np.uint16).reshape(header["h"], header["w"])


def encode_points(xyz, rgb):
    return xyz.astype(np.float16).tobytes() + rgb.astype(np.uint8).tobytes()


def decode_points(payload, header):
    n = header["n"]
    xyz = np.frombuffer(payload[: n * 6], np.float16).reshape(n, 3).astype(np.float32)
    rgb = np.frombuffer(payload[n * 6:], np.uint8).reshape(n, 3)
    return xyz, rgb
