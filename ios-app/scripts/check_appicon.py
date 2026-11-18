"""Verify the rendered AppIcon PNGs deterministically.

Two things are proven here, both by measurement (not by looking):

1. PIXELS — each 1024 master is the geometry it claims. Coordinates come from
   assets-master/*.svg (1024 canvas): the operator's block letter H is three
   flat rectangles — left bar x 254–346, right bar x 676–768, both y 241–745;
   crossbar x 346–676, y 447–539. The field is sampled at the corners. The
   tinted master is monochrome by contract (white H, no hue).
2. WIRING — actool (the asset compiler Xcode uses) compiles the catalog,
   leaves no unassigned child, and emits CFBundleIconName == AppIcon.
   This is the check that matters: build_check.sh is swiftc-only and would
   happily pass while the operator's home screen still showed "H".

Usage: python3 scripts/check_appicon.py
Exit:  0 all green, 1 a failure.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SET = ROOT / "Assets.xcassets" / "AppIcon.appiconset"

# The app is dark-only, so the default and dark appearances are the same art:
# pure black field with the #FF5722 block H. Tinted is the monochrome variant
# iOS recolours with the user's chosen tint.
BLACK_ORANGE = {"field": (0x00, 0x00, 0x00),
                "left": (0xFF, 0x57, 0x22),
                "right": (0xFF, 0x57, 0x22),
                "cross": (0xFF, 0x57, 0x22)}
TINTED = {"field": (0x00, 0x00, 0x00),
          "left": (0xFF, 0xFF, 0xFF),
          "right": (0xFF, 0xFF, 0xFF),
          "cross": (0xFF, 0xFF, 0xFF)}

# Interior points of the three H strokes + a background point.
PTS = {"field": (32, 1000), "left": (300, 400), "right": (720, 600),
       "cross": (512, 493)}

# Measured spans of the H at row 400 (bar-to-bar) and column 512 (crossbar).
ROW_Y = 400
ROW_SPAN = (254, 767)          # x 254–768 exclusive
COL_X = 512
COL_SPAN = (447, 538)          # y 447–539 exclusive (rasterise rounds the last row)

# filename -> expected tones (default entry is the shared black+H master)
FILES = {"AppIcon.png": BLACK_ORANGE,
         "AppIcon-dark.png": BLACK_ORANGE,
         "AppIcon-tinted.png": TINTED}


def close(a, b, tol=10):
    return all(abs(x - y) <= tol for x, y in zip(a[:3], b[:3]))


def check_pixels() -> bool:
    ok = True
    for name, expect in FILES.items():
        path = SET / name
        if not path.exists():
            print(f"FAIL {name}: missing")
            ok = False
            continue
        im = Image.open(path).convert("RGB")
        if im.size != (1024, 1024):
            print(f"FAIL {name}: size {im.size}, expected (1024,1024)")
            ok = False
            continue
        file_ok = True
        for key, (x, y) in PTS.items():
            got = im.getpixel((x, y))
            if not close(got, expect[key]):
                print(f"FAIL {name}.{key} at {(x, y)}: got {got}, want {expect[key]}")
                file_ok = False
        for cx, cy in ((4, 4), (1020, 4), (4, 1020), (1020, 1020)):
            if not close(im.getpixel((cx, cy)), expect["field"], tol=6):
                print(f"FAIL {name}: corner {(cx, cy)} not field colour")
                file_ok = False
        mark = expect["left"]
        xs = [x for x in range(1024) if close(im.getpixel((x, ROW_Y)), mark)]
        ys = [y for y in range(1024) if close(im.getpixel((COL_X, y)), mark)]
        if not xs or abs(xs[0] - ROW_SPAN[0]) > 1 or abs(xs[-1] - ROW_SPAN[1]) > 1:
            print(f"FAIL {name}: H bar span at row {ROW_Y} is "
                  f"{(xs[0], xs[-1]) if xs else None}, want {ROW_SPAN}")
            file_ok = False
        if not ys or abs(ys[0] - COL_SPAN[0]) > 1 or abs(ys[-1] - COL_SPAN[1]) > 1:
            print(f"FAIL {name}: crossbar span at column {COL_X} is "
                  f"{(ys[0], ys[-1]) if ys else None}, want {COL_SPAN}")
            file_ok = False
        if name == "AppIcon-tinted.png":
            # Monochrome contract: the H carries no hue, so iOS can tint it.
            for key in ("left", "right", "cross"):
                r, g, b = im.getpixel(PTS[key])[:3]
                if not (r > 0xF0 and abs(r - g) < 4 and abs(g - b) < 4):
                    print(f"FAIL tinted.{key}: {(r, g, b)} is not a white/grey value")
                    file_ok = False
        if file_ok:
            print(f"ok   {name}: 1024x1024, block-H geometry + field colours verified")
        ok = ok and file_ok
    return ok


def check_contents() -> bool:
    """Contents.json must match Xcode 27's own single-size template shape."""
    ok = True
    data = json.loads((SET / "Contents.json").read_text())
    imgs = data.get("images", [])
    if len(imgs) != 3:
        print(f"FAIL Contents.json: {len(imgs)} entries, expected 3 (default+dark+tinted)")
        ok = False
    appearances = [
        tuple(a["value"] for a in e.get("appearances", [])) for e in imgs
    ]
    if appearances != [(), ("dark",), ("tinted",)]:
        # "light" is NOT a legal appearance value for an app icon; the light
        # appearance is the DEFAULT entry with no `appearances` key.
        print(f"FAIL Contents.json appearances {appearances}, expected [(), ('dark',), ('tinted',)]")
        ok = False
    for e in imgs:
        if e.get("size") != "1024x1024" or e.get("idiom") != "universal":
            print(f"FAIL entry {e.get('filename')}: idiom/size wrong")
            ok = False
        if not (SET / e.get("filename", "")).exists():
            print(f"FAIL entry {e.get('filename')}: file not at top level of the set")
            ok = False
    if ok:
        print("ok   Contents.json: single-size, default+dark+tinted (matches Xcode template)")
    return ok


def check_actool() -> bool:
    """actool must compile the catalog AND wire it (CFBundleIconName)."""
    with tempfile.TemporaryDirectory() as out:
        plist = f"{out}/partial.plist"
        proc = subprocess.run(
            ["xcrun", "actool", str(ROOT / "Assets.xcassets"), "--compile", out,
             "--output-format", "human-readable-text", "--warnings",
             "--platform", "iphoneos", "--minimum-deployment-target", "26.0",
             "--app-icon", "AppIcon", "--output-partial-info-plist", plist],
            capture_output=True, text=True)
        log = proc.stdout + proc.stderr
        if proc.returncode != 0:
            print(f"FAIL actool exited {proc.returncode}\n{log[:600]}")
            return False
        if "unassigned child" in log or "does not exist" in log:
            bad = [l for l in log.splitlines() if "unassigned child" in l or "does not exist" in l]
            print("FAIL actool left the icon set incomplete:\n  " + "\n  ".join(sorted(set(bad))[:4]))
            return False
        try:
            import plistlib
            with open(plist, "rb") as fh:
                info = plistlib.load(fh)
        except OSError:
            print("FAIL actool emitted no partial Info.plist")
            return False
        # The partial plist is XML; read it with plistlib (grepping raw XML for
        # a quoted key silently never matches — that false FAIL happened here).
        names = []
        for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
            icon = info.get(key, {}).get("CFBundlePrimaryIcon", {})
            if icon.get("CFBundleIconName"):
                names.append(icon["CFBundleIconName"])
        if "AppIcon" not in names:
            print(f"FAIL no CFBundleIconName => AppIcon (got {names or 'nothing'}); "
                  "the home screen would still show 'H'")
            return False
        if not Path(out, "AppIcon60x60@2x.png").exists():
            print("FAIL actool derived no device sizes")
            return False
    print("ok   actool: catalog compiles, CFBundleIconName => AppIcon, sizes derived")
    return True


ok = check_pixels() & check_contents() & check_actool()
print("APPICON_CHECK", "PASS" if ok else "FAIL")
print("note: verified by compiler + pixel measurement; never run on a device.")
sys.exit(0 if ok else 1)
