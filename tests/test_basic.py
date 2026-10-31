"""No hardware needed: every size and page renders inside its tiles, the sensors read a fake sysfs, and what reaches an
emulated display controller (through every kind of bus write) is exactly the picture.
Run: python -m unittest discover -s tests -v"""
import enum
import errno
import os
import random
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PIL import Image  # noqa: E402

from bc250_oled import displays, screens, sensors  # noqa: E402
from bc250_oled.sensors import SAMPLE, Stats  # noqa: E402

SIZES = [(p.driver, p.width, p.height) for p in displays.PRESETS.values()] + [
    ("ssd1306", 96, 16), ("sh1107", 64, 128), ("sh1107", 80, 128), ("ssd1305", 128, 32), ("sh1106", 128, 32)]


# --- an SSD1306/SH1106 that keeps its picture memory, fed by a fake smbus2 ----------------------------------------------

class Controller:
    ARGS = {0x81: 1, 0xA8: 1, 0xD3: 1, 0xD5: 1, 0xD9: 1, 0xDA: 1, 0xDB: 1, 0x8D: 1, 0xAD: 1, 0x21: 2, 0x22: 2}

    def __init__(self, kind):
        self.kind = kind  # "ssd1306": horizontal addressing in a window; "sh1106": page addressing
        self.ram = [[0] * 132 for _ in range(16)]
        self.pending, self.args = None, []
        self.col_range, self.page_range = (0, 127), (0, 7)
        self.col, self.page = 0, 0
        self.writes = 0
        self.on = False
        self.turned_off = 0  # "display off" commands received

    def power_cycle(self):
        """Unplugged and plugged back: picture memory full of noise, settings lost, display off."""
        rng = random.Random(7)
        self.__init__(self.kind)
        self.ram = [[rng.randrange(256) for _ in range(132)] for _ in range(16)]

    def feed(self, control, values):
        self.writes += 1
        for v in values:
            self.data(v) if control == 0x40 else self.command(v)

    def command(self, v):
        if self.pending is not None:
            self.args.append(v)
            if len(self.args) == self.ARGS.get(self.pending, 1):
                if self.pending == 0x21:
                    self.col_range, self.col = tuple(self.args), self.args[0]
                elif self.pending == 0x22:
                    self.page_range, self.page = tuple(self.args), self.args[0]
                self.pending, self.args = None, []
            return
        if v in self.ARGS or (v == 0x20 and self.kind == "ssd1306"):
            self.pending = v
        elif v in (0xAE, 0xAF):
            self.on = v == 0xAF
            self.turned_off += v == 0xAE
        elif self.kind == "sh1106" and 0xB0 <= v <= 0xBF:
            self.page = v - 0xB0
        elif self.kind == "sh1106" and v < 0x10:
            self.col = (self.col & 0xF0) | v
        elif self.kind == "sh1106" and v < 0x20:
            self.col = (self.col & 0x0F) | (v & 0x0F) << 4

    def data(self, v):
        self.ram[self.page][self.col] = v
        if self.kind == "sh1106":
            self.col += 1
            return
        if self.col < self.col_range[1]:
            self.col += 1
        else:
            self.col = self.col_range[0]
            self.page = self.page + 1 if self.page < self.page_range[1] else self.page_range[0]

    def picture(self, width, height, first_column):
        image = Image.new("L", (width, height))
        for y in range(height):
            for x in range(width):
                if self.ram[y // 8][first_column + x] >> (y % 8) & 1:
                    image.putpixel((x, y), 255)
        return image


class I2cFunc(enum.IntFlag):
    I2C = 0x1
    SMBUS_QUICK = 0x00010000
    SMBUS_WRITE_BYTE_DATA = 0x00100000
    SMBUS_WRITE_I2C_BLOCK = 0x08000000


BC250_BUSES = [(0, "AMDGPU DM i2c hw bus 0"), (1, "AMDGPU DM i2c hw bus 1"), (2, "AMDGPU DM aux hw bus 0"),
               (3, "AMDGPU DM aux hw bus 1"), (4, "SMBus PIIX4 adapter port 0 at 0b00"),
               (5, "SMBus PIIX4 adapter port 2 at 0b00"), (6, "SMBus PIIX4 adapter port 3 at 0b00"),
               (7, "SMBus PIIX4 adapter port 4 at 0b00"), (8, "SMBus PIIX4 adapter port 1 at 0b20")]


def fake_smbus2(controller, funcs, reads=True, busy_every=0, display_bus=None, timeout_buses=()):
    """A bus with the display at 0x3C; reads=False: one that only answers writes, as many modules do;
    busy_every=N: every Nth transfer is refused with EBUSY, as the BC-250's firmware does while it holds the bus;
    display_bus: the display is on that bus only (default: any); timeout_buses: transfers there hang and time out,
    as on the BC-250's unconnected SMBus ports. module.touched lists the bus of every transfer."""
    calls = [0]
    touched = []

    class SMBus:
        def __init__(self, bus):
            self.funcs = funcs
            self.bus = bus

        def _transfer(self):
            touched.append(self.bus)
            if self.bus in timeout_buses:
                raise OSError(errno.ETIMEDOUT, "Connection timed out")
            calls[0] += 1
            if busy_every and calls[0] % busy_every == 0:
                raise OSError(errno.EBUSY, "Device or resource busy")
            if display_bus is not None and self.bus != display_bus:
                raise OSError(errno.ENXIO, "No such device or address")

        def read_byte(self, address):
            self._transfer()
            if address != 0x3C or not reads:
                raise OSError(121, "Remote I/O error")
            return 0x03 if controller.on else 0x43  # SSD1306 status: bit 6 set while the display is off

        def write_quick(self, address):
            self._transfer()
            if address != 0x3C:
                raise OSError(121, "Remote I/O error")

        def write_byte_data(self, address, register, value):
            self._transfer()
            controller.feed(register, [value])

        def write_i2c_block_data(self, address, register, values):
            self._transfer()
            assert len(values) <= 32
            controller.feed(register, values)

        def i2c_rdwr(self, *messages):
            self._transfer()
            for m in messages:
                controller.feed(m[0], m[1:])

        def close(self):
            pass

    module = types.ModuleType("smbus2")
    module.SMBus, module.I2cFunc, module.touched = SMBus, I2cFunc, touched
    module.i2c_msg = types.SimpleNamespace(write=lambda address, data: list(data))
    return module


class Fixture(unittest.TestCase):
    def setUp(self):
        self._smbus2 = sys.modules.get("smbus2")
        self._config = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()  # where the display was found: not in the real ~/.config
        self._buses = displays.buses

    def tearDown(self):
        displays.buses = self._buses
        if self._config is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._config
        if self._smbus2 is None:
            sys.modules.pop("smbus2", None)
        else:
            sys.modules["smbus2"] = self._smbus2

    def use_bus(self, kind, funcs, reads=True, busy_every=0, **layout):
        controller = Controller(kind)
        sys.modules["smbus2"] = fake_smbus2(controller, funcs, reads, busy_every, **layout)
        return controller


# --- tests --------------------------------------------------------------------------------------------------------------

class Layouts(unittest.TestCase):
    def test_every_size_and_page(self):
        for (driver, width, height), theme in ((size, theme) for size in SIZES for theme in screens.THEMES):
            for stats in (SAMPLE, Stats()):
                s = screens.Screens(width, height, list(screens.PAGES), theme=theme)
                for _ in range(3):
                    s.record(stats)
                for i, (tiles, lines) in enumerate(s.screens(stats)):
                    with self.subTest(size=f"{width}x{height}", theme=theme, screen=i, empty=stats == Stats()):
                        for kind, (x, y, w, h) in tiles:
                            self.assertTrue(x >= 0 and y >= 0 and x + w <= width and y + h <= height,
                                            f"{kind} {(x, y, w, h)} is off the screen")
                            self.assertGreaterEqual(min(w, h), 7, f"{kind} is too small for any text")
                        for a, (kind_a, (ax, ay, aw, ah)) in enumerate(tiles):
                            for kind_b, (bx, by, bw, bh) in tiles[a + 1:]:
                                self.assertFalse(ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah,
                                                 f"{kind_a} overlaps {kind_b}")
                        picture = s.render(stats, i)
                        self.assertEqual(picture.size, (width, height))
                        self.assertIsNotNone(picture.getbbox(), "nothing drawn")

    def test_values_are_as_big_as_they_fit(self):
        image = Image.new("L", (63, 48))
        c = screens.content("cpu", SAMPLE, 90)
        screens.tile(image, (0, 0, 63, 48), c)
        # "41°" at 3x is 21 pixels high: the value's rows hold that many lit rows' span
        rows = [y for y in range(15, 40) if any(image.getpixel((x, y)) for x in range(63))]
        self.assertEqual(rows[-1] - rows[0] + 1, 21)

    def test_hot_values_are_inverted(self):
        image = Image.new("L", (63, 48))
        screens.tile(image, (0, 0, 63, 48), screens.content("cpu", Stats(cpu_temp=95), 90))
        self.assertEqual(image.getpixel((62, 47)), 255)

    def test_every_character_used_is_in_the_font(self):
        for kind in ("cpu", "gpu", "nvme", "fan", "power", "vrm", "ram"):
            for theme, stats in ((t, st) for t in screens.THEMES for st in (SAMPLE, Stats(cpu_temp=95, gpu_temp=95))):
                c = screens.content(kind, stats, 90, theme)
                for part in (c.label, c.value, c.right, c.sub):
                    self.assertTrue(set(part.upper()) <= set(screens.GLYPHS), f"{kind} {theme}: {part!r}")
        self.assertTrue(set(screens.footer_line(SAMPLE, 500)) <= set(screens.GLYPHS))
        self.assertTrue(all(len(rows) == screens.CAP and len(set(map(len, rows))) == 1
                            for rows in screens.GLYPHS.values()))

    def test_halloween(self):
        """Pumpkins on CPU and GPU, BOO! on a hot tile, and a start-up animation that fits every size."""
        c = screens.content("cpu", Stats(cpu_temp=95, cpu_load=50), 90, "halloween")
        self.assertEqual((c.label, c.right, c.hot), ("🎃 CPU", "BOO!", True))
        self.assertEqual(screens.content("cpu", SAMPLE, 90, "classic").label, "CPU")
        for driver, width, height in SIZES:
            frames = screens.Screens(width, height, theme="halloween").splash_frames()
            self.assertGreater(len(frames), 5)
            self.assertLess(sum(seconds for _, seconds in frames), 4)
            for picture, _ in frames:
                self.assertEqual(picture.size, (width, height))
                self.assertIsNotNone(picture.getbbox())
        with self.assertRaises(ValueError):
            screens.Screens(128, 64, theme="spooky")

    def test_digits_share_one_width(self):
        self.assertEqual(len({screens.text_width(d) for d in "0123456789"}), 1)


class Sensors(unittest.TestCase):
    def test_fake_bc250(self):
        root = tempfile.mkdtemp()

        def put(path, value):
            path = os.path.join(root, path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(f"{value}\n")
        put("sys/class/hwmon/hwmon0/name", "k10temp")
        put("sys/class/hwmon/hwmon0/temp1_input", 52500)
        put("sys/class/hwmon/hwmon1/name", "amdgpu")
        put("sys/class/hwmon/hwmon1/temp1_input", 48000)
        put("sys/class/hwmon/hwmon1/freq1_input", 1500000000)
        put("sys/class/hwmon/hwmon1/power1_average", 95000000)
        put("sys/class/hwmon/hwmon2/name", "nvme")
        put("sys/class/hwmon/hwmon2/temp1_input", 39850)
        put("sys/class/hwmon/hwmon3/name", "nct6687")
        put("sys/class/hwmon/hwmon3/fan1_input", 0)
        put("sys/class/hwmon/hwmon3/fan2_input", 1210)
        put("sys/class/hwmon/hwmon3/temp3_label", "VRM MOS")
        put("sys/class/hwmon/hwmon3/temp3_input", 61000)
        for cpu, khz in enumerate((3000000, 4000000)):
            put(f"sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq", khz)
        put("proc/meminfo", "MemTotal:       16000000 kB\nMemAvailable:    4000000 kB")
        put("proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0")
        put("proc/42/fdinfo/7", "drm-client-id:\t5\ndrm-engine-gfx:\t1000 ns")
        old = sensors.SYS, sensors.PROC
        sensors.SYS, sensors.PROC = os.path.join(root, "sys"), os.path.join(root, "proc")
        try:
            reader = sensors.Sensors()
            first = reader.read()
            put("proc/stat", "cpu  200 0 200 1400 0 0 0 0 0 0")  # 200 busy of 800 ticks
            time.sleep(0.05)  # Windows' clock ticks every ~15 ms: two reads in one tick measure no time
            second = reader.read()
        finally:
            sensors.SYS, sensors.PROC = old
        self.assertEqual((first.cpu_temp, first.gpu_temp, first.nvme_temp), (52.5, 48.0, 39.85))
        self.assertEqual((first.cpu_ghz, first.gpu_mhz, first.gpu_watts), (3.5, 1500.0, 95.0))
        self.assertEqual((first.fan_rpm, first.vrm_temp, first.ram_pct), (1210.0, 61.0, 75.0))
        self.assertIsNone(first.cpu_load)  # needs two readings
        self.assertAlmostEqual(second.cpu_load, 25.0)
        self.assertEqual(second.gpu_load, 0.0)  # the client's busy time didn't grow

    def test_nothing_there(self):
        old = sensors.SYS, sensors.PROC
        sensors.SYS = sensors.PROC = tempfile.mkdtemp()
        try:
            self.assertEqual(sensors.Sensors().read(), Stats())
        finally:
            sensors.SYS, sensors.PROC = old


class Panel(Fixture):
    def frames(self, width, height, n=12):
        """Pictures with small changes, like a stats screen, and a few big ones."""
        rng = random.Random(width * height)
        s = screens.Screens(width, height, list(screens.PAGES))
        for i in range(n):
            stats = Stats(**{k: (rng.uniform(0, 99) if v is not None else None) for k, v in SAMPLE.__dict__.items()})
            s.record(stats)
            yield s.render(stats, i // 4)

    def check(self, kind, driver, width, height, funcs, rotate=0, busy_every=0):
        controller = self.use_bus(kind, funcs, busy_every=busy_every)
        device = displays.open_device(driver, width, height, 1, 0x3C, rotate)
        device.persist = True
        panel = displays.Panel(device)
        first_column = device._colstart if kind == "ssd1306" else device._page_address_offset
        sent = []
        for picture in self.frames(width, height):
            before = panel.sent
            panel.show(picture)
            sent.append(panel.sent - before)
            expected = device.preprocess(picture.convert("1", dither=Image.Dither.NONE)).convert("L")
            got = controller.picture(device._w, device._h, first_column)
            self.assertIsNone(Image.frombytes("L", got.size, bytes(a ^ b for a, b in zip(
                got.tobytes(), expected.tobytes()))).getbbox(), f"{kind} {width}x{height} shows something else")
        full = width * height // 8
        self.assertEqual(sent[0], full)
        self.assertLess(sum(sent[1:]), full * (len(sent) - 1), "every update sent the whole frame")
        if busy_every:
            self.assertGreater(device._serial_interface.retried[0], 0)
        return device._serial_interface.mode

    def test_partial_updates(self):
        every = I2cFunc.I2C | I2cFunc.SMBUS_WRITE_I2C_BLOCK | I2cFunc.SMBUS_WRITE_BYTE_DATA
        for kind, driver, width, height in (("ssd1306", "ssd1306", 128, 64), ("ssd1306", "ssd1306", 64, 48),
                                            ("ssd1306", "ssd1306", 128, 32), ("ssd1306", "ssd1309", 128, 64),
                                            ("ssd1306", "ssd1315", 128, 64), ("sh1106", "sh1106", 128, 64)):
            for rotate in (0, 180):
                with self.subTest(driver=driver, size=f"{width}x{height}", rotate=rotate):
                    self.check(kind, driver, width, height, every, rotate)

    def test_every_link(self):
        """The AMD chipset SMBus (piix4) can only write byte by byte: that has to work as well as raw I2C."""
        for funcs, mode in ((I2cFunc.I2C | I2cFunc.SMBUS_WRITE_BYTE_DATA, "i2c"),
                            (I2cFunc.SMBUS_WRITE_I2C_BLOCK | I2cFunc.SMBUS_WRITE_BYTE_DATA, "block"),
                            (I2cFunc.SMBUS_WRITE_BYTE_DATA, "byte")):
            for kind in ("ssd1306", "sh1106"):
                with self.subTest(link=mode, controller=kind):
                    self.assertEqual(self.check(kind, kind, 128, 64, funcs), mode)

    def test_a_replugged_display_comes_back(self):
        """Unplugged and plugged back between two updates: writes still succeed, so only setting it up again brings
        it back. Displays with a status byte are checked with one read; that must not make a working display blink."""
        for kind in ("ssd1306", "sh1106"):
            for reads in (True, False):
                with self.subTest(controller=kind, status_byte=reads):
                    controller = self.use_bus(kind, I2cFunc.SMBUS_WRITE_BYTE_DATA, reads=reads)
                    device = displays.open_device(kind, 128, 64, 1, 0x3C)
                    device.persist = True
                    panel = displays.Panel(device)
                    panel.watch()
                    self.assertEqual(panel._status_works, reads)
                    frames = list(self.frames(128, 64, 3))
                    panel.show(frames[0])
                    writes = controller.writes
                    calls = 6 if not reads else 1
                    woke = [panel.keepalive() for _ in range(calls)]
                    self.assertEqual(woke, [False] * calls if reads else [False] * 5 + [True])
                    if reads:
                        self.assertEqual(controller.writes, writes, "checking a working display wrote to it")
                    self.assertEqual(controller.turned_off, 1, "the check turned a working display off")
                    controller.power_cycle()
                    panel.show(frames[1])  # only the changes: still dark
                    self.assertFalse(controller.on)
                    woke = [panel.keepalive(contrast=40) for _ in range(calls)]
                    self.assertTrue(woke[-1])
                    self.assertTrue(controller.on)
                    self.assertEqual(controller.turned_off, 0)
                    first_column = device._colstart if kind == "ssd1306" else device._page_address_offset
                    got = controller.picture(128, 64, first_column).tobytes()
                    self.assertEqual(got, frames[1].point(lambda v: 255 * (v >= 128)).tobytes())

    def test_little_traffic_on_the_shared_bus(self):
        """The SMBus is shared with the board's firmware: a stats update must take few transfers, not a whole frame
        (changes in opposite corners used to be merged into one rectangle: ~925 transfers a second)."""
        for kind, theme in (("ssd1306", "classic"), ("sh1106", "classic"), ("ssd1306", "halloween")):
            with self.subTest(controller=kind, theme=theme):
                controller = self.use_bus(kind, I2cFunc.SMBUS_WRITE_BYTE_DATA)
                device = displays.open_device(kind, 128, 64, 1, 0x3C)
                device.persist = True
                panel = displays.Panel(device)
                s = screens.Screens(128, 64, theme=theme)
                rng, stats = random.Random(1), SAMPLE
                panel.show(s.render(stats))
                controller.writes = 0
                for _ in range(30):  # half a minute of realistic drift
                    stats = Stats(**{**stats.__dict__,
                                     "cpu_load": max(0, min(100, stats.cpu_load + rng.uniform(-8, 8))),
                                     "gpu_load": max(0, min(100, stats.gpu_load + rng.uniform(-8, 8))),
                                     "cpu_temp": stats.cpu_temp + rng.choice([0, 0, 1, -1]),
                                     "cpu_ghz": rng.uniform(2.8, 3.5), "gpu_mhz": rng.choice([1500, 1873, 2000]),
                                     "gpu_watts": rng.uniform(90, 160)})
                    s.record(stats)
                    panel.show(s.render(stats))
                self.assertLess(controller.writes / 30, 200, "transfers a second")

    def test_a_busy_bus(self):
        """The firmware takes the SMBus now and then: those transfers are tried again, and the picture stays exact."""
        for funcs, every in ((I2cFunc.SMBUS_WRITE_BYTE_DATA, 97), (I2cFunc.I2C, 3)):  # i2c: few, big transfers
            for kind in ("ssd1306", "sh1106"):
                with self.subTest(link=funcs, controller=kind):
                    self.check(kind, kind, 128, 64, funcs, busy_every=every)

    def test_errors_that_are_not_busy_are_not_retried(self):
        self.use_bus("ssd1306", I2cFunc.SMBUS_WRITE_BYTE_DATA)
        calls = []

        def unplugged():
            calls.append(1)
            raise OSError(errno.ENXIO, "No such device or address")
        with self.assertRaises(OSError):
            displays.retry(unplugged)
        self.assertEqual(len(calls), 1)

    def test_a_bus_busy_for_too_long_gives_up(self):
        old = displays.RETRY_FOR
        displays.RETRY_FOR = 0.05
        try:
            with self.assertRaises(OSError) as raised:
                displays.retry(lambda: (_ for _ in ()).throw(OSError(errno.EBUSY, "busy")))
            self.assertEqual(raised.exception.errno, errno.EBUSY)
        finally:
            displays.RETRY_FOR = old

    def test_no_usable_write(self):
        self.use_bus("ssd1306", I2cFunc(0))
        with self.assertRaises(OSError):
            displays.Link(1, 0x3C)

    def test_other_drivers_take_whole_frames(self):
        device = displays.open_device("ssd1327", 128, 128, None, None)
        device.persist = True
        panel = displays.Panel(device)
        for picture in self.frames(128, 128, 3):
            panel.show(picture)
        panel.show(picture)  # unchanged: nothing sent
        self.assertEqual(panel.sent, 3 * 128 * 128 * 3)


class OneAtATime(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "file locks are for Linux (previews and tests only on Windows)")
    def test_a_second_program_is_refused(self):
        from bc250_oled.__main__ import InUse, claim_display, main
        first = claim_display()
        try:
            with self.assertRaises(InUse):
                claim_display()
            self.assertEqual(main(["detect"]), 1)  # says to stop the service, probes nothing
        finally:
            first.close()
        claim_display().close()


class Detect(Fixture):
    FUNCS = I2cFunc.SMBUS_QUICK | I2cFunc.SMBUS_WRITE_BYTE_DATA

    def setUp(self):
        super().setUp()
        displays.buses = lambda: displays.search_order(BC250_BUSES)

    def test_modules_that_ignore_reads_are_found(self):
        for reads, how in ((True, "read"), (False, "quick")):
            self.use_bus("ssd1306", self.FUNCS, reads)
            self.assertEqual(displays.probe(3, 0x3C), how)
            self.assertIsNone(displays.probe(3, 0x3D))
        self.use_bus("ssd1306", I2cFunc.SMBUS_WRITE_BYTE_DATA, reads=False)  # no quick write either
        self.assertIsNone(displays.probe(3, 0x3C))

    def test_the_search_stops_at_the_display(self):
        """The BC-250: display on the chipset SMBus port 0 (i2c-4), ports 2 and 3 hang. Searched first, port 0 is
        the only bus touched."""
        self.use_bus("ssd1306", self.FUNCS, display_bus=4, timeout_buses={5, 6})
        result = displays.find()
        self.assertEqual(displays.displays_in(result), [(4, 0x3C)])
        self.assertEqual(set(sys.modules["smbus2"].touched), {4})
        self.assertTrue(all(answers == "skipped" for bus, (name, answers) in result.items() if bus != 4))

    def test_hung_buses_are_left_alone(self):
        """No display: each bus that hangs gets one transfer, never retried (each hangs for a quarter of a second,
        and on the chipset SMBus a hung port can disturb the others)."""
        self.use_bus("ssd1306", self.FUNCS, display_bus=99, timeout_buses={5, 6})
        result = displays.find()
        touched = sys.modules["smbus2"].touched
        self.assertEqual((touched.count(5), touched.count(6)), (1, 1))
        self.assertEqual((result[5][1], result[6][1]), ("timeout", "timeout"))
        self.assertEqual(displays.displays_in(result), [])

        def hangs():
            raise OSError(errno.ETIMEDOUT, "Connection timed out")
        calls = []
        with self.assertRaises(OSError):
            displays.retry(lambda: calls.append(1) or hangs())
        self.assertEqual(len(calls), 1)

    def test_the_service_goes_straight_to_the_display(self):
        from bc250_oled.__main__ import locate, main
        self.use_bus("ssd1306", self.FUNCS, display_bus=4, timeout_buses={5, 6})
        self.assertEqual(main(["detect"]), 0)
        self.assertEqual(displays.remembered(), (4, 0x3C))
        # next boot, the bus numbers changed: the adapter's name still finds it, with nothing probed
        displays.buses = lambda: displays.search_order([(n + 1, name) for n, name in BC250_BUSES])
        sys.modules["smbus2"].touched.clear()
        self.assertEqual(locate(types.SimpleNamespace(bus=None, address=None)), (5, 0x3C))
        self.assertEqual(sys.modules["smbus2"].touched, [])


class Run(Fixture):
    def test_run_draws_the_stats(self):
        """Through a byte-at-a-time bus that is busy now and then, like the BC-250's."""
        from bc250_oled.__main__ import main
        controller = self.use_bus("ssd1306", I2cFunc.SMBUS_WRITE_BYTE_DATA, busy_every=50)
        old = sensors.SYS, sensors.PROC
        sensors.SYS = sensors.PROC = tempfile.mkdtemp()  # no readings: dashes
        try:
            result = []
            runner = threading.Thread(target=lambda: result.append(main(
                ["run", "--bus", "3", "--no-splash", "--frames", "2", "--interval", "0.05"])), daemon=True)
            runner.start()
            runner.join(30)
            self.assertEqual(result, [0], "the display never got its pictures (it keeps reconnecting)")
        finally:
            sensors.SYS, sensors.PROC = old
        expected = screens.Screens(128, 64).render(Stats())
        # the display is turned off at the end, but its memory still holds the last picture
        self.assertEqual(controller.picture(128, 64, 0).tobytes(), expected.point(lambda v: 255 * (v >= 128)).tobytes())

    def test_preview_and_displays(self):
        from bc250_oled.__main__ import main
        out = os.path.join(tempfile.mkdtemp(), "p.png")
        for preset in displays.PRESETS:
            self.assertEqual(main(["preview", "--preset", preset, "--out", out, "--zoom", "2"]), 0)
            self.assertTrue(os.path.getsize(out) > 0)
        self.assertEqual(main(["displays"]), 0)


if __name__ == "__main__":
    unittest.main()
