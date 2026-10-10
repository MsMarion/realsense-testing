"""Watches every camera's color stream for the ChArUco board.

Publishes:
    calib/<serial>/board   board pose in that camera (header only; see BoardDetector.detect)
    calib/pair             when two cameras see the board at the same moment:
                           T = pose of `other` camera in the `ref` camera's frame (T_ref_other)

Usage: python -m rs_hub.board_node
"""
import time

import cv2
import numpy as np

from rs_hub import bus
from rs_hub.board import BoardDetector, invert

DETECT_HZ = 10
PAIR_MAX_DT = 0.12  # cameras aren't hardware-synced: board must be still, detections this close in time


def main():
    sub = bus.make_sub(["cam/"], hwm=10)
    pub = bus.make_pub()
    det = BoardDetector()
    print(f"[board] tracking {det.cfg}", flush=True)
    info, last_run, latest = {}, {}, {}
    while True:
        # drain the queue, keeping only the newest color frame per camera
        frames = {}
        msg = bus.recv(sub, 500)
        while msg:
            topic, hdr, payload = msg
            _, serial, kind = topic.split("/")
            if kind == "info":
                info[serial] = hdr
            elif kind == "color":
                frames[serial] = (hdr, payload)
            msg = bus.recv(sub, 0)

        now = time.time()
        for serial, (hdr, payload) in frames.items():
            if serial not in info or now - last_run.get(serial, 0) < 1 / DETECT_HZ:
                continue
            last_run[serial] = now
            gray = cv2.cvtColor(bus.decode_jpeg(payload), cv2.COLOR_BGR2GRAY)
            res = det.detect(gray, info[serial]["intr"])
            res["grab_ts"] = hdr["grab_ts"]
            latest[serial] = res
            bus.publish(pub, f"calib/{serial}/board", res)
            if res["found"]:
                publish_pair(pub, serial, res, latest, info)


def publish_pair(pub, serial, res, latest, info):
    for other, o in latest.items():
        if other == serial or not o["found"] or abs(o["grab_ts"] - res["grab_ts"]) > PAIR_MAX_DT:
            continue
        # reference = the D435-family camera (the fixed one), else the lower serial
        a, b = sorted([serial, other], key=lambda s: ("435" not in info[s].get("model", ""), s))
        Ta = np.array(latest[a]["T"]).reshape(4, 4)
        Tb = np.array(latest[b]["T"]).reshape(4, 4)
        T_ref_other = Ta @ invert(Tb)
        bus.publish(pub, "calib/pair", {
            "ref": a, "other": b, "T": T_ref_other.ravel().tolist(),
            "err_px": [latest[a]["err_px"], latest[b]["err_px"]],
            "corners": [latest[a]["corners"], latest[b]["corners"]],
            "dt": abs(o["grab_ts"] - res["grab_ts"]),
        })


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
