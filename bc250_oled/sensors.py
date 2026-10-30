"""Readings for the display, read-only from sysfs and procfs: nothing here needs root or writes anywhere.

Every value is None when its source is missing, and the screen shows "--" for it."""
import glob
import os
import re
import time
from dataclasses import dataclass, fields
from typing import Optional

SYS = os.environ.get("BC250_OLED_SYS", "/sys")  # tests point these at a fake tree
PROC = os.environ.get("BC250_OLED_PROC", "/proc")


@dataclass
class Stats:
    cpu_temp: Optional[float] = None  # °C (k10temp Tctl)
    cpu_ghz: Optional[float] = None
    cpu_load: Optional[float] = None  # %
    gpu_temp: Optional[float] = None  # °C (amdgpu edge)
    gpu_mhz: Optional[float] = None
    gpu_load: Optional[float] = None  # %
    gpu_watts: Optional[float] = None  # the whole APU, as amdgpu reports it
    fan_rpm: Optional[float] = None  # needs the Super I/O driver (setup.sh loads it)
    vrm_temp: Optional[float] = None  # idem, with nct6687 only
    nvme_temp: Optional[float] = None  # °C
    ram_pct: Optional[float] = None


SAMPLE = Stats(cpu_temp=41, cpu_ghz=3.49, cpu_load=37, gpu_temp=39, gpu_mhz=1873, gpu_load=12, gpu_watts=148,
               fan_rpm=929, vrm_temp=46, nvme_temp=41, ram_pct=37)  # previews and tests


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _number(path, divide=1.0):
    value = _read(path)
    try:
        return float(value) / divide
    except (TypeError, ValueError):
        return None


def hwmon(*names):
    """The first hwmon folder whose driver name is one of `names`."""
    for folder in sorted(glob.glob(os.path.join(SYS, "class", "hwmon", "hwmon*"))):
        if _read(os.path.join(folder, "name")) in names:
            return folder
    return None


class Sensors:
    def __init__(self):
        self._cpu_previous = None
        self._gpu_previous = None

    def read(self) -> Stats:
        stats = Stats()
        for reader in (self._cpu, self._gpu, self._board, self._nvme, self._ram):
            try:
                reader(stats)
            except Exception:  # one broken source must never stop the display
                pass
        return stats

    def _cpu(self, s):
        k10 = hwmon("k10temp")
        if k10:
            s.cpu_temp = _number(os.path.join(k10, "temp1_input"), 1000)
        khz = [_number(p) for p in glob.glob(os.path.join(SYS, "devices", "system", "cpu", "cpu[0-9]*",
                                                         "cpufreq", "scaling_cur_freq"))]
        khz = [v for v in khz if v]
        if khz:
            s.cpu_ghz = sum(khz) / len(khz) / 1e6
        line = (_read(os.path.join(PROC, "stat")) or "").splitlines()[:1]
        if line and line[0].startswith("cpu "):
            ticks = [int(v) for v in line[0].split()[1:]]
            idle, total = ticks[3] + (ticks[4] if len(ticks) > 4 else 0), sum(ticks)
            if self._cpu_previous and total > self._cpu_previous[1]:
                busy = 1 - (idle - self._cpu_previous[0]) / (total - self._cpu_previous[1])
                s.cpu_load = max(0.0, min(100.0, busy * 100))
            self._cpu_previous = (idle, total)

    def _gpu(self, s):
        amd = hwmon("amdgpu")
        if amd:
            s.gpu_temp = _number(os.path.join(amd, "temp1_input"), 1000)
            s.gpu_mhz = _number(os.path.join(amd, "freq1_input"), 1e6)
            s.gpu_watts = (_number(os.path.join(amd, "power1_average"), 1e6)
                           or _number(os.path.join(amd, "power1_input"), 1e6))
            busy = _number(os.path.join(amd, "device", "gpu_busy_percent"))
            s.gpu_load = busy if busy is not None else self._gpu_load_from_clients()

    def _gpu_load_from_clients(self):
        """The BC-250's GPU has no gpu_busy_percent: add up the graphics engine time of every program using it
        (/proc/*/fdinfo, as nvtop does) between two readings. Only the current user's programs are visible, so run
        the display as the user who plays (setup.sh does)."""
        clients, now = {}, time.monotonic()
        for fdinfo in glob.glob(os.path.join(PROC, "[0-9]*", "fdinfo", "*")):
            text = _read(fdinfo)
            if not text or "drm-engine-gfx" not in text:
                continue
            client = re.search(r"drm-client-id:\s*(\d+)", text)
            busy = re.search(r"drm-engine-gfx:\s*(\d+)\s*ns", text)
            if client and busy:
                clients[client.group(1)] = int(busy.group(1))
        previous, self._gpu_previous = self._gpu_previous, (now, clients)
        if not previous:
            return None
        elapsed = (now - previous[0]) * 1e9
        busy = sum(max(0, ns - previous[1][c]) for c, ns in clients.items() if c in previous[1])
        return max(0.0, min(100.0, busy * 100 / elapsed)) if elapsed > 0 else None

    def _board(self, s):
        """Fan and VRM from the board's Super I/O (Nuvoton NCT6686D): nct6683 (in the kernel, loaded with
        force=1) or nct6687 (out of tree). Both name themselves after the chip."""
        chip = hwmon("nct6686", "nct6687", "nct6683")
        if not chip:
            return
        rpms = [_number(p) for p in glob.glob(os.path.join(chip, "fan*_input"))]
        rpms = [v for v in rpms if v]
        s.fan_rpm = max(rpms) if rpms else None
        for label in glob.glob(os.path.join(chip, "temp*_label")):
            if "VRM" in (_read(label) or "").upper():
                s.vrm_temp = _number(label.replace("_label", "_input"), 1000)

    def _nvme(self, s):
        nvme = hwmon("nvme")
        if nvme:
            s.nvme_temp = _number(os.path.join(nvme, "temp1_input"), 1000)  # "Composite"

    def _ram(self, s):
        info = dict(re.findall(r"^(\w+):\s+(\d+)", _read(os.path.join(PROC, "meminfo")) or "", re.M))
        if "MemTotal" in info and "MemAvailable" in info:
            s.ram_pct = 100 * (1 - int(info["MemAvailable"]) / int(info["MemTotal"]))


def names():
    return [f.name for f in fields(Stats)]
