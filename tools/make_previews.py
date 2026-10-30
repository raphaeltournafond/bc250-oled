"""The README pictures in docs/: python tools/make_previews.py [--check]"""
import os
import sys

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, REPO)

from PIL import Image  # noqa: E402

from bc250_oled.__main__ import sample_pictures, sheet  # noqa: E402
from bc250_oled.displays import GREYSCALE, PRESETS  # noqa: E402
from bc250_oled.screens import Screens  # noqa: E402


def pictures():
    yield "preview-0.96.png", sheet(sample_pictures(Screens(128, 64)), tint="yellow-blue", zoom=4)
    yield "pages-0.96.png", sheet(sample_pictures(Screens(128, 64, ["overview", "system", "graphs"])), zoom=3)
    for name in ("0.49", "0.66", "0.91", "1.5"):
        p = PRESETS[name]
        grey = p.driver in GREYSCALE
        yield f"preview-{name}.png", sheet(sample_pictures(Screens(p.width, p.height, grey=grey)), grey, zoom=3)


def main(check):
    stale = []
    for name, image in pictures():
        path = os.path.join(REPO, "docs", name)
        if check:
            if not os.path.exists(path) or Image.open(path).convert("RGB").tobytes() != image.tobytes():
                stale.append(name)
        else:
            image.save(path)
            print(path)
    if stale:
        print("Out of date (run python tools/make_previews.py):", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
