"""Diagnose pythonnet assembly loading (non-elevated, no hardware access)."""
import sys
import traceback

LHM_DIR = r"C:\Users\jacks\HQ\CoolingControl\lhm"
LHM_DLL = LHM_DIR + r"\LibreHardwareMonitorLib.dll"
OUT = r"C:\Users\jacks\HQ\CoolingControl\diag_out.txt"
lines = []

try:
    import clr
    lines.append("clr imported OK")
    sys.path.insert(0, LHM_DIR)
    import System
    lines.append("System OK: " + str(System.String))
    try:
        clr.AddReference("LibreHardwareMonitorLib")
        lines.append("AddReference OK")
    except Exception as e:
        lines.append(f"AddReference failed: {e!r}")
    names = [str(a.GetName().Name) for a in System.AppDomain.CurrentDomain.GetAssemblies()]
    lines.append("LibreHardwareMonitorLib in loaded assemblies: " + str("LibreHardwareMonitorLib" in names))
    try:
        asm = System.Reflection.Assembly.LoadFrom(LHM_DLL)
        lines.append("LoadFrom OK: " + str(asm.FullName))
    except Exception as e:
        lines.append(f"LoadFrom failed: {e!r}")
    try:
        import LibreHardwareMonitor
        lines.append("namespace import OK, has Computer: " + str(hasattr(LibreHardwareMonitor, "Computer")))
    except Exception as e:
        lines.append(f"namespace import failed: {e!r}")
    try:
        from LibreHardwareMonitor import Computer
        lines.append("from import OK: " + str(Computer))
    except Exception as e:
        lines.append(f"from import failed: {e!r}")
except Exception:
    lines.append("FATAL: " + traceback.format_exc())

with open(OUT, "w") as f:
    f.write("\n".join(lines))
