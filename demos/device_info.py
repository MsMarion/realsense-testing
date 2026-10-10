#!/usr/bin/env python3
"""Enumerate connected RealSense devices: model, firmware, USB, sensors, stream profiles, options.

Usage: python demos/device_info.py [--profiles] [--options]
"""
import argparse

import pyrealsense2 as rs


def info(dev, key):
    return dev.get_info(key) if dev.supports(key) else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", action="store_true", help="list every supported stream profile")
    ap.add_argument("--options", action="store_true", help="list every sensor option with range")
    args = ap.parse_args()

    ctx = rs.context()
    devices = list(ctx.query_devices())
    if not devices:
        print("No RealSense devices found.")
        return

    for dev in devices:
        print("=" * 70)
        print(f"{info(dev, rs.camera_info.name)}")
        print(f"  serial:    {info(dev, rs.camera_info.serial_number)}")
        print(f"  firmware:  {info(dev, rs.camera_info.firmware_version)}"
              f"  (recommended: {info(dev, rs.camera_info.recommended_firmware_version)})")
        print(f"  USB:       {info(dev, rs.camera_info.usb_type_descriptor)}")
        print(f"  product:   {info(dev, rs.camera_info.product_line)} / id {info(dev, rs.camera_info.product_id)}")
        for s in dev.query_sensors():
            print(f"  - sensor: {info(s, rs.camera_info.name)}")
            streams = sorted({p.stream_name() for p in s.get_stream_profiles()})
            print(f"      streams: {', '.join(streams)}")
            if args.profiles:
                for p in s.get_stream_profiles():
                    if p.is_video_stream_profile():
                        v = p.as_video_stream_profile()
                        print(f"        {p.stream_name():10s} {v.width()}x{v.height()} @{p.fps()} {p.format()}")
                    else:
                        print(f"        {p.stream_name():10s} @{p.fps()} {p.format()}")
            if args.options:
                for opt in s.get_supported_options():
                    try:
                        r = s.get_option_range(opt)
                        print(f"        {str(opt):40s} = {s.get_option(opt):g}  [{r.min:g}..{r.max:g} step {r.step:g}]")
                    except RuntimeError:
                        pass


if __name__ == "__main__":
    main()
