import cv2
import numpy as np
from pathlib import Path

for name in ['emulator-5564', 'emulator-5554']:
    frame = cv2.imread(f'screenshots/{name}.png')
    h, w = frame.shape[:2]
    print(f"\n=== {name} ({w}x{h}) ===")

    # Scan bottom 120px row by row
    for y in range(h - 120, h, 10):
        colored = []
        for x in range(0, w, 15):
            bgr = frame[y, x]
            b, g, r = int(bgr[0]), int(bgr[1]), int(bgr[2])
            if (b > 200 and g > 200 and r > 200) or (b < 60 and g < 60 and r < 60):
                continue
            hsv = cv2.cvtColor(np.array([[[b, g, r]]], dtype=np.uint8), cv2.COLOR_BGR2HSV)[0][0]
            colored.append(f"  ({x},{y}) BGR({b},{g},{r}) HSV({hsv[0]},{hsv[1]},{hsv[2]})")
        for c in colored:
            print(c)

    # Scan the whole screen for non-white, non-black colored blobs
    hsv_full = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    # Warm colors: hue 0-30, saturation > 50
    warm = cv2.inRange(hsv_full, np.array([0, 50, 100]), np.array([30, 255, 255]))
    ys, xs = np.where(warm > 0)
    if len(xs) > 0:
        print(f"\n  Warm colored total: {len(xs)} px")
        print(f"  X: {np.min(xs)}-{np.max(xs)}, Y: {np.min(ys)}-{np.max(ys)}")

    # Pink/magenta: hue 140-180
    pink = cv2.inRange(hsv_full, np.array([140, 50, 100]), np.array([180, 255, 255]))
    pys, pxs = np.where(pink > 0)
    if len(pxs) > 0:
        print(f"  Pink/magenta total: {len(pxs)} px")
        print(f"  X: {np.min(pxs)}-{np.max(pxs)}, Y: {np.min(pys)}-{np.max(pys)}")
