"""Read-only cooling diagnostic: dumps the Nuvoton NCT6791D fan controller
state through the PawnIO LpcIO module (the same signed driver/module family
LibreHardwareMonitor already uses on this machine).

WHY: the app commands 100% duty on every channel, but only two channels
report tach (~1400-1450 RPM). Typical AIO radiator fans are rated up to
~2400 RPM and Asetek pumps ~2800+ RPM. This probe reads the chip's own
registers to see what each channel is ACTUALLY outputting (vs commanded),
which control mode each channel is in (manual vs SmartFan/thermal cruise),
and the PWM/DC output mode bits - the places a hidden speed cap would live.

READ-ONLY in effect: no fan, PWM, or SmartFan setting is modified. The only
port writes performed are register-address selections plus the standard
SuperIO config-mode enter/exit handshake (0x87 x2 / 0xAA on port 0x2E) and
bank selects - the same navigation every hardware monitor performs. The
ISABUS mutex is held for the whole probe so LHM cannot interleave a
transaction mid-read.

Caller must run as admin (PawnIO driver requires it). No auto-elevation.

Usage:
    python cooling_probe.py [--output PATH]

    --output PATH  Write results to PATH (under HQ) instead of stdout.
"""
import argparse
import ctypes
import os
import sys
from ctypes import wintypes

PAWNIOLIB_PATH = r"C:\Program Files\PawnIO\PawnIOLib.dll"
_HERE = os.path.dirname(os.path.abspath(__file__))
LPCIO_BINS = [
    os.path.join(_HERE, "..", "pawnio_modules_024", "LpcIO.bin"),
    os.path.join(_HERE, "LpcIO.bin"),
]

# Named mutant used industry-wide (LHM, HWiNFO, SIV, AIDA64) to serialize
# SuperIO/LPC access. Acquire before any superio transaction.
ISABUS_MUTEX_NAME = r"Global\Access_ISABUS.HTP.Method"

# NCT6791D (LHM Nct677X.cs register map, NCT6791-family branch):
#   16-bit register addresses are (bank << 8) | offset.
FAN_PWM_OUT_REG = [0x001, 0x003, 0x011, 0x013, 0x015, 0x017]   # actual output, bank 0
FAN_PWM_COMMAND_REG = [0x109, 0x209, 0x309, 0x809, 0x909, 0xA09]  # manual duty
FAN_CONTROL_MODE_REG = [0x102, 0x202, 0x302, 0x802, 0x902, 0xA02]  # control mode
FAN_COUNT_REG = [0x4B0, 0x4B2, 0x4B4, 0x4B6, 0x4B8, 0x4BA]      # 16-bit tach counts
BANK_SELECT_REGISTER = 0x4E

# SmartFan config offsets inside each fan's own bank (bank = channel + 1 or
# 8/9/10 for channels 3-5). Dumped raw; semantics per NCT6791D datasheet.
FAN_BANKS = [1, 2, 3, 8, 9, 10]
SMARTFAN_OFFSETS = list(range(0x00, 0x10))  # 0x00-0x0F of each fan bank

# Bank-0 registers holding per-channel PWM-vs-DC mode bits (nct6775 driver):
#   reg 0x04 bits 0,1 -> fans 0,1 ; reg 0x12 bits 0-3 -> fans 2-5.
PWM_MODE_REGS = [(0x04, 0x01), (0x04, 0x02), (0x12, 0x01),
                 (0x12, 0x02), (0x12, 0x04), (0x12, 0x08)]

MIN_FAN_COUNT = 0x15
MAX_FAN_COUNT = 0x1FFF


class PawnIO:
    """Thin wrapper around PawnIOLib for one loaded module."""

    def __init__(self, blob_paths):
        if not os.path.exists(PAWNIOLIB_PATH):
            raise RuntimeError(f"PawnIOLib.dll not found at {PAWNIOLIB_PATH}")
        self.lib = ctypes.OleDLL(PAWNIOLIB_PATH)
        lib = self.lib
        lib.pawnio_version.argtypes = [ctypes.POINTER(wintypes.ULONG)]
        lib.pawnio_version.restype = ctypes.c_long
        lib.pawnio_open.argtypes = [ctypes.POINTER(wintypes.HANDLE)]
        lib.pawnio_open.restype = ctypes.c_long
        lib.pawnio_load.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(ctypes.c_ubyte),
                                    ctypes.c_size_t]
        lib.pawnio_load.restype = ctypes.c_long
        lib.pawnio_execute.argtypes = [wintypes.HANDLE,
                                       ctypes.c_char_p,
                                       ctypes.POINTER(ctypes.c_ulonglong),
                                       ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_ulonglong),
                                       ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
        lib.pawnio_execute.restype = ctypes.c_long
        lib.pawnio_close.argtypes = [wintypes.HANDLE]
        lib.pawnio_close.restype = ctypes.c_long

        version = wintypes.ULONG(0)
        hr = lib.pawnio_version(ctypes.byref(version))
        if hr != 0:
            raise RuntimeError(f"pawnio_version failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
        print(f"PawnIO library version: "
              f"{(version.value >> 16) & 0xFFFF}.{(version.value >> 8) & 0xFF}.{version.value & 0xFF}")

        self.handle = wintypes.HANDLE(0)
        hr = lib.pawnio_open(ctypes.byref(self.handle))
        if hr != 0:
            raise RuntimeError(f"pawnio_open failed (HRESULT 0x{hr & 0xFFFFFFFF:08X}). "
                               f"Is the PawnIO driver running? Run as admin.")
        if not self.handle.value:
            raise RuntimeError("pawnio_open returned null handle")

        self.loaded_path = None
        last_error = None
        try:
            for path in blob_paths:
                if not os.path.exists(path):
                    continue
                with open(path, "rb") as f:
                    module_bytes = f.read()
                blob = (ctypes.c_ubyte * len(module_bytes)).from_buffer_copy(module_bytes)
                hr = lib.pawnio_load(self.handle, blob, len(module_bytes))
                if hr == 0:
                    self.loaded_path = path
                    break
                last_error = f"HRESULT 0x{hr & 0xFFFFFFFF:08X}"
                print(f"  pawnio_load failed for {path} ({last_error}), trying next")
            if self.loaded_path is None:
                raise RuntimeError(f"pawnio_load failed for all module blobs (last: {last_error})")
            print(f"LpcIO module loaded: {self.loaded_path}")
        except Exception:
            self.close()
            raise

    def execute(self, name, inputs=(), out_count=0):
        in_buf = (ctypes.c_ulonglong * max(1, len(inputs)))(*inputs)
        out_buf = (ctypes.c_ulonglong * max(1, out_count))()
        ret_size = ctypes.c_size_t(0)
        hr = self.lib.pawnio_execute(
            self.handle, name.encode(),
            in_buf, len(inputs), out_buf, out_count, ctypes.byref(ret_size))
        if hr != 0:
            raise RuntimeError(f"pawnio_execute({name}) failed "
                               f"(HRESULT 0x{hr & 0xFFFFFFFF:08X})")
        return list(out_buf)[:ret_size.value]

    def close(self):
        if self.handle.value:
            hr = self.lib.pawnio_close(self.handle)
            self.handle = wintypes.HANDLE(0)
            if hr != 0:
                print(f"WARNING: pawnio_close returned HRESULT 0x{hr & 0xFFFFFFFF:08X}")


class IsaBusMutex:
    """Acquires the industry-standard SuperIO mutex via kernel32."""

    def __init__(self, timeout_ms=10000):
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._mutex = None
        self._timeout = timeout_ms

    def __enter__(self):
        self._mutex = self._k32.CreateMutexW(None, False, ISABUS_MUTEX_NAME)
        if not self._mutex:
            raise RuntimeError("CreateMutexW failed "
                               f"(error {ctypes.get_last_error()})")
        result = self._k32.WaitForSingleObject(self._mutex, self._timeout)
        if result != 0:  # WAIT_OBJECT_0
            self._k32.CloseHandle(self._mutex)
            self._mutex = None
            raise RuntimeError(f"Timed out waiting for ISABUS mutex "
                               f"(WaitForSingleObject -> {result}); "
                               f"is a monitoring tool holding it?")
        print("ISABUS mutex acquired (serialized with LHM/HWiNFO-style tools)")
        return self

    def __exit__(self, *exc):
        if self._mutex:
            self._k32.ReleaseMutex(self._mutex)
            self._k32.CloseHandle(self._mutex)
            self._mutex = None
        return False


class NCT6791D:
    """Read-only access to the NCT6791D SuperIO + hardware monitor."""

    def __init__(self, pio):
        self.pio = pio
        self.bar = None

    # ----- config-space helpers (require config mode entered) -----
    def _select_slot(self, slot):
        self.pio.execute("ioctl_select_slot", [slot])

    def _enter_config(self, reg_port):
        self.pio.execute("ioctl_pio_outb", [reg_port, 0x87])
        self.pio.execute("ioctl_pio_outb", [reg_port, 0x87])

    def _exit_config(self, reg_port):
        self.pio.execute("ioctl_pio_outb", [reg_port, 0xAA])

    def _sio_inb(self, reg):
        return self.pio.execute("ioctl_superio_inb", [reg], 1)[0]

    def _sio_inw(self, reg):
        return self.pio.execute("ioctl_superio_inw", [reg], 1)[0]

    def _sio_outb(self, reg, val):
        self.pio.execute("ioctl_superio_outb", [reg, val])

    # ----- HWM helpers (no config mode needed) -----
    def _hwm_read(self, reg16):
        """Read one byte of the NCT6791D banked hardware-monitor space."""
        bank = (reg16 >> 8) & 0xFF
        offset = reg16 & 0xFF
        addr_port = self.bar + 0x05
        data_port = self.bar + 0x06
        self.pio.execute("ioctl_pio_outb", [addr_port, BANK_SELECT_REGISTER])
        self.pio.execute("ioctl_pio_outb", [data_port, bank])
        self.pio.execute("ioctl_pio_outb", [addr_port, offset])
        return self.pio.execute("ioctl_pio_inb", [data_port], 1)[0]

    def open(self):
        """Find the chip, enter config mode, discover the HWM BAR, exit."""
        for slot in (0, 1):
            self._select_slot(slot)
            reg_port = 0x2E + slot * 0x20
            self._enter_config(reg_port)
            chip_id = self._sio_inw(0x20)
            if chip_id not in (0x0000, 0xFFFF):
                print(f"SuperIO slot {slot} (ports 0x{reg_port:X}/0x{reg_port+1:X}): "
                      f"chip ID 0x{chip_id:04X}")
                if (chip_id & 0xFFF0) == 0xC800:
                    print("  -> matches NCT6791D family (0xC8xx)")
                self._discover_hwm_bar(reg_port)
                self.pio.execute("ioctl_find_bars")
                self._exit_config(reg_port)
                if self.bar:
                    return
            else:
                self._exit_config(reg_port)
        raise RuntimeError("No SuperIO chip responded on slots 0/1")

    def _discover_hwm_bar(self, reg_port):
        # NCT6791D hardware monitor is logical device 0x0B.
        self._sio_outb(0x07, 0x0B)          # DEVICE_SELECT -> HWM
        activate = self._sio_inb(0x30)      # SIO_REG_ENABLE
        self.bar = self._sio_inw(0x60)      # primary BAR
        print(f"  HWM logical device (LDN 0x0B): enable=0x{activate:02X}, "
              f"BAR=0x{self.bar:04X}")
        if not (activate & 0x01):
            print("  WARNING: HWM logical device reports disabled")
        if not self.bar or self.bar == 0xFFFF:
            raise RuntimeError("HWM BAR not populated")

    def dump(self):
        out_regs = FAN_PWM_OUT_REG
        cmd_regs = FAN_PWM_COMMAND_REG
        mode_regs = FAN_CONTROL_MODE_REG
        count_regs = FAN_COUNT_REG

        print()
        print("=== Per-channel state ===")
        print("(cmd = manual duty register, out = value the chip is actually")
        print("driving on the pin, rpm = tach counter converted)")
        pwm_mode = {reg: self._hwm_read(reg) for reg, _ in PWM_MODE_REGS}
        for i in range(6):
            cmd = self._hwm_read(cmd_regs[i])
            mode = self._hwm_read(mode_regs[i])
            out = self._hwm_read(out_regs[i])
            hi = self._hwm_read(count_regs[i])
            lo = self._hwm_read(count_regs[i] + 1)
            count = ((hi << 8) | lo) & MAX_FAN_COUNT
            if count >= MIN_FAN_COUNT:
                rpm = 1.35e6 / count
                rpm_str = f"{rpm:6.0f}"
            else:
                rpm_str = "   n/a" if count == 0 else " stalled"
            mode_reg, mode_mask = PWM_MODE_REGS[i]
            mode_bit = bool(pwm_mode[mode_reg] & mode_mask)
            print(f"  ch{i}: cmd={cmd:3d} ({cmd * 100 // 255:3d}%) "
                  f"out={out:3d} ({out * 100 // 255:3d}%) "
                  f"mode_reg=0x{mode:02X} pwm_mode_bit={int(mode_bit)} "
                  f"rpm={rpm_str}")
            if out < cmd:
                print(f"       ^ OUT < CMD: chip is outputting less than the "
                      f"commanded duty - a SmartFan/thermal cap may be active")

        print()
        print("=== PWM/DC mode bits (raw) ===")
        print(f"  bank0 reg 0x04 = 0x{pwm_mode[0x04]:02X} (bits 0-1 -> ch0,ch1)")
        print(f"  bank0 reg 0x12 = 0x{pwm_mode[0x12]:02X} (bits 0-3 -> ch2-ch5)")
        print("  NOTE: exact bit semantics (PWM vs DC) are chip-config;")
        print("  compare against datasheet before changing anything.")

        print()
        print("=== SmartFan config (raw bank dumps, offsets 0x00-0x0F) ===")
        for i, bank in enumerate(FAN_BANKS):
            values = [self._hwm_read((bank << 8) | off) for off in SMARTFAN_OFFSETS]
            hexed = " ".join(f"{v:02X}" for v in values)
            print(f"  ch{i} (bank {bank}): {hexed}")


def main():
    parser = argparse.ArgumentParser(
        description="Read-only NCT6791D fan-controller probe via PawnIO LpcIO")
    parser.add_argument("--output", metavar="PATH", default=None,
                        help="Write results to PATH instead of stdout")
    args = parser.parse_args()

    output_file = None
    if args.output:
        output_path = os.path.abspath(args.output)
        hq = os.path.abspath(os.path.join(_HERE, ".."))
        if not output_path.startswith(hq):
            print("ERROR: --output path must be under HQ")
            sys.exit(1)
        output_file = open(output_path, "w")
        original_stdout = sys.stdout
        sys.stdout = output_file

    try:
        print("=== Cooling Probe (READ-ONLY) ===")
        print("Dumps NCT6791D fan channel state via PawnIO LpcIO module.")
        print("No fan/PWM/SmartFan settings are modified.")
        print()

        try:
            pio = PawnIO(LPCIO_BINS)
        except Exception as e:
            print(f"FAILED to initialize PawnIO: {e}")
            print()
            print("PLAIN ENGLISH: Could not connect to the PawnIO driver.")
            print("Make sure it is running and you have admin privileges.")
            sys.exit(1)

        try:
            with IsaBusMutex():
                chip = NCT6791D(pio)
                chip.open()
                chip.dump()
        finally:
            pio.close()

        print()
        print("=== Summary ===")
        print("No fan, PWM, or SmartFan setting was changed.")
        print("Compare 'out' vs 'cmd' per channel: out < cmd means the chip")
        print("(or BIOS SmartFan config) is capping that channel below the")
        print("requested duty - that is where cooling headroom could exist.")

    finally:
        if output_file:
            sys.stdout = original_stdout
            output_file.close()
            print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
