"""Diag 2: find which dependency blocks type resolution, pre-load all DLLs, retry."""
import glob
import sys
import traceback

LHM_DIR = r"C:\Users\jacks\HQ\CoolingControl\lhm"
LHM_DLL = LHM_DIR + r"\LibreHardwareMonitorLib.dll"
OUT = r"C:\Users\jacks\HQ\CoolingControl\diag2_out.txt"
lines = []

try:
    import clr
    sys.path.insert(0, LHM_DIR)
    import System
    asm = System.Reflection.Assembly.LoadFrom(LHM_DLL)
    try:
        types = asm.GetTypes()
        lines.append(f"GetTypes OK: {len(types)} types")
    except Exception as e:
        lines.append(f"GetTypes FAILED: {type(e).__name__}")
        loader = getattr(e, "LoaderExceptions", None)
        if loader:
            for i, le in enumerate(loader[:10]):
                if le is not None:
                    lines.append(f"  loader exc {i}: {le.Message}")

    lines.append("--- pre-loading all DLLs in folder ---")
    for dll in sorted(glob.glob(LHM_DIR + r"\*.dll")):
        try:
            System.Reflection.Assembly.LoadFrom(dll)
            lines.append(f"loaded {dll.split(chr(92))[-1]}")
        except Exception as e:
            lines.append(f"FAILED {dll}: {e.Message}")

    try:
        types = asm.GetTypes()
        lines.append(f"GetTypes after preload: {len(types)} types")
        names = [str(t.FullName) for t in types if "Computer" in str(t.FullName)]
        lines.append("Computer types: " + str(names))
    except Exception as e:
        lines.append(f"GetTypes still FAILED: {e}")

    try:
        from LibreHardwareMonitor import Computer
        lines.append("from import OK: " + str(Computer))
    except Exception as e:
        lines.append(f"from import failed: {e!r}")
except Exception:
    lines.append("FATAL: " + traceback.format_exc())

with open(OUT, "w") as f:
    f.write("\n".join(lines))
