"""No hardware needed: every size and page renders inside its tiles, the sensors read a fake sysfs, and what reaches an
emulated display controller (through every kind of bus write) is exactly the picture.
Run: python -m unittest discover -s tests -v"""
import enum
import os
import random
import sys
import tempfile
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


def fake_smbus2(controller, funcs, reads=True):
    """A bus with the display at 0x3C; reads=False: one that only answers writes, as many modules do."""
    class SMBus:
        def __init__(self, bus):
            self.funcs = funcs

        def read_byte(self, address):
            if address != 0x3C or not reads:
                raise OSError(121, "Remote I/O error")
            return 0x43

        def write_quick(self, address):
            if address != 0x3C:
                raise OSError(121, "Remote I/O error")

        def write_byte_data(self, address, register, value):
            controller.feed(register, [value])

        def write_i2c_block_data(self, address, register, values):
            assert len(values) <= 32
            controller.feed(register, values)

        def i2c_rdwr(self, *messages):
            for m in messages:
                controller.feed(m[0], m[1:])

        def close(self):
            pass

    module = types.ModuleType("smbus2")
    module.SMBus, module.I2cFunc = SMBus, I2cFunc
    module.i2c_msg = types.SimpleNamespace(write=lambda address, data: list(data))
    return module


class Fixture(unittest.TestCase):
    def setUp(self):
        self._smbus2 = sys.modules.get("smbus2")

    def tearDown(self):
        if self._smbus2 is None:
            sys.modules.pop("smbus2", None)
        else:
            sys.modules["smbus2"] = self._smbus2

    def use_bus(self, kind, funcs, reads=True):
        controller = Controller(kind)
        sys.modules["smbus2"] = fake_smbus2(controller, funcs, reads)
        return controller


# --- tests --------------------------------------------------------------------------------------------------------------

class Layouts(unittest.TestCase):
    def test_every_size_and_page(self):
        for driver, width, height in SIZES:
            for stats in (SAMPLE, Stats()):
                s = screens.Screens(width, height, list(screens.PAGES))
                for _ in range(3):
                    s.record(stats)
                for i, (tiles, lines) in enumerate(s.screens(stats)):
                    with self.subTest(size=f"{width}x{height}", screen=i, empty=stats == Stats()):
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
            c = screens.content(kind, SAMPLE, 90)
            for part in (c.label, c.value, c.right, c.sub):
                self.assertTrue(set(part.upper()) <= set(screens.GLYPHS), f"{kind}: {part!r}")
        self.assertTrue(set(screens.footer_line(SAMPLE, 500)) <= set(screens.GLYPHS))
        self.assertTrue(all(len(rows) == screens.CAP and len(set(map(len, rows))) == 1
                            for rows in screens.GLYPHS.values()))

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

    def check(self, kind, driver, width, height, funcs, rotate=0):
        controller = self.use_bus(kind, funcs)
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
        it back, and that must not make a display that never lost power blink."""
        for kind in ("ssd1306", "sh1106"):
            with self.subTest(controller=kind):
                controller = self.use_bus(kind, I2cFunc.SMBUS_WRITE_BYTE_DATA)
                device = displays.open_device(kind, 128, 64, 1, 0x3C)
                device.persist = True
                panel = displays.Panel(device)
                frames = list(self.frames(128, 64, 3))
                panel.show(frames[0])
                panel.wake()
                self.assertEqual(controller.turned_off, 1, "waking a working display turned it off")
                controller.power_cycle()
                panel.show(frames[1])  # only the changes: still dark
                self.assertFalse(controller.on)
                panel.wake(contrast=40)
                self.assertTrue(controller.on)
                self.assertEqual(controller.turned_off, 0)
                first_column = device._colstart if kind == "ssd1306" else device._page_address_offset
                got = controller.picture(128, 64, first_column).tobytes()
                self.assertEqual(got, frames[1].point(lambda v: 255 * (v >= 128)).tobytes())

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


class Detect(Fixture):
    def test_modules_that_ignore_reads_are_found(self):
        old = displays.buses
        displays.buses = lambda: [(0, "AMDGPU DM i2c hw bus 0"), (3, "SMBus PIIX4 adapter port 0 at 0b00")]
        try:
            for reads, how in ((True, "read"), (False, "quick")):
                self.use_bus("ssd1306", I2cFunc.SMBUS_QUICK | I2cFunc.SMBUS_WRITE_BYTE_DATA, reads)
                self.assertEqual(displays.probe(3, 0x3C), how)
                self.assertIsNone(displays.probe(3, 0x3D))
            self.use_bus("ssd1306", I2cFunc.SMBUS_WRITE_BYTE_DATA, reads=False)  # no quick write either
            self.assertIsNone(displays.probe(3, 0x3C))
        finally:
            displays.buses = old


class Run(Fixture):
    def test_run_draws_the_stats(self):
        from bc250_oled.__main__ import main
        controller = self.use_bus("ssd1306", I2cFunc.SMBUS_WRITE_BYTE_DATA)
        old = sensors.SYS, sensors.PROC
        sensors.SYS = sensors.PROC = tempfile.mkdtemp()  # no readings: dashes
        try:
            self.assertEqual(main(["run", "--bus", "3", "--no-splash", "--frames", "2", "--interval", "0.05"]), 0)
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
