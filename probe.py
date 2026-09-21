"""Probe v2: check Ring0 driver state, warm up sensors, dump everything (run as admin)."""
import ctypes
import os
import sys
import time
import traceback

LHM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lhm")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sensors_out.txt")


def main():
    lines = []
    lines.append(f"admin={bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    sys.path.insert(0, LHM_DIR)
    import clr
    clr.AddReference("LibreHardwareMonitorLib")
    from LibreHardwareMonitor.Hardware import Computer

    c = Computer()
    c.IsCpuEnabled = True
    c.IsMotherboardEnabled = True
    c.Open()
    try:
        from LibreHardwareMonitor.Hardware import Ring0
        lines.append(f"Ring0.IsOpen = {Ring0.IsOpen}")
    except ImportError:
        lines.append("Ring0 not importable (internal class) - relying on sensor values")

    for i in range(5):
        for hw in c.Hardware:
            hw.Update()
        time.sleep(1)

    for hw in c.Hardware:
        lines.append(f"HARDWARE: {hw.Name} [{hw.HardwareType}] subhardware={len(hw.SubHardware)}")
        for s in hw.Sensors:
            v = "" if s.Value is None else f" = {s.Value}"
            lines.append(f"  SENSOR: {s.Name} ({s.SensorType}){v}")
        for sub in hw.SubHardware:
            lines.append(f"  SUB: {sub.Name} [{sub.HardwareType}]")
            for s in sub.Sensors:
                v = "" if s.Value is None else f" = {s.Value}"
                lines.append(f"    SENSOR: {s.Name} ({s.SensorType}){v}  id={s.Identifier}")
    c.Close()
    lines.append("DONE")
    with open(OUT, "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(OUT, "w") as f:
            f.write("ERROR:\n" + traceback.format_exc())
