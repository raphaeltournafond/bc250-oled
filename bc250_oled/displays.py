"""The displays this works with, the I2C link to them, and a search for one that sends no data.

Safety: the BC-250's SMBus also reaches chips that matter (Super I/O, power controllers on some buses). Nothing here
writes anywhere but the display's own address, and detection only knocks at 0x3C and 0x3D, the two addresses every
SSD1306/SH1106-class module uses: a one-byte read, or, for the many modules that ignore reads, the address alone with
no data (an SMBus "quick write", what i2cdetect does at these addresses)."""
import glob
import os
from dataclasses import dataclass

ADDRESSES = (0x3C, 0x3D)  # silkscreened "0x78"/"0x7A" on many modules (the same addresses, shifted left by one)


@dataclass(frozen=True)
class Preset:
    driver: str  # a luma.oled device class
    width: int
    height: int
    note: str


PRESETS = {
    "0.49": Preset("ssd1306", 64, 32, '0.49" 64x32, SSD1306'),
    "0.66": Preset("ssd1306", 64, 48, '0.66" 64x48, SSD1306 (Wemos D1 mini shield)'),
    "0.91": Preset("ssd1306", 128, 32, '0.91" 128x32, SSD1306'),
    "0.96": Preset("ssd1306", 128, 64, '0.96" 128x64, SSD1306 or SSD1315 (the most common one)'),
    "1.3": Preset("sh1106", 128, 64, '1.3" 128x64, SH1106 (if the picture is shifted 2 px, it is an SSD1306: '
                                     'use --driver ssd1306)'),
    "1.5": Preset("ssd1327", 128, 128, '1.5" 128x128 greyscale, SSD1327'),
    "1.5-sh1107": Preset("sh1107", 128, 128, '1.5" 128x128, SH1107'),
    "1.54": Preset("ssd1309", 128, 64, '1.54" 128x64, SSD1309'),
    "2.42": Preset("ssd1309", 128, 64, '2.42" 128x64, SSD1309'),
}
DEFAULT_PRESET = "0.96"
# every monochrome/greyscale I2C driver luma.oled has, for --driver with --size (luma checks the size is one it knows)
DRIVERS = ("ssd1306", "ssd1315", "ssd1309", "ssd1305", "ssd1316", "sh1106", "sh1107", "ch1115", "ssd1327", "ssd1362",
           "ssd1322", "ssd1325")
GREYSCALE = ("ssd1327", "ssd1362", "ssd1322", "ssd1325")


def resolve(preset=None, driver=None, size=None):
    """(driver, width, height) from a preset name, overridden by --driver and --size WxH."""
    base = PRESETS[preset or DEFAULT_PRESET]
    width, height = base.width, base.height
    if size:
        width, height = (int(v) for v in size.lower().split("x"))
    return driver or base.driver, width, height


# --- the link -----------------------------------------------------------------------------------------------------------

class Link:
    """A luma serial interface (command/data/cleanup) that works on any Linux I2C adapter.

    luma's own `i2c` needs raw I2C transfers or I2C block writes, which the AMD chipset SMBus (the piix4 driver, the
    bus most BC-250 headers lead to) cannot do. This picks the best the adapter offers:
      i2c   raw transfers, a whole frame at once (designware and most other I2C controllers)
      block SMBus I2C-block writes, 32 bytes at a time
      byte  one byte per SMBus write: slow (about a third of a second for a full 128x64 frame on piix4), fine for a
            stats screen that only redraws what changed"""

    MODES = ("i2c", "block", "byte")

    def __init__(self, bus, address, mode="auto"):
        from smbus2 import I2cFunc, SMBus, i2c_msg
        self._msg = i2c_msg
        self.bus, self.address = bus, address
        self._smbus = SMBus(bus)
        funcs = self._smbus.funcs
        available = [m for m, flag in (("i2c", I2cFunc.I2C), ("block", I2cFunc.SMBUS_WRITE_I2C_BLOCK),
                                       ("byte", I2cFunc.SMBUS_WRITE_BYTE_DATA)) if funcs & flag]
        if mode == "auto":
            if not available:
                raise OSError(f"I2C bus {bus} can't do any kind of write this program knows")
            mode = available[0]
        elif mode not in available:
            raise OSError(f"I2C bus {bus} can't do '{mode}' writes (it can: {', '.join(available) or 'none'})")
        self.mode = mode
        self.recording = None  # a list while the controller is being set up: see replay()
        self.setup = []

    def _send(self, control, values):
        values = list(values)
        if self.mode == "i2c":
            for i in range(0, len(values), 4096):
                self._smbus.i2c_rdwr(self._msg.write(self.address, [control] + values[i:i + 4096]))
        elif self.mode == "block":
            for i in range(0, len(values), 32):
                self._smbus.write_i2c_block_data(self.address, control, values[i:i + 32])
        else:
            # the controllers keep parsing a multi-byte command across writes, as Adafruit's drivers rely on
            for value in values:
                self._smbus.write_byte_data(self.address, control, value)

    def command(self, *cmd):
        if self.recording is not None:
            self.recording.append(cmd)
        self._send(0x00, cmd)

    def replay(self):
        """Sends the setup commands again, which wakes a display that was unplugged and plugged back (it comes back
        blank and off, while writes still succeed). Leaves out the "display off" that starts the setup, so a display
        that never lost power doesn't blink."""
        for i, cmd in enumerate(self.setup):
            if i == 0 and cmd and cmd[0] == 0xAE:
                cmd = cmd[1:]
            if cmd:
                self._send(0x00, cmd)

    def data(self, data):
        self._send(0x40, data)

    def cleanup(self):
        self._smbus.close()


def open_device(driver, width, height, bus, address, rotate=0, mode="auto"):
    """A luma.oled device on the display; bus=None makes one that draws nowhere (previews and tests)."""
    import luma.oled.device as devices
    from luma.core.interface.serial import noop
    serial = noop() if bus is None else Link(bus, address, mode)
    try:
        if bus is not None:
            serial.recording = serial.setup
        device = getattr(devices, driver)(serial, width=width, height=height, rotate=rotate // 90 % 4)
        if bus is not None:
            serial.recording = None
        return device
    except Exception:
        serial.cleanup()
        raise


class Panel:
    """Shows greyscale ("L") pictures on a device, sending only the part that changed.

    luma resends the whole frame every time; on the byte-at-a-time SMBus link that is most of a second of a busy bus
    for each update. For the common controllers (SSD1306 family, SH1106) only the 8-pixel rows and columns that
    changed are sent; others go through luma as a whole frame."""

    def __init__(self, device):
        from luma.oled.device import sh1106, ssd1305, ssd1306
        self.device = device
        self._kind = ("ssd1306" if isinstance(device, ssd1306) and not isinstance(device, ssd1305)
                      else "sh1106" if isinstance(device, sh1106) else None)
        self._last = None  # what the display shows, in its own (unrotated) orientation
        self._picture = None
        self.sent = 0  # bytes of picture sent so far

    def wake(self, contrast=None):
        """Sets the display up again and sends the whole picture: see Link.replay()."""
        link = self.device._serial_interface
        if isinstance(link, Link):
            link.replay()
            if contrast is not None:
                self.device.contrast(contrast)
            self.device.show()
        if self._picture is not None:
            self.show(self._picture, full=True)

    def show(self, picture, full=False):
        self._picture = picture
        from PIL import Image, ImageChops
        if self.device.mode == "1":
            frame = picture.convert("1", dither=Image.Dither.NONE)
        else:
            frame = picture.convert(self.device.mode)
        if self._kind is None:
            if full or self._last is None or frame.tobytes() != self._last.tobytes():
                self.device.display(frame)
                self.sent += len(frame.tobytes())
                self._last = frame
            return
        native = self.device.preprocess(frame).convert("L")  # rotated the way the controller sees it
        box = (0, 0) + native.size if full or self._last is None else ImageChops.difference(self._last, native).getbbox()
        self._last = native
        if not box:
            return
        x0, y0, x1, y1 = box
        first, last = y0 // 8, (y1 - 1) // 8
        pixels = native.load()
        pages = [bytes(sum(1 << i for i in range(8) if pixels[x, page * 8 + i]) for x in range(x0, x1))
                 for page in range(first, last + 1)]
        d = self.device
        if self._kind == "ssd1306":  # horizontal addressing: one window, the bytes wrap from page to page
            d.command(0x21, d._colstart + x0, d._colstart + x1 - 1, 0x22, first, last)
            d.data(list(b"".join(pages)))
        else:  # page addressing: each page starts at its own column
            column = d._page_address_offset + x0
            for page, data in zip(range(first, last + 1), pages):
                d.command(0xB0 + page, column & 0x0F, 0x10 | column >> 4)
                d.data(list(data))
        self.sent += sum(len(p) for p in pages)


# --- read-only search -----------------------------------------------------------------------------------------------------

def buses():
    """[(number, adapter name)] of every I2C bus Linux exposes as /dev/i2c-N."""
    found = []
    for folder in glob.glob("/sys/bus/i2c/devices/i2c-*"):
        try:
            number = int(folder.rsplit("-", 1)[1])
        except ValueError:
            continue
        try:
            with open(os.path.join(folder, "name")) as f:
                name = f.read().strip()
        except OSError:
            name = "?"
        found.append((number, name))
    # the chipset SMBus first: that's where the BC-250's headers lead
    return sorted(found, key=lambda b: (not b[1].startswith("SMBus"), b[0]))


def probe(bus, address):
    """How something at `address` answered: "read" (a one-byte read: SSD1306s give their status byte), "quick" (only
    the address, no data), None if nothing did. Raises PermissionError if the bus can't be opened (run setup.sh)."""
    from smbus2 import I2cFunc, SMBus
    try:
        smbus = SMBus(bus)
    except PermissionError:
        raise
    except OSError:
        return None
    try:
        try:
            smbus.read_byte(address)
            return "read"
        except OSError:
            pass
        if smbus.funcs & I2cFunc.SMBUS_QUICK:
            try:
                smbus.write_quick(address)
                return "quick"
            except OSError:
                pass
        return None
    finally:
        smbus.close()


def find():
    """[(bus, address, adapter name)] of everything answering at a display address, and the buses that couldn't be
    opened."""
    found, denied = [], []
    for number, name in buses():
        for address in ADDRESSES:
            try:
                if probe(number, address):
                    found.append((number, address, name))
            except PermissionError:
                denied.append(number)
                break
    return found, denied
