"""CPU OC mailbox down-step DISTINGUISHING test.

The previous test wrote cap=30x then cap=38x to MSR 0x150; both writes
completed without error, but re-querying GET_PER_CORE_RATIO_LIMIT (0x02)
showed no change - either the writes were ignored, or 0x02 only reports
fused limits that never reflect runtime caps.

This script distinguishes the two cases by watching REAL CPU frequency:
  1. Measure actual core frequency under all-core load (baseline).
  2. WRITE cap=30x (the already-approved down-step payload).
  3. Measure frequency under load again - ~3.0GHz means the cap took
     effect; ~3.5GHz+ means the write was ignored.
  4. WRITE cap=38x restore (the already-approved restore payload).
  5. Measure once more to confirm the cap lifted.

Scope (hard limits):
  - Writes ONLY to MSR 0x150, ONLY the two previously approved payloads:
    cmd 0x11 domain 0 data=30 and data=38. Nothing else can be sent.
  - No voltage change (all voltage fields zero = adaptive/stock).
  - Frequency is measured with the built-in Windows `typeperf` tool
    (% Processor Performance x base clock). No other MSRs, no SET
    commands beyond the two approved payloads.

Requires admin. Caller must elevate explicitly - no self-elevation.

Usage:
    python cpu_cap_observe.py --output PATH
"""
import argparse
import ctypes
import multiprocessing
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes

PAWNIOLIB_PATH = r"C:\Program Files\PawnIO\PawnIOLib.dll"
_HERE = os.path.dirname(os.path.abspath(__file__))
INTELMSR_BINS = [
    os.path.join(_HERE, "..", "pawnio_modules_024", "IntelMSR.bin"),
    os.path.join(_HERE, "IntelMSR.bin"),
]

MSR_OC_MAILBOX = 0x150
BUSY_BIT = 1 << 63
DOMAIN_IA_CORE = 0
CMD_SET_VOLTAGE_FREQUENCY = 0x11

STOCK_MAX_RATIO = 38
TEST_CAP_RATIO = 30
BASE_CLOCK_MHZ = 3400  # i7-6800K base clock - % Processor Performance is relative to this


def mailbox_cmd(command, domain, data):
    return ((domain & 0xFF) << 40) | ((command & 0xFF) << 32) | BUSY_BIT | (data & 0xFFFFFFFF)


SET_STEPS = (
    ("down-step: cap ratios at 30x", mailbox_cmd(CMD_SET_VOLTAGE_FREQUENCY, DOMAIN_IA_CORE, TEST_CAP_RATIO)),
    ("restore: cap ratios back at 38x", mailbox_cmd(CMD_SET_VOLTAGE_FREQUENCY, DOMAIN_IA_CORE, STOCK_MAX_RATIO)),
)
ALLOWED_WRITES = frozenset(v for _, v in SET_STEPS)


def write_msr(lib, handle, addr, value):
    """Guarded write: only MSR 0x150, only the two pre-approved payloads."""
    if addr != MSR_OC_MAILBOX:
        raise ValueError(f"write to MSR 0x{addr:X} not allowed")
    if value not in ALLOWED_WRITES:
        raise ValueError(f"mailbox payload 0x{value:016X} not in approved set")
    in_buf = (ctypes.c_ulonglong * 2)(addr, value)
    out_buf = (ctypes.c_ulonglong * 1)(0)
    ret_size = ctypes.c_size_t(0)
    hr = lib.pawnio_execute(handle, b"ioctl_write_msr",
                            in_buf, 2, out_buf, 1, ctypes.byref(ret_size))
    if hr != 0:
        raise RuntimeError(f"write_msr 0x{addr:X} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def setup_pawnio():
    if not os.path.exists(PAWNIOLIB_PATH):
        raise RuntimeError(f"PawnIOLib.dll not found at {PAWNIOLIB_PATH}")
    blob_candidates = [p for p in INTELMSR_BINS if os.path.exists(p)]
    if not blob_candidates:
        raise RuntimeError(f"No IntelMSR.bin found in: {INTELMSR_BINS}")
    lib = ctypes.OleDLL(PAWNIOLIB_PATH)
    lib.pawnio_version.argtypes = [ctypes.POINTER(wintypes.ULONG)]
    lib.pawnio_version.restype = ctypes.c_long
    lib.pawnio_open.argtypes = [ctypes.POINTER(wintypes.HANDLE)]
    lib.pawnio_open.restype = ctypes.c_long
    lib.pawnio_load.argtypes = [wintypes.HANDLE,
                              ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t]
    lib.pawnio_load.restype = ctypes.c_long
    lib.pawnio_execute.argtypes = [wintypes.HANDLE, ctypes.c_char_p,
                                   ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
    lib.pawnio_execute.restype = ctypes.c_long
    lib.pawnio_close.argtypes = [wintypes.HANDLE]
    lib.pawnio_close.restype = ctypes.c_long
    version = wintypes.ULONG(0)
    hr = lib.pawnio_version(ctypes.byref(version))
    if hr != 0:
        raise RuntimeError(f"pawnio_version failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    print(f"PawnIO library version: {(version.value >> 16) & 0xFFFF}."
          f"{(version.value >> 8) & 0xFF}.{version.value & 0xFF}")
    handle = wintypes.HANDLE(0)
    hr = lib.pawnio_open(ctypes.byref(handle))
    if hr != 0 or not handle.value:
        raise RuntimeError(f"pawnio_open failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    try:
        loaded = None
        for path in blob_candidates:
            data = open(path, "rb").read()
            blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            hr = lib.pawnio_load(handle, blob, len(data))
            if hr == 0:
                loaded = path
                break
            print(f"  pawnio_load failed for {path}, trying next")
        if loaded is None:
            raise RuntimeError("pawnio_load failed for all module blobs")
        print(f"IntelMSR module loaded: {loaded}")
    except Exception:
        lib.pawnio_close(handle)
        raise
    return lib, handle


def _burn(stop):
    while time.time() < stop:
        pass


def measure_freq_mhz(sample_seconds=3, workers=None):
    """Load all cores, then report max observed MHz (% perf x base clock)."""
    workers = workers or (os.cpu_count() or 4)
    stop = time.time() + sample_seconds + 2  # extra headroom for warmup+sample
    procs = [multiprocessing.Process(target=_burn, args=(stop,), daemon=True)
             for _ in range(workers)]
    for p in procs:
        p.start()
    time.sleep(1.0)  # let the load ramp so clocks boost before sampling
    try:
        out = subprocess.run(
            ["typeperf", r"\Processor Information(_Total)\% Processor Performance",
             "-sc", str(sample_seconds), "-si", "1"],
            capture_output=True, text=True, timeout=sample_seconds + 15)
        pcts = [float(m) for m in re.findall(r'"(\d+\.?\d*)"', out.stdout)]
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.join(timeout=2)
    if not pcts:
        raise RuntimeError(f"typeperf returned no samples: {out.stdout!r} {out.stderr!r}")
    max_pct = max(pcts)
    mhz = max_pct / 100.0 * BASE_CLOCK_MHZ
    print(f"  typeperf samples (%): {['%.1f' % p for p in pcts]}")
    print(f"  -> max {max_pct:.1f}% of {BASE_CLOCK_MHZ} MHz base = ~{mhz:.0f} MHz under load")
    return mhz


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", metavar="PATH", default=None)
    args = parser.parse_args()
    output_file = None
    if args.output:
        path = os.path.abspath(args.output)
        hq = os.path.abspath(os.path.join(_HERE, ".."))
        if not path.startswith(hq):
            print("ERROR: --output path must be under HQ")
            sys.exit(1)
        output_file = open(path, "w")
        sys.stdout = output_file
    try:
        print("=== CPU cap distinguishing test: observe REAL frequency ===")
        print("Approved scope: two writes to MSR 0x150 only -")
        print(f"  cmd 0x11 domain 0 data.MaxOcRatio={TEST_CAP_RATIO} (down), then {STOCK_MAX_RATIO} (restore).")
        print("Voltage fields zero = adaptive/stock. Frequency via typeperf.")
        print()
        try:
            admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            admin = False
        if not admin:
            print("ERROR: requires administrator. Run from an elevated terminal.")
            sys.exit(1)
        try:
            lib, handle = setup_pawnio()
        except Exception as e:
            print(f"FAILED to initialize PawnIO: {e}")
            sys.exit(1)
        try:
            print("--- baseline (no cap write yet) ---")
            base_mhz = measure_freq_mhz()
            print()
            print(f"--- down-step: write cap={TEST_CAP_RATIO}x ---")
            write_msr(lib, handle, MSR_OC_MAILBOX, SET_STEPS[0][1])
            print(f"  wrote 0x{SET_STEPS[0][1]:016X}")
            time.sleep(0.05)
            capped_mhz = measure_freq_mhz()
            print()
            print(f"--- restore: write cap={STOCK_MAX_RATIO}x ---")
            write_msr(lib, handle, MSR_OC_MAILBOX, SET_STEPS[1][1])
            print(f"  wrote 0x{SET_STEPS[1][1]:016X}")
            time.sleep(0.05)
            restored_mhz = measure_freq_mhz()
            print()
        finally:
            lib.pawnio_close(handle)
        cap_mhz = TEST_CAP_RATIO * 100
        print("=== Summary ===")
        print(f"baseline ~{base_mhz:.0f} MHz, capped ~{capped_mhz:.0f} MHz, restored ~{restored_mhz:.0f} MHz")
        if capped_mhz <= cap_mhz + 150 and capped_mhz < base_mhz - 150:
            print("RESULT: frequency DID drop to ~3.0GHz while capped -> the write WORKED.")
            print("        The 0x02 re-query reports fused limits, not runtime caps.")
        elif capped_mhz >= base_mhz - 150:
            print("RESULT: frequency did NOT drop -> the ratio write was IGNORED.")
            print("        Likely a BIOS/firmware lock on ratio programming.")
        else:
            print("RESULT: ambiguous - frequency dropped but not to the expected cap.")
        print("CPU restored to stock cap. Reboot also clears everything.")
    finally:
        if output_file:
            sys.stdout = sys.__stdout__
            output_file.close()
            print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
