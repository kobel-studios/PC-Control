"""Cooling Control GUI - pump/radiator/case fan control via LibreHardwareMonitor.

Two groups:
  Liquid Cooling = pump + radiator fans (one slider, they ramp together)
  Case Fans      = intake/exhaust fans (own slider, gentler curve)
Auto mode follows CPU temperature. Safety: pump floor 40%, all 100% over 90C,
motherboard control restored on exit. Requires admin (self-elevates).
"""
import csv
import ctypes
import glob
import io
import json
import math
import re
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
from contextlib import contextmanager
import time
import traceback

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
from toggle_switch import ToggleSwitch

LHM_DIR = os.path.join(APP_DIR, "lhm")
CONFIG_PATH = os.path.join(APP_DIR, "cooling_config.json")

PUMP_FLOOR = 40
CASE_FLOOR = 25
CRIT_TEMP = 90
BASE_DUTY = 50.0
SENSOR_TIMEOUT = 5.0
GPU_APPLY_TEMP = 80.0
GPU_RESET_TEMP = 83.0
GPU_OC_FULL_TEMP = 65.0
CPU_OC_APPLY_TEMP = 80.0
CPU_OC_RESET_TEMP = 88.0
CPU_OC_MAX_RATIO = 45
CPU_OC_DEFAULT_RATIO = 40


def target_duty(previous, temp, elapsed, load=0, gpu_temp=None, gpu_load=0):
    if temp is None or not math.isfinite(temp) or not 0 <= temp < CRIT_TEMP:
        return 100.0
    wanted = curve_value([(40, 50), (50, 65), (60, 80), (70, 100)], temp)
    if gpu_temp is not None and math.isfinite(gpu_temp) and 0 <= gpu_temp <= 125:
        wanted = max(wanted, curve_value([(40, 50), (60, 60), (70, 80), (80, 100)], gpu_temp))
    for value in (load, gpu_load):
        if value is not None and math.isfinite(value):
            wanted = max(wanted, 50 + 0.35 * min(100, max(0, value)))
    elapsed = min(5.0, max(0.0, elapsed))
    change = min(2 * elapsed, max(-0.4 * elapsed, wanted - previous))
    return min(100.0, max(BASE_DUTY, previous + change))


def parse_gpu_telemetry(text):
    rows = list(csv.reader(text.strip().splitlines()))
    if len(rows) != 1 or len(rows[0]) != 8:
        raise RuntimeError("GPU monitoring requires one NVIDIA GPU with eight telemetry fields")
    values = [v.strip() for v in rows[0]]
    data = {"uuid": values[0], "name": values[1]}
    for key, raw in zip(("temp", "load", "fan", "core_clock", "memory_clock", "power"), values[2:]):
        try:
            value = float(raw)
            data[key] = value if math.isfinite(value) else None
        except ValueError:
            data[key] = None
    data["updated_at"] = time.monotonic()
    return data


def read_gpu_telemetry():
    executable = shutil.which("nvidia-smi")
    if not executable:
        raise RuntimeError("NVIDIA monitoring tool was not found")
    result = subprocess.run([executable,
        "--query-gpu=uuid,name,temperature.gpu,utilization.gpu,fan.speed,clocks.gr,clocks.mem,power.draw",
        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("NVIDIA monitoring query failed")
    return parse_gpu_telemetry(result.stdout)


def gpu_apply_problem(cpu, gpu, info, now):
    for data, key, limit, label in ((cpu, "cpu_temp", 80, "CPU"), (gpu, "temp", GPU_APPLY_TEMP, "GPU")):
        value = data.get(key)
        if now - data.get("updated_at", 0) > SENSOR_TIMEOUT or value is None or not math.isfinite(value) or value < 0:
            return f"Fresh {label} temperature required before applying an overclock"
        if value >= limit:
            return f"Let the {label} cool below {limit * 9 / 5 + 32:.0f} F before applying an overclock"
    if info.get("command"):
        return "GPU clock service is processing another command"
    if info.get("flags", 0) & 0x40000:
        return ("A custom GPU voltage/frequency curve is active; FPS Control never "
                "changes voltage - reset GPU settings to stock first")
    return None

ROLE_NAMES = ["Unused", "Liquid Cooling", "Case Fans"]


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_and_exit():
    params = subprocess.list2cmdline([os.path.abspath(__file__), *sys.argv[1:]])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    sys.exit(0)


def curve_value(curve, temp):
    if temp <= curve[0][0]:
        return curve[0][1]
    for (t0, v0), (t1, v1) in zip(curve, curve[1:]):
        if temp <= t1:
            if t1 == t0:
                return v1
            return v0 + (v1 - v0) * (temp - t0) / (t1 - t0)
    return curve[-1][1]


MACM_SIGNATURE = 0x4D41434D
MACM_FLUSH = 0x00AB0001
CORE_OFFSET = 47 * 4
MEMORY_OFFSET = 51 * 4


def macm_layout(data):
    if len(data) < 36:
        raise RuntimeError("GPU clock service header is incomplete")
    signature, version, header, count, entry, master, flags, stamp, command = struct.unpack_from("<9I", data)
    if signature != MACM_SIGNATURE or not 0x20000 <= version <= 0x20003:
        raise RuntimeError("GPU clock service is not initialized or has an unsupported version")
    if count != 1 or master != 0:
        raise RuntimeError("GPU tuning currently requires exactly one GPU")
    if not 36 <= header <= 4096 or not 220 <= entry <= 65536:
        raise RuntimeError("Invalid GPU clock service layout")
    return header, entry, command


def decode_macm(data):
    header, entry_size, command = macm_layout(data)
    if len(data) < header + entry_size:
        raise RuntimeError("GPU clock service entry is incomplete")
    flags = struct.unpack_from("<I", data, header)[0]
    if flags & 0x1800 != 0x1800:
        raise RuntimeError("GPU clock service does not expose both clock-offset controls")
    core = struct.unpack_from("<4i", data, header + CORE_OFFSET)
    memory = struct.unpack_from("<4i", data, header + MEMORY_OFFSET)
    for current, lower, upper, default in (core, memory):
        if not -10000000 <= lower <= default <= upper <= 10000000 or not lower <= current <= upper:
            raise RuntimeError("Invalid GPU clock-offset limits")
    return {"core": [v / 1000 for v in core], "memory": [v / 1000 for v in memory],
            "flags": flags, "command": command,
            "is_default": core[0] == core[3] and memory[0] == memory[3]}


class MacmMemory:
    def __init__(self, writable=False):
        self.writable = writable
        self.handle = None
        self.address = None
        self.mutex = None

    def __enter__(self):
        if sys.platform != "win32":
            raise RuntimeError("GPU clock service is only available on Windows")
        from ctypes import wintypes as wt
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenFileMappingW": ([wt.DWORD, wt.BOOL, wt.LPCWSTR], wt.HANDLE),
            "MapViewOfFile": ([wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t], ctypes.c_void_p),
            "UnmapViewOfFile": ([ctypes.c_void_p], wt.BOOL),
            "CloseHandle": ([wt.HANDLE], wt.BOOL),
            "CreateMutexW": ([ctypes.c_void_p, wt.BOOL, wt.LPCWSTR], wt.HANDLE),
            "WaitForSingleObject": ([wt.HANDLE, wt.DWORD], wt.DWORD),
            "ReleaseMutex": ([wt.HANDLE], wt.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        access = 6 if self.writable else 4
        try:
            self.handle = self.api.OpenFileMappingW(access, False, "MACMSharedMemory")
            if not self.handle:
                raise RuntimeError("GPU clock service is not running yet")
            self.mutex = self.api.CreateMutexW(None, False, "Global\\Access_MACMSharedMemory")
            if not self.mutex:
                raise ctypes.WinError(ctypes.get_last_error())
            self.address = self.api.MapViewOfFile(self.handle, access, 0, 0, 36)
            if not self.address:
                raise ctypes.WinError(ctypes.get_last_error())
            with self.lock():
                header, entry, _ = macm_layout(ctypes.string_at(self.address, 36))
            self.size = header + entry
            self.api.UnmapViewOfFile(self.address)
            self.address = None
            self.address = self.api.MapViewOfFile(self.handle, access, 0, 0, self.size)
            if not self.address:
                raise ctypes.WinError(ctypes.get_last_error())
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    @contextmanager
    def lock(self):
        result = self.api.WaitForSingleObject(self.mutex, 1000)
        if result not in (0, 0x80):
            raise RuntimeError("GPU clock service is busy")
        try:
            if result == 0x80:
                raise RuntimeError("GPU clock service mutex was abandoned; restart FPS Control")
            yield
        finally:
            self.api.ReleaseMutex(self.mutex)

    def read(self):
        return ctypes.string_at(self.address, self.size)

    def write_int(self, offset, value):
        if not self.writable or not 0 <= offset <= self.size - 4:
            raise RuntimeError("Invalid shared-memory write")
        encoded = struct.pack("<i", value)
        ctypes.memmove(self.address + offset, encoded, 4)

    def __exit__(self, *_):
        if self.address:
            self.api.UnmapViewOfFile(self.address)
            self.address = None
        for handle in (self.mutex, self.handle):
            if handle:
                self.api.CloseHandle(handle)
        self.mutex = self.handle = None


def notify_afterburner():
    if sys.platform != "win32":
        return
    from ctypes import wintypes as wt
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user.FindWindowW.restype = wt.HWND
    user.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
    user.RegisterWindowMessageW.restype = wt.UINT
    user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user.PostMessageW.restype = wt.BOOL
    window = user.FindWindowW(None, "MSI Afterburner ")
    message = user.RegisterWindowMessageW("MACMCmdNotification")
    if window and message:
        user.PostMessageW(window, message, 0, 0)


INTELMSR_BINS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pawnio_modules_024", "IntelMSR.bin"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "IntelMSR.bin"),
]
PAWNIOLIB_PATH = r"C:\Program Files\PawnIO\PawnIOLib.dll"
OC_MAILBOX_ADDR = 0x150
MSR_TURBO_RATIO_LIMIT = 0x1AD
MSR_RAPL_POWER_UNIT = 0x606
MSR_PKG_POWER_LIMIT = 0x610
OC_CMD_GET_CAPS = 0x01
OC_CMD_GET_VF = 0x10
OC_CMD_SET_VF = 0x11


class CpuOcBackend:
    """PawnIO OC-mailbox backend for the CPU ratio override.

    Verified on this i7-6800K: SET_VOLTAGE_FREQUENCY (0x11) carries
    MaxOcRatio in bits 7:0 of the data dword and data=0 clears the
    override. Voltage fields stay zero; readback via GET_VF (0x10).
    """

    def __init__(self, lib_path=None, blobs=None):
        self.lib_path = lib_path or PAWNIOLIB_PATH
        self.blobs = blobs or INTELMSR_BINS
        self.lib = None
        self.handle = None

    def connect(self):
        if sys.platform != "win32":
            raise RuntimeError("CPU overclocking is only available on Windows")
        from ctypes import wintypes as wt
        if not os.path.exists(self.lib_path):
            raise RuntimeError("PawnIOLib.dll not found; install PawnIO first")
        try:
            self.lib = ctypes.OleDLL(self.lib_path)
        except OSError as exc:
            raise RuntimeError(f"load PawnIOLib: {exc}")
        for name in ("pawnio_open", "pawnio_load", "pawnio_execute", "pawnio_close"):
            if not hasattr(self.lib, name):
                raise RuntimeError(f"PawnIOLib missing export {name}")
        self.lib.pawnio_open.argtypes = [ctypes.POINTER(wt.HANDLE)]
        self.lib.pawnio_open.restype = ctypes.c_long
        self.lib.pawnio_load.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t]
        self.lib.pawnio_load.restype = ctypes.c_long
        self.lib.pawnio_execute.argtypes = [wt.HANDLE, ctypes.c_char_p,
                                            ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
                                            ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
                                            ctypes.POINTER(ctypes.c_size_t)]
        self.lib.pawnio_execute.restype = ctypes.c_long
        self.lib.pawnio_close.argtypes = [wt.HANDLE]
        self.lib.pawnio_close.restype = ctypes.c_long
        handle = wt.HANDLE(0)
        try:
            hr = self.lib.pawnio_open(ctypes.byref(handle))
        except OSError as exc:
            raise RuntimeError(f"pawnio_open: {exc} (is the PawnIO driver service running?)")
        if hr != 0 or not handle.value:
            raise RuntimeError(f"pawnio_open failed (HRESULT 0x{hr & 0xFFFFFFFF:08X}); run FPS Control as administrator")
        self.handle = handle
        blob = None
        last_err = None
        for path in self.blobs:
            try:
                data = open(path, "rb").read()
            except OSError:
                continue
            try:
                self.lib.pawnio_load(handle, (ctypes.c_ubyte * len(data)).from_buffer_copy(data), len(data))
                blob = path
                break
            except OSError as exc:
                last_err = exc
        if blob is None:
            self.close()
            raise RuntimeError(f"IntelMSR module failed to load{f': {last_err}' if last_err else ''}")
        self.blob = blob

    def _read(self, addr):
        if addr not in (OC_MAILBOX_ADDR, MSR_TURBO_RATIO_LIMIT, MSR_RAPL_POWER_UNIT,
                        MSR_PKG_POWER_LIMIT, 0x1A2, 0x1B1, 0x19C):
            raise RuntimeError("CPU backend reads only whitelisted registers")
        in_buf = (ctypes.c_ulonglong * 1)(addr)
        out_buf = (ctypes.c_ulonglong * 1)(0)
        ret_size = ctypes.c_size_t(0)
        try:
            self.lib.pawnio_execute(self.handle, b"ioctl_read_msr", in_buf, 1, out_buf, 1, ctypes.byref(ret_size))
        except OSError as exc:
            raise RuntimeError(f"MSR read failed: {exc}")
        return out_buf[0]

    def _write(self, addr, value):
        if addr == MSR_PKG_POWER_LIMIT:
            if not 0 <= value < (1 << 64):
                raise RuntimeError("Invalid power-limit value")
            in_buf = (ctypes.c_ulonglong * 2)(addr, value)
            out_buf = (ctypes.c_ulonglong * 1)(0)
            ret_size = ctypes.c_size_t(0)
            try:
                self.lib.pawnio_execute(self.handle, b"ioctl_write_msr", in_buf, 2, out_buf, 1, ctypes.byref(ret_size))
            except OSError as exc:
                raise RuntimeError(f"MSR write failed: {exc}")
            return
        if addr != OC_MAILBOX_ADDR or not 0 <= value < (1 << 64):
            raise RuntimeError("CPU backend writes only whitelisted registers")
        cmd_dword = (value >> 32) & 0xFFFFFFFF
        command = cmd_dword & 0xFF
        if command not in (OC_CMD_GET_CAPS, OC_CMD_GET_VF, OC_CMD_SET_VF) or not (cmd_dword & 0x80000000):
            raise RuntimeError("CPU backend allows only voltage/frequency commands")
        data = value & 0xFFFFFFFF
        if command == OC_CMD_SET_VF and ((data & ~0xFF) != 0 or (data & 0xFF) > CPU_OC_MAX_RATIO):
            raise RuntimeError("SET_VF data outside allowed bounds; voltage fields must stay zero")
        in_buf = (ctypes.c_ulonglong * 2)(addr, value)
        out_buf = (ctypes.c_ulonglong * 1)(0)
        ret_size = ctypes.c_size_t(0)
        try:
            self.lib.pawnio_execute(self.handle, b"ioctl_write_msr", in_buf, 2, out_buf, 1, ctypes.byref(ret_size))
        except OSError as exc:
            raise RuntimeError(f"MSR write failed: {exc}")

    def mailbox(self, cmd, p1=0, p2=0, data=0):
        if cmd not in (OC_CMD_GET_CAPS, OC_CMD_GET_VF, OC_CMD_SET_VF):
            raise RuntimeError("CPU backend allows only capability and voltage/frequency commands")
        if not 0 <= data <= 0xFFFFFFFF or not 0 <= p1 <= 0xFF or not 0 <= p2 <= 0xFF:
            raise RuntimeError("Mailbox parameter out of range")
        cmd_dword = 0x80000000 | (p2 << 16) | (p1 << 8) | cmd
        self._write(OC_MAILBOX_ADDR, (cmd_dword << 32) | data)
        for _ in range(5):
            time.sleep(0.000003)
            resp = self._read(OC_MAILBOX_ADDR)
            if not (resp & (1 << 63)):
                status = (resp >> 32) & 0xFFFFFFFF
                if status:
                    raise RuntimeError(f"CPU mailbox rejected command 0x{cmd:02X} (status 0x{status:X})")
                return resp & 0xFFFFFFFF
        raise RuntimeError("CPU mailbox stayed busy - timed out")

    def get_vf(self):
        return self.mailbox(OC_CMD_GET_VF)

    def probe(self):
        """Query OC capabilities and stock turbo limit. Read-only."""
        caps = self.mailbox(OC_CMD_GET_CAPS)
        info = {"ratio_oc": bool(caps & 0x100), "max_ratio": caps & 0xFF, "stock_ratio": None}
        try:
            turbo = self._read(MSR_TURBO_RATIO_LIMIT)
            ratios = [(turbo >> (i * 8)) & 0xFF for i in range(8)]
            nonzero = [r for r in ratios if r]
            if nonzero:
                info["stock_ratio"] = max(nonzero)
        except Exception:
            pass
        return info

    def set_ratio(self, ratio):
        if not isinstance(ratio, int) or not 0 < ratio <= CPU_OC_MAX_RATIO:
            raise RuntimeError(f"Ratio must be 1..{CPU_OC_MAX_RATIO}; got {ratio!r}")
        self.mailbox(OC_CMD_SET_VF, data=ratio)
        readback = self.get_vf() & 0xFF
        if readback != ratio:
            raise RuntimeError(f"Ratio readback mismatch: requested {ratio}x, mailbox shows {readback}x")

    def restore(self):
        self.mailbox(OC_CMD_SET_VF, data=0)
        if self.get_vf() & 0xFF:
            raise RuntimeError("CPU restore failed: ratio override still active")

    def read_power_limits(self):
        """Return package power limits in watts plus the raw MSR value."""
        unit = self._read(MSR_RAPL_POWER_UNIT)
        pu = 1.0 / (1 << (unit & 0xF))
        raw = self._read(MSR_PKG_POWER_LIMIT)
        return {"raw": raw, "power_unit": pu,
                "pl1_w": (raw & 0x7FFF) * pu, "pl2_w": ((raw >> 32) & 0x7FFF) * pu}

    def set_power_limits(self, pl1_w, pl2_w):
        """Raise package power limits; preserves enable bits and time windows."""
        if not 0 < pl1_w <= 250 or not 0 < pl2_w <= 250:
            raise RuntimeError("Power limits must be 1..250 W")
        info = self.read_power_limits()
        pl1 = int(pl1_w / info["power_unit"]) & 0x7FFF
        pl2 = int(pl2_w / info["power_unit"]) & 0x7FFF
        new = (info["raw"] & ~(0x7FFF | (0x7FFF << 32))) | pl1 | (pl2 << 32)
        self._write(MSR_PKG_POWER_LIMIT, new)
        back = self.read_power_limits()["raw"]
        if (back & 0x7FFF) != pl1 or ((back >> 32) & 0x7FFF) != pl2:
            raise RuntimeError("Power-limit readback mismatch")

    def restore_power_limits(self, raw):
        self._write(MSR_PKG_POWER_LIMIT, raw)
        if self._read(MSR_PKG_POWER_LIMIT) != raw:
            raise RuntimeError("Power-limit restore failed")

    def package_temp(self):
        """CPU package temperature in Celsius via thermal MSRs (any Intel CPU)."""
        tjmax = (self._read(0x1A2) >> 16) & 0xFF
        offset = (self._read(0x1B1) >> 16) & 0x7F
        if not tjmax or offset >= tjmax:
            return None
        return float(tjmax - offset)

    def close(self):
        if self.lib is not None and self.handle:
            try:
                self.lib.pawnio_close(self.handle)
            except Exception:
                pass
        self.handle = None


class GpuSettingsChangedError(RuntimeError):
    pass


NVAPI_BUFFER_U32 = 1854  # NV_GPU_PSTATES20_V2: 5 + 16*114 + 25 u32 = 7416 bytes
NVAPI_VERSION = 7416 | (2 << 16)
NV_CLOCK_GRAPHICS = 0
NV_CLOCK_MEMORY = 4


class NvApiControl:
    """Built-in GPU overclocking via the NVIDIA driver (NVAPI Pstates20).

    Reads/edits the P0 clock deltas directly - no Afterburner required.
    Works on NVIDIA GPUs whose driver exposes editable clock deltas.
    """

    NAME = "NVIDIA driver (built-in)"

    def __init__(self):
        self.dll = None
        self.gpu = None

    def _fn(self, fid, *args):
        addr = self.dll.nvapi_QueryInterface(fid)
        if not addr:
            raise RuntimeError(f"NVAPI function 0x{fid:08X} not found")
        return ctypes.CFUNCTYPE(ctypes.c_int32, *args)(addr)

    def connect(self):
        if sys.platform != "win32":
            raise RuntimeError("GPU control is only available on Windows")
        self.dll = ctypes.CDLL("nvapi64.dll")
        self.dll.nvapi_QueryInterface.argtypes = [ctypes.c_uint32]
        self.dll.nvapi_QueryInterface.restype = ctypes.c_void_p
        init = self._fn(0x0150E828)
        status = init()
        if status != 0:
            raise RuntimeError(f"NvAPI_Initialize failed ({status})")
        self.unload = self._fn(0xD22BDD7E)
        self.get_pstates = self._fn(0x6FF81213, ctypes.c_void_p, ctypes.c_void_p)
        self.set_pstates = self._fn(0x0F4DAE6B, ctypes.c_void_p, ctypes.c_void_p)
        enum = self._fn(0xE5AC921F, ctypes.c_void_p, ctypes.c_void_p)
        handles = (ctypes.c_void_p * 64)()
        count = ctypes.c_uint32(0)
        status = enum(handles, ctypes.byref(count))
        if status != 0 or not count.value:
            raise RuntimeError(f"No NVIDIA GPU found (status {status})")
        self.gpu = handles[0]
        self.power_info = self._fn(0x34206D86, ctypes.c_void_p, ctypes.c_void_p)
        self.power_get = self._fn(0x70916171, ctypes.c_void_p, ctypes.c_void_p)
        self.power_set = self._fn(0xAD95F5ED, ctypes.c_void_p, ctypes.c_void_p)
        self.power_raised = False
        self.cooler_get = self._fn(0xDA141340, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p)
        self.cooler_set = self._fn(0x891FA0AE, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p)
        self.fan_default = None
        # Pascal-era clock control: shifting the VF/boost table is how driver-level
        # core offsets are applied when SetPstates20 is not supported.
        self.mask_get = self._fn(0x507B4B59, ctypes.c_void_p, ctypes.c_void_p)
        self.table_get = self._fn(0x23F1B133, ctypes.c_void_p, ctypes.c_void_p)
        self.table_set = self._fn(0x0733E009, ctypes.c_void_p, ctypes.c_void_p)
        self.boost_ok = False
        self.pstates_write_ok = False
        self._pstate_ranges = {}
        try:
            m, t = self._boost_state()
            self.boost_ok = bool(self._enabled_entries(m)) and \
                self.table_set(self.gpu, t) == 0  # no-op write probe
        except Exception:
            pass
        try:
            buf = self._get()
            self._pstate_ranges = self._deltas(buf)
            self.pstates_write_ok = self.set_pstates(self.gpu, buf) == 0
        except Exception:
            pass
        self.clock_ok = self.boost_ok or self.pstates_write_ok

    def _power_info(self):
        buf = (ctypes.c_uint32 * 46)()
        buf[0] = 184 | (1 << 16)
        status = self.power_info(self.gpu, buf)
        if status != 0:
            raise RuntimeError(f"GetPowerPoliciesInfo failed (status {status})")
        return buf

    def _power_status(self):
        buf = (ctypes.c_uint32 * 18)()
        buf[0] = 72 | (1 << 16)
        status = self.power_get(self.gpu, buf)
        if status != 0:
            raise RuntimeError(f"GetPowerPoliciesStatus failed (status {status})")
        return buf

    def set_power(self, raise_to_max):
        """Raise P0 power limit to the card's max, or restore defaults."""
        info = self._power_info()
        current = self._power_status()
        out = (ctypes.c_uint32 * 18)()
        out[0] = 72 | (1 << 16)
        out[1] = 4
        for i in range(4):
            e = 2 + i * 4     # status entry: pstate, unk, power_mW, unk
            ie = 2 + i * 11   # info entry: pstate, unk, unk, min, unk, unk, default, unk, unk, max, unk
            out[e] = info[ie]
            out[e + 1] = 0
            out[e + 2] = info[ie + 9] if raise_to_max else info[ie + 6]
            out[e + 3] = 0
        status = self.power_set(self.gpu, out)
        if status != 0:
            raise RuntimeError(f"SetPowerPoliciesStatus failed (status {status})")
        self.power_raised = raise_to_max

    def cooler_settings(self):
        buf = (ctypes.c_int32 * 242)()
        buf[0] = 968 | (2 << 16)
        status = self.cooler_get(self.gpu, 0, buf)
        if status != 0:
            raise RuntimeError(f"GetCoolerSettings failed ({status})")
        return [{"level": buf[2 + i * 12 + 6], "policy": buf[2 + i * 12 + 8],
                 "min": buf[2 + i * 12 + 4], "max": buf[2 + i * 12 + 5]}
                for i in range(min(buf[1], 20))]

    def set_fan(self, level):
        """level 0-100 sets the GPU fan manually; None restores the card's policy."""
        coolers = self.cooler_settings()
        if not coolers:
            return
        if self.fan_default is None:
            self.fan_default = coolers
        buf = (ctypes.c_int32 * 41)()
        buf[0] = 164 | (1 << 16)
        for i in range(20):
            d = self.fan_default[i] if i < len(self.fan_default) else {"level": 0, "policy": 0x20}
            if i < len(coolers) and level is not None:
                buf[1 + i * 2] = max(0, min(100, int(level)))
                buf[2 + i * 2] = 1  # manual policy
            else:
                buf[1 + i * 2] = d["level"]
                buf[2 + i * 2] = d["policy"]
        status = self.cooler_set(self.gpu, 0, buf)
        if status != 0:
            raise RuntimeError(f"SetCoolerLevels failed ({status})")

    def _get(self):
        buf = (ctypes.c_uint32 * NVAPI_BUFFER_U32)()
        buf[0] = NVAPI_VERSION
        status = self.get_pstates(self.gpu, buf)
        if status != 0:
            raise RuntimeError(f"GetPstates20 failed (status {status})")
        return buf

    @staticmethod
    def _clock_entry(buf, state_off, domain):
        for j in range(8):
            off = state_off + 2 + j * 11
            if buf[off] == domain:
                return off
        return None

    def _deltas(self, buf):
        """Return {domain: (value_kHz, min_kHz, max_kHz)} for P0."""
        state_off = 5  # first pstate entry
        out = {}
        for domain in (NV_CLOCK_GRAPHICS, NV_CLOCK_MEMORY):
            off = self._clock_entry(buf, state_off, domain)
            if off is not None:
                to_i32 = lambda v: v - 0x100000000 if v >= 0x80000000 else v
                out[domain] = tuple(to_i32(buf[off + k]) for k in (3, 4, 5))
        return out

    # NV_GPU_CLOCK_MASKS: version(4) + mask[32] + unk[32] + clocks[255] * 24 bytes
    # NV_GPU_CLOCK_TABLE: version(4) + mask[32] + unk[32] + clocks[255] * 36 bytes
    #   table entry: clockType(4) + unk[16] + frequencyDeltaKHz(4, stored x2) + unk[12]
    NVAPI_MASKS_BYTES = 4 + 32 + 32 + 255 * 24
    NVAPI_TABLE_BYTES = 4 + 32 + 32 + 255 * 36

    def _boost_state(self):
        m = (ctypes.c_uint8 * self.NVAPI_MASKS_BYTES)()
        m[0:4] = (self.NVAPI_MASKS_BYTES | (1 << 16)).to_bytes(4, "little")
        if self.mask_get(self.gpu, m) != 0:
            raise RuntimeError("GetClockBoostMask failed")
        t = (ctypes.c_uint8 * self.NVAPI_TABLE_BYTES)()
        t[0:4] = (self.NVAPI_TABLE_BYTES | (1 << 16)).to_bytes(4, "little")
        t[4:36] = m[4:36]
        if self.table_get(self.gpu, t) != 0:
            raise RuntimeError("GetClockBoostTable failed")
        return m, t

    @staticmethod
    def _enabled_entries(mask_buf):
        """Enabled core-clock (clockType 0) table indices."""
        return [i for i in range(255)
                if mask_buf[68 + i * 24 + 4] == 1
                and int.from_bytes(mask_buf[68 + i * 24: 68 + i * 24 + 4], "little") == 0]

    @staticmethod
    def _boost_deltas(mask_buf, table_buf):
        """Raw frequencyDeltaKHz values (stored x2) for enabled entries."""
        return {i: int.from_bytes(table_buf[68 + i * 36 + 20: 68 + i * 36 + 24],
                                  "little", signed=True)
                for i in NvApiControl._enabled_entries(mask_buf)}

    def _boost_write(self, core_mhz):
        """Uniform VF-curve shift. Delta field stores kHz * 2."""
        m, t = self._boost_state()
        raw = int(round(core_mhz * 2000))
        for i in self._enabled_entries(m):
            off = 68 + i * 36 + 20
            t[off:off + 4] = raw.to_bytes(4, "little", signed=True)
        status = self.table_set(self.gpu, t)
        if status != 0:
            raise RuntimeError(f"SetClockBoostTable failed (status {status})")

    def _core_offset_mhz(self):
        """Current uniform core offset in MHz; non-uniform curves report max
        and set the 0x40000 'custom curve' flag in read()."""
        m, t = self._boost_state()
        deltas = self._boost_deltas(m, t)
        return deltas, max(deltas.values(), default=0) / 2000.0

    def read(self):
        result = {"flags": 0, "command": 0}
        core_cur = 0.0
        if self.boost_ok:
            deltas, core_cur = self._core_offset_mhz()
            if len(set(deltas.values())) > 1:
                result["flags"] |= 0x40000  # non-uniform custom curve
        elif NV_CLOCK_GRAPHICS in self._pstate_ranges:
            core_cur = self._pstate_ranges[NV_CLOCK_GRAPHICS][0] / 1000.0
        else:
            raise RuntimeError("GPU clock deltas not exposed by the driver")
        lo, hi = (self._pstate_ranges.get(NV_CLOCK_GRAPHICS, (0, -200000, 500000))[1:3])
        result["core"] = [core_cur, lo / 1000.0, min(hi, 500000) / 1000.0, 0.0]
        mem = self._pstate_ranges.get(NV_CLOCK_MEMORY, (0, 0, 0))
        mem_hi = mem[2] / 1000.0 if self.pstates_write_ok else 0.0
        result["memory"] = [mem[0] / 1000.0, mem[1] / 1000.0, mem_hi, 0.0]
        result["is_default"] = core_cur == 0 and mem[0] == 0
        return result

    def _pstates_apply(self, core_khz, mem_khz):
        """Write both deltas through SetPstates20 (drivers that support it)."""
        buf = self._get()
        for domain, khz in ((NV_CLOCK_GRAPHICS, core_khz), (NV_CLOCK_MEMORY, mem_khz)):
            off = self._clock_entry(buf, 5, domain)
            if off is None:
                raise RuntimeError(f"Clock domain {domain} not editable on this GPU")
            buf[off + 3] = khz & 0xFFFFFFFF
        status = self.set_pstates(self.gpu, buf)
        if status != 0:
            raise RuntimeError(f"SetPstates20 failed (status {status}); GPU state may be unchanged")

    def apply(self, core_mhz=None, memory_mhz=None, reset=False, expected=None):
        cur = self.read()
        if expected is not None and [cur["core"][0], cur["memory"][0]] != list(expected):
            raise GpuSettingsChangedError("Clock offsets changed elsewhere; refresh before applying")
        if cur["flags"] & 0x40000 and not reset:
            raise GpuSettingsChangedError(
                "A custom GPU voltage/frequency curve is active; reset GPU settings to stock first")
        core_t = 0.0 if reset else (core_mhz or 0.0)
        mem_t = 0.0 if reset else (memory_mhz or 0.0)
        if not reset:
            if not 0 <= core_t <= min(100, max(0.0, cur["core"][2])):
                raise ValueError("Core offset is outside the supported range")
            if not 0 <= mem_t <= min(250, max(0.0, cur["memory"][2])):
                raise ValueError("Memory offset is outside the supported range")
        if core_t != cur["core"][0] and not (self.boost_ok or self.pstates_write_ok):
            raise RuntimeError("This GPU's driver does not allow core clock writes")
        if mem_t != cur["memory"][0] and not self.pstates_write_ok:
            raise RuntimeError("This GPU's driver does not allow memory clock writes")
        if core_t != cur["core"][0]:
            if self.boost_ok:
                self._boost_write(core_t)
            else:
                self._pstates_apply(int(round(core_t * 1000)),
                                    int(round(cur["memory"][0] * 1000)))
        if mem_t != cur["memory"][0]:
            self._pstates_apply(int(round(core_t * 1000)), int(round(mem_t * 1000)))
        time.sleep(0.05)
        verify = self.read()
        if verify["core"][0] != core_t or verify["memory"][0] != mem_t:
            raise RuntimeError("Driver readback differs from the request; check GPU state")
        try:
            self.set_power(bool(core_t or mem_t))
        except Exception:
            pass
        return verify

    def close(self):
        if self.dll and self.gpu:
            if self.fan_default is not None:
                try:
                    self.set_fan(None)
                except Exception:
                    pass
            if self.power_raised:
                try:
                    self.set_power(False)
                except Exception:
                    pass
            try:
                self.unload()
            except Exception:
                pass
        self.gpu = None


class AfterburnerControl:
    def __init__(self, memory_factory=MacmMemory, notify=notify_afterburner):
        self.memory_factory = memory_factory
        self.notify = notify

    def read(self):
        with self.memory_factory() as memory:
            with memory.lock():
                return decode_macm(memory.read())

    def apply(self, core_mhz=None, memory_mhz=None, reset=False, expected=None):
        progress = [False]
        try:
            return self._apply(core_mhz, memory_mhz, reset, expected, progress)
        except Exception as exc:
            exc.gpu_may_have_applied = progress[0]
            raise

    def _apply(self, core_mhz, memory_mhz, reset, expected, progress):
        with self.memory_factory(writable=True) as memory:
            with memory.lock():
                data = memory.read()
                info = decode_macm(data)
                if info["command"]:
                    raise RuntimeError("GPU clock service has a pending command; wait and try again")
                if info["flags"] & 0x40000:
                    raise GpuSettingsChangedError(
                        "A custom GPU voltage/frequency curve is active; reset GPU settings to stock first")
                if expected is not None and [info[key][0] for key in ("core", "memory")] != list(expected):
                    raise GpuSettingsChangedError("Clock offsets changed in another app; refresh before applying")
                header, _, _ = macm_layout(data)
                desired = []
                for key, value, cap in (("core", core_mhz, 100), ("memory", memory_mhz, 250)):
                    current, lower, upper, default = info[key]
                    target = default if reset else value
                    if target is None or not math.isfinite(target) or not lower <= target <= upper:
                        raise ValueError("Clock offset is outside the GPU's supported range")
                    if not reset and not default <= target <= min(upper, default + cap):
                        raise ValueError("Clock offset exceeds FPS Control's limited adjustment range")
                    desired.append(int(round(target * 1000)))
                progress[0] = True
                memory.write_int(header + CORE_OFFSET, desired[0])
                memory.write_int(header + MEMORY_OFFSET, desired[1])
                memory.write_int(32, MACM_FLUSH)
            self.notify()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                time.sleep(0.1)
                with memory.lock():
                    result = decode_macm(memory.read())
                if result["command"] == 0:
                    actual = [int(round(result[key][0] * 1000)) for key in ("core", "memory")]
                    if actual != desired:
                        raise RuntimeError("GPU clock readback differs from the request; reset GPU offsets and try again")
                    return result
            raise RuntimeError("GPU clock service did not acknowledge the request; GPU state is unknown.")


def oc_scale(temp):
    """Fraction of the user-tested preset allowed at this GPU temperature.

    Full preset at or below GPU_OC_FULL_TEMP, ramping to zero at
    GPU_APPLY_TEMP. Never exceeds 1.0 - the tested preset is the ceiling.
    """
    return min(1.0, max(0.0, (GPU_APPLY_TEMP - temp) / (GPU_APPLY_TEMP - GPU_OC_FULL_TEMP)))


def valid_auto_preset(preset):
    if not isinstance(preset, dict) or (preset.get("user_tested") is not True
                                       and preset.get("auto_default") is not True):
        return False
    if not isinstance(preset.get("uuid"), str) or not preset["uuid"].startswith("GPU-"):
        return False
    values = [preset.get("core"), preset.get("memory")]
    return (all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= limit
                for v, limit in zip(values, (100, 250))) and any(values))


class AutoGpuPolicy:
    def __init__(self):
        self.high_since = None
        self.low_since = None
        self.last_sample = None
        self.last_gap = None
        self.cooldown_until = 0

    def decide(self, now, load, temp, active, healthy=True):
        if self.last_sample is not None and not 0 <= now - self.last_sample <= SENSOR_TIMEOUT:
            self.last_gap = now - self.last_sample
            self.high_since = self.low_since = None
        self.last_sample = now
        if (not healthy or load is None or temp is None or not math.isfinite(load)
                or not math.isfinite(temp) or not 0 <= load <= 100 or not 0 <= temp < GPU_RESET_TEMP):
            self.high_since = self.low_since = None
            self.cooldown_until = now + 60
            return "reset" if active else None
        if active:
            self.high_since = self.low_since = None
            return None
        else:
            self.low_since = None
            if temp < 75 and now >= self.cooldown_until:
                if self.high_since is None:
                    self.high_since = now
                if now - self.high_since >= 3:
                    self.high_since = None
                    self.cooldown_until = now + 60
                    return "apply"
            else:
                self.high_since = None
        return None


HIGH_PRIORITY_CLASS = 0x00000080
NORMAL_PRIORITY_CLASS = 0x00000020
PROCESS_SET_INFORMATION = 0x0200
HIGH_PERF_SCHEME = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
ULTIMATE_SCHEME = "e9a42b02-d5df-448d-aa00-03f14749eb61"  # hidden on Win10; created via -duplicatescheme
SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"      # powercfg subgroup
CPMINCORES = "0cc5b647-c1df-4637-891a-dec35c318583"         # min cores unparked (%)
PERFBOOSTMODE = "be337238-0d82-4146-a960-4f3749d470c7"      # boost aggressiveness (2 = Aggressive)
IDLEDISABLE = "5d76a2ca-e8c0-402f-a133-2158492d58ad"        # disable C-states (1 = no idle)
SUB_USB = "2a737441-1930-4402-8d77-b2bebba308a3"            # USB subgroup
SUB_PCIEXPRESS = "501a4d13-42af-4429-9fd1-a8218c268e20"     # PCIe subgroup
USB_SUSPEND = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"        # USB selective suspend
PCIE_ASPM = "ee12f906-d277-404b-b6da-e5fa1a576df5"          # PCIe link power saving
# AC settings applied to the gaming power scheme while a listed game runs;
# previous values are restored afterwards.
BOOST_POWER_SETTINGS = {
    (SUB_PROCESSOR, CPMINCORES): "100",      # unpark all cores
    (SUB_PROCESSOR, PERFBOOSTMODE): "2",     # aggressive boost
    (SUB_PROCESSOR, IDLEDISABLE): "1",       # no C-state sleep
    (SUB_USB, USB_SUSPEND): "0",             # no USB suspend latency
    (SUB_PCIEXPRESS, PCIE_ASPM): "0",        # no PCIe link power-saving
}
# Demand-start services paused while gaming (downloads/updates/telemetry/
# xbox junk). Resumed on game exit. NOT XboxGipSvc - that handles controllers.
BOOST_PAUSE_SERVICES = ("wuauserv", "usosvc", "dosvc", "xblgamesave",
                        "xboxnetapisvc", "sysmain", "wsearch", "diagtrack",
                        "mapsbroker", "lfsvc", "wisvc")
# Registry tweaks applied while Game Boost is on, restored when it's turned
# off. (hive, path, name, value, kind)
BOOST_REG_TWEAKS = (
    ("HKLM", r"SYSTEM\CurrentControlSet\Control\PriorityControl",
     "Win32PrioritySeparation", 0x26, "dword"),   # short quantum + fg boost
    ("HKLM", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
     "SystemResponsiveness", 0, "dword"),          # games get the whole CPU
    ("HKLM", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
     "NetworkThrottlingIndex", 0xFFFFFFFF, "dword"),  # no net throttle
    ("HKLM", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
     "GPU Priority", 8, "dword"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
     "Priority", 6, "dword"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
     "Scheduling Category", "High", "sz"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
     "SFIO Priority", "High", "sz"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects",
     "VisualFXSetting", 2, "dword"),              # best performance effects
    ("HKCU", r"Control Panel\Desktop", "ForegroundLockTimeout", 0, "dword"),
    ("HKCU", r"System\GameConfigStore", "GameDVR_Enabled", 0, "dword"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\GameDVR",
     "AllowGameDVR", 0, "dword"),
)
TCPIP_IFACES = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
APPCOMPAT_LAYERS = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
STANDBY_PURGE_EVERY = 600.0  # seconds between standby-list purges while gaming

# FPS cleanup: processes that must never be touched, and known non-essential
# hogs that can be closed without asking (restartable, no unsaved work).
CLEANUP_PROTECTED = frozenset((
    "system", "registry", "smss", "csrss", "wininit", "winlogon", "services",
    "lsass", "svchost", "dwm", "fontdrvhost", "memory compression",
    "secure system", "system idle process", "idle", "consent", "sihost",
    "ctfmon", "taskmgr", "msmpeng", "securityhealthservice", "gamemon",
    "gamemon.des", "easyanticheat", "beservice", "vgtray", "vgc",
    # Antivirus / VPN: killing these can drop the whole connection
    # (VPN kill switches) or leave security in a broken state.
    "vpnsvc", "nortonui", "nortonsecurity", "wsc_proxy", "nav",
    "aswidsagent", "aswidsagenta", "avastui", "avastsvc", "avast",
    "aswengsrv", "mbamservice", "mbamtray", "mcshield", "mfemms",
    "bdagent", "avguard", "ekrn", "avp", "savservice", "sophoshealth",
    "openvpn", "openvpnserv", "wireguard", "nordvpn-service", "expressvpn",
    "expressvpnd", "tailscaled", "zerotier-one_x64", "zerotier", "wgtunnel",
    "cfwhelper", "clash", "v2ray", "protonvpn", "psiphon3",
    # Windows shell / security: closing these breaks the desktop or AV.
    "explorer", "shellexperiencehost", "startmenuexperiencehost", "searchhost",
    "searchui", "runtimebroker", "applicationframehost", "dllhost", "conhost",
    "audiodg", "wudfhost", "dashost", "spoolsv",
    # Cooling/telemetry + GPU driver helpers this app depends on.
    "librehardwaremonitor", "openhardwaremonitor", "nvcontainer",
    "nvdisplay.container", "nvidia share", "nvidia web helper",
    "msiafterburner", "afterburnerhelper", "rtss", "rtsshooksloader",
    "rtsshooksloader64", "rtsscl",
    # Ambiguous hosts: python/pythonw processes are resolved to script names
    # before classification; if resolution fails they stay protected. Script
    # stems below are this app and its helper tooling - never close them.
    "python", "pythonw", "cooling_gui", "code_verifier", "reopen_helper",
))
CLEANUP_SAFE_CLOSE = frozenset((
    # Media - restartable, no unsaved work. Browsers deliberately NOT here:
    # the user's main browser may be in use mid-game, so browsers get asked.
    "spotify", "robloxplayerbeta", "msedgewebview2",
    # Other game launchers/helpers - the running game only needs Steam itself.
    "epicgameslauncher", "steamwebhelper", "steamservice", "eadesktop",
    "ealauncher", "origin", "originwebhelperservice", "ubisoftconnect", "upc",
    "battle.net", "galaxyclient", "wegame", "riotclientservices",
    # Cloud sync + update/telemetry junk - nothing user-facing is lost.
    "onedrive", "dropbox", "googledrivefs", "updater", "crashpad",
    "googleupdate", "adobearm", "jusched", "adobecollabsync", "ccxprocess",
    "cclibrary", "adobegcclient", "adobeipcbroker", "core.sync",
    "ituneshelper", "applemobiledeviceservice", "mdnsresponder", "bonjour",
    "qtaudio", "obs-browser-page",
    # Windows Search indexer - a classic background CPU hog during games;
    # Windows restarts it on its own and nothing is lost by closing it.
    "searchindexer", "searchprotocolhost", "searchfilterhost",
))
# Ask-first: could be in active use during a game (voice chat, etc.)
# - discord*, overlays, recording/streaming tools land here naturally since
#   they are not on the safe list and not protected.
CLEANUP_CPU_MIN = 25.0  # PercentProcessorTime above this = worth closing
CLEANUP_RECLOSE_AFTER = 120.0  # respawned apps can be closed again after this
TIMER_RES_100NS = 5000  # 0.5 ms


class POWER_THROTTLE_STATE(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong)]


class GameBooster:
    """High process priority + fast system timer while a listed game runs.

    Best-effort: anti-cheat protected processes may refuse the priority
    change; the timer boost still applies system-wide.
    """
    def __init__(self):
        self.boosted = set()
        self.denied = set()
        self.timer_raised = False
        self.last_purge = 0.0
        try:
            self.k32 = ctypes.WinDLL("kernel32")
            self.ntdll = ctypes.WinDLL("ntdll")
        except Exception:
            self.k32 = self.ntdll = None

    def purge_standby(self):
        """Empty the standby memory list - cached pages can cause hitching
        when a game needs RAM quickly. Same trick as ISLC's standby purge."""
        try:
            cmd = ctypes.c_int(4)  # MemoryPurgeStandbyList
            self.ntdll.NtSetSystemInformation(80, ctypes.byref(cmd), 4)
        except Exception:
            pass

    def _timer(self, raise_it):
        cur = ctypes.c_ulong(0)
        self.ntdll.NtSetTimerResolution(TIMER_RES_100NS, raise_it, ctypes.byref(cur))
        self.timer_raised = raise_it

    def _set_priority(self, pid, level):
        handle = self.k32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            return bool(self.k32.SetPriorityClass(handle, level))
        finally:
            self.k32.CloseHandle(handle)

    def apply(self, pids):
        if not self.k32:
            return
        if not self.timer_raised:
            self._timer(True)
        now = time.monotonic()
        if now - self.last_purge >= STANDBY_PURGE_EVERY:
            self.purge_standby()
            self.last_purge = now
        for pid in pids:
            if pid in self.boosted or pid in self.denied:
                continue
            if self._set_priority(pid, HIGH_PRIORITY_CLASS):
                self.boosted.add(pid)
                self._dethrottle(pid)
            else:
                self.denied.add(pid)

    def _dethrottle(self, pid):
        """Best-effort per-process wins: opt the game out of EcoQoS power
        throttling and raise its I/O priority for asset streaming."""
        h = self.k32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
        if not h:
            return
        try:
            try:
                st = POWER_THROTTLE_STATE(1, 1, 0)  # version, mask=EXECUTION_SPEED, state=off
                self.k32.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st))
            except Exception:
                pass
            try:
                iop = ctypes.c_ulong(3)  # IoPriorityHigh
                self.ntdll.NtSetInformationProcess(h, 33, ctypes.byref(iop), 4)  # ProcessIoPriority
            except Exception:
                pass
        finally:
            self.k32.CloseHandle(h)

    def release(self):
        if not self.k32:
            return
        for pid in list(self.boosted):
            self._set_priority(pid, NORMAL_PRIORITY_CLASS)
        self.boosted.clear()
        self.denied.clear()
        if self.timer_raised:
            self._timer(False)


class CoolingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Gaming FPS Control")
        try:
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
            self.root.iconphoto(True, tk.PhotoImage(file=icon_path))
        except Exception:
            pass
        self.root.geometry("860x860")
        self.root.minsize(780, 800)
        self.nvapi = None
        try:
            backend = NvApiControl()
            backend.connect()
            self.nvapi = backend
            if backend.clock_ok:
                self.gpu_backend = backend
                self.gpu_backend_name = backend.NAME
            else:
                self.gpu_backend = AfterburnerControl()
                self.gpu_backend_name = "MSI Afterburner"
        except Exception:
            self.gpu_backend = AfterburnerControl()
            self.gpu_backend_name = "MSI Afterburner"
        self.ab_launch_attempted = False
        self.ab_install_attempted = False
        self.gpu_data = {}
        self.gpu_control = {}
        self.gpu_results = queue.Queue()
        self.gpu_busy = False
        self.gpu_owned = False
        self.gpu_reset_failed = False
        self.close_pending = False
        self.gpu_last_poll = 0
        self.gpu_auto_enabled = tk.BooleanVar(value=False)
        self.component_session = False
        self.gpu_managed_offsets = None
        self.gpu_components = {key: {"enabled": tk.BooleanVar(value=False),
                                    "auto": tk.BooleanVar(value=True),
                                    "policy": AutoGpuPolicy(), "applied": False}
                               for key in ("core", "memory")}
        self.cpu_auto_value = tk.BooleanVar(value=True)
        self.cpu_oc_value = tk.BooleanVar(value=False)
        self.cpu_backend = CpuOcBackend()
        self.cpu_connected = False
        self.cpu_oc_active = False
        self.cpu_oc_paused = None
        self.cpu_oc_fault = None
        self.cpu_load_high_since = None
        self.cpu_load_low_since = None
        self.cpu_power_raw = None
        self.oc_auto_armed = False
        self.cpu_auto_armed = False
        self.oc_arm_deadline = time.monotonic() + 90
        self.oc_stable_since = None
        self.oc_tuner_done = False
        self.oc_last_event_check = 0
        self.oc_error_events = 0
        self.tune_status = tk.StringVar(value="")
        self.game_boost_value = tk.BooleanVar(value=False)
        self.game_exes_var = tk.StringVar(value="helldivers2.exe")
        self.game_booster = GameBooster()
        self.boosting_games = set()
        self.fps_cleanup_value = tk.BooleanVar(value=False)
        self.fps_cleanup_active = False
        self.cleanup_last_scan = 0.0
        self.cleanup_candidates = []
        self.cleanup_asked = set()
        self.cleanup_queue = []
        self.cleanup_dialog = None
        self.cleanup_closed = {}
        self.cleanup_always = set()
        self.cleanup_never = set()
        self.cleanup_desc = {}
        self.running_procs = {}
        self.proc_last_scan = 0
        self.prev_power_scheme = None
        self.prev_power_settings = {}
        self.paused_services = []
        self._ultimate_guid = None
        self.tweak_saved = {}
        self.compat_saved = {}
        self.compat_pids = set()
        self.gpu_fan_manual = False
        self.cpu_ratio_preset = CPU_OC_DEFAULT_RATIO
        self.cpu_ratio_from_config = False
        self.cpu_stock_ratio = 38
        self.gpu_stock_clock = None
        self.gpu_auto_preset = None
        self.gpu_auto_policy = AutoGpuPolicy()
        self.gpu_stop_after_busy = False
        self.gpu_auto_status = tk.StringVar(value="Auto OC off - save a user-tested preset first")
        self.gpu_switch_value = tk.IntVar(value=-1)
        self.gpu_core_value = tk.DoubleVar(value=0)
        self.gpu_memory_value = tk.DoubleVar(value=0)
        self.gpu_notice = tk.StringVar(value="Connecting to GPU controls; no overclock applied by this app")
        self.computer = None
        self.controls = {}          # channel index -> ISensor (Control type)
        self.rpm_sensors = {}       # channel index -> ISensor (Fan type)
        self.cpu_temp_sensor = None
        self.cpu_load_sensor = None
        self.snapshot = {"cpu_temp": None, "cpu_load": None, "duty": {}, "rpm": {}}
        self.auto_mode = tk.BooleanVar(value=True)
        self.liquid_val = tk.DoubleVar(value=50)
        self.case_val = tk.DoubleVar(value=50)
        self.manual_values = [50.0, 50.0]
        self.auto_boost = [50.0, 50.0]
        self.auto_duty = BASE_DUTY
        self.last_control_time = time.monotonic()
        self._programmatic = False
        self.hardware_lock = threading.RLock()
        self.roles = {i: tk.StringVar() for i in range(8)}
        self.test_channel = None
        self.test_until = 0
        self.last_applied = (50, 50)
        self.load_config()
        self.build_gui()
        self.open_hardware()
        self.connect_cpu_backend()
        self.stop_flag = threading.Event()
        self.worker = threading.Thread(target=self.poll_loop, daemon=True)
        self.worker.start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(1000, self.tick)

    # ---------- config ----------
    def load_config(self):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            for i, role in cfg.get("roles", {}).items():
                if int(i) in self.roles:
                    self.roles[int(i)].set(role)
            saved = cfg.get("manual_values", [50, 50])
            self.manual_values = [min(100.0, max(BASE_DUTY, float(v))) for v in saved]
            if len(self.manual_values) != 2:
                self.manual_values = [50.0, 50.0]
            self.auto_mode.set(cfg.get("auto", True))
            ratio = cfg.get("cpu_ratio_preset", CPU_OC_DEFAULT_RATIO)
            if isinstance(ratio, int) and 0 < ratio <= CPU_OC_MAX_RATIO:
                self.cpu_ratio_preset = ratio
                self.cpu_ratio_from_config = True
            self.game_boost_value.set(bool(cfg.get("game_boost", False)))
            self.fps_cleanup_value.set(bool(cfg.get("fps_cleanup", False)))
            for key, target in (("cleanup_always", self.cleanup_always),
                                ("cleanup_never", self.cleanup_never)):
                names = cfg.get(key)
                if isinstance(names, str):
                    target.update(n.strip().lower() for n in names.split(",") if n.strip())
            exes = cfg.get("game_exes")
            if isinstance(exes, str) and exes.strip():
                self.game_exes_var.set(exes)
            preset = cfg.get("gpu_auto_preset")
            self.gpu_auto_preset = preset if valid_auto_preset(preset) else None
            if self.gpu_auto_preset:
                self.gpu_auto_status.set(f"Auto OC off - saved user-tested preset: core {preset['core']:+.0f}, memory {preset['memory']:+.0f} MHz")
            if not self.auto_mode.get():
                self.liquid_val.set(self.manual_values[0])
                self.case_val.set(self.manual_values[1])
            return
        except Exception:
            pass
        # defaults: typical ASRock NCT6791D layout (1=CPU fan, 2=CPU fan2/w_pump)
        for i in (1, 2):
            self.roles[i].set("Liquid Cooling")
        for i in (0, 3, 4, 5):
            self.roles[i].set("Case Fans")

    def save_config(self):
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump({"roles": {str(i): v.get() for i, v in self.roles.items()},
                           "manual_values": self.manual_values,
                           "auto": self.auto_mode.get(),
                           "gpu_auto_preset": self.gpu_auto_preset,
                           "cpu_ratio_preset": self.cpu_ratio_preset,
                           "game_boost": self.game_boost_value.get(),
                           "fps_cleanup": self.fps_cleanup_value.get(),
                           "cleanup_always": ",".join(sorted(self.cleanup_always)),
                           "cleanup_never": ",".join(sorted(self.cleanup_never)),
                           "game_exes": self.game_exes_var.get()}, f)
        except Exception:
            pass

    # ---------- hardware ----------
    def open_hardware(self):
        sys.path.insert(0, LHM_DIR)
        import clr
        clr.AddReference("LibreHardwareMonitorLib")
        from LibreHardwareMonitor.Hardware import Computer

        self.computer = Computer()
        self.computer.IsCpuEnabled = True
        self.computer.IsMotherboardEnabled = True
        self.computer.Open()
        temps, loads = [], []
        for hw in self.computer.Hardware:
            if str(hw.HardwareType) == "Cpu":
                for s in hw.Sensors:
                    st = str(s.SensorType)
                    if st == "Temperature":
                        temps.append(s)
                    elif st == "Load":
                        loads.append(s)
            for sub in getattr(hw, "SubHardware", []) or []:
                for s in sub.Sensors:
                    st = str(s.SensorType)
                    if st == "Control":
                        self.controls[s.Index] = s
                    elif st == "Fan":
                        self.rpm_sensors[s.Index] = s

        def pick(sensors, preferred):
            for name in preferred:
                for s in sensors:
                    if str(s.Name) == name:
                        return s
            for key in ("Package", "Tctl", "Tdie", "CCD", "Core"):
                for s in sensors:
                    if key.lower() in str(s.Name).lower():
                        return s
            return sensors[0] if sensors else None

        self.cpu_temp_sensor = pick(temps, ("CPU Package",))
        self.cpu_load_sensor = pick(loads, ("CPU Total", "Total"))

    def poll_loop(self):
        while not self.stop_flag.is_set():
            try:
                with self.hardware_lock:
                    for hw in self.computer.Hardware:
                        hw.Update()
                        for sub in getattr(hw, "SubHardware", []) or []:
                            sub.Update()
                            for sensor in sub.Sensors:
                                if str(sensor.SensorType) == "Fan":
                                    self.rpm_sensors[sensor.Index] = sensor
                temp = self.cpu_temp_sensor.Value if self.cpu_temp_sensor else None
                if temp is None and self.cpu_connected:
                    try:
                        temp = self.cpu_backend.package_temp()
                    except Exception:
                        pass
                load = self.cpu_load_sensor.Value if self.cpu_load_sensor else None
                snap = {"cpu_temp": None if temp is None else float(temp),
                        "cpu_load": None if load is None else float(load),
                        "duty": {}, "rpm": {}, "updated_at": time.monotonic()}
                clocks = []
                for hw in self.computer.Hardware:
                    if str(hw.HardwareType) == "Cpu":
                        snap["cpu_name"] = str(hw.Name)
                        clocks.extend(float(s.Value) for s in hw.Sensors if str(s.SensorType) == "Clock"
                                      and str(s.Name).startswith("CPU Core") and s.Value is not None)
                snap["cpu_clock"] = max(clocks) if clocks else None
                for i, s in self.controls.items():
                    snap["duty"][i] = None if s.Value is None else float(s.Value)
                for i, s in self.rpm_sensors.items():
                    snap["rpm"][i] = None if s.Value is None else float(s.Value)
                self.snapshot = snap
            except Exception:
                pass
            if time.monotonic() - self.gpu_last_poll >= 2:
                self.gpu_last_poll = time.monotonic()
                try:
                    self.gpu_data = read_gpu_telemetry()
                except Exception as exc:
                    self.gpu_data = {"error": str(exc)}
                try:
                    self.gpu_control = {"info": self.gpu_backend.read(), "updated_at": time.monotonic()}
                except Exception as exc:
                    self.gpu_control = {"error": str(exc)}
                    if isinstance(self.gpu_backend, AfterburnerControl):
                        self.ensure_afterburner()
            if time.monotonic() - self.oc_last_event_check >= 35:
                self.oc_last_event_check = time.monotonic()
                try:
                    out = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "(Get-WinEvent -FilterHashtable @{LogName='System'; "
                         "StartTime=(Get-Date).AddSeconds(-45)} -ErrorAction SilentlyContinue | "
                         "Where-Object {$_.ProviderName -match 'WHEA|nvlddmkm|Display'} | "
                         "Measure-Object).Count"],
                        capture_output=True, text=True, timeout=20,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.oc_error_events += int(out.stdout.strip() or 0)
                except Exception:
                    pass
            if time.monotonic() - self.proc_last_scan >= 5:
                self.proc_last_scan = time.monotonic()
                try:
                    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                                         capture_output=True, text=True, timeout=15,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                    procs = {}
                    for row in csv.reader(io.StringIO(out)):
                        if len(row) >= 2 and row[1].isdigit():
                            procs.setdefault(row[0].lower(), []).append(int(row[1]))
                    self.running_procs = procs
                except Exception:
                    pass
            if self.fps_cleanup_active and time.monotonic() - self.cleanup_last_scan >= 12:
                self.cleanup_last_scan = time.monotonic()
                try:
                    out = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | "
                         "Where-Object {$_.PercentProcessorTime -gt 10} | "
                         "Select-Object Name,IDProcess,PercentProcessorTime | ConvertTo-Csv -NoTypeInformation"],
                        capture_output=True, text=True, timeout=15,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                    rows = []
                    for row in csv.DictReader(io.StringIO(out)):
                        try:
                            # WMI appends "#N" for repeated process names; strip it
                            # so classification and remembered choices match by app.
                            rows.append((row["Name"].strip().lower().split("#")[0],
                                         int(row["IDProcess"]),
                                         float(row["PercentProcessorTime"])))
                        except (KeyError, ValueError):
                            pass
                    # python/pythonw processes all share a name - resolve each to
                    # its script stem so the user sees e.g. "volume_guard_watcher"
                    # instead of "python#1". Unresolved ones stay "python"/"pythonw".
                    if any(name in ("python", "pythonw") for name, _, _ in rows):
                        try:
                            out2 = subprocess.run(
                                ["powershell", "-NoProfile", "-Command",
                                 "Get-CimInstance Win32_Process | Where-Object {$_.Name -match '^pythonw?\\.exe$'} | "
                                 "Select-Object ProcessId,CommandLine | ConvertTo-Csv -NoTypeInformation"],
                                capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                            script_by_pid = {}
                            for prow in csv.DictReader(io.StringIO(out2)):
                                try:
                                    cmd = prow.get("CommandLine") or ""
                                    script = next((os.path.splitext(os.path.basename(t))[0]
                                                   for t in cmd.split() if t.lower().endswith(".py")), None)
                                    if script:
                                        script_by_pid[int(prow["ProcessId"])] = script.lower()
                                except (KeyError, ValueError):
                                    pass
                            if script_by_pid:
                                rows = [(script_by_pid.get(pid, name), pid, cpu)
                                        for name, pid, cpu in rows]
                        except Exception:
                            pass
                    # Friendly "what is this" text for the ask dialog: the exe's
                    # own file description (e.g. chrome -> "Google Chrome").
                    if rows:
                        try:
                            out3 = subprocess.run(
                                ["powershell", "-NoProfile", "-Command",
                                 "Get-Process | Select-Object Id,Description | ConvertTo-Csv -NoTypeInformation"],
                                capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                            desc = {}
                            for drow in csv.DictReader(io.StringIO(out3)):
                                try:
                                    if drow.get("Description"):
                                        desc[int(drow["Id"])] = drow["Description"].strip()
                                except (KeyError, ValueError):
                                    pass
                            self.cleanup_desc = desc
                        except Exception:
                            pass
                    self.cleanup_candidates = rows
                except Exception:
                    pass
            self.stop_flag.wait(1)

    def set_duty(self, channel, value):
        sensor = self.controls.get(channel)
        if sensor is None or sensor.Control is None:
            return
        with self.hardware_lock:
            sensor.Control.SetSoftware(int(round(value)))

    def restore_all(self):
        for sensor in self.controls.values():
            try:
                if sensor.Control is not None:
                    with self.hardware_lock:
                        sensor.Control.SetDefault()
            except Exception:
                pass

    def channels_for_role(self, role):
        return [i for i in self.controls if i in self.roles and self.roles[i].get() == role]

    # ---------- gui ----------
    def build_gui(self):
        global tk, ttk
        import tkinter as tk
        from tkinter import ttk

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg = "#1e1f24"
        fg = "#e8e8e8"
        self.root.configure(bg=bg)
        style.configure(".", background=bg, foreground=fg, fieldbackground="#2a2b31")
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("Header.TLabel", background=bg, foreground="#7ec8ff", font=("Segoe UI", 22, "bold"))
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TScale", background=bg)
        style.configure("TButton", background="#2a2b31", foreground=fg)
        style.configure("Accent.TButton", background="#2563a8", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#3178c6")])

        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=16, pady=(12, 4))
        self.temp_label = ttk.Label(header, text="-- F", style="Header.TLabel")
        self.temp_label.pack(side="left")
        self.status_label = ttk.Label(header, text="starting...")
        self.status_label.pack(side="right")

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=12, pady=8)
        canvas = tk.Canvas(body, background=bg, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        self.page = ttk.Frame(canvas)
        page_window = canvas.create_window((0, 0), window=self.page, anchor="nw")
        self.page.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(page_window, width=event.width))
        self.root.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        self.cooling_tab = self.performance_tab = self.page
        self.build_performance_tab()
        info = ttk.Frame(self.cooling_tab)
        info.pack(fill="x", padx=16)
        self.load_label = ttk.Label(info, text="CPU load: --%")
        self.load_label.pack(side="left")
        self.auto_cb = ToggleSwitch(info, text="Auto - respond to temperature and workload",
                                       variable=self.auto_mode, command=self.on_mode_change)
        self.auto_cb.pack(side="right")
        ttk.Label(self.cooling_tab, text="Load-aware cooling; 50% base. Drag upward for extra cooling.").pack(
            anchor="w", padx=16, pady=4)

        self.build_group_card("Liquid Cooling  (pump + radiator fans) - requested speed",
                              self.liquid_val, BASE_DUTY)
        self.build_group_card("Case Fans  (intake / exhaust) - requested speed",
                              self.case_val, BASE_DUTY)

        chan = ttk.LabelFrame(self.cooling_tab, text=" Channels ")
        chan.pack(fill="both", expand=True, padx=16, pady=10)
        ttk.Label(chan, text="Target is not guaranteed at full load. Channel readings below are readbacks.\n"
                             "Header roles must match your wiring; software cannot identify the pump.",
                  wraplength=650).pack(anchor="w", padx=8, pady=(4, 2))
        self.chan_rows = {}
        for i in sorted(self.roles):
            row = ttk.Frame(chan)
            row.pack(fill="x", padx=8, pady=2)
            ttk.Label(row, text=f"Fan #{i + 1}", width=10).pack(side="left")
            combo = ttk.Combobox(row, textvariable=self.roles[i], values=ROLE_NAMES,
                                state="readonly", width=16)
            combo.pack(side="left", padx=6)
            combo.bind("<<ComboboxSelected>>", lambda e, channel=i: self.on_role_change(channel))
            ttk.Button(row, text="Test", width=5,
                       command=lambda idx=i: self.start_test(idx)).pack(side="left", padx=4)
            info_lbl = ttk.Label(row, text="")
            info_lbl.pack(side="left", padx=10)
            self.chan_rows[i] = (combo, info_lbl)
        ttk.Label(chan, foreground="#8a8f98",
                  text="Exit restores motherboard control automatically. Close other monitoring "
                       "tools (FanControl/LHM) while this runs.").pack(anchor="w", padx=8, pady=4)

    def build_performance_tab(self):
        panel = self.performance_tab
        self.gpu_monitor_label = ttk.Label(panel, text="GPU telemetry: connecting...", wraplength=740)
        self.gpu_monitor_label.pack(anchor="w", padx=16, pady=6)
        self.cpu_monitor_label = ttk.Label(panel, text="CPU telemetry: connecting...", wraplength=740)
        self.cpu_monitor_label.pack(anchor="w", padx=16, pady=6)
        gpu = ttk.LabelFrame(panel, text=" Overclock controls - maximum overclock has NOT been established ")
        gpu.pack(fill="x", padx=16, pady=8)
        columns = ttk.Frame(gpu)
        columns.pack(fill="x", padx=8, pady=8)
        self.gpu_sliders = []
        self.component_widgets = {}
        for index, (key, title) in enumerate((("core", "GPU core"), ("cpu", "CPU"))):
            columns.columnconfigure(index, weight=1, uniform="component")
            card = ttk.Frame(columns)
            card.grid(row=0, column=index, sticky="nsew", padx=8)
            ttk.Label(card, text=title, font=("Segoe UI", 12, "bold")).pack(pady=4)
            if key == "cpu":
                auto = ToggleSwitch(card, text="Auto CPU", variable=self.cpu_auto_value,
                                    command=self.on_cpu_auto_change, state="disabled")
                switch = ToggleSwitch(card, text="CPU OC", variable=self.cpu_oc_value,
                                      command=self.on_cpu_toggle, state="disabled")
                self.cpu_oc_switch = switch
                self.cpu_auto_switch = auto
            else:
                component = self.gpu_components[key]
                auto = ToggleSwitch(card, text="Auto GPU core",
                    variable=component["auto"], command=lambda part=key: self.on_component_auto_change(part))
                switch = ToggleSwitch(card, text="GPU core OC",
                    variable=component["enabled"],
                    command=lambda part=key: self.on_component_toggle(part), state="disabled")
            auto.pack(anchor="w", pady=(4, 8))
            switch.pack(anchor="w", pady=4)
            label = ttk.Label(card, text="CPU: connecting..." if key == "cpu"
                              else "Locked - detecting GPU clock limits...",
                              wraplength=205)
            label.pack(anchor="w", pady=6)
            self.component_widgets[key] = {"auto": auto, "switch": switch, "status": label}
        buttons = ttk.Frame(gpu)
        buttons.pack(fill="x", padx=12, pady=6)
        self.gpu_save_preset_button = ttk.Button(buttons, text="Use current offsets as tested preset...", command=self.save_gpu_preset)
        self.gpu_save_preset_button.pack(side="left", padx=(0, 8))
        self.gpu_reset_button = ttk.Button(buttons, text="GPU OFF / Reset offsets", command=lambda: self.submit_gpu(reset=True))
        self.gpu_reset_button.pack(side="left")
        self.cpu_driver_button = ttk.Button(buttons, text="Install CPU driver (PawnIO)...",
                                            command=self.install_pawnio)
        self.gpu_readback_label = ttk.Label(gpu, text="Clock-offset state unknown", wraplength=720)
        self.gpu_readback_label.pack(anchor="w", padx=12, pady=4)
        ttk.Label(gpu, textvariable=self.gpu_notice, wraplength=720).pack(anchor="w", padx=12, pady=4)
        ttk.Label(gpu, textvariable=self.gpu_auto_status, wraplength=720).pack(anchor="w", padx=12, pady=4)
        ttk.Label(gpu, text="Auto is selected by default and everything configures itself - no manual values needed.\n"
            "FPS Control creates a conservative clock preset automatically; the auto-tuner can raise it gradually.\n"
            "With Auto selected it waits for sustained GPU load, then scales the preset\n"
            "by temperature: full below 149 F, tapering to zero near 176 F, off if hotter or idle.\n"
            "Temperature monitoring cannot guarantee stability. Thermal/fault resets remain active with Auto off.",
            wraplength=720).pack(anchor="w", padx=12, pady=8)
        self.gpu_save_preset_button.state(["disabled"])
        self.gpu_reset_button.state(["disabled"])
        self.oc_benefit_label = ttk.Label(gpu, text="OC benefit: --",
                                        font=("Segoe UI", 11, "bold"))
        self.oc_benefit_label.pack(anchor="w", padx=12, pady=(0, 8))
        ttk.Label(gpu, textvariable=self.tune_status, wraplength=720).pack(anchor="w", padx=12, pady=(0, 8))

        game = ttk.LabelFrame(panel, text=" Gaming ")
        game.pack(fill="x", padx=16, pady=8)
        grow = ttk.Frame(game)
        grow.pack(fill="x", padx=8, pady=6)
        ToggleSwitch(grow, text="Game boost", variable=self.game_boost_value,
                     command=self.on_game_boost_toggle).pack(side="left")
        ttk.Label(grow, text="Game .exe names:").pack(side="left", padx=(18, 4))
        ttk.Entry(grow, textvariable=self.game_exes_var, width=38).pack(side="left")
        self.game_exes_var.trace_add("write", lambda *a: self.save_config())
        self.game_boost_status = ttk.Label(game, text="off - boosts the listed game while it runs",
                                           wraplength=720)
        self.game_boost_status.pack(anchor="w", padx=12, pady=(0, 4))
        ttk.Label(game, text="While a listed game runs: High CPU + I/O priority, faster Windows timer, Ultimate\n"
            "Performance plan, cores unparked, aggressive boost, no idle sleep, no USB/PCIe power saving, standby\n"
            "memory purged, Windows Update/Search/telemetry services paused. While boost is on: Game DVR off,\n"
            "Game Mode on, no network throttling (Nagle off), foreground priority boost, best-performance visuals,\n"
            "and exclusive-fullscreen compat for listed games. Everything restores when boost is turned off.",
            wraplength=720).pack(anchor="w", padx=12, pady=(0, 8))
        crow = ttk.Frame(game)
        crow.pack(fill="x", padx=8, pady=6)
        ToggleSwitch(crow, text="FPS cleanup", variable=self.fps_cleanup_value,
                     command=self.on_fps_cleanup_toggle).pack(side="left")
        ttk.Button(crow, text="Reset choices", command=self.reset_cleanup_choices).pack(side="right")
        self.fps_cleanup_status = ttk.Label(game, text="off - frees CPU for the listed game",
                                            wraplength=720)
        self.fps_cleanup_status.pack(anchor="w", padx=12, pady=(0, 4))
        ttk.Label(game, text="While a listed game runs: automatically closes background junk (browsers, launchers,\n"
            "sync tools, updaters, search indexing) that is eating CPU. It asks before closing anything that might\n"
            "matter to you - including browsers - 'always close'/'never' are remembered, 'leave it' means not now.",
            wraplength=720).pack(anchor="w", padx=12, pady=(0, 8))

    def stop_components(self, reason):
        self.component_session = False
        self.gpu_auto_enabled.set(False)
        for component in self.gpu_components.values():
            component["enabled"].set(False)
            component["applied"] = False
            component["policy"] = AutoGpuPolicy()
        self.gpu_auto_status.set(reason)

    def ensure_afterburner(self):
        """Start the GPU clock-offset service; silently install the bundled
        copy first when needed. Fully automatic - no user steps."""
        if self.ab_launch_attempted:
            return
        for path in (os.path.expandvars(r"%ProgramFiles(x86)%\MSI Afterburner\MSIAfterburner.exe"),
                     os.path.expandvars(r"%ProgramFiles%\MSI Afterburner\MSIAfterburner.exe")):
            if os.path.exists(path):
                self.ab_launch_attempted = True
                try:
                    subprocess.Popen([path], close_fds=True)
                    self.gpu_notice.set("GPU clock service is starting; controls activate automatically")
                except Exception:
                    pass
                return
        if self.ab_install_attempted:
            return
        self.ab_install_attempted = True
        installers = (glob.glob(os.path.join(APP_DIR, "afterburner*", "MSIAfterburnerSetup*.exe"))
                      + glob.glob(os.path.join(APP_DIR, "MSIAfterburnerSetup*.exe")))
        if not installers:
            self.gpu_notice.set(
                "GPU clock control unavailable: this GPU's driver does not allow direct clock writes")
            return
        try:
            subprocess.Popen([installers[0], "/S"], close_fds=True)
            self.gpu_notice.set("Installing GPU clock component; controls activate automatically")
        except Exception:
            pass

    def component_connection_ready(self):
        info = self.gpu_control.get("info")
        return bool(info and not info["command"] and
                    time.monotonic() - self.gpu_control.get("updated_at", 0) <= SENSOR_TIMEOUT)

    def on_component_toggle(self, key):
        from tkinter import messagebox
        component = self.gpu_components[key]
        if not component["enabled"].get():
            component["policy"] = AutoGpuPolicy()
            component["applied"] = False
            if not any(part["enabled"].get() for part in self.gpu_components.values()):
                self.stop_components("Overclock requests OFF; Auto preferences do not turn them on")
                if self.gpu_owned or self.gpu_busy:
                    self.submit_gpu(reset=True, automatic=True)
            elif self.component_session:
                self.tick_components(time.monotonic(), self.component_connection_ready())
            return
        preset = self.gpu_auto_preset
        info = self.gpu_control.get("info")
        problem = None
        if (not valid_auto_preset(preset) or preset["uuid"] != self.gpu_data.get("uuid")) \
                and self.gpu_data.get("uuid") and info:
            preset = {"core": min(50.0, max(0.0, info["core"][2])),
                      "memory": min(100.0, max(0.0, info["memory"][2])),
                      "uuid": self.gpu_data["uuid"], "auto_default": True}
            if valid_auto_preset(preset):
                self.gpu_auto_preset = preset
                self.save_config()
        if not valid_auto_preset(preset) or preset["uuid"] != self.gpu_data.get("uuid"):
            problem = "Waiting for the automatic GPU clock preset."
        elif not self.component_connection_ready() or self.gpu_busy or self.gpu_reset_failed:
            problem = "Wait for GPU clock readback and resolve any reset failure first."
        elif preset[key] <= info[key][3]:
            problem = ("This GPU's driver does not allow adjusting this clock"
                       if info[key][2] <= info[key][3]
                       else "The saved preset has no overclock for this component.")
        elif not info["is_default"] and not self.gpu_owned:
            problem = "Clock offsets from another app are active; press GPU OFF / Reset first."
        else:
            problem = gpu_apply_problem(self.snapshot, self.gpu_data, info, time.monotonic())
            if component["auto"].get() and problem and problem.startswith("Let the "):
                problem = None
        if problem:
            component["enabled"].set(False)
            self.gpu_notice.set(problem)
            return
        label = "GPU core" if key == "core" else "GPU memory"
        behavior = "wait for sustained load and temperature headroom" if component["auto"].get() else "request this offset continuously"
        if not messagebox.askyesno(f"Enable {label} overclock?",
            f"Use the saved user-tested {preset[key]:+.0f} MHz {label} offset?\n"
            f"This will {behavior}.\n\n"
            "This is NOT a measured maximum. Independent core/memory combinations also need stability testing.\n"
            "Temperature monitoring cannot prevent every crash. Voltage and power limits are unchanged.", parent=self.root):
            component["enabled"].set(False)
            return
        component["policy"] = AutoGpuPolicy()
        component["applied"] = False
        self.component_session = True
        self.gpu_auto_enabled.set(True)
        self.gpu_auto_status.set("Enabled requests use the tested preset; actual offsets are shown below")
        self.tick_components(time.monotonic(), self.component_connection_ready())

    def connect_cpu_backend(self):
        widgets = getattr(self, "component_widgets", {}).get("cpu")
        ident = os.environ.get("PROCESSOR_IDENTIFIER", "")
        if "AuthenticAMD" in ident:
            if widgets:
                widgets["status"].config(text="CPU control unavailable: AMD CPUs use a different OC interface")
            return
        if "GenuineIntel" not in ident:
            if widgets:
                widgets["status"].config(text="CPU control unavailable: unsupported CPU vendor")
            return
        if not os.path.exists(self.cpu_backend.lib_path):
            installer = os.path.join(APP_DIR, "PawnIO_setup.exe")
            if os.path.exists(installer):
                try:
                    subprocess.run([installer, "/S"], timeout=180,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:
                    pass
        try:
            self.cpu_backend.connect()
            caps = self.cpu_backend.probe()
        except Exception as exc:
            self.cpu_connected = False
            self.cpu_backend.close()
            if widgets:
                widgets["status"].config(text=f"CPU control unavailable: {exc}")
            if "PawnIOLib" in str(exc) or "pawnio_open" in str(exc):
                self.cpu_driver_button.pack(side="left", padx=8)
            return
        self.cpu_driver_button.pack_forget()
        if not caps["ratio_oc"]:
            self.cpu_backend.close()
            if widgets:
                widgets["status"].config(text="CPU control unavailable: this CPU reports no ratio-OC support")
            return
        if caps["stock_ratio"]:
            self.cpu_stock_ratio = caps["stock_ratio"]
        self.cpu_connected = True
        if not self.cpu_ratio_from_config and caps["stock_ratio"]:
            self.cpu_ratio_preset = min(caps["stock_ratio"] + 2, CPU_OC_MAX_RATIO)
        self.cpu_oc_switch.state(["!disabled"])
        self.cpu_auto_switch.state(["!disabled"])
        if widgets:
            stock = f"stock cap {caps['stock_ratio']}x" if caps["stock_ratio"] else "stock cap unknown"
            widgets["status"].config(
                text=f"Ready - {stock}; ON applies a verified {self.cpu_ratio_preset}x cap, OFF restores stock")

    def install_pawnio(self):
        from tkinter import messagebox
        if os.path.exists(PAWNIOLIB_PATH):
            self.connect_cpu_backend()
            return
        installer = os.path.join(APP_DIR, "PawnIO_setup.exe")
        if not os.path.exists(installer):
            messagebox.showerror("Installer missing",
                f"PawnIO_setup.exe not found in {APP_DIR}.\n"
                "Download PawnIO from https://pawnio.eu and install it, then restart FPS Control.",
                parent=self.root)
            return
        if messagebox.askyesno("Install PawnIO driver?",
            "This runs the PawnIO installer (admin prompt) so the CPU overclock\n"
            "switch can work. After it finishes, click this button again to connect.",
            parent=self.root):
            os.startfile(installer)

    def auto_arm_oc(self, now):
        """Zero-setup mode: enable OC switches automatically once everything
        is detected and healthy. Auto policies decide when to actually apply."""
        if self.oc_auto_armed or now > self.oc_arm_deadline:
            self.oc_auto_armed = True
            return
        if not self.cpu_auto_armed:
            self.cpu_auto_armed = True
            if self.cpu_connected and not self.cpu_oc_fault and not self.cpu_oc_value.get():
                self.cpu_oc_value.set(True)
                self.component_widgets["cpu"]["status"].config(
                    text="ON (Auto) - applies the cap under sustained CPU load, releases when idle")
        gpu = self.gpu_data
        info = self.gpu_control.get("info")
        if (info and gpu.get("uuid")
                and self.component_connection_ready() and not self.gpu_reset_failed
                and not self.gpu_busy):
            if not valid_auto_preset(self.gpu_auto_preset) \
                    or self.gpu_auto_preset.get("uuid") != gpu["uuid"]:
                preset = {"core": min(50.0, max(0.0, info["core"][2])),
                          "memory": min(100.0, max(0.0, info["memory"][2])),
                          "uuid": gpu["uuid"], "auto_default": True}
                if valid_auto_preset(preset):
                    self.gpu_auto_preset = preset
                    self.save_config()
            if (valid_auto_preset(self.gpu_auto_preset)
                    and self.gpu_auto_preset["uuid"] == gpu["uuid"]
                    and info["is_default"] and not self.gpu_owned):
                armed = False
                for key in ("core", "memory"):
                    if self.gpu_auto_preset[key] > info[key][3]:
                        self.gpu_components[key]["enabled"].set(True)
                        armed = True
                if armed:
                    self.component_session = True
                    self.gpu_auto_enabled.set(True)
                    self.gpu_auto_status.set(
                        "Auto-enabled with a conservative built-in preset; applies under sustained GPU load")
        if self.component_session:
            self.oc_auto_armed = True

    def tick_oc_tuner(self, now):
        """Gradual step-up tuner: raises GPU offsets and CPU ratio during
        sustained healthy load; backs off two steps on driver/WHEA errors."""
        if self.oc_tuner_done:
            return
        if self.oc_error_events:
            preset = self.gpu_auto_preset
            if valid_auto_preset(preset):
                preset["core"] = max(0.0, preset["core"] - 30)
                preset["memory"] = max(0.0, preset["memory"] - 60)
            self.cpu_ratio_preset = max(self.cpu_stock_ratio + 1, self.cpu_ratio_preset - 2)
            self.oc_tuner_done = True
            self.save_config()
            self.tune_status.set("Auto-tune: errors detected - stepped down and locked for this session")
            if self.gpu_owned and not self.gpu_busy:
                self.submit_gpu(reset=True, automatic=True)
            if self.cpu_oc_active:
                self.cpu_reset("CPU OC stepped down after error events")
            return
        load = max(self.snapshot.get("cpu_load") or 0, self.gpu_data.get("load") or 0)
        cpu_temp = self.snapshot.get("cpu_temp")
        gpu_temp = self.gpu_data.get("temp")
        temps_ok = (cpu_temp is None or cpu_temp < 75) and (gpu_temp is None or gpu_temp < 75)
        active = self.component_session or self.cpu_oc_value.get()
        if not (active and load >= 60 and temps_ok):
            self.oc_stable_since = None
            return
        if self.oc_stable_since is None:
            self.oc_stable_since = now
            return
        if now - self.oc_stable_since < 60:
            return
        self.oc_stable_since = now
        stepped = False
        preset = self.gpu_auto_preset
        if self.component_session and valid_auto_preset(preset):
            info = self.gpu_control.get("info") or {}
            core_max = min(100, info.get("core", (0, 0, 0, 0))[2])
            mem_max = min(250, info.get("memory", (0, 0, 0, 0))[2])
            if preset["core"] + 15 <= core_max:
                preset["core"] += 15
                preset["memory"] = min(mem_max, preset["memory"] + 30)
                stepped = True
        if self.cpu_connected and self.cpu_oc_value.get() and self.cpu_ratio_preset < CPU_OC_MAX_RATIO:
            self.cpu_ratio_preset += 1
            stepped = True
        if stepped:
            self.save_config()
            gpu_txt = (f"GPU +{preset['core']:.0f}/+{preset['memory']:.0f} MHz, "
                       if valid_auto_preset(preset) else "")
            self.tune_status.set(f"Auto-tune stepped up: {gpu_txt}CPU cap {self.cpu_ratio_preset}x")
        else:
            self.oc_tuner_done = True
            self.tune_status.set("Auto-tune reached its configured ceiling")

    def on_cpu_auto_change(self):
        from tkinter import messagebox
        if not self.cpu_auto_value.get() and not messagebox.askyesno("Turn off Auto CPU?",
            "With Auto off, the CPU cap is not re-applied automatically after a thermal reset.\n"
            "Continue?", parent=self.root):
            self.cpu_auto_value.set(True)

    def on_cpu_toggle(self):
        from tkinter import messagebox
        if not self.cpu_oc_value.get():
            self.cpu_oc_paused = False
            self.cpu_reset("CPU OC off - stock ratio limit restored")
            return
        temp = self.snapshot.get("cpu_temp")
        problem = None
        if not self.cpu_connected:
            problem = "CPU control unavailable: PawnIO connection failed."
        elif self.cpu_oc_fault:
            problem = f"Clear this fault before enabling: {self.cpu_oc_fault}"
        elif (time.monotonic() - self.snapshot.get("updated_at", 0) > SENSOR_TIMEOUT
                or temp is None or not math.isfinite(temp) or not 0 <= temp <= 125):
            problem = "CPU temperature unavailable; refusing to enable."
        elif temp >= CPU_OC_RESET_TEMP:
            problem = f"CPU too hot ({temp:.0f} C); refusing to enable."
        if problem:
            self.cpu_oc_value.set(False)
            self.component_widgets["cpu"]["status"].config(text=problem)
            return
        ratio = self.cpu_ratio_preset
        behavior = "apply under sustained CPU load and release when idle" if self.cpu_auto_value.get() \
                   else "apply the cap continuously"
        if not messagebox.askyesno("Enable CPU overclock?",
            f"Raise the maximum turbo ratio cap to {ratio}x (about {ratio / 10.0:.1f} GHz ceiling)?\n"
            f"With Auto {'on, it will ' + behavior if self.cpu_auto_value.get() else 'off, it will ' + behavior}.\n"
            "The CPU still boosts on demand - this raises the ceiling, not a fixed speed.\n\n"
            "This is NOT a measured maximum; stability under load is unverified.\n"
            "Voltage settings are unchanged. Thermal reset restores stock automatically.", parent=self.root):
            self.cpu_oc_value.set(False)
            return
        self.cpu_load_high_since = self.cpu_load_low_since = None
        if self.cpu_auto_value.get():
            self.component_widgets["cpu"]["status"].config(
                text="ON (Auto) - applies the cap under sustained CPU load, releases when idle")
        else:
            self.cpu_apply()

    def cpu_apply(self):
        ratio = self.cpu_ratio_preset
        try:
            self.cpu_backend.set_ratio(ratio)
            if self.cpu_power_raw is None:
                try:
                    pl = self.cpu_backend.read_power_limits()
                    self.cpu_power_raw = pl["raw"]
                    self.cpu_backend.set_power_limits(min(pl["pl1_w"] * 1.25, 250),
                                                      max(pl["pl2_w"], min(pl["pl1_w"] * 1.5, 250)))
                except Exception:
                    self.cpu_power_raw = None
        except Exception as exc:
            self.cpu_oc_active = False
            self.cpu_oc_value.set(False)
            self.cpu_oc_fault = str(exc)
            self.component_widgets["cpu"]["status"].config(
                text=f"CPU apply failed: {exc} - attempting restore")
            self.cpu_reset(None)
            return
        self.cpu_oc_active = True
        self.cpu_oc_paused = False
        self.component_widgets["cpu"]["status"].config(
            text=f"ON - ratio cap {ratio}x verified | OFF restores stock")

    def cpu_reset(self, reason):
        if not self.cpu_connected:
            self.cpu_oc_active = False
            if reason:
                self.component_widgets["cpu"]["status"].config(text=reason)
            return True
        try:
            self.cpu_backend.restore()
            if self.cpu_power_raw is not None:
                try:
                    self.cpu_backend.restore_power_limits(self.cpu_power_raw)
                except Exception:
                    pass
                self.cpu_power_raw = None
        except Exception as exc:
            self.cpu_oc_fault = str(exc)
            self.component_widgets["cpu"]["status"].config(
                text=f"CPU reset FAILED - {exc}. Stock state unknown; check and retry.")
            return False
        self.cpu_oc_active = False
        if reason:
            self.component_widgets["cpu"]["status"].config(text=reason)
        return True

    def tick_cpu(self, now):
        if not self.cpu_oc_value.get() or not self.cpu_connected or self.cpu_oc_fault:
            return
        temp = self.snapshot.get("cpu_temp")
        load = self.snapshot.get("cpu_load")
        healthy = (now - self.snapshot.get("updated_at", 0) <= SENSOR_TIMEOUT
                   and temp is not None and math.isfinite(temp) and 0 <= temp <= 125)
        if self.cpu_oc_active and (not healthy or temp >= CPU_OC_RESET_TEMP):
            self.cpu_oc_paused = "thermal"
            self.cpu_load_high_since = self.cpu_load_low_since = None
            self.cpu_reset("CPU OC auto-reset: temperature too high or unavailable")
            return
        if self.cpu_auto_value.get():
            busy = load is not None and load >= 70
            idle = load is None or load <= 20
            if busy:
                self.cpu_load_high_since = self.cpu_load_high_since or now
                self.cpu_load_low_since = None
            elif idle:
                self.cpu_load_low_since = self.cpu_load_low_since or now
                self.cpu_load_high_since = None
            else:
                self.cpu_load_high_since = self.cpu_load_low_since = None
            if (self.cpu_oc_active and self.cpu_load_low_since
                    and now - self.cpu_load_low_since >= 30):
                self.cpu_oc_paused = "idle"
                self.cpu_reset("CPU OC auto: idle - stock restored until load returns")
            elif (not self.cpu_oc_active and healthy and temp < CPU_OC_APPLY_TEMP
                    and self.cpu_load_high_since and now - self.cpu_load_high_since >= 10):
                self.cpu_oc_paused = None
                self.cpu_apply()

    def on_component_auto_change(self, key):
        from tkinter import messagebox
        component = self.gpu_components[key]
        component["policy"] = AutoGpuPolicy()
        if not component["enabled"].get():
            return
        if not component["auto"].get() and not messagebox.askyesno("Use continuous overclock?",
            "With Auto off, this component requests its tested preset continuously until switched OFF.\n"
            "Thermal and fault resets still apply. Continue?", parent=self.root):
            component["auto"].set(True)
            return
        self.tick_components(time.monotonic(), self.component_connection_ready())

    def tick_components(self, now, connected):
        if not self.component_session or self.gpu_busy:
            return
        preset = self.gpu_auto_preset
        info = self.gpu_control.get("info")
        if valid_auto_preset(preset) and self.gpu_data.get("uuid") and preset["uuid"] != self.gpu_data["uuid"]:
            self.stop_components("GPU changed; no settings sent to the different GPU")
            self.gpu_owned = False
            return
        fresh = connected and now - self.gpu_data.get("updated_at", 0) <= SENSOR_TIMEOUT and now - self.snapshot.get("updated_at", 0) <= SENSOR_TIMEOUT
        if (not fresh or not valid_auto_preset(preset) or self.gpu_reset_failed
                or preset["uuid"] != self.gpu_data.get("uuid") or info["flags"] & 0x40000):
            self.stop_components("Overclock requests stopped: connection, sensor, or preset check failed")
            if self.gpu_owned and not self.gpu_reset_failed:
                self.submit_gpu(reset=True, automatic=True)
            return
        actual = [info[key][0] for key in ("core", "memory")]
        defaults = [info[key][3] for key in ("core", "memory")]
        if ((self.gpu_owned and self.gpu_managed_offsets is not None and actual != list(self.gpu_managed_offsets))
                or (not self.gpu_owned and not info["is_default"])):
            self.stop_components("External offsets changed; no automatic reset sent.")
            self.gpu_owned = False
            return
        cpu, temp, load = self.snapshot.get("cpu_temp"), self.gpu_data.get("temp"), self.gpu_data.get("load")
        valid = (cpu is not None and temp is not None and load is not None and math.isfinite(cpu)
                 and math.isfinite(temp) and math.isfinite(load) and cpu >= 0 and temp >= 0 and 0 <= load <= 100)
        if not valid:
            self.stop_components("Overclock requests stopped: sensor reading unavailable")
            if self.gpu_owned:
                self.submit_gpu(reset=True, automatic=True)
            return
        hot = cpu >= CRIT_TEMP or temp >= GPU_RESET_TEMP
        scale = oc_scale(temp)
        desired = list(actual)
        for index, key in enumerate(("core", "memory")):
            component = self.gpu_components.get(key)
            if component is None:
                desired[index] = defaults[index]
                continue
            if not component["enabled"].get() or hot:
                component["applied"] = False
                desired[index] = defaults[index]
                if hot:
                    component["policy"].decide(now, load, temp, actual[index] != defaults[index], False)
            elif not component["auto"].get():
                component["applied"] = True
                desired[index] = preset[key]
            else:
                action = component["policy"].decide(now, load, temp, component["applied"])
                if action == "apply":
                    component["applied"] = True
                elif action == "reset":
                    component["applied"] = False
                desired[index] = (round(preset[key] * scale) if component["applied"]
                                  else defaults[index])
        if desired != actual:
            significant = (desired == defaults or any(desired[i] == defaults[i]
                           or abs(desired[i] - actual[i]) >= 3 for i in range(2)))
            if significant:
                self.submit_gpu(reset=desired == defaults, automatic=True, desired_offsets=desired)
        if desired == defaults:
            self.gpu_auto_status.set("Waiting for load/temperature headroom")
        else:
            self.gpu_auto_status.set(
                f"Preset active, scaled to {scale * 100:.0f}% by temperature; thermal monitoring continues")

    def save_gpu_preset(self):
        from tkinter import messagebox
        info = self.gpu_control.get("info")
        now = time.monotonic()
        if self.gpu_busy or self.gpu_auto_enabled.get():
            self.gpu_auto_status.set("Turn Auto OC off and wait for any pending operation before saving a preset")
            return
        if not info or now - self.gpu_control.get("updated_at", 0) > SENSOR_TIMEOUT:
            self.gpu_auto_status.set("Fresh GPU readback required to save a preset")
            return
        # Saving only captures current offsets - no overclock is applied here,
        # so temperature limits do not apply. A pending command or active VF
        # curve can still make the readback unrepresentative, so keep those.
        if info.get("command"):
            self.gpu_auto_status.set("GPU clock service is processing another command")
            return
        if info.get("flags", 0) & 0x40000:
            self.gpu_auto_status.set("A custom GPU voltage/frequency curve is active; reset GPU settings to stock first")
            return
        staged = [info[key][0] for key in ("core", "memory")]
        preset = {"core": staged[0], "memory": staged[1], "uuid": self.gpu_data.get("uuid"), "user_tested": True}
        if not valid_auto_preset(preset) or staged != [info[key][0] for key in ("core", "memory")]:
            self.gpu_auto_status.set("Apply and manually test these exact nonzero offsets before saving them")
            return
        if not messagebox.askyesno("Save your tested GPU preset?",
            f"Have you manually tested core {staged[0]:+.0f} MHz / memory {staged[1]:+.0f} MHz\n"
            "independently AND together under sustained load without crashes or visual glitches?\n\n"
            "FPS Control has NOT tested stability. Save only if you have tested these exact settings.", parent=self.root):
            return
        self.gpu_auto_preset = preset
        self.save_config()
        self.gpu_auto_status.set("User-tested settings saved; maximum still uncalibrated. Reset offsets, then use the ON switches.")

    def on_gpu_auto_toggle(self):
        from tkinter import messagebox
        self.gpu_auto_policy = AutoGpuPolicy()
        if not self.gpu_auto_enabled.get():
            self.gpu_auto_status.set("Auto OC off; returning app-managed offsets to defaults")
            if self.gpu_owned or self.gpu_busy:
                self.submit_gpu(reset=True, automatic=True)
            return
        preset = self.gpu_auto_preset
        info = self.gpu_control.get("info")
        problem = None
        if not valid_auto_preset(preset) or preset["uuid"] != self.gpu_data.get("uuid"):
            problem = "Save a tested preset for this GPU before enabling Auto OC"
        elif self.gpu_busy or self.gpu_reset_failed or not info or not info["is_default"]:
            problem = "Reset clock offsets and wait for confirmed defaults before enabling Auto OC"
        elif time.monotonic() - self.gpu_control.get("updated_at", 0) > SENSOR_TIMEOUT:
            problem = "Fresh GPU readback required before enabling Auto OC"
        else:
            problem = gpu_apply_problem(self.snapshot, self.gpu_data, info, time.monotonic())
        if problem:
            self.gpu_auto_enabled.set(False)
            self.gpu_auto_status.set(problem)
            return
        if not messagebox.askyesno("Enable Auto GPU OC?",
            f"Automatically use your user-tested core {preset['core']:+.0f} MHz / memory {preset['memory']:+.0f} MHz\n"
            "preset under sustained GPU load, and reset clock offsets when idle or too hot?\n\n"
            "This does not find or verify a stable overclock. Auto OC starts off next launch.", parent=self.root):
            self.gpu_auto_enabled.set(False)
            return
        self.gpu_auto_status.set("Auto OC armed - waiting for sustained GPU load and temperature headroom")

    def tick_gpu_auto(self, now, connected):
        if not self.gpu_auto_enabled.get() or self.gpu_busy:
            return
        preset = self.gpu_auto_preset
        info = self.gpu_control.get("info")
        fresh = (connected and now - self.gpu_data.get("updated_at", 0) <= SENSOR_TIMEOUT
                 and now - self.snapshot.get("updated_at", 0) <= SENSOR_TIMEOUT)
        if valid_auto_preset(preset) and self.gpu_data.get("uuid") and preset["uuid"] != self.gpu_data["uuid"]:
            self.gpu_auto_enabled.set(False)
            self.gpu_owned = False
            self.gpu_auto_status.set("Auto OC disabled: GPU changed; no settings sent to the different GPU")
            return
        if (not fresh or self.gpu_reset_failed or not valid_auto_preset(preset)
                or preset["uuid"] != self.gpu_data.get("uuid") or info["flags"] & 0x40000):
            self.gpu_auto_enabled.set(False)
            self.gpu_auto_status.set("Auto OC disabled: connection, sensor, GPU identity, or preset check failed")
            if self.gpu_owned and not self.gpu_reset_failed:
                self.submit_gpu(reset=True, automatic=True)
            return
        actual = [info[key][0] for key in ("core", "memory")]
        if ((self.gpu_owned and actual != [preset["core"], preset["memory"]])
                or (not self.gpu_owned and not info["is_default"])):
            self.gpu_auto_enabled.set(False)
            self.gpu_owned = False
            self.gpu_auto_status.set("Auto OC disabled: clock offsets were changed by another app")
            return
        cpu = self.snapshot.get("cpu_temp")
        healthy = cpu is not None and math.isfinite(cpu) and 0 <= cpu < CRIT_TEMP
        action = self.gpu_auto_policy.decide(now, self.gpu_data.get("load"), self.gpu_data.get("temp"),
                                            self.gpu_owned, healthy)
        if action == "apply":
            self.submit_gpu(automatic=True)
        elif action == "reset":
            self.submit_gpu(reset=True, automatic=True)
        elif self.gpu_owned:
            self.gpu_auto_status.set("Auto OC: user-tested offsets active")
        else:
            gap = self.gpu_auto_policy.last_gap
            suffix = f" | Sampling gap {gap:.1f}s restarted load timers" if gap is not None else ""
            self.gpu_auto_status.set("Auto OC armed: waiting for sustained load, below 167 F, and cooldown" + suffix)

    def on_gpu_toggle(self):
        reset = self.gpu_switch_value.get() == 0
        self.gpu_switch_value.set(-1)
        self.submit_gpu(reset=reset)

    def submit_gpu(self, reset=False, automatic=False, desired_offsets=None):
        from tkinter import messagebox
        if reset and not automatic:
            self.stop_components("GPU overclock requests OFF")
        if self.gpu_busy:
            if reset:
                self.gpu_stop_after_busy = True
            return
        if not reset and not automatic and self.gpu_auto_enabled.get():
            self.gpu_notice.set("Turn off Auto GPU OC before applying manual offsets")
            return
        if automatic and not reset and (not self.gpu_auto_enabled.get()
                or not valid_auto_preset(self.gpu_auto_preset)
                or self.gpu_auto_preset["uuid"] != self.gpu_data.get("uuid")):
            self.gpu_auto_enabled.set(False)
            self.gpu_auto_status.set("Auto OC disabled: tested preset is missing or belongs to another GPU")
            return
        if self.gpu_reset_failed and not reset:
            self.gpu_notice.set("Reset clock offsets before requesting another overclock")
            return
        info = self.gpu_control.get("info")
        reducing = False
        if desired_offsets is not None and not reset:
            if (not automatic or not info or not valid_auto_preset(self.gpu_auto_preset) or len(desired_offsets) != 2
                    or any(not (info[key][3] <= value <= self.gpu_auto_preset[key])
                           for key, value in zip(("core", "memory"), desired_offsets))):
                self.gpu_notice.set("Component request must stay between default and the user-tested preset")
                return
            reducing = all(value <= info[key][0] for key, value in zip(("core", "memory"), desired_offsets))
        if not reset:
            problem = "GPU clock readback unavailable"
            if info and time.monotonic() - self.gpu_control.get("updated_at", 0) <= SENSOR_TIMEOUT:
                problem = None if reducing else gpu_apply_problem(self.snapshot, self.gpu_data, info, time.monotonic())
            if problem:
                self.gpu_notice.set(problem)
                return
            if not info["is_default"] and not self.gpu_owned:
                self.gpu_notice.set("Existing offsets detected. Reset them before FPS Control takes ownership.")
                return
            if desired_offsets is not None:
                core, memory = desired_offsets
            elif automatic:
                core, memory = self.gpu_auto_preset["core"], self.gpu_auto_preset["memory"]
            else:
                core, memory = round(self.gpu_core_value.get()), round(self.gpu_memory_value.get())
            if core == info["core"][3] and memory == info["memory"][3]:
                self.gpu_notice.set("No overclock requested. Both staged offsets are defaults.")
                return
            if not automatic and not messagebox.askyesno("Apply GPU clock offsets?",
                f"Request core {core:+d} MHz and memory {memory:+d} MHz?\n\n"
                "An unstable offset can crash the game or PC. Save your work first.\n"
                "Voltage and power limits will not be increased.", parent=self.root):
                return
            problem = None if reducing else gpu_apply_problem(self.snapshot, self.gpu_data, info, time.monotonic())
            if problem:
                self.gpu_notice.set(problem)
                return
        else:
            core = memory = None
        expected = None if reset else [info[key][0] for key in ("core", "memory")]
        was_owned = self.gpu_owned
        self.gpu_busy = True
        self.gpu_owned = True
        self.gpu_reset_failed = False
        self.gpu_switch_value.set(-1)
        self.gpu_notice.set("Resetting clock offsets..." if reset else "Applying requested offsets; waiting for readback...")

        def operation():
            try:
                result = self.gpu_backend.apply(core, memory, reset=reset, expected=expected)
                self.gpu_results.put((reset, result, None))
            except Exception as exc:
                self.gpu_results.put((reset, None, {"message": str(exc), "was_owned": was_owned,
                    "touched": getattr(exc, "gpu_may_have_applied", True),
                    "external": isinstance(exc, GpuSettingsChangedError)}))

        threading.Thread(target=operation, daemon=True).start()

    def refresh_performance(self):
        while not self.gpu_results.empty():
            reset, result, error = self.gpu_results.get_nowait()
            self.gpu_busy = False
            if error:
                self.stop_components("Overclock requests stopped after an operation failure")
                self.gpu_stop_after_busy = False
                details = error if isinstance(error, dict) else {"message": error, "touched": True, "was_owned": True}
                self.gpu_notice.set("GPU request failed: " + details["message"])
                self.gpu_switch_value.set(-1)
                if details.get("external"):
                    self.gpu_owned = False
                    self.gpu_reset_failed = False
                    self.gpu_notice.set("External clock settings changed; no automatic reset sent.")
                elif reset:
                    self.gpu_owned = details["touched"] or details["was_owned"]
                    self.gpu_reset_failed = True
                    self.close_pending = False
                elif details["touched"] or details["was_owned"]:
                    self.submit_gpu(reset=True, automatic=True)
                else:
                    self.gpu_owned = False
                if self.close_pending and not self.gpu_owned:
                    self.close_pending = False
                    self.root.after_idle(self.on_close)
            else:
                self.gpu_control = {"info": result, "updated_at": time.monotonic()}
                self.gpu_owned = not result["is_default"]
                self.gpu_managed_offsets = tuple(result[key][0] for key in ("core", "memory"))
                self.gpu_notice.set("Default clock offsets confirmed." if result["is_default"] else
                                    "Offsets applied; stability has not been tested.")
                if reset:
                    self.gpu_stop_after_busy = False
                    self.gpu_core_value.set(result["core"][3])
                    self.gpu_memory_value.set(result["memory"][3])
                elif self.gpu_stop_after_busy:
                    self.gpu_stop_after_busy = False
                    self.submit_gpu(reset=True, automatic=True)
                if self.close_pending:
                    self.close_pending = False
                    self.root.after_idle(self.on_close)
        now = time.monotonic()
        gpu = self.gpu_data
        gpu_fresh = now - gpu.get("updated_at", 0) <= SENSOR_TIMEOUT
        def show(value, unit=""):
            return "--" if value is None else f"{value:.0f}{unit}"
        temp = gpu.get("temp") if gpu_fresh else None
        text = (f"{gpu.get('name', 'NVIDIA GPU')} | "
                f"{show(None if temp is None else temp * 9 / 5 + 32, ' F')} | "
                f"Load {show(gpu.get('load') if gpu_fresh else None, '%')} | "
                f"GPU fan command {show(gpu.get('fan') if gpu_fresh else None, '%')}\n"
                f"Core {show(gpu.get('core_clock') if gpu_fresh else None, ' MHz')} | "
                f"Memory {show(gpu.get('memory_clock') if gpu_fresh else None, ' MHz')} | "
                f"Power {show(gpu.get('power') if gpu_fresh else None, ' W')}")
        self.gpu_monitor_label.config(text=text)
        self.cpu_monitor_label.config(text=f"{self.snapshot.get('cpu_name', 'CPU')} | "
            f"Load {show(self.snapshot.get('cpu_load'), '%')} | "
            f"Highest reported core clock {show(self.snapshot.get('cpu_clock'), ' MHz')}")
        info = self.gpu_control.get("info")
        connected = bool(info and now - self.gpu_control.get("updated_at", 0) <= SENSOR_TIMEOUT
                         and not info.get("command"))
        usable = connected and not self.gpu_busy and not (info["flags"] & 0x40000)
        self.gpu_save_preset_button.state(["!disabled"] if usable and not self.gpu_auto_enabled.get() else ["disabled"])
        self.gpu_reset_button.state(["!disabled"] if usable else ["disabled"])
        preset_ok = valid_auto_preset(self.gpu_auto_preset) and self.gpu_auto_preset["uuid"] == gpu.get("uuid")
        for key, component in self.gpu_components.items():
            widgets = self.component_widgets.get(key)
            if not widgets:
                continue
            ready = usable and preset_ok and self.gpu_auto_preset[key] > info[key][3] and not self.gpu_reset_failed
            widgets["switch"].state(["!disabled"] if component["enabled"].get() or ready else ["disabled"])
            if not preset_ok:
                text = "Locked - waiting for the automatic GPU clock preset."
            elif not connected:
                text = "Connection unavailable; actual speed unknown."
            else:
                mode = "Auto" if component["auto"].get() else "Continuous"
                request = "ON" if component["enabled"].get() else "OFF"
                text = f"Requested {request} ({mode})\nReadback {info[key][0]:+.0f} MHz\nPreset {self.gpu_auto_preset[key]:+.0f} MHz, not a measured maximum"
            widgets["status"].config(text=text)
        if usable:
            self.gpu_switch_value.set(-1 if self.gpu_reset_failed else int(not info["is_default"]))
            self.gpu_readback_label.config(text=f"GPU readback: core {info['core'][0]:+.0f} MHz, "
                f"memory {info['memory'][0]:+.0f} MHz. " + ("Default offsets." if info["is_default"] else "Offsets active."))
        else:
            self.gpu_switch_value.set(-1)
            self.gpu_readback_label.config(text=self.gpu_control.get("error", "Waiting for GPU clock readback"))
        if self.component_session:
            self.tick_components(now, connected)
        else:
            self.tick_gpu_auto(now, connected)
        if self.gpu_owned and not self.gpu_busy and not self.gpu_reset_failed:
            cpu = self.snapshot.get("cpu_temp")
            unsafe = (not gpu_fresh or temp is None or not math.isfinite(temp) or not 0 <= temp < GPU_RESET_TEMP
                or now - self.snapshot.get("updated_at", 0) > SENSOR_TIMEOUT
                or cpu is None or not math.isfinite(cpu) or not 0 <= cpu < CRIT_TEMP)
            if unsafe:
                self.submit_gpu(reset=True, automatic=True)

    def build_group_card(self, title, var, floor):
        card = ttk.LabelFrame(self.cooling_tab, text=f" {title} ")
        card.pack(fill="x", padx=16, pady=8)
        scale = ttk.Scale(card, from_=floor, to=100, variable=var,
                          command=lambda _v: self.on_slider(var))
        scale.pack(fill="x", padx=12, pady=(8, 0))
        row = ttk.Frame(card)
        row.pack(fill="x", padx=12, pady=(0, 8))
        lbl = ttk.Label(row, text="")
        lbl.pack(side="left")
        ttk.Label(row, text=f"range {floor}-100%").pack(side="right")
        var._card_label = lbl

    def on_role_change(self, channel):
        if self.roles[channel].get() == "Unused" and channel in self.controls:
            with self.hardware_lock:
                self.controls[channel].Control.SetDefault()
        self.save_config()

    def on_slider(self, var):
        if self._programmatic:
            return
        index = 0 if var is self.liquid_val else 1
        values = self.auto_boost if self.auto_mode.get() else self.manual_values
        values[index] = min(100.0, max(BASE_DUTY, var.get()))
        self.apply_speeds()
        self.save_config()

    def on_mode_change(self):
        if self.auto_mode.get():
            self.auto_boost = [BASE_DUTY, BASE_DUTY]
        else:
            self.manual_values = list(self.last_applied)
        self.apply_speeds()
        self.save_config()

    def start_test(self, channel):
        if channel not in self.controls or self.roles[channel].get() not in ROLE_NAMES[1:]:
            return
        self.test_channel = channel
        self.test_until = time.monotonic() + 4
        self.apply_speeds()

    # ---------- main loop ----------
    def apply_speeds(self):
        now = time.monotonic()
        temp = self.snapshot.get("cpu_temp")
        if now - self.snapshot.get("updated_at", 0) > SENSOR_TIMEOUT:
            temp = None
        gpu = getattr(self, "gpu_data", {})
        gpu_fresh = now - gpu.get("updated_at", 0) <= SENSOR_TIMEOUT
        self.auto_duty = target_duty(self.auto_duty, temp, now - self.last_control_time,
                                    load=self.snapshot.get("cpu_load"),
                                    gpu_temp=gpu.get("temp") if gpu_fresh else None,
                                    gpu_load=gpu.get("load") if gpu_fresh else None)
        self.last_control_time = now
        if self.test_channel is not None and now >= self.test_until:
            self.test_channel = None
        minimums = self.auto_boost if self.auto_mode.get() else self.manual_values
        requested = tuple(min(100.0, max(self.auto_duty, value)) for value in minimums)
        for role, value in zip(ROLE_NAMES[1:], requested):
            for ch in self.channels_for_role(role):
                self.set_duty(ch, 100 if ch == self.test_channel else value)
        self.last_applied = requested
        self._programmatic = True
        try:
            self.liquid_val.set(requested[0])
            self.case_val.set(requested[1])
        finally:
            self._programmatic = False

    def tick(self):
        try:
            self.apply_speeds()
            temp = self.snapshot.get("cpu_temp")
            if (time.monotonic() - self.snapshot.get("updated_at", 0) > SENSOR_TIMEOUT
                    or temp is None or not math.isfinite(temp) or not 0 <= temp <= 125):
                temp = None
            load = self.snapshot.get("cpu_load")
            if temp is None:
                self.temp_label.config(text="-- F")
            else:
                self.temp_label.config(text=f"{temp * 9.0 / 5.0 + 32:.0f} F")
                color = "#7ec8ff" if temp < 60 else "#ffd166" if temp < 75 else "#ff6b6b"
                self.temp_label.config(foreground=color)
            self.load_label.config(text=f"CPU load: {'--' if load is None else f'{load:.0f}'}%")
            if temp is None:
                status = "Temperature unavailable - requesting 100%"
            elif temp >= CRIT_TEMP:
                status = "CRITICAL TEMP - requesting 100%"
            elif min(self.last_applied) >= 100:
                status = "Load-aware cooling - full cooling requested"
            else:
                status = "Load-aware | " + ("AUTO" if self.auto_mode.get() else "MANUAL + thermal minimum")
            self.status_label.config(text=status)
            la, ca = self.last_applied
            self.liquid_val._card_label.config(text=f"Requested: {la:.0f}% | base: 50%")
            self.case_val._card_label.config(text=f"Requested: {ca:.0f}% | base: 50%")
            for i, (_, info_lbl) in self.chan_rows.items():
                duty = self.snapshot["duty"].get(i)
                rpm = self.snapshot["rpm"].get(i)
                parts = []
                if duty is not None:
                    parts.append(f"{duty:.0f}%")
                if rpm is not None:
                    parts.append(f"{rpm:.0f} RPM")
                info_lbl.config(text=" | ".join(parts) if parts else "no data")
        except Exception:
            self.restore_all()
            self.status_label.config(text="Control error - attempted return to motherboard control")
        try:
            self.refresh_performance()
        except Exception:
            self.gpu_notice.set("GPU monitoring error; cooling continues")
            if self.gpu_owned and not self.gpu_busy and not self.gpu_reset_failed:
                self.submit_gpu(reset=True, automatic=True)
        try:
            self.tick_cpu(time.monotonic())
        except Exception:
            pass
        try:
            self.auto_arm_oc(time.monotonic())
        except Exception:
            pass
        try:
            self.tick_oc_tuner(time.monotonic())
        except Exception:
            pass
        try:
            self.tick_game(time.monotonic())
            self.tick_cleanup(time.monotonic())
        except Exception:
            pass
        try:
            self.tick_gpu_fan()
        except Exception:
            pass
        try:
            now = time.monotonic()
            cpu_clk = self.snapshot.get("cpu_clock")
            if now - self.snapshot.get("updated_at", 0) > SENSOR_TIMEOUT:
                cpu_clk = None
            gpu_clk = self.gpu_data.get("core_clock")
            if now - self.gpu_data.get("updated_at", 0) > SENSOR_TIMEOUT:
                gpu_clk = None
            info = self.gpu_control.get("info")
            gpu_fresh = now - self.gpu_data.get("updated_at", 0) <= SENSOR_TIMEOUT
            if info and info.get("is_default") and gpu_fresh:
                load = self.gpu_data.get("load") or 0
                if gpu_clk and load >= 50:
                    self.gpu_stock_clock = max(self.gpu_stock_clock or 0, gpu_clk)
            parts = []
            if self.cpu_connected:
                cap_pct = (self.cpu_ratio_preset / self.cpu_stock_ratio - 1) * 100
                stock_mhz = self.cpu_stock_ratio * 100
                if self.cpu_oc_active and cpu_clk and cpu_clk > stock_mhz:
                    parts.append(f"CPU +{(cpu_clk / stock_mhz - 1) * 100:.1f}% past stock")
                elif self.cpu_oc_active:
                    parts.append(f"CPU cap +{cap_pct:.0f}% (idle - boosts when needed)")
                elif self.cpu_oc_paused:
                    parts.append("CPU OC paused (temperature)")
                elif self.cpu_oc_value.get():
                    parts.append(f"CPU OC ready (cap +{cap_pct:.0f}%)")
                else:
                    parts.append("CPU at stock")
            applied = self.gpu_managed_offsets or (0, 0)
            if any(v > 0 for v in applied):
                gpu_parts = []
                for offset, clock, name in ((applied[0], gpu_clk, "core"),
                                            (applied[1], self.gpu_data.get("memory_clock"), "mem")):
                    if offset > 0:
                        base = clock - offset if clock and clock > offset else None
                        gpu_parts.append(f"{name} ~{(offset / base) * 100:.1f}%" if base
                                         else f"{name} +{offset:.0f} MHz")
                parts.append("GPU " + ", ".join(gpu_parts) + " faster" if gpu_parts else "GPU OC applied")
            elif self.component_session:
                parts.append("GPU OC on - 0% applied (idle or hot)")
            else:
                parts.append("GPU at stock")
            self.oc_benefit_label.config(text="OC benefit: " + " | ".join(parts))
        except Exception:
            pass
        if not self.stop_flag.is_set():
            self.root.after(1000, self.tick)

    def on_game_boost_toggle(self):
        self.save_config()
        if not self.game_boost_value.get():
            self.boosting_games = set()
            self.game_booster.release()
            self._restore_power_scheme()
            self._resume_services()
            self._boost_tweaks(False)
            self.game_boost_status.config(text="off - boosts the listed game while it runs")
        else:
            self._boost_tweaks(True)
            self.game_boost_status.config(text="watching for a listed game...")

    def _reg_set(self, hive, path, name, val, kind="dword"):
        """Save the previous value (once) then write the boost value."""
        import winreg
        hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
        try:
            with winreg.OpenKey(hives[hive], path, 0,
                                winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                key_id = (hive, path, name)
                if key_id not in self.tweak_saved:
                    try:
                        self.tweak_saved[key_id] = winreg.QueryValueEx(key, name)[0]
                    except OSError:
                        self.tweak_saved[key_id] = None
                winreg.SetValueEx(key, name, 0,
                                  winreg.REG_SZ if kind == "sz" else winreg.REG_DWORD, val)
        except OSError:
            pass

    def _boost_tweaks(self, on):
        """Registry-level gaming tweaks: DVR/Game Bar off, Game Mode on, no
        multimedia CPU reservation, no network throttling, foreground boost,
        best-performance visual effects, MMCSS game profile. Everything is
        saved and restored when boost is turned off."""
        import winreg
        if on:
            if not self.tweak_saved:
                for hive, path, name, val, kind in BOOST_REG_TWEAKS:
                    self._reg_set(hive, path, name, val, kind)
                # Kill Nagle + delayed ACK on interfaces that have an IP.
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, TCPIP_IFACES,
                                        0, winreg.KEY_READ) as root_key:
                        i = 0
                        while True:
                            try:
                                sub = winreg.EnumSubKey(root_key, i)
                            except OSError:
                                break
                            i += 1
                            ipath = TCPIP_IFACES + "\\" + sub
                            try:
                                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ipath,
                                                    0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as ik:
                                    try:
                                        ip = winreg.QueryValueEx(ik, "DhcpIPAddress")[0]
                                    except OSError:
                                        try:
                                            ip = winreg.QueryValueEx(ik, "IPAddress")[0]
                                        except OSError:
                                            ip = ""
                                    if not ip or ip == "0.0.0.0":
                                        continue
                                    for n, v in (("TCPNoDelay", 1), ("TcpDelAckTicks", 0)):
                                        key_id = ("HKLM", ipath, n)
                                        try:
                                            self.tweak_saved.setdefault(
                                                key_id, winreg.QueryValueEx(ik, n)[0])
                                        except OSError:
                                            self.tweak_saved.setdefault(key_id, None)
                                        winreg.SetValueEx(ik, n, 0, winreg.REG_DWORD, v)
                            except OSError:
                                continue
                except Exception:
                    pass
                self._broadcast_settings()
            try:
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\GameBar")
                winreg.SetValueEx(key, "AllowAutoGameMode", 0, winreg.REG_DWORD, 1)
                winreg.CloseKey(key)
            except OSError:
                pass
        else:
            hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
            for (hive, path, name), prev in list(self.tweak_saved.items()):
                try:
                    with winreg.OpenKey(hives[hive], path, 0,
                                        winreg.KEY_SET_VALUE) as key:
                        if prev is None:
                            winreg.DeleteValue(key, name)
                        elif isinstance(prev, int):
                            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, prev)
                        else:
                            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, prev)
                except OSError:
                    pass
            self.tweak_saved = {}
            self._broadcast_settings()
            self._compat_restore()

    def _broadcast_settings(self):
        """Tell running apps the registry changed (visual effects etc.)."""
        try:
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 2, 1000, None)
        except Exception:
            pass

    def _exe_paths(self, pids):
        paths = set()
        for pid in pids:
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ExecutablePath"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
                if out:
                    paths.add(out)
            except Exception:
                pass
        return paths

    def _compat_game_exes(self, pids):
        """Disable DX 'maximized windowed' fullscreen emulation for the game
        exe - forces the classic exclusive-fullscreen path, which can help
        frame pacing. Applies to the exe, so it takes effect on next launch;
        kept while boost is enabled, restored when it's turned off."""
        import winreg
        new = [p for p in pids if p not in self.compat_pids]
        if not new:
            return
        self.compat_pids.update(new)
        for path in self._exe_paths(new):
            if path in self.compat_saved:
                continue
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPCOMPAT_LAYERS, 0,
                                    winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                    try:
                        prev = winreg.QueryValueEx(key, path)[0]
                    except OSError:
                        prev = None
                    self.compat_saved[path] = prev
                    flags = prev or ""
                    if "DISABLEDXMAXIMIZEDWINDOWEDMODE" not in flags.upper():
                        new_flags = (flags + " DISABLEDXMAXIMIZEDWINDOWEDMODE").strip()
                        if not new_flags.startswith("~"):
                            new_flags = "~ " + new_flags
                        winreg.SetValueEx(key, path, 0, winreg.REG_SZ, new_flags)
            except OSError:
                pass

    def _compat_restore(self):
        import winreg
        for path, prev in list(self.compat_saved.items()):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPCOMPAT_LAYERS, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    if prev is None:
                        winreg.DeleteValue(key, path)
                    else:
                        winreg.SetValueEx(key, path, 0, winreg.REG_SZ, prev)
            except OSError:
                pass
        self.compat_saved = {}
        self.compat_pids = set()

    def _ultimate_scheme(self):
        """GUID of the Ultimate Performance plan, creating it once if missing.
        Win10 ships it hidden on most installs."""
        if self._ultimate_guid is None:
            self._ultimate_guid = ""
            try:
                out = subprocess.run(["powercfg", "/list"], capture_output=True,
                                     text=True, timeout=10,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                for line in out.splitlines():
                    if "ultimate" in line.lower():
                        m = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", line)
                        if m:
                            self._ultimate_guid = m.group(0)
                            break
                if not self._ultimate_guid:
                    out = subprocess.run(["powercfg", "/duplicatescheme", ULTIMATE_SCHEME],
                                         capture_output=True, text=True, timeout=10,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                    m = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", out)
                    if m:
                        self._ultimate_guid = m.group(0)
            except Exception:
                pass
        return self._ultimate_guid or None

    def _powercfg(self, args):
        return subprocess.run(["powercfg"] + args, capture_output=True,
                              text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _apply_power_scheme(self):
        try:
            out = self._powercfg(["/getactivescheme"]).stdout
            m = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", out)
            self.prev_power_scheme = m.group(0) if m else None
            target = self._ultimate_scheme() or HIGH_PERF_SCHEME
            if self.prev_power_scheme != target:
                self._powercfg(["/setactive", target])
            # Save each setting's AC value on the gaming scheme, then apply
            # the boost values: unpark all cores, aggressive boost, no idle.
            self.prev_power_settings = {}
            for (sub, guid), val in BOOST_POWER_SETTINGS.items():
                q = self._powercfg(["/query", "SCHEME_CURRENT", sub, guid]).stdout
                m = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", q)
                if m:
                    self.prev_power_settings[(sub, guid)] = int(m.group(1), 16)
                self._powercfg(["/setacvalueindex", "SCHEME_CURRENT",
                                sub, guid, val])
            self._powercfg(["/setactive", "SCHEME_CURRENT"])
        except Exception:
            pass

    def _restore_power_scheme(self):
        try:
            for (sub, guid), prev in self.prev_power_settings.items():
                self._powercfg(["/setacvalueindex", "SCHEME_CURRENT",
                                sub, guid, str(prev)])
            if self.prev_power_settings:
                self._powercfg(["/setactive", "SCHEME_CURRENT"])
            if self.prev_power_scheme and self.prev_power_scheme != HIGH_PERF_SCHEME:
                self._powercfg(["/setactive", self.prev_power_scheme])
        except Exception:
            pass
        self.prev_power_scheme = None
        self.prev_power_settings = {}

    def _pause_services(self):
        """Stop demand-start services that download/update or write telemetry
        mid-game. Anything we stopped is restarted on game exit."""
        self.paused_services = []
        for svc in BOOST_PAUSE_SERVICES:
            try:
                q = subprocess.run(["sc", "query", svc], capture_output=True, text=True,
                                   timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                if "RUNNING" in q.upper():
                    subprocess.run(["net", "stop", svc], capture_output=True, timeout=15,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.paused_services.append(svc)
            except Exception:
                pass
        # Per-user Game Bar presence writer (name has a random suffix).
        try:
            out = subprocess.run(["sc", "query", "type=", "service", "state=", "all"],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for block in re.split(r"\n\s*\n", out):
                m = re.search(r"SERVICE_NAME:\s*(\S*bcastdvr\w*)", block, re.I)
                if m and "RUNNING" in block.upper():
                    svc = m.group(1)
                    subprocess.run(["net", "stop", svc], capture_output=True, timeout=15,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.paused_services.append(svc)
        except Exception:
            pass

    def _resume_services(self):
        for svc in self.paused_services:
            try:
                subprocess.run(["net", "start", svc], capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
        self.paused_services = []

    def tick_game(self, now):
        enabled = self.game_boost_value.get()
        wanted = {n.strip().lower() for n in self.game_exes_var.get().split(",") if n.strip()}
        matched = {}
        if enabled and wanted:
            for name in wanted:
                pids = self.running_procs.get(name)
                if pids:
                    matched[name] = pids
        if matched:
            if not self.boosting_games:
                self._apply_power_scheme()
                self._pause_services()
                self._boost_tweaks(True)
            game_pids = [p for ps in matched.values() for p in ps]
            self.game_booster.apply(game_pids)
            self._compat_game_exes(game_pids)
            self.boosting_games = set(matched)
            txt = ("boosting " + ", ".join(sorted(matched))
                   + " - priority + timer + ultimate perf + unparked cores")
            if self.game_booster.denied:
                txt += f" ({len(self.game_booster.denied)} protected by anti-cheat)"
            self.game_boost_status.config(text=txt)
        else:
            if self.boosting_games or self.game_booster.timer_raised:
                self.boosting_games = set()
                self.game_booster.release()
                self._restore_power_scheme()
                self._resume_services()
            self.game_boost_status.config(
                text="watching for a listed game..." if enabled
                else "off - boosts the listed game while it runs")

    def on_fps_cleanup_toggle(self):
        self.save_config()
        if not self.fps_cleanup_value.get():
            self.fps_cleanup_active = False
            self.fps_cleanup_status.config(text="off - frees CPU for the listed game")
        else:
            self.fps_cleanup_status.config(text="watching for a listed game...")

    def reset_cleanup_choices(self):
        self.cleanup_always.clear()
        self.cleanup_never.clear()
        self.cleanup_asked.clear()
        self.save_config()
        self.fps_cleanup_status.config(text="remembered close/never choices cleared")

    def _cleanup_game_names(self):
        return {n.strip().lower().removesuffix(".exe")
                for n in self.game_exes_var.get().split(",") if n.strip()}

    def tick_cleanup(self, now):
        enabled = self.fps_cleanup_value.get()
        gaming = bool(self.boosting_games)
        self.fps_cleanup_active = bool(enabled and gaming)
        if not enabled:
            self.fps_cleanup_status.config(text="off - frees CPU for the listed game")
            return
        if not gaming:
            # "Leave it" answers re-arm next session - some apps are wanted
            # open sometimes but not every time.
            self.cleanup_asked.clear()
            self.fps_cleanup_status.config(text="watching for a listed game...")
            return
        protected = CLEANUP_PROTECTED | self.cleanup_never | self._cleanup_game_names()
        closed = 0
        for name, pid, cpu in list(self.cleanup_candidates):
            if pid == os.getpid() or cpu < CLEANUP_CPU_MIN or name in protected:
                continue
            if name in self.cleanup_closed and now - self.cleanup_closed[name] < CLEANUP_RECLOSE_AFTER:
                continue
            if name in CLEANUP_SAFE_CLOSE or name in self.cleanup_always:
                self._cleanup_close(pid, name)
                closed += 1
            elif name not in self.cleanup_asked:
                self.cleanup_asked.add(name)
                self.cleanup_queue.append((name, pid, cpu))
        self._cleanup_next_ask()
        if closed:
            self.fps_cleanup_status.config(text=f"closed {closed} background hog(s) to free CPU")

    def _cleanup_close(self, pid, name):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.cleanup_closed[name] = time.monotonic()
            self.root.after(4000, lambda p=pid: self._cleanup_force(p))
        except Exception:
            pass

    def _cleanup_force(self, pid):
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            if str(pid) in out:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass

    def _cleanup_next_ask(self):
        if self.cleanup_dialog is not None or not self.cleanup_queue:
            return
        name, pid, cpu = self.cleanup_queue.pop(0)
        dlg = tk.Toplevel(self.root)
        self.cleanup_dialog = dlg
        dlg.title("FPS cleanup")
        dlg.transient(self.root)
        dlg.attributes("-topmost", True)
        dlg.configure(bg="#1e1f24")
        cpu_pct = min(100.0, cpu / max(1, os.cpu_count() or 1))
        desc = self.cleanup_desc.get(pid, "").strip()
        if desc.lower() in ("python", "pythonw"):
            desc = "Python script"  # name is already the resolved script stem
        shown = f"{desc} ({name})" if desc and desc.lower() != name else name
        ttk.Label(dlg, text=f'"{shown}" is using about {cpu_pct:.0f}% of your CPU while your game is running.\n'
                            "Close it to free resources?",
                  wraplength=360).pack(padx=16, pady=(14, 8))
        row = ttk.Frame(dlg)
        row.pack(pady=(0, 14))

        def choose(choice):
            self.cleanup_dialog = None
            dlg.destroy()
            self._cleanup_choose(name, pid, choice)

        for text, choice in (("Close once", "close"), ("Always close", "always"),
                             ("Leave it", "skip"), ("Never ask", "never")):
            ttk.Button(row, text=text, command=lambda c=choice: choose(c)).pack(side="left", padx=4)
        dlg.protocol("WM_DELETE_WINDOW", lambda: choose("skip"))

    def _cleanup_choose(self, name, pid, choice):
        if choice in ("close", "always"):
            if choice == "always":
                self.cleanup_always.add(name)
                self.save_config()
            self._cleanup_close(pid, name)
            self.fps_cleanup_status.config(text=f"closed {name} to free CPU")
        elif choice == "never":
            self.cleanup_never.add(name)
            self.save_config()
        self._cleanup_next_ask()

    def tick_gpu_fan(self):
        """Pin the GPU fan to 100% when hot; hand it back to auto when cool."""
        backend = self.nvapi
        if not backend or not backend.gpu:
            return
        applied = bool(self.gpu_managed_offsets and any(self.gpu_managed_offsets))
        if applied != backend.power_raised:
            try:
                backend.set_power(applied)
            except Exception:
                pass
        temp = self.gpu_data.get("temp")
        if not isinstance(temp, (int, float)):
            return
        if not self.gpu_fan_manual and temp >= 76:
            backend.set_fan(100)
            self.gpu_fan_manual = True
        elif self.gpu_fan_manual and temp <= 70:
            backend.set_fan(None)
            self.gpu_fan_manual = False

    def on_close(self):
        if self.gpu_fan_manual and self.nvapi:
            try:
                self.nvapi.set_fan(None)
            except Exception:
                pass
            self.gpu_fan_manual = False
        if self.nvapi:
            self.nvapi.close()
        self.game_booster.release()
        self._restore_power_scheme()
        self._resume_services()
        self._boost_tweaks(False)
        self.stop_components("Closing: overclock requests off")
        if self.cpu_oc_active:
            self.cpu_reset("Closing: restoring stock CPU ratio limit")
        self.cpu_backend.close()
        if self.gpu_busy:
            self.gpu_stop_after_busy = True
            self.close_pending = True
            self.gpu_notice.set("Waiting for GPU operation before closing; offsets will be reset")
            return
        if self.gpu_owned:
            self.close_pending = True
            self.submit_gpu(reset=True, automatic=True)
            return
        self.stop_flag.set()
        self.worker.join()
        self.restore_all()
        self.save_config()
        try:
            self.computer.Close()
        except Exception:
            pass
        self.root.destroy()


def close_previous_controller(user, title="Cooling Control"):
    window = user.FindWindowW(None, title)
    if not window:
        return
    answer = user.MessageBoxW(None,
        f"An older {title} window is still running.\n\n"
        "Close it normally and open FPS Control? The old app will be asked to restore motherboard control.\n"
        "If it does not close, the new app will not take over.", "FPS Control - update", 0x124)
    if answer != 6:
        raise RuntimeError("Update canceled; the existing cooling controller was left running")
    if not user.PostMessageW(window, 0x0010, 0, 0):
        raise RuntimeError("Could not request a normal close. Close Cooling Control manually.")
    deadline = time.monotonic() + 10
    while user.IsWindow(window):
        if time.monotonic() >= deadline:
            raise RuntimeError("The old controller has not closed. No second controller was started.")
        time.sleep(0.1)


@contextmanager
def single_instance():
    from ctypes import wintypes as wt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    kernel.CreateMutexW.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user.FindWindowW.restype = wt.HWND
    user.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
    user.MessageBoxW.restype = ctypes.c_int
    user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user.PostMessageW.restype = wt.BOOL
    user.IsWindow.argtypes = [wt.HWND]
    user.IsWindow.restype = wt.BOOL
    close_previous_controller(user)
    if "--restart" in sys.argv:
        close_previous_controller(user, "Gaming FPS Control")
        close_previous_controller(user, "FPS Control")
        close_previous_controller(user, "PC Control")
    handle = kernel.CreateMutexW(None, False, "Local\\HQ-PC-Control")
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == 183:
            raise RuntimeError("FPS Control is already running. Use its existing window.")
        yield
    finally:
        kernel.CloseHandle(handle)


def main():
    global tk, ttk
    if not is_admin():
        elevate_and_exit()
        return
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = None
    app = None
    try:
        with single_instance():
            root = tk.Tk()
            app = CoolingApp(root)
            root.mainloop()
    except Exception as exc:
        with open(os.path.join(APP_DIR, "gui_error.txt"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        messagebox.showerror("FPS Control", str(exc), parent=root)
        if root is not None:
            root.destroy()


tk = None
ttk = None

if __name__ == "__main__":
    main()
