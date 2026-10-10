"""Generate the printable ChArUco calibration board.

Writes calib/board.json (the board definition every tool reads) and
calib/charuco_board.html — open it in a browser and print at 100% / "Actual size".
Then measure one black square with a ruler; if it isn't exactly square_mm,
put the measured value in board.json (and scale marker_mm by the same ratio).

Usage: python calib/make_board.py [--squares-x 5 --squares-y 7 --square-mm 30 --marker-mm 22]
"""
import argparse
import base64
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rs_hub.board import BOARD_FILE, CALIB_DIR, load_board_config, make_board  # noqa: E402

PX_PER_MM = 20


def main():
    current = load_board_config()  # start from the saved board so a measured square_mm survives
    ap = argparse.ArgumentParser()
    ap.add_argument("--squares-x", type=int, default=current["squares_x"])
    ap.add_argument("--squares-y", type=int, default=current["squares_y"])
    ap.add_argument("--square-mm", type=float, default=current["square_mm"])
    ap.add_argument("--marker-mm", type=float, default=current["marker_mm"])
    ap.add_argument("--dictionary", default=current["dictionary"])
    args = ap.parse_args()

    cfg = dict(dictionary=args.dictionary, squares_x=args.squares_x, squares_y=args.squares_y,
               square_mm=args.square_mm, marker_mm=args.marker_mm)
    w_mm, h_mm = cfg["squares_x"] * cfg["square_mm"], cfg["squares_y"] * cfg["square_mm"]
    img = make_board(cfg).generateImage((round(w_mm * PX_PER_MM), round(h_mm * PX_PER_MM)), marginSize=0)
    png = cv2.imencode(".png", img)[1].tobytes()

    CALIB_DIR.mkdir(exist_ok=True)
    BOARD_FILE.write_text(json.dumps(cfg, indent=2))
    (CALIB_DIR / "charuco_board.png").write_bytes(png)
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>ChArUco board</title>
<style>
  @page {{ size: auto; margin: 10mm; }}
  body {{ margin: 0; font: 11px system-ui, sans-serif; color: #000; background: #fff; }}
  .sheet {{ display: flex; flex-direction: column; align-items: center; gap: 6mm; padding-top: 4mm; }}
  img {{ width: {w_mm}mm; height: {h_mm}mm; image-rendering: pixelated; outline: 8mm solid #fff; }}
  .ruler {{ width: 100mm; height: 3mm; border: 0.3mm solid #000; border-top: 0; }}
  p {{ margin: 0; text-align: center; }}
  @media screen {{ body {{ background: #ddd; }} .sheet {{ background: #fff; width: fit-content; margin: 10mm auto; padding: 10mm; }} }}
</style></head>
<body><div class="sheet">
  <img src="data:image/png;base64,{base64.b64encode(png).decode()}" alt="ChArUco board">
  <div class="ruler"></div>
  <p><b>The line above should measure exactly 100 mm.</b> Print at 100% / "Actual size" (not "Fit to page").</p>
  <p>{cfg['squares_x']}×{cfg['squares_y']} squares · square {cfg['square_mm']} mm · marker {cfg['marker_mm']} mm · {cfg['dictionary']}</p>
  <p>Mount it perfectly flat (clipboard, foam board, glass). A curled board ruins calibration.</p>
</div></body></html>"""
    (CALIB_DIR / "charuco_board.html").write_text(html, encoding="utf-8")
    print(f"wrote {BOARD_FILE}\nwrote {CALIB_DIR / 'charuco_board.html'}  ({w_mm:.0f} x {h_mm:.0f} mm)")


if __name__ == "__main__":
    main()
