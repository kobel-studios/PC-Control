# PC Control

A hardware-aware cooling and overclocking app for Windows. It detects your
hardware on first run and configures itself — no manual setup.

## Features

- **Cooling control** — liquid-cooling pump/radiator fans and case fans as
  separate groups, load- and temperature-aware automatic curves, a 50% baseline,
  and Fahrenheit telemetry. Works on any motherboard LibreHardwareMonitor supports.
- **GPU overclock** — built-in NVIDIA driver path (NVAPI); no Afterburner
  needed for NVIDIA cards. Core and memory offsets, plus automatic power-limit
  headroom. Afterburner remains a fallback for AMD.
- **CPU overclock** — raises the turbo ratio cap on unlocked Intel CPUs through
  the PawnIO OC mailbox, plus package power-limit headroom. Auto-detects CPU
  support; locked chips show a clear "unavailable" reason.
- **Auto-tuner** — steps overclocks upward during sustained load and backs off
  if Windows reports hardware errors.
- **Safety** — thermal reset, stale-sensor reset, restore-on-exit, verified
  readback on every write, bounded values only.

## Requirements

- Windows 10/11, x64
- Python 3.10+ (64-bit) with `pythonnet` (`pip install pythonnet`)
- Administrator rights (required for hardware access)
- NVIDIA GPU for built-in GPU overclock; unlocked Intel CPU for CPU overclock

## Run

```bat
python cooling_gui.py
```

Approve the admin prompt — that's the only step. The app installs its driver,
detects hardware, and arms supported controls automatically.

## Development

```bat
python -m unittest discover -s . -p "test*.py"
```

All hardware writes are mocked in tests; tests do not certify overclock
stability.

## Safety notes

- This software changes clock offsets, power limits, and fan speeds on real
  hardware. Values are bounded and reversible, and every write is verified by
  readback — but no software can guarantee overclock stability.
- Overclocks release automatically on overheat, stale telemetry, or app exit.
