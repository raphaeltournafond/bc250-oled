"""What the display shows, drawn for any size: pages made of tiles, laid out from the display's width and height.

Pictures are greyscale ("L", 0-255): pure white on black, plus dim graph fills on greyscale displays. Text uses the classic 5x7 character-LCD font below, scaled up by whole pixels for the big
numbers: crisp on any OLED, and no font file to install."""
from collections import deque
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageOps

CAP = 7  # every glyph is 7 pixels high
MAX_SCALE = 4
DIM = 90  # graph fills on greyscale displays
PAGES = ("overview", "system", "graphs")
THEMES = ("classic", "halloween")

GLYPHS = {  # 7 rows each; digits share one width so numbers don't jiggle as they change
    "0": ".###. #...# #..## #.#.# ##..# #...# .###.", "1": "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",
    "2": ".###. #...# ....# ...#. ..#.. .#... #####", "3": "##### ...#. ..#.. ...#. ....# #...# .###.",
    "4": "...#. ..##. .#.#. #..#. ##### ...#. ...#.", "5": "##### #.... ####. ....# ....# #...# .###.",
    "6": "..##. .#... #.... ####. #...# #...# .###.", "7": "##### ....# ...#. ..#.. .#... .#... .#...",
    "8": ".###. #...# #...# .###. #...# #...# .###.", "9": ".###. #...# #...# .#### ....# ...#. .##..",
    "A": ".###. #...# #...# ##### #...# #...# #...#", "B": "####. #...# #...# ####. #...# #...# ####.",
    "C": ".###. #...# #.... #.... #.... #...# .###.", "D": "###.. #..#. #...# #...# #...# #..#. ###..",
    "E": "##### #.... #.... ####. #.... #.... #####", "F": "##### #.... #.... ####. #.... #.... #....",
    "G": ".###. #...# #.... #.### #...# #...# .####", "H": "#...# #...# #...# ##### #...# #...# #...#",
    "I": "### .#. .#. .#. .#. .#. ###", "J": "..### ...#. ...#. ...#. ...#. #..#. .##..",
    "K": "#...# #..#. #.#.. ##... #.#.. #..#. #...#", "L": "#.... #.... #.... #.... #.... #.... #####",
    "M": "#...# ##.## #.#.# #.#.# #...# #...# #...#", "N": "#...# #...# ##..# #.#.# #..## #...# #...#",
    "O": ".###. #...# #...# #...# #...# #...# .###.", "P": "####. #...# #...# ####. #.... #.... #....",
    "Q": ".###. #...# #...# #...# #.#.# #..#. .##.#", "R": "####. #...# #...# ####. #.#.. #..#. #...#",
    "S": ".#### #.... #.... .###. ....# ....# ####.", "T": "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    "U": "#...# #...# #...# #...# #...# #...# .###.", "V": "#...# #...# #...# .#.#. .#.#. .#.#. ..#..",
    "W": "#...# #...# #...# #.#.# #.#.# #.#.# .#.#.", "X": "#...# #...# .#.#. ..#.. .#.#. #...# #...#",
    "Y": "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..", "Z": "##### ....# ...#. ..#.. .#... #.... #####",
    " ": "... ... ... ... ... ... ...", "°": ".##. #..# #..# .##. .... .... ....",
    "%": "##... ##..# ...#. ..#.. .#... #..## ...##", ".": ".. .. .. .. .. ## ##",
    ":": ".. ## ## .. ## ## ..", "-": "..... ..... ..... ##### ..... ..... .....",
    "/": "..... ....# ...#. ..#.. .#... #.... .....", "+": "..... ..#.. ..#.. ##### ..#.. ..#.. .....",
    "?": ".###. #...# ....# ...#. ..#.. ..... ..#..",
    # halloween icons
    "🎃": "...#... .##.##. ####### #.###.# ####### #.....# .#####.",
    "🦇": "....... #.....# ##.#.## ####### .#####. ..#.#.. .......",
    "☠": ".###. ##### #.#.# ##### .###. .#.#. .....", "!": "# # # # # . #",
}
GLYPHS = {c: rows.split() for c, rows in GLYPHS.items()}


# --- text ---------------------------------------------------------------------------------------------------------------

def _glyphs(s):
    return [GLYPHS.get(c, GLYPHS["?"]) for c in s.upper()]


def text_width(s, scale=1):
    return (sum(len(g[0]) + 1 for g in _glyphs(s)) - 1) * scale if s else 0


def text(image, x, y, s, scale=1, align="left", fill=255):
    """Draws `s` (in capitals) with its top at y; returns its width."""
    if not s:
        return 0
    width = text_width(s, scale)
    x -= {"left": 0, "center": width // 2, "right": width}[align]
    mask = Image.new("L", (width // scale, CAP))
    column = 0
    for glyph in _glyphs(s):
        for row, line in enumerate(glyph):
            for i, bit in enumerate(line):
                if bit == "#":
                    mask.putpixel((column + i, row), 255)
        column += len(glyph[0]) + 1
    if scale > 1:
        mask = mask.resize((width, CAP * scale), Image.Resampling.NEAREST)
    image.paste(fill, (x, y, x + mask.width, y + mask.height), mask)
    return width


def fit_scale(s, width, height, most=MAX_SCALE):
    """The largest scale at which `s` fits in width x height (0 if not even the smallest)."""
    for scale in range(most, 0, -1):
        if text_width(s, scale) <= width and CAP * scale <= height:
            return scale
    return 0


# --- values -------------------------------------------------------------------------------------------------------------

def _f(value, pattern, missing="--"):
    return missing if value is None else pattern.format(value)


@dataclass
class Content:
    label: str
    value: str
    right: str = ""  # small, at the end of the label row
    bar: float = None  # 0..1, a bar under the label
    sub: str = ""  # small, under the value
    hot: bool = False  # shown inverted


ICONS = {"cpu": "🎃", "gpu": "🎃", "fan": "🦇", "nvme": "☠", "vrm": "☠"}  # halloween


def content(kind, s, alert, theme="classic"):
    c = _content(kind, s, alert)
    if theme == "halloween":
        if kind in ICONS:
            c.label = f"{ICONS[kind]} {c.label}"
        if c.hot:
            c.right = "BOO!"
    return c


def _content(kind, s, alert):
    if kind == "cpu":
        return Content("CPU", _f(s.cpu_temp, "{:.0f}°"), _f(s.cpu_load, "{:.0f}%", ""),
                       None if s.cpu_load is None else s.cpu_load / 100, _f(s.cpu_ghz, "{:.2f}GHZ", ""),
                       s.cpu_temp is not None and s.cpu_temp >= alert)
    if kind == "gpu":
        return Content("GPU", _f(s.gpu_temp, "{:.0f}°"), _f(s.gpu_load, "{:.0f}%", ""),
                       None if s.gpu_load is None else s.gpu_load / 100, _f(s.gpu_mhz, "{:.0f}MHZ", ""),
                       s.gpu_temp is not None and s.gpu_temp >= alert)
    if kind == "nvme":
        return Content("NVME", _f(s.nvme_temp, "{:.0f}°"))
    if kind == "fan":
        return Content("FAN", _f(s.fan_rpm, "{:.0f}"), "RPM")
    if kind == "power":
        return Content("POWER", _f(s.gpu_watts, "{:.0f}"), "W")
    if kind == "vrm":
        return Content("VRM", _f(s.vrm_temp, "{:.0f}°"), "", hot=s.vrm_temp is not None and s.vrm_temp >= alert)
    if kind == "ram":
        return Content("RAM", _f(s.ram_pct, "{:.0f}%"), "", None if s.ram_pct is None else s.ram_pct / 100)
    raise ValueError(kind)


SYSTEM = ("nvme", "fan", "power", "vrm", "ram")  # the system page shows the first ones this machine has
SOURCE = {"nvme": "nvme_temp", "fan": "fan_rpm", "power": "gpu_watts", "vrm": "vrm_temp", "ram": "ram_pct"}


def system_kinds(s, most):
    have = [k for k in SYSTEM if getattr(s, SOURCE[k]) is not None]
    return (have or ["nvme", "power"])[:most]


def footer_line(s, width):
    """One line of the system values, as many as fit."""
    items = [f"NVME {s.nvme_temp:.0f}°" if s.nvme_temp is not None else "",
             f"FAN {s.fan_rpm:.0f}" if s.fan_rpm is not None else "",
             f"{s.gpu_watts:.0f}W" if s.gpu_watts is not None else "",
             f"VRM {s.vrm_temp:.0f}°" if s.vrm_temp is not None else "",
             f"RAM {s.ram_pct:.0f}%" if s.ram_pct is not None else ""]
    items = [i for i in items if i] or ["NVME --"]
    while items:
        for gap in ("  ", " "):
            line = gap.join(items)
            if text_width(line) <= width:
                return line
        items.pop()
    return ""


# --- tiles --------------------------------------------------------------------------------------------------------------

def tile(image, box, c):
    """A label row, an optional load bar, the value as big as the box allows, and an optional line under it.
    The bar and that line are left out when keeping them would shrink the value."""
    x, y, w, h = box
    if h < 20:  # one line: "CPU 41° 37%"
        for line in (f"{c.label} {c.value} {c.right}".strip(), f"{c.label} {c.value}", c.value):
            scale = fit_scale(line, w - 2, h)
            if scale:
                text(image, x + 1, y + (h - CAP * scale) // 2, line, scale)
                break
    else:
        text(image, x + 1, y + 1, c.label)
        if c.right and text_width(c.label) + text_width(c.right) + 4 <= w - 2:
            text(image, x + w - 1, y + 1, c.right, align="right")
        top, bottom = y + CAP + 3, y + h

        def scale(bar, sub):
            return fit_scale(c.value, w - 2, bottom - top - 5 * bar - (CAP + 2) * sub - 1)
        best = scale(False, False)
        bar = c.bar is not None and scale(True, False) >= min(best, 3)
        sub = bool(c.sub) and text_width(c.sub) <= w - 2 and scale(bar, True) >= min(best, 3)
        size = max(1, scale(bar, sub))
        if bar:
            filled = round(max(0.0, min(1.0, c.bar)) * (w - 2))
            for px in range(x + 1, x + w - 1, 2):  # dotted track
                image.putpixel((px, top + 1), 255)
            if filled:
                ImageDraw.Draw(image).rectangle((x + 1, top, x + filled, top + 2), fill=255)
            top += 5
        if sub:
            text(image, x + 1, bottom - CAP - 1, c.sub)
            bottom -= CAP + 2
        text(image, x + 1, top + (bottom - top - CAP * size) // 2, c.value, size)
    if c.hot:
        region = (x, y, x + w, y + h)
        image.paste(ImageOps.invert(image.crop(region)), region[:2])


def graph(image, box, label, values, current, grey=False):
    """A load history (0-100 %), newest on the right, filled dim on greyscale displays, dotted on the others."""
    x, y, w, h = box
    right = _f(current, "{:.0f}%")
    if text_width(label) + text_width(right) + 4 > w - 2:
        label = label.split()[0]
    if text_width(label) + text_width(right) + 4 > w - 2:  # very narrow: the graph shows the load anyway
        right = ""
    text(image, x + 1, y + 1, label)
    text(image, x + w - 1, y + 1, right, align="right")
    top, bottom = y + CAP + 3, y + h - 1
    draw = ImageDraw.Draw(image)
    draw.line((x, bottom, x + w - 1, bottom), fill=255)
    points = [(x + w - len(values) + i, bottom - round(v / 100 * (bottom - top))) for i, v in enumerate(values)
              if v is not None and x + w - len(values) + i >= x]
    for px, py in points:
        for fy in range(py + 1, bottom):
            if grey or (px + fy) % 2 == 0:
                image.putpixel((px, fy), DIM if grey else 255)
    if len(points) > 1:
        draw.line(points, fill=255)
    elif points:
        draw.point(points, fill=255)


# --- layouts ------------------------------------------------------------------------------------------------------------

def default_pages(width, height):
    if width >= 96 and height < 48:
        return ["overview", "system"]  # 128x32, 96x16: too short for everything at once
    if width < 96 and height < 96:
        return ["overview", "system"]  # 64x32, 64x48: one value at a time
    return ["overview"]


def layout(page, width, height, s):
    """[(kind, box)] for one page, and the lines between tiles. A page may expand to several screens on small
    displays: returns a list of (tiles, lines)."""
    W, H = width, height
    half = (W - 1) // 2
    if W >= 96 and H >= 48:
        top = 48 if H < 96 else 50
        if page == "overview":
            tiles = [("cpu", (0, 0, half, top)), ("gpu", (half + 1, 0, W - half - 1, top))]
            lines = [(half, 0, half, top - 1), (0, top + 1, W - 1, top + 1)]
            if H >= 96:  # 128x128: everything on one screen, with history graphs below
                rest = H - top - 3
                tiles += [("footer", (0, top + 3, W, 11)),
                          ("graph:cpu", (0, top + 15, W, (rest - 14) // 2)),
                          ("graph:gpu", (0, top + 16 + (rest - 14) // 2, W, H - top - 16 - (rest - 14) // 2))]
            else:
                tiles.append(("footer", (0, top + 2, W, H - top - 2)))
            return [(tiles, lines)]
        if page == "graphs":
            return [([("graph:cpu", (0, 0, W, H // 2 - 1)), ("graph:gpu", (0, H // 2 + 1, W, H - H // 2 - 1))], [])]
        kinds = system_kinds(s, 4)
        cols = 2 if len(kinds) > 1 else 1
        rows = (len(kinds) + 1) // 2 if cols == 2 else 1
        cell_w, cell_h = (W - cols + 1) // cols, (H - rows + 1) // rows
        tiles = [(k, ((i % cols) * (cell_w + 1), (i // cols) * (cell_h + 1), cell_w, cell_h))
                 for i, k in enumerate(kinds)]
        lines = [(cell_w, 0, cell_w, H - 1)] if cols == 2 else []
        lines += [(0, r * (cell_h + 1) - 1, W - 1, r * (cell_h + 1) - 1) for r in range(1, rows)]
        return [(tiles, lines)]
    if W >= 96:  # wide and short: side by side, or stacked one-line rows when very short
        kinds = {"overview": ["cpu", "gpu"], "graphs": ["graph:cpu", "graph:gpu"]}.get(page) or system_kinds(s, 3)
        if H < 24 or page == "graphs":
            n = 1 if page == "graphs" else 2  # graphs need the height
            pages = [kinds[i:i + n] for i in range(0, len(kinds), n)]
            return [([(k, (0, j * (H // len(p)), W, H // len(p))) for j, k in enumerate(p)], []) for p in pages]
        cell = (W - len(kinds) + 1) // len(kinds)
        return [([(k, (i * (cell + 1), 0, cell, H)) for i, k in enumerate(kinds)],
                 [(i * (cell + 1) - 1, 0, i * (cell + 1) - 1, H - 1) for i in range(1, len(kinds))])]
    # narrow: a column of tiles when tall, else one tile per screen
    kinds = {"overview": ["cpu", "gpu"], "graphs": ["graph:cpu", "graph:gpu"]}.get(page) or system_kinds(s, 4)
    if H >= 96:
        if page == "overview":
            kinds = ["cpu", "gpu"] + system_kinds(s, 2)
        cell = (H - len(kinds) + 1) // len(kinds)
        return [([(k, (0, i * (cell + 1), W, cell)) for i, k in enumerate(kinds)],
                 [(0, i * (cell + 1) - 1, W - 1, i * (cell + 1) - 1) for i in range(1, len(kinds))])]
    return [([(k, (0, 0, W, H))], []) for k in kinds]


class Screens:
    """The sequence of screens to show, from the chosen pages, and the pictures for them."""

    def __init__(self, width, height, pages=None, alert=90, grey=False, theme="classic"):
        self.width, self.height, self.grey = width, height, grey
        if theme not in THEMES:
            raise ValueError(f"unknown theme {theme}: choose from {', '.join(THEMES)}")
        self.theme = theme
        self.pages = list(pages or default_pages(width, height))
        unknown = set(self.pages) - set(PAGES)
        if unknown:
            raise ValueError(f"unknown page(s) {', '.join(sorted(unknown))}: choose from {', '.join(PAGES)}")
        self.alert = alert
        self.history = {"cpu": deque(maxlen=width), "gpu": deque(maxlen=width)}

    def record(self, s):
        self.history["cpu"].append(s.cpu_load)
        self.history["gpu"].append(s.gpu_load)

    def screens(self, s):
        return [screen for page in self.pages for screen in layout(page, self.width, self.height, s)]

    def render(self, s, index=0):
        screens = self.screens(s)
        tiles, lines = screens[index % len(screens)]
        image = Image.new("L", (self.width, self.height))
        draw = ImageDraw.Draw(image)
        for line in lines:
            if line[1] == line[3] and self.theme == "halloween":
                stitches(image, line[0], line[2], line[1])
                continue
            for px in range(line[0], line[2] + 1, 2) if line[1] == line[3] else ():  # dotted, lighter on the eye
                image.putpixel((px, line[1]), 255)
            if line[0] == line[2]:
                draw.line(line, fill=255)
        for kind, box in tiles:
            if kind == "footer":
                line = footer_line(s, box[2] - 2)
                text(image, box[0] + 1, box[1] + (box[3] - CAP) // 2, line)
            elif kind.startswith("graph:"):
                which = kind.split(":")[1]
                graph(image, box, f"{which.upper()} LOAD", list(self.history[which]),
                      s.cpu_load if which == "cpu" else s.gpu_load, self.grey)
            else:
                tile(image, box, content(kind, s, self.alert, self.theme))
        return image

    def splash_frames(self):
        """The start-up screen, as [(picture, seconds)]: a still one, or a short animation."""
        if self.theme == "halloween":
            return halloween_splash(self.width, self.height)
        return [(self.splash(), 2.0)]

    def splash(self):
        image = Image.new("L", (self.width, self.height))
        title = "BC-250"
        scale = fit_scale(title, self.width - 4, self.height - CAP - 4)
        if not scale:
            if self.height > self.width:  # narrow portrait
                stacked_title(image, 0, self.height, "OLED")
            return image
        block = CAP * scale + (CAP + 3 if self.height >= CAP * (scale + 1) + 6 else 0)
        y = (self.height - block) // 2
        text(image, self.width // 2, y, title, scale, align="center")
        if block > CAP * scale:
            text(image, self.width // 2, y + CAP * scale + 3, "OLED", align="center")
        return image


# --- halloween -------------------------------------------------------------------------------------------------------------

BAT = ((-8, -2), (-6, -4), (-4, -3), (-2, -4), (-1, -2), (-1, -4), (0, -3), (1, -4), (1, -2), (2, -4), (4, -3),
       (6, -4), (8, -2), (6, -1), (5, 0), (4, -1), (2, 0), (1, 1), (0, 2), (-1, 1), (-2, 0), (-4, -1), (-5, 0),
       (-6, -1))  # wings spread, two ears, in units of its scale


def stitches(image, x1, x2, y):
    """A separator stitched like a scar: a line crossed by a short stitch every 6 pixels, one pixel above and below
    (a tile's content never reaches the rows next to its edge)."""
    ImageDraw.Draw(image).line((x1, y, x2, y), fill=255)
    for x in range(x1 + 3, x2 - 1, 6):
        for dy in (-1, 1):
            if 0 <= y + dy < image.height:
                image.putpixel((x, y + dy), 255)


def bat(image, cx, cy, scale, wings_up=True):
    points = [(cx + px * scale, cy + py * scale * (1 if wings_up or py >= 0 else 0.6)) for px, py in BAT]
    ImageDraw.Draw(image).polygon(points, fill=255)


def pumpkin(image, cx, cy, r, lit=True):
    """A jack-o'-lantern: the body outlined with its ribs, a stem, and the carved face glowing (or just outlined,
    the candle flickering low)."""
    d = ImageDraw.Draw(image)
    rx, ry = r * 1.2, r
    d.rectangle((cx - 1, cy - ry - max(2, r // 4), cx + 1, cy - ry + 1), fill=255)
    d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), outline=255)
    d.arc((cx - rx * 0.5, cy - ry, cx + rx * 0.5, cy + ry), 250, 290, fill=255)
    d.arc((cx - rx * 0.5, cy - ry, cx + rx * 0.5, cy + ry), 70, 110, fill=255)

    def poly(*points):
        return [(cx + px * rx, cy + py * ry) for px, py in points]
    face = [poly((-0.6, -0.05), (-0.15, -0.05), (-0.38, -0.5)), poly((0.15, -0.05), (0.6, -0.05), (0.38, -0.5)),
            poly((-0.65, 0.22), (-0.35, 0.32), (-0.2, 0.22), (0.2, 0.22), (0.35, 0.32), (0.65, 0.22), (0.45, 0.6),
                 (0, 0.68), (-0.45, 0.6))]
    for hole in face:
        d.polygon(hole, fill=255 if lit else 0, outline=255)


def stacked_title(image, top, bottom, subtitle):
    """BC-250 centred between rows top and bottom, on two lines (BC, 250) if one is too wide, and `subtitle` under it
    when there is room: for portrait screens."""
    width = image.width - 4
    room = bottom - top
    for lines in (["BC-250"], ["BC", "250"]):
        for sub in (subtitle, ""):
            if sub and text_width(sub) > width:
                continue
            extra = CAP + 3 if sub else 0
            scale = min(fit_scale(line, width, (room - extra - 3 * (len(lines) - 1)) // len(lines)) for line in lines)
            if scale:
                y = top + (room - (CAP * scale + 3) * len(lines) + 3 - extra) // 2
                for line in lines:
                    text(image, image.width // 2, y, line, scale, align="center")
                    y += CAP * scale + 3
                if sub:
                    text(image, image.width // 2, y, sub, align="center")
                return


def halloween_splash(width, height):
    """A jack-o'-lantern beside the logo (above it in portrait); its candle flickers while a bat flies past, about
    2.5 s."""
    base = Image.new("L", (width, height))
    title = "BC-250"
    if height > width:
        r = max(5, min(int((width // 2 - 3) / 1.2), height // 6))
        cx, cy = width // 2, height // 3 + r // 2
        stacked_title(base, cy + r + 3, height, "BOO!")
        scale = 0
    else:
        r = max(5, min(height // 2 - 4, width // 7))
        cx, cy = round(r * 1.2) + 2, height // 2 + max(1, r // 6)
        x0 = cx + round(r * 1.2) + 4
        room = width - x0 - 2
        scale = fit_scale(title, room, height - CAP - 4) or fit_scale(title, room, height)
    if scale:
        block = CAP * scale + (CAP + 3 if height >= CAP * (scale + 1) + 6 else 0)
        y = (height - block) // 2 + (2 if height >= 48 else 0)
        tx = x0 + room // 2
        text(base, tx, y, title, scale, align="center")
        if block > CAP * scale:
            text(base, tx, y + CAP * scale + 3, "BOO!" if room < text_width("HAPPY HALLOWEEN") else "HAPPY HALLOWEEN",
                 align="center")
    frames, steps = [], 14
    scale_bat = 1.2 if height < 48 else 2
    for i in range(steps):
        image = base.copy()
        pumpkin(image, cx, cy, r, lit=i % 5 != 3)  # the candle gutters now and then
        x = width + 10 - (width + 20) * i / (steps - 1)
        bat(image, x, 4 * scale_bat + 1 + (1 if i % 2 else 0), scale_bat, wings_up=i % 2 == 0)
        frames.append((image, 0.18))
    last = base.copy()
    pumpkin(last, cx, cy, r)
    frames.append((last, 0.6))
    return frames
