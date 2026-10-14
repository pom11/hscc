"""Verify the rendered AppIcon PNGs deterministically.

Two things are proven here, both by measurement (not by looking):

1. PIXELS — each 1024 master is the geometry it claims. Coordinates come from
   assets-master/*.svg (1024 canvas): live node (512,262), parked node
   (280,712), waking node (744,712), field sampled at the corners.
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

DARK = {"field": (0x16, 0x18, 0x1D), "live": (0x7A, 0xE2, 0xB0),
        "parked": (0xA7, 0xB0, 0xC0), "waking": (0xF2, 0xA6, 0x5A)}
LIGHT = {"field": (0xF1, 0xF2, 0xF6), "live": (0x1B, 0x7E, 0x55),
         "parked": (0x55, 0x60, 0x6E), "waking": (0xB4, 0x53, 0x09)}
TINTED = {"field": (0x1D, 0x21, 0x29), "live": (0xFF, 0xFF, 0xFF),
          "parked": None, "waking": None}

PTS = {"field": (32, 1000), "live": (512, 262), "parked": (280, 712), "waking": (744, 712)}

# filename -> expected tones (default entry is the LIGHT master)
FILES = {"AppIcon.png": LIGHT, "AppIcon-dark.png": DARK, "AppIcon-tinted.png": TINTED}


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
        for key, (x, y) in PTS.items():
            want = expect[key]
            if want is None:
                continue
            got = im.getpixel((x, y))
            if not close(got, want):
                print(f"FAIL {name}.{key} at {(x, y)}: got {got}, want {want}")
                ok = False
        if name == "AppIcon-tinted.png":
            # monochrome variant: lower nodes are white at reduced alpha over
            # the field — assert a light grey, not a colour.
            for key in ("parked", "waking"):
                x, y = PTS[key]
                r, g, b = im.getpixel((x, y))[:3]
                if not (r > 0x60 and abs(r - g) < 14 and abs(g - b) < 14):
                    print(f"FAIL tinted.{key}: {(r, g, b)} is not a grey value")
                    ok = False
        for cx, cy in ((4, 4), (1020, 4), (4, 1020), (1020, 1020)):
            if not close(im.getpixel((cx, cy)), expect["field"], tol=6):
                print(f"FAIL {name}: corner {(cx, cy)} not field colour")
                ok = False
        if ok:
            print(f"ok   {name}: 1024x1024, node + field colours verified")
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
