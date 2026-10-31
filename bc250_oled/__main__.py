"""python -m bc250_oled [run|detect|preview|displays] [options]   (see --help of each)"""
import argparse
import datetime
import math
import signal
import sys
import time

from . import __version__
from .displays import ADDRESSES, DEFAULT_PRESET, DRIVERS, GREYSCALE, PRESETS, Link, Panel, buses, find, open_device, probe, resolve
from .screens import PAGES, Screens
from .sensors import SAMPLE, Sensors

SHIFTS = ((0, 0), (1, 0), (1, 1), (0, 1))  # burn-in care: the picture moves by a pixel every few minutes


def log(*args):
    print(*args, flush=True)


def display_options(parser):
    group = parser.add_argument_group("display")
    group.add_argument("--preset", choices=sorted(PRESETS), default=DEFAULT_PRESET,
                       help=f"the module's size (default {DEFAULT_PRESET}); 'displays' lists them")
    group.add_argument("--driver", choices=DRIVERS, help="override the preset's controller chip")
    group.add_argument("--size", metavar="WxH", help="override the preset's size in pixels, e.g. 128x32")
    group.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0)
    group.add_argument("--pages", help=f"comma-separated, shown in turn: {', '.join(PAGES)} (default: what fits "
                                       "the size)")
    group.add_argument("--alert", type=float, default=90, help="°C at which a temperature shows inverted (90)")


def link_options(parser):
    group = parser.add_argument_group("connection")
    group.add_argument("--bus", type=int, help="I2C bus number (default: the first bus where a display answers)")
    group.add_argument("--address", type=lambda v: int(v, 0), default=None,
                       help="0x3C or 0x3D (default: whichever answers)")
    group.add_argument("--link", choices=("auto",) + Link.MODES, default="auto",
                       help="how to write to the bus (default: the fastest it supports)")


def make_screens(args, driver, width, height):
    pages = args.pages.split(",") if args.pages else None
    return Screens(width, height, pages, args.alert, grey=driver in GREYSCALE)


def locate(args):
    """(bus, address) from the options, else the first display found."""
    if args.bus is not None:
        if args.address is not None:
            return args.bus, args.address
        for address in ADDRESSES:
            if probe(args.bus, address):
                return args.bus, address
        return args.bus, ADDRESSES[0]
    found, denied = find()
    found = [(b, a) for b, a, _ in found if args.address in (None, a)]
    if not found:
        raise OSError("no display answers at 0x3C/0x3D on any I2C bus"
                      + (f" this account may open (no access to i2c-{', i2c-'.join(map(str, denied))}: run setup.sh)"
                         if denied else " (is it wired and powered? try: python -m bc250_oled detect)"))
    return found[0]


def parse_night(value):
    if not value:
        return None
    start, end = value.split("-")
    return tuple(datetime.time.fromisoformat(t if len(t) > 2 else t + ":00") for t in (start, end))


def is_night(night, now):
    if not night:
        return False
    start, end = night
    t = now.time()
    return start <= t < end if start < end else (t >= start or t < end)


# --- commands -----------------------------------------------------------------------------------------------------------

def cmd_run(args):
    driver, width, height = resolve(args.preset, args.driver, args.size)
    screens = make_screens(args, driver, width, height)
    night = parse_night(args.night)
    sensors = Sensors()
    running = [True]
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))
    device = None
    failures = frames = 0
    while running[0] and frames != args.frames:
        try:
            if device is None:
                bus, address = locate(args)
                device = open_device(driver, width, height, bus, address, args.rotate, args.link)
                panel = Panel(device)
                if args.contrast is not None:
                    device.contrast(args.contrast)
                log(f"{driver} {width}x{height} on I2C bus {bus} at 0x{address:02X} "
                    f"({device._serial_interface.mode} writes)")
                if failures == 0 and not args.no_splash:
                    panel.show(screens.splash(), full=True)
                    time.sleep(2)
                failures, hidden, started, last_wake = 0, False, time.monotonic(), time.monotonic()
            stats = sensors.read()
            screens.record(stats)
            now = datetime.datetime.now()
            if is_night(night, now):
                if not hidden:
                    device.hide()
                    hidden = True
            else:
                if hidden:
                    device.show()
                    hidden = False
                elapsed = time.monotonic() - started
                picture = screens.render(stats, int(elapsed // args.page_seconds))
                if not args.no_shift:
                    dx, dy = SHIFTS[int(elapsed // 180) % len(SHIFTS)]
                    if dx or dy:
                        from PIL import Image
                        shifted = Image.new("L", picture.size)
                        shifted.paste(picture, (dx, dy))
                        picture = shifted
                panel.show(picture)
                # a display unplugged and plugged back comes back blank and off, and writes to it still succeed:
                # now and then, set it up again and send the whole picture (also clears any glitch from the bus)
                if args.keepalive and time.monotonic() - last_wake >= args.keepalive:
                    panel.wake(args.contrast)
                    last_wake = time.monotonic()
            frames += 1
            time.sleep(max(0.05, args.interval - time.monotonic() % args.interval))
        except OSError as e:
            failures += 1
            if failures == 1 or failures % 60 == 0:
                log(f"display not reachable ({e}); retrying every 5 s")
            if device is not None:
                device.persist = True  # luma would otherwise try to clear it at exit, through the closed bus
                try:
                    device._serial_interface.cleanup()
                except OSError:
                    pass
            device = None
            time.sleep(5)
    if device is not None:
        device.hide()  # turned off rather than left frozen on the last values


def cmd_detect(args):
    """Lists the I2C buses and what answers at the display addresses (0x3C/0x3D), sending no data."""
    found_any = False
    listed = buses()
    if not listed:
        log("No I2C buses: load the driver with  sudo modprobe i2c-dev  (setup.sh makes it permanent).")
        return 1
    for number, name in listed:
        try:
            answers = {a: probe(number, a) for a in ADDRESSES}
        except PermissionError:
            log(f"  i2c-{number:<3} {name[:48]:<48} no access (run setup.sh, or try with sudo)")
            continue
        status = ", ".join(f"display at 0x{a:02X} (answers {how})" for a, how in answers.items() if how) or "-"
        if any(answers.values()):
            found_any = True
            try:
                link = Link(number, next(a for a, how in answers.items() if how))
                status += f", {link.mode} writes"
                link.cleanup()
            except OSError as e:
                status += f", but can't write: {e}"
        log(f"  i2c-{number:<3} {name[:48]:<48} {status}")
    if not found_any:
        log("No display found. Check the wiring (README, 'How to connect') and that the board is on: the header's "
            "3.3 V pin only has power while the BC-250 runs.")
        return 1
    return 0


def cmd_test(args):
    """Step by step on the real display, timing each step: for wiring and driver trouble."""
    from PIL import Image, ImageDraw
    driver, width, height = resolve(args.preset, args.driver, args.size)
    bus, address = locate(args)
    log(f"Display: {driver} {width}x{height}, I2C bus {bus}, address 0x{address:02X}")
    t = time.monotonic()
    device = open_device(driver, width, height, bus, address, args.rotate, args.link)
    device.persist = True
    link = device._serial_interface
    log(f"  set up in {time.monotonic() - t:.2f} s ({link.mode} writes, {sum(map(len, link.setup))} command bytes)")
    panel = Panel(device)
    steps = [("all white", Image.new("L", device.size, 255)), ("frame and cross", Image.new("L", device.size))]
    draw = ImageDraw.Draw(steps[1][1])
    draw.rectangle((0, 0, width - 1, height - 1), outline=255)
    draw.line((0, 0, width - 1, height - 1), fill=255)
    draw.line((0, height - 1, width - 1, 0), fill=255)
    steps.append(("stats", make_screens(args, driver, width, height).render(SAMPLE)))
    for name, picture in steps:
        t = time.monotonic()
        panel.show(picture, full=True)
        log(f"  {name}: {time.monotonic() - t:.2f} s for a full picture. Shown? (next in 3 s)")
        time.sleep(3)
    log("  unplug and plug the display back within the next 10 s...")
    time.sleep(10)
    panel.wake(None)
    log("  woken up again: the stats should be back. Turning it off in 5 s.")
    time.sleep(5)
    device.hide()
    link.cleanup()
    return 0


def sample_pictures(screens, stats=SAMPLE, splash=False):
    """Every screen with `stats`, after a made-up minute of history for the graphs."""
    for i in range(screens.width):
        load = 45 + 30 * math.sin(i / 9) * math.cos(i / 23)
        screens.record(type(stats)(**{**stats.__dict__, "cpu_load": load, "gpu_load": 100 - load}))
    screens.record(stats)
    if splash:
        return [screens.splash()]
    return [screens.render(stats, i) for i in range(len(screens.screens(stats)))]


def sheet(pictures, grey=False, tint="white", zoom=4):
    """The pictures side by side, each pixel drawn as a dot the way the OLED shows it."""
    from PIL import Image
    tints = {"white": (235, 240, 255), "blue": (80, 200, 255), "yellow": (255, 220, 60)}
    width, height = pictures[0].size
    image = Image.new("RGB", (len(pictures) * (width * zoom + 8) + 8, height * zoom + 16), (24, 24, 28))
    for n, picture in enumerate(pictures):
        ox = 8 + n * (width * zoom + 8)
        image.paste((0, 0, 0), (ox, 8, ox + width * zoom, 8 + height * zoom))
        for y in range(height):
            color = tints["yellow" if tint == "yellow-blue" and y < 16 else "blue" if tint == "yellow-blue" else tint]
            for x in range(width):
                v = picture.getpixel((x, y))
                if not grey:
                    v = 255 if v >= 128 else 0  # what a monochrome display makes of it
                if v:
                    image.paste(tuple(int(c * v / 255) for c in color),
                                (ox + x * zoom, 8 + y * zoom, ox + (x + 1) * zoom - (zoom > 2),
                                 8 + (y + 1) * zoom - (zoom > 2)))
    return image


def cmd_preview(args):
    """Saves what the display would show, with sample readings (or this machine's, with --live)."""
    driver, width, height = resolve(args.preset, args.driver, args.size)
    open_device(driver, width, height, None, None).persist = True  # checks luma knows this driver and size
    screens = make_screens(args, driver, width, height)
    stats = SAMPLE
    if args.live:
        sensors = Sensors()
        sensors.read()
        time.sleep(1)
        stats = sensors.read()
    pictures = sample_pictures(screens, stats, args.splash)
    sheet(pictures, driver in GREYSCALE, args.tint, args.zoom).save(args.out)
    log(f"{args.out}: {len(pictures)} screen(s), {driver} {width}x{height}")
    return 0


def cmd_displays(args):
    log("Presets (--preset NAME):")
    for name, p in PRESETS.items():
        log(f"  {name:<11} {p.note}")
    log(f"Other controllers: --driver {{{','.join(DRIVERS)}}} --size WxH")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bc250_oled", description="BC-250 stats on a small I2C OLED display.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")

    run = commands.add_parser("run", help="show the stats (default)")
    display_options(run)
    link_options(run)
    run.add_argument("--contrast", type=int, help="0-255; lower is dimmer and wears the OLED less")
    run.add_argument("--night", metavar="HH:MM-HH:MM", help="display off during these hours, e.g. 23:00-07:30")
    run.add_argument("--interval", type=float, default=1.0, help="seconds between updates (1)")
    run.add_argument("--page-seconds", type=float, default=6, help="seconds per screen when several (6)")
    run.add_argument("--no-shift", action="store_true", help="don't move the picture by a pixel now and then")
    run.add_argument("--no-splash", action="store_true")
    run.add_argument("--keepalive", type=float, default=10,
                     help="seconds between setting the display up again, which brings it back after it was "
                          "unplugged (10; 0: never)")
    run.add_argument("--frames", type=int, help=argparse.SUPPRESS)  # tests: stop after this many updates
    run.set_defaults(func=cmd_run)

    detect = commands.add_parser("detect", help="find the display (sends no data)")
    detect.set_defaults(func=cmd_detect)

    test = commands.add_parser("test", help="test patterns and timings on the display, for wiring trouble")
    display_options(test)
    link_options(test)
    test.set_defaults(func=cmd_test)

    preview = commands.add_parser("preview", help="save a picture of the screens (no display needed)")
    display_options(preview)
    preview.add_argument("--out", default="preview.png")
    preview.add_argument("--live", action="store_true", help="this machine's readings instead of samples")
    preview.add_argument("--splash", action="store_true", help="the start-up screen instead")
    preview.add_argument("--zoom", type=int, default=4)
    preview.add_argument("--tint", choices=("white", "blue", "yellow", "yellow-blue"), default="white",
                         help="yellow-blue: the two-colour 0.96\" modules (top 16 rows yellow)")
    preview.set_defaults(func=cmd_preview)

    commands.add_parser("displays", help="list the supported displays").set_defaults(func=cmd_displays)

    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv = ["run"] + list(argv)
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 0
    except OSError as e:  # detect and test: no display, no access...
        log(f"error: {e}")
        return 1
    except ValueError as e:
        parser.error(str(e))


if __name__ == "__main__":
    sys.exit(main())
