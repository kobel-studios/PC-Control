# FPS Control

A hardware-aware gaming performance app for Windows. It detects your
hardware on first run and configures itself — no manual setup.

## Features

- **Cooling control** — liquid-cooling pump/radiator fans and case fans as
  separate groups, load- and temperature-aware automatic curves, a 50% baseline,
  and Fahrenheit telemetry. Works on any motherboard LibreHardwareMonitor supports.
- **GPU overclock** — built-in NVIDIA driver path (NVAPI); no Afterburner
  needed on supported cards. Core clock offsets (plus memory where the driver
  exposes it) with automatic power-limit headroom. Cards whose drivers reject
  direct clock writes fall back to a managed helper component automatically.
- **CPU overclock** — raises the turbo ratio cap on unlocked Intel CPUs through
  the PawnIO OC mailbox, plus package power-limit headroom. Auto-detects CPU
  support; locked chips show a clear "unavailable" reason.
- **Game Boost** — while a listed game runs: High CPU priority, a faster
  Windows timer, and the High Performance power plan. Helps frame pacing;
  anti-cheat may block the priority change, the rest still applies.
- **FPS Cleanup** — while a listed game runs: automatically closes known
  non-essential background apps that are eating CPU, and asks before closing
  anything that might be important. "Always close" / "never" answers are
  remembered; the game, the app itself, Windows, drivers, and cooling or
  monitoring services are never touched.
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
- FPS Cleanup only closes user applications; it never touches Windows,
  security, driver, or hardware-monitoring processes. It reduces background
  CPU/RAM/disk load — it does not guarantee an FPS increase.
