"""Fight test: hammer channels 0-3 with 100% writes at 10Hz to see if the
motherboard's smart-fan override can be out-paced (run as admin)."""
import ctypes
import os
import sys
import time
import traceback

LHM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lhm")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duty_fight.txt")


def update_all(c):
    for hw in c.Hardware:
        hw.Update()
        for sub in getattr(hw, "SubHardware", []) or []:
            sub.Update()


def main():
    lines = [f"checked at {time.strftime('%H:%M:%S')}",
             f"admin={bool(ctypes.windll.shell32.IsUserAnAdmin())}"]
    sys.path.insert(0, LHM_DIR)
    import clr
    clr.AddReference("LibreHardwareMonitorLib")
    from LibreHardwareMonitor.Hardware import Computer

    c = Computer()
    c.IsMotherboardEnabled = True
    c.Open()
    controls = {}
    for hw in c.Hardware:
        for sub in getattr(hw, "SubHardware", []) or []:
            for s in sub.Sensors:
                if str(s.SensorType) == "Control":
                    controls[s.Index] = s
    lines.append(f"found {len(controls)} control channels")

    update_all(c)
    time.sleep(1)
    update_all(c)
    lines.append("baseline: " + ", ".join(
        f"ch{i}={'None' if controls[i].Value is None else f'{float(controls[i].Value):.1f}%'}"
        for i in sorted(controls)))

    for ch in sorted(controls):
        if ch > 3:
            continue
        end = time.time() + 10
        while time.time() < end:
            controls[ch].Control.SetSoftware(100)
            time.sleep(0.1)
        time.sleep(1)
        update_all(c)
        time.sleep(1)
        v = controls[ch].Value
        lines.append(f"after 10Hz hammering for 10s, channel {ch}: "
                     f"{'None' if v is None else f'{float(v):.1f}%'}")
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
