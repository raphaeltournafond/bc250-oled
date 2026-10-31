"""python -m bc250_oled [run|detect|preview|displays] [options]   (see --help of each)"""
import argparse
import datetime
import math
import os
import signal
import sys
import tempfile
import threading
import time

from . import __version__
from .displays import (ADDRESSES, DEFAULT_PRESET, DRIVERS, GREYSCALE, PRESETS, Link, Panel, buses,
                       displays_in, find, open_device, probe, remember, remembered, resolve)
from .screens import PAGES, THEMES, Screens
from .sensors import SAMPLE, Sensors

SHIFTS = ((0, 0), (1, 0), (1, 1), (0, 1))  # burn-in care: the picture moves by a pixel every few minutes


def log(*args):
    print(*args, flush=True)


class InUse(OSError):
    pass


def claim_display():
    """Only one program at a time may talk to the display: two (the service and a detect, or the service started
    twice) mix their transfers on a bus the firmware also uses. Returns the lock to keep; raises InUse if taken."""
    try:
        import fcntl
    except ImportError:  # Windows: previews and tests only
        return None
    # the user's own runtime folder; in the shared temp folder, a name of one's own, and "a" so that a link planted
    # there by someone else can't make this empty a file
    folder = os.environ.get("XDG_RUNTIME_DIR")
    path = os.path.join(folder, "bc250-oled.lock") if folder else \
        os.path.join(tempfile.gettempdir(), f"bc250-oled-{os.getuid()}.lock")
    lock = open(path, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise InUse("the display is in use by another bc250-oled, most likely the service: stop it first with "
                    "'systemctl --user stop bc250-oled' (start it again with 'systemctl --user start bc250-oled')")
    return lock


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
    group.add_argument("--theme", choices=THEMES, default="classic", help="look: classic, or halloween (pumpkins, "
                                                                           "a bat, dripping lines, BOO! when hot)")


def link_options(parser):
    group = parser.add_argument_group("connection")
    group.add_argument("--bus", type=int, help="I2C bus number (default: the first bus where a display answers)")
    group.add_argument("--address", type=lambda v: int(v, 0), default=None,
                       help="0x3C or 0x3D (default: whichever answers)")
    group.add_argument("--link", choices=("auto",) + Link.MODES, default="auto",
                       help="how to write to the bus (default: the fastest it supports)")


def make_screens(args, driver, width, height):
    pages = args.pages.split(",") if args.pages else None
    return Screens(width, height, pages, args.alert, grey=driver in GREYSCALE, theme=args.theme)


def locate(args):
    """(bus, address): from the options, else where the display was found last (nothing else is touched), else the
    first display found by knocking bus after bus."""
    if args.bus is not None:
        if args.address is not None:
            return args.bus, args.address
        for address in ADDRESSES:
            if probe(args.bus, address):
                return args.bus, address
        return args.bus, ADDRESSES[0]
    last = remembered()
    if last and args.address in (None, last[1]):
        return last
    result = find()
    found = [(b, a) for b, a in displays_in(result) if args.address in (None, a)]
    if not found:
        denied = [b for b, (_, answers) in result.items() if answers == "denied"]
        raise OSError("no display answers at 0x3C/0x3D on any I2C bus"
                      + (f" this account may open (no access to i2c-{', i2c-'.join(map(str, denied))}: run setup.sh)"
                         if denied else " (is it wired and powered? try: python -m bc250_oled detect)"))
    remember(*found[0])
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
    try:
        lock = claim_display()
    except InUse as e:
        log(f"error: {e}")
        return 1
    running = [True]
    if threading.current_thread() is threading.main_thread():  # signals only reach the main thread (tests aren't)
        signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))
    device = None
    failures = frames = 0
    while running[0] and frames != args.frames:
        try:
            if device is None:
                bus, address = locate(args)
                device = open_device(driver, width, height, bus, address, args.rotate, args.link)
                device.persist = True  # at exit: just turned off (below), not cleared with a whole frame of writes
                panel = Panel(device)
                if args.contrast is not None:
                    device.contrast(args.contrast)
                panel.watch()
                log(f"{driver} {width}x{height} on I2C bus {bus} at 0x{address:02X} "
                    f"({device._serial_interface.mode} writes, "
                    f"{'status byte' if panel._status_works else 'no status byte'})"
                    + (f", back after {failures} tries" if failures else ""))
                if failures == 0 and not args.no_splash:
                    for n, (picture, seconds) in enumerate(screens.splash_frames()):
                        panel.show(picture, full=n == 0)
                        time.sleep(seconds)
                failures, hidden, started, last_wake = 0, False, time.monotonic(), time.monotonic()
                last_report, reported = time.monotonic(), 0
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
                # a display unplugged and plugged back comes back blank and off, and writes to it still succeed
                if args.keepalive and time.monotonic() - last_wake >= args.keepalive:
                    if panel.keepalive(args.contrast) and panel._status_works:
                        log("the display was off (unplugged?): set up again")
                    last_wake = time.monotonic()
            retried = device._serial_interface.retried[0]
            if retried != reported and time.monotonic() - last_report >= 600:
                log(f"bus busy (the board's firmware using it): {retried - reported} transfers tried again in "
                    f"{(time.monotonic() - last_report) / 60:.0f} min")
                last_report, reported = time.monotonic(), retried
            frames += 1
            time.sleep(max(0.05, args.interval - time.monotonic() % args.interval))
        except OSError as e:
            failures += 1
            if failures <= 3 or failures % 12 == 0:
                log(f"display not reachable ({e}); trying again in 5 s (attempt {failures})")
            if failures == 3 and device is None:
                log("it doesn't answer where it was found last: unplugged, or the bus is locked (shut the BC-250 "
                    "down completely, then power it on). Moved it? Run setup.sh to search again.")
            if device is not None:
                try:
                    device._serial_interface.cleanup()
                except OSError:
                    pass
            device = None
            time.sleep(5)
    if device is not None:
        try:
            device.hide()  # turned off rather than left frozen on the last values
        except OSError:
            pass
    if lock:
        lock.close()
    return 0


def cmd_detect(args):
    """Lists the I2C buses and what answers at the display addresses (0x3C/0x3D), sending no data."""
    lock = claim_display()  # raises InUse, reported by main
    try:
        return find_displays(args.everywhere)
    finally:
        if lock:
            lock.close()


def find_displays(everywhere=False):
    if not buses():
        log("No I2C buses: load the driver with  sudo modprobe i2c-dev  (setup.sh makes it permanent).")
        return 1
    result = find(everywhere)
    for number, (name, answers) in result.items():
        if answers == "denied":
            status = "not open to your account (setup opens only the SMBus port 0 the display uses; --all-buses)"
        elif answers == "timeout":
            status = "transfers time out (nothing connected?): left alone"
        elif answers == "skipped":
            status = "not searched (display already found; --everywhere to search anyway)"
        else:
            status = ", ".join(f"display at 0x{a:02X} (answers {how})" for a, how in answers.items() if how) or "-"
            if any(answers.values()):
                try:
                    link = Link(number, next(a for a, how in answers.items() if how))
                    status += f", {link.mode} writes"
                    link.cleanup()
                except OSError as e:
                    status += f", but can't write: {e}"
        log(f"  i2c-{number:<3} {name[:48]:<48} {status}")
    found = displays_in(result)
    if not found:
        log("No display found. Check the wiring (README, 'How to connect') and that the board is on: the header's "
            "3.3 V pin only has power while the BC-250 runs. It worked before? The bus may be locked: shut the "
            "BC-250 down completely, then power it on.")
        return 1
    remember(*found[0])  # the service goes straight there from now on
    return 0


def cmd_test(args):
    """Step by step on the real display, timing each step: for wiring and driver trouble."""
    driver, width, height = resolve(args.preset, args.driver, args.size)
    lock = claim_display()
    try:
        return test_display(args, driver, width, height)
    finally:
        if lock:
            lock.close()


def test_display(args, driver, width, height):
    from PIL import Image, ImageDraw
    bus, address = locate(args)
    log(f"Display: {driver} {width}x{height}, I2C bus {bus}, address 0x{address:02X}")
    t = time.monotonic()
    device = open_device(driver, width, height, bus, address, args.rotate, args.link)
    device.persist = True
    link = device._serial_interface
    log(f"  set up in {time.monotonic() - t:.2f} s ({link.mode} writes, {sum(map(len, link.setup))} command bytes)")
    panel = Panel(device)
    panel.watch()
    log(f"  status byte: {'yes, a replugged display is noticed with one read' if panel._status_works else 'no, the display is set up again every minute instead'}")
    steps = [("all white", Image.new("L", device.size, 255)), ("frame and cross", Image.new("L", device.size))]
    draw = ImageDraw.Draw(steps[1][1])
    draw.rectangle((0, 0, width - 1, height - 1), outline=255)
    draw.line((0, 0, width - 1, height - 1), fill=255)
    draw.line((0, height - 1, width - 1, 0), fill=255)
    steps.append(("stats", make_screens(args, driver, width, height).render(SAMPLE)))
    for name, picture in steps:
        t = time.monotonic()
        panel.show(picture, full=True)
        log(f"  {name}: {time.monotonic() - t:.2f} s for a full picture, {link.retried[0]} busy-bus retries so "
            f"far. Shown? (next in 3 s)")
        time.sleep(3)
    log("  unplug and plug the display back within the next 10 s...")
    time.sleep(10)
    woke = panel.keepalive(every=1)
    log(f"  {'set up again' if woke else 'still on, nothing to do'}: the stats should be there. Turning it off in 5 s.")
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
    if splash:  # a still start-up screen, or three moments of an animated one
        frames = [picture for picture, _ in screens.splash_frames()]
        return frames if len(frames) == 1 else [frames[1], frames[len(frames) // 2], frames[-1]]
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
                     help="seconds between checks that the display is still on, which bring it back after it was "
                          "unplugged (10; 0: never)")
    run.add_argument("--frames", type=int, help=argparse.SUPPRESS)  # tests: stop after this many updates
    run.set_defaults(func=cmd_run)

    detect = commands.add_parser("detect", help="find the display (sends no data)")
    detect.add_argument("--everywhere", action="store_true",
                        help="keep searching the other buses after finding a display")
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
