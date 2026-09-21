"""Read current fan duty values directly from the SuperIO chip (run as admin).

Independent of the GUI: proves what the hardware is actually doing.
"""
import ctypes
import os
import sys
import time
import traceback

LHM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lhm")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duty_check.txt")


def main():
    lines = [f"checked at {time.strftime('%H:%M:%S')}",
             f"admin={bool(ctypes.windll.shell32.IsUserAnAdmin())}"]
    sys.path.insert(0, LHM_DIR)
    import clr
    clr.AddReference("LibreHardwareMonitorLib")
    from LibreHardwareMonitor.Hardware import Computer

    def sample(hw, record):
        hw.Update()
        if record:
            for sensor in hw.Sensors:
                kind = str(sensor.SensorType)
                if kind not in ("Control", "Fan", "Temperature", "Load"):
                    continue
                value = sensor.Value
                text = "unavailable" if value is None else f"{float(value):.1f}"
                if value is not None and kind == "Temperature":
                    if "Distance to TjMax" in str(sensor.Name):
                        text = f"{float(value) * 9 / 5:.1f} F below thermal limit"
                    else:
                        text = f"{float(value) * 9 / 5 + 32:.1f} F ({float(value):.1f} C)"
                elif kind == "Fan":
                    text += " RPM"
                elif kind in ("Control", "Load"):
                    text += "%"
                lines.append(f"{hw.Name} | {sensor.Name} | {kind}: {text} | {sensor.Identifier}")
        for sub in hw.SubHardware:
            sample(sub, record)

    c = Computer()
    c.IsMotherboardEnabled = True
    c.IsCpuEnabled = True
    try:
        c.Open()
        for index in range(5):
            record = index >= 2
            if record:
                lines.append(f"--- SAMPLE {index - 1} at {time.strftime('%H:%M:%S')} ---")
            for hw in c.Hardware:
                sample(hw, record)
            time.sleep(2)
    finally:
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
