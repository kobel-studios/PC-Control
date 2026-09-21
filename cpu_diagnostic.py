"""Read-only CPU diagnostic: checks if BIOS allows CPU overclocking.

Reads MSR 0xCE (PLATFORM_INFO) bit 28 (PrgTurboRatioEn) through PawnIO.
This is a READ-ONLY operation - no writes to any MSR, no changes to CPU state.

If bit 28 is set, the BIOS allows turbo ratio programming.
If bit 28 is clear, the BIOS has locked turbo ratio - CPU OC via software is blocked.

Usage (requires admin):
    python cpu_diagnostic.py
"""
import ctypes
import os
import sys
from ctypes import wintypes


PAWNIOLIB_PATH = r"C:\Program Files\PawnIO\PawnIOLib.dll"
INTELMSR_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "IntelMSR.bin")

MSR_PLATFORM_INFO = 0xCE
PRG_TURBO_RATIO_EN_BIT = 28


def check_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def read_msr_platform_info():
    """Read MSR 0xCE through PawnIO IntelMSR module. Returns 64-bit value or raises."""
    if not os.path.exists(PAWNIOLIB_PATH):
        raise RuntimeError(f"PawnIOLib.dll not found at {PAWNIOLIB_PATH}")
    if not os.path.exists(INTELMSR_BIN):
        raise RuntimeError(f"IntelMSR.bin not found at {INTELMSR_BIN}")

    with open(INTELMSR_BIN, "rb") as f:
        module_bytes = f.read()

    lib = ctypes.OleDLL(PAWNIOLIB_PATH)

    # pawnio_version(ULONG* version)
    lib.pawnio_version.argtypes = [ctypes.POINTER(wintypes.ULONG)]
    lib.pawnio_version.restype = None

    # pawnio_open(HANDLE* handle) -> HRESULT
    lib.pawnio_open.argtypes = [ctypes.POINTER(wintypes.HANDLE)]
    lib.pawnio_open.restype = ctypes.c_long  # HRESULT

    # pawnio_load(HANDLE handle, UCHAR* blob, SIZE_T size) -> HRESULT
    lib.pawnio_load.argtypes = [wintypes.HANDLE,
                                ctypes.POINTER(ctypes.c_ubyte),
                                ctypes.c_size_t]
    lib.pawnio_load.restype = ctypes.c_long

    # pawnio_execute(HANDLE handle, PCSTR name, ULONG64* in, SIZE_T in_size,
    #                 ULONG64* out, SIZE_T out_size, SIZE_T* return_size) -> HRESULT
    lib.pawnio_execute.argtypes = [wintypes.HANDLE,
                                   ctypes.c_char_p,
                                   ctypes.POINTER(ctypes.c_ulonglong),
                                   ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_ulonglong),
                                   ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
    lib.pawnio_execute.restype = ctypes.c_long

    # pawnio_close(HANDLE handle)
    lib.pawnio_close.argtypes = [wintypes.HANDLE]
    lib.pawnio_close.restype = None

    # Check version
    version = wintypes.ULONG(0)
    lib.pawnio_version(ctypes.byref(version))
    print(f"PawnIO library version: {version.value}")

    # Open device
    handle = wintypes.HANDLE(0)
    hr = lib.pawnio_open(ctypes.byref(handle))
    if hr != 0:
        raise RuntimeError(f"pawnio_open failed (HRESULT 0x{hr & 0xFFFFFFFF:08X}). "
                          f"Is the PawnIO driver running? Run as admin.")
    if not handle.value:
        raise RuntimeError("pawnio_open returned null handle")

    try:
        # Load IntelMSR module
        blob = (ctypes.c_ubyte * len(module_bytes)).from_buffer_copy(module_bytes)
        hr = lib.pawnio_load(handle, blob, len(module_bytes))
        if hr != 0:
            raise RuntimeError(f"pawnio_load failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
        print("IntelMSR module loaded successfully")

        # Read MSR 0xCE (PLATFORM_INFO) - READ ONLY, no writes
        in_buf = (ctypes.c_ulonglong * 1)(MSR_PLATFORM_INFO)
        out_buf = (ctypes.c_ulonglong * 1)(0)
        ret_size = ctypes.c_size_t(0)
        hr = lib.pawnio_execute(handle, b"ioctl_read_msr",
                                in_buf, 1, out_buf, 1, ctypes.byref(ret_size))
        if hr != 0:
            raise RuntimeError(f"pawnio_execute(ioctl_read_msr) failed "
                              f"(HRESULT 0x{hr & 0xFFFFFFFF:08X})")
        if ret_size.value != 1:
            raise RuntimeError(f"Expected 1 output cell, got {ret_size.value}")

        value = out_buf[0]
        return value

    finally:
        lib.pawnio_close(handle)


def main():
    print("=== CPU Overclock Diagnostic (READ-ONLY) ===")
    print()

    if not check_admin():
        print("ERROR: This script requires administrator privileges.")
        print("Right-click your terminal and 'Run as administrator', or")
        print("the script will self-elevate.")
        try:
            params = f'"{os.path.abspath(__file__)}"'
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
            sys.exit(0)
        except Exception:
            sys.exit(1)

    print(f"Checking MSR 0xCE (PLATFORM_INFO)...")
    print(f"This is a READ-ONLY operation. No CPU settings will be changed.")
    print()

    try:
        value = read_msr_platform_info()
    except Exception as e:
        print(f"FAILED: {e}")
        print()
        print("PLAIN ENGLISH: Could not read CPU info. The PawnIO driver may")
        print("not be running, or the IntelMSR module could not be loaded.")
        sys.exit(1)

    print(f"MSR 0xCE (PLATFORM_INFO) = 0x{value:016X}")
    print(f"  Binary: {value:064b}")
    print()

    prg_turbo = (value >> PRG_TURBO_RATIO_EN_BIT) & 1
    print(f"Bit {PRG_TURBO_RATIO_EN_BIT} (PrgTurboRatioEn) = {prg_turbo}")
    print()

    if prg_turbo == 1:
        print("RESULT: BIOS ALLOWS turbo ratio programming.")
        print("PLAIN ENGLISH: Your BIOS lets software change the CPU speed.")
        print("This means CPU overclocking from a program MAY be possible.")
        print("Next step: check if the CPU supports ratio OC via the OC mailbox.")
    else:
        print("RESULT: BIOS HAS LOCKED turbo ratio programming.")
        print("PLAIN ENGLISH: Your BIOS does not let software change CPU speed.")
        print("CPU overclocking from a program is BLOCKED.")
        print("You would need to change a setting in the BIOS (press Delete")
        print("while the computer starts) to unlock this.")

    print()
    print("No changes were made to any CPU settings.")


if __name__ == "__main__":
    main()
