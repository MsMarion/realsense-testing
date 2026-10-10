"""ChArUco board definition, detection and pose math shared by the calibration tools.

Poses are 4x4 matrices. T_a_b maps points from frame b into frame a
(so T_cam_board takes board coordinates into camera coordinates).
"""
import json
from pathlib import Path

import cv2
import numpy as np

CALIB_DIR = Path(__file__).resolve().parent.parent / "calib"
BOARD_FILE = CALIB_DIR / "board.json"
EXTRINSICS_FILE = CALIB_DIR / "extrinsics.json"

DEFAULT_BOARD = dict(dictionary="DICT_4X4_50", squares_x=5, squares_y=7, square_mm=30.0, marker_mm=22.0)
MIN_CORNERS = 6


def load_board_config():
    cfg = dict(DEFAULT_BOARD)
    if BOARD_FILE.exists():
        cfg.update(json.loads(BOARD_FILE.read_text()))
    return cfg


def make_board(cfg):
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg["dictionary"]))
    return cv2.aruco.CharucoBoard((cfg["squares_x"], cfg["squares_y"]),
                                  cfg["square_mm"] / 1000, cfg["marker_mm"] / 1000, dictionary)


def normalize_pixels(px, intr):
    """Pixel coords -> undistorted normalized image coords, matching librealsense's models."""
    x = (px[:, 0] - intr["ppx"]) / intr["fx"]
    y = (px[:, 1] - intr["ppy"]) / intr["fy"]
    c = intr.get("coeffs") or [0] * 5
    model = intr.get("model", "none")
    if model == "inverse_brown_conrady":
        r2 = x * x + y * y
        f = 1 + c[0] * r2 + c[1] * r2 * r2 + c[4] * r2 * r2 * r2
        ux = x * f + 2 * c[2] * x * y + c[3] * (r2 + 2 * x * x)
        uy = y * f + 2 * c[3] * x * y + c[2] * (r2 + 2 * y * y)
        return np.stack([ux, uy], 1)
    if model == "brown_conrady" and any(c):
        K = np.array([[intr["fx"], 0, intr["ppx"]], [0, intr["fy"], intr["ppy"]], [0, 0, 1]])
        return cv2.undistortPoints(px.reshape(-1, 1, 2), K, np.array(c[:5])).reshape(-1, 2)
    return np.stack([x, y], 1)


def project(points_cam, intr):
    """3D camera-frame points -> pixel coords (pinhole, for drawing overlays)."""
    p = np.asarray(points_cam, float)
    return np.stack([p[:, 0] / p[:, 2] * intr["fx"] + intr["ppx"],
                     p[:, 1] / p[:, 2] * intr["fy"] + intr["ppy"]], 1)


def to_T(rvec, tvec):
    T = np.eye(4)
    T[:3, :3] = cv2.Rodrigues(rvec)[0]
    T[:3, 3] = np.ravel(tvec)
    return T


def invert(T):
    R, t = T[:3, :3], T[:3, 3]
    Ti = np.eye(4)
    Ti[:3, :3] = R.T
    Ti[:3, 3] = -R.T @ t
    return Ti


def average_poses(Ts):
    """Mean pose: mean translation, rotation = nearest rotation to the mean matrix."""
    Ts = np.asarray(Ts)
    U, _, Vt = np.linalg.svd(Ts[:, :3, :3].sum(0))
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1
        R = U @ Vt
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = Ts[:, :3, 3].mean(0)
    return T


def pose_spread(Ts, T_mean):
    """Per-sample translation (mm) and rotation (deg) deviation from T_mean."""
    dt, dr = [], []
    for T in Ts:
        dt.append(np.linalg.norm(T[:3, 3] - T_mean[:3, 3]) * 1000)
        R = T_mean[:3, :3].T @ T[:3, :3]
        dr.append(np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1))))
    return np.array(dt), np.array(dr)


def euler_deg(R):
    """Roll/pitch/yaw (x, y, z, degrees) for display."""
    return np.degrees([np.arctan2(R[2, 1], R[2, 2]),
                       np.arcsin(-np.clip(R[2, 0], -1, 1)),
                       np.arctan2(R[1, 0], R[0, 0])]).tolist()


class BoardDetector:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_board_config()
        self.board = make_board(self.cfg)
        self.detector = cv2.aruco.CharucoDetector(self.board)

    def detect(self, gray, intr):
        """Returns a dict with the board pose in camera coords, or {'found': False, ...}."""
        ch_corners, ch_ids, mk_corners, mk_ids = self.detector.detectBoard(gray)
        n_markers = 0 if mk_ids is None else len(mk_ids)
        if ch_ids is None or len(ch_ids) < MIN_CORNERS:
            return {"found": False, "markers": n_markers}
        obj, img = self.board.matchImagePoints(ch_corners, ch_ids)
        obj = obj.reshape(-1, 3)
        px = img.reshape(-1, 2).astype(np.float64)
        norm = normalize_pixels(px, intr)
        ok, rvec, tvec = cv2.solvePnP(obj, norm, np.eye(3), None, flags=cv2.SOLVEPNP_IPPE)
        if not ok:
            return {"found": False, "markers": n_markers}
        rvec, tvec = cv2.solvePnPRefineLM(obj, norm, np.eye(3), None, rvec, tvec)
        reproj, _ = cv2.projectPoints(obj, rvec, tvec, np.eye(3), None)
        err_px = float(np.sqrt(np.mean(np.sum((reproj.reshape(-1, 2) - norm) ** 2, 1))) * intr["fx"])
        T = to_T(rvec, tvec)

        # overlay geometry: detected corners + board axes, normalized 0..1 to the image size
        s = self.cfg["square_mm"] / 1000
        axes = np.array([[0, 0, 0], [2 * s, 0, 0], [0, 2 * s, 0], [0, 0, -2 * s]])
        axes_cam = (T[:3, :3] @ axes.T).T + T[:3, 3]
        wh = np.array([intr["w"], intr["h"]])
        return {
            "found": True, "markers": n_markers, "corners": len(px), "err_px": round(err_px, 3),
            "dist_m": round(float(np.linalg.norm(T[:3, 3])), 4), "T": T.ravel().tolist(),
            "px": (px / wh).round(4).tolist(), "axes": (project(axes_cam, intr) / wh).round(4).tolist(),
        }
