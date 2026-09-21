"""Read-only CPU diagnostic: reads MSR 0xCE (PLATFORM_INFO), MSR 0x1AD
(TURBO_RATIO_LIMIT), and MSR 0x150 (OC_MAILBOX, access check only) through
the PawnIO IntelMSR module. Prefers the official signed 0.2.4 module blob
(pawnio_modules_024/IntelMSR.bin under HQ) which adds OC mailbox access;
falls back to the module extracted from the installed LibreHardwareMonitor.

READ-ONLY: no writes to any MSR, no mailbox commands, no CPU state changes.
Returns raw 64-bit values and interprets well-known bits, but does NOT claim
that any single bit proves the BIOS is unlocked or that overclocking will work.

Caller must run as admin (PawnIO driver requires it). No auto-elevation.

Usage:
    python cpu_probe.py [--output PATH]

    --output PATH  Write results to PATH (under HQ) instead of stdout.
"""
import argparse
import ctypes
import os
import sys
from ctypes import wintypes

PAWNIOLIB_PATH = r"C:\Program Files\PawnIO\PawnIOLib.dll"
_HERE = os.path.dirname(os.path.abspath(__file__))
# The IntelMSR module bundled with the installed LHM predates PawnIO.Modules
# 0.2.4 and has no OC mailbox / write support. Prefer the official signed
# 0.2.4 release blob (write list: 0x610 power limits, 0x150 OC mailbox);
# fall back to the LHM-extracted copy if loading it fails.
INTELMSR_BINS = [
    os.path.join(_HERE, "..", "pawnio_modules_024", "IntelMSR.bin"),
    os.path.join(_HERE, "IntelMSR.bin"),
]

# Only these MSRs are read. No other addresses are accepted.
# 0x150 is included only as a module-access check: the OC mailbox is a
# command/response register, so issuing an actual mailbox command would
# require a WRITE - which this probe never performs.
ALLOWED_MSRS = frozenset({0xCE, 0x1AD, 0x150})

MSR_PLATFORM_INFO = 0xCE
MSR_TURBO_RATIO_LIMIT = 0x1AD
MSR_OC_MAILBOX = 0x150


def read_msr(lib, handle, addr):
    """Read a single MSR via PawnIO IntelMSR ioctl_read_msr. Returns 64-bit int.

    Only MSR addresses in ALLOWED_MSRS are accepted. This function performs
    NO writes to any MSR.
    """
    if addr not in ALLOWED_MSRS:
        raise ValueError(f"MSR 0x{addr:X} is not in the allowed read set "
                         f"({', '.join(f'0x{a:X}' for a in sorted(ALLOWED_MSRS))})")
    in_buf = (ctypes.c_ulonglong * 1)(addr)
    out_buf = (ctypes.c_ulonglong * 1)(0)
    ret_size = ctypes.c_size_t(0)
    hr = lib.pawnio_execute(handle, b"ioctl_read_msr",
                            in_buf, 1, out_buf, 1, ctypes.byref(ret_size))
    if hr != 0:
        raise RuntimeError(f"pawnio_execute(ioctl_read_msr 0x{addr:X}) failed "
                          f"(HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    if ret_size.value != 1:
        raise RuntimeError(f"Expected 1 output cell for MSR 0x{addr:X}, got {ret_size.value}")
    return out_buf[0]


def setup_pawnio():
    """Load PawnIOLib, set up argtypes, open handle, load IntelMSR module.

    ABI verified against:
      https://raw.githubusercontent.com/namazso/PawnIO/master/PawnIOLib/include/PawnIOLib.h
    pawnio_version returns HRESULT (not void) per the header.
    pawnio_close returns HRESULT (not void) per the header.
    """
    if not os.path.exists(PAWNIOLIB_PATH):
        raise RuntimeError(f"PawnIOLib.dll not found at {PAWNIOLIB_PATH}")

    blob_candidates = [p for p in INTELMSR_BINS if os.path.exists(p)]
    if not blob_candidates:
        raise RuntimeError(f"No IntelMSR.bin found in: {INTELMSR_BINS}")

    lib = ctypes.OleDLL(PAWNIOLIB_PATH)

    # pawnio_version(PULONG version) -> HRESULT
    lib.pawnio_version.argtypes = [ctypes.POINTER(wintypes.ULONG)]
    lib.pawnio_version.restype = ctypes.c_long  # HRESULT

    # pawnio_open(PHANDLE handle) -> HRESULT
    lib.pawnio_open.argtypes = [ctypes.POINTER(wintypes.HANDLE)]
    lib.pawnio_open.restype = ctypes.c_long

    # pawnio_load(HANDLE, const UCHAR*, SIZE_T) -> HRESULT
    lib.pawnio_load.argtypes = [wintypes.HANDLE,
                                ctypes.POINTER(ctypes.c_ubyte),
                                ctypes.c_size_t]
    lib.pawnio_load.restype = ctypes.c_long

    # pawnio_execute(HANDLE, PCSTR, const ULONG64*, SIZE_T, PULONG64, SIZE_T, PSIZE_T) -> HRESULT
    lib.pawnio_execute.argtypes = [wintypes.HANDLE,
                                   ctypes.c_char_p,
                                   ctypes.POINTER(ctypes.c_ulonglong),
                                   ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_ulonglong),
                                   ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
    lib.pawnio_execute.restype = ctypes.c_long

    # pawnio_close(HANDLE) -> HRESULT
    lib.pawnio_close.argtypes = [wintypes.HANDLE]
    lib.pawnio_close.restype = ctypes.c_long  # HRESULT

    version = wintypes.ULONG(0)
    hr = lib.pawnio_version(ctypes.byref(version))
    if hr != 0:
        raise RuntimeError(f"pawnio_version failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    major = (version.value >> 16) & 0xFFFF
    minor = (version.value >> 8) & 0xFF
    patch = version.value & 0xFF
    print(f"PawnIO library version: {major}.{minor}.{patch}")

    handle = wintypes.HANDLE(0)
    hr = lib.pawnio_open(ctypes.byref(handle))
    if hr != 0:
        raise RuntimeError(f"pawnio_open failed (HRESULT 0x{hr & 0xFFFFFFFF:08X}). "
                          f"Is the PawnIO driver running? Run as admin.")
    if not handle.value:
        raise RuntimeError("pawnio_open returned null handle")

    loaded_path = None
    last_error = None
    try:
        for path in blob_candidates:
            with open(path, "rb") as f:
                module_bytes = f.read()
            blob = (ctypes.c_ubyte * len(module_bytes)).from_buffer_copy(module_bytes)
            hr = lib.pawnio_load(handle, blob, len(module_bytes))
            if hr == 0:
                loaded_path = path
                break
            last_error = f"HRESULT 0x{hr & 0xFFFFFFFF:08X}"
            print(f"  pawnio_load failed for {path} ({last_error}), trying next")
        if loaded_path is None:
            raise RuntimeError(f"pawnio_load failed for all module blobs (last: {last_error})")
        print(f"IntelMSR module loaded: {loaded_path}")
    except Exception:
        lib.pawnio_close(handle)
        raise

    return lib, handle, loaded_path


def interpret_platform_info(value):
    """Interpret well-known bits of MSR 0xCE (PLATFORM_INFO)."""
    print(f"  Raw: 0x{value:016X}")
    print(f"  Binary: {value:064b}")
    print()
    prg_turbo = (value >> 28) & 1
    print(f"  Bit 28 (PrgTurboRatioEn) = {prg_turbo}")
    if prg_turbo == 1:
        print("    -> Bit 28 is SET (capability indicator only)")
    else:
        print("    -> Bit 28 is CLEAR (capability indicator only)")
    print("    NOTE: this bit is a capability indicator, NOT proof that BIOS")
    print("    is unlocked or locked. It does not guarantee software OC will")
    print("    work. Further verification is required.")
    max_non_turbo = (value >> 8) & 0xFF
    print(f"  Bits 15:8 (Max Non-Turbo Ratio) = {max_non_turbo}")
    if max_non_turbo:
        print(f"    -> Base clock ratio: {max_non_turbo}x")


def interpret_turbo_ratio_limit(value):
    """Interpret MSR 0x1AD (TURBO_RATIO_LIMIT) for a 6-core CPU."""
    print(f"  Raw: 0x{value:016X}")
    print(f"  Binary: {value:064b}")
    print()
    # i7-6800K is 6-core, so fields 1C-6C are relevant
    for i in range(6):
        ratio = (value >> (i * 8)) & 0xFF
        if ratio:
            print(f"  {i+1}-core max turbo ratio: {ratio}x ({ratio * 100} MHz)")
        else:
            print(f"  {i+1}-core max turbo ratio: (not set)")
    print()
    print("  NOTE: these are current programmed turbo limits, NOT guaranteed")
    print("  factory-fused values. On Broadwell-E, changing these requires")
    print("  either BIOS or the OC mailbox (MSR 0x150).")
    print("  PawnIO's IntelMSR module allows READS of 0x1AD but NOT writes.")
    print("  Ratio changes (if possible) go through the OC mailbox, not 0x1AD.")


def main():
    parser = argparse.ArgumentParser(description="Read-only CPU MSR probe via PawnIO")
    parser.add_argument("--output", metavar="PATH", default=None,
                       help="Write results to PATH instead of stdout")
    args = parser.parse_args()

    # Redirect output if --output is specified
    output_file = None
    if args.output:
        output_path = os.path.abspath(args.output)
        # Ensure the path is under HQ
        hq = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if not output_path.startswith(hq):
            print("ERROR: --output path must be under HQ")
            sys.exit(1)
        output_file = open(output_path, "w")
        original_stdout = sys.stdout
        sys.stdout = output_file

    try:
        print("=== CPU Probe (READ-ONLY) ===")
        print("Reads MSR 0xCE (PLATFORM_INFO) and MSR 0x1AD (TURBO_RATIO_LIMIT)")
        print("No writes, no mailbox commands, no CPU state changes.")
        print()

        try:
            lib, handle, loaded_path = setup_pawnio()
        except Exception as e:
            print(f"FAILED to initialize PawnIO: {e}")
            print()
            print("PLAIN ENGLISH: Could not connect to the PawnIO driver.")
            print("Make sure it is running and you have admin privileges.")
            sys.exit(1)

        try:
            print()
            print("--- MSR 0xCE (PLATFORM_INFO) ---")
            try:
                value_ce = read_msr(lib, handle, MSR_PLATFORM_INFO)
                interpret_platform_info(value_ce)
            except Exception as e:
                print(f"  FAILED to read MSR 0xCE: {e}")

            print()
            print("--- MSR 0x1AD (TURBO_RATIO_LIMIT) ---")
            try:
                value_1ad = read_msr(lib, handle, MSR_TURBO_RATIO_LIMIT)
                interpret_turbo_ratio_limit(value_1ad)
            except Exception as e:
                print(f"  FAILED to read MSR 0x1AD: {e}")

            print()
            print("--- MSR 0x150 (OC_MAILBOX) access check ---")
            print("  Read-only check: does the loaded module permit access to the")
            print("  OC mailbox register at all? NOTE: issuing an actual mailbox")
            print("  command (e.g. GET_OC_CAPABILITIES) would require a WRITE to")
            print("  0x150, which this probe does NOT perform.")
            try:
                value_150 = read_msr(lib, handle, MSR_OC_MAILBOX)
                print(f"  Raw: 0x{value_150:016X}")
                print("  -> Module permits OC mailbox access (read succeeded)")
            except Exception as e:
                print(f"  OC mailbox not accessible via this module: {e}")
                print("  -> Loaded module is likely the older read-only build")

        finally:
            hr = lib.pawnio_close(handle)
            if hr != 0:
                print(f"WARNING: pawnio_close returned HRESULT 0x{hr & 0xFFFFFFFF:08X}")

        print()
        print("=== Summary ===")
        print("No changes were made to any CPU settings.")
        print("This was a read-only probe of two CPU registers.")

    finally:
        if output_file:
            sys.stdout = original_stdout
            output_file.close()
            print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
