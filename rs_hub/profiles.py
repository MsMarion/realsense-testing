"""Per-model stream settings.

Each model has resolution tiers (richest first); the node tries every tier with
every stream set until one starts. Later tiers exist for USB 2 connections,
which can't carry 848x480@30 depth + color + IR.
"""

USB2_TIERS = [
    dict(color=(640, 480, 15), depth=(640, 480, 15), ir=(640, 480, 15)),
    dict(color=(424, 240, 15), depth=(480, 270, 15), ir=(480, 270, 15)),
]

PROFILES = {
    "D435I": dict(
        tiers=[dict(color=(640, 480, 30), depth=(848, 480, 30), ir=(848, 480, 30))] + USB2_TIERS,
        imu=True, max_range=4.0,
    ),
    "D435": dict(
        tiers=[dict(color=(640, 480, 30), depth=(848, 480, 30), ir=(848, 480, 30))] + USB2_TIERS,
        imu=False, max_range=4.0,
    ),
    "D405": dict(
        tiers=[dict(color=(848, 480, 30), depth=(848, 480, 30), ir=(848, 480, 30))] + USB2_TIERS,
        imu=False, max_range=0.8,
    ),
}
DEFAULT = dict(
    tiers=[dict(color=(640, 480, 30), depth=(640, 480, 30), ir=(640, 480, 30))] + USB2_TIERS,
    imu=False, max_range=4.0,
)

# Stream sets tried in order within each tier.
STREAM_SETS = [
    ("color", "depth", "ir_left", "ir_right"),
    ("color", "depth", "ir_left"),
    ("color", "depth"),
]


def profile_for(name):
    name = name.upper()
    for key in sorted(PROFILES, key=len, reverse=True):
        if key in name:
            return key, PROFILES[key]
    return "UNKNOWN", DEFAULT
