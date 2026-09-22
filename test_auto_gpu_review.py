"""Review tests for Auto GPU OC disable paths.

Covers the cases Devin-PCControl requested:
  - Auto disables while an apply is pending (no second write starts)
  - Apply or reset failure disarms Auto and leaves gpu_owned/gpu_reset_failed correct
  - External offset changes disarm Auto without writing
  - GPU identity change disarms Auto without writing to the different GPU

All tests are mocked - no hardware calls, no Afterburner process needed.
"""
import importlib.util
import math
import struct
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("cooling_gui", Path(__file__).with_name("cooling_gui.py"))
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)


class FakeMemory:
    def __init__(self, acknowledge=True):
        self.data = bytearray(36 + 220)
        struct.pack_into("<9I", self.data, 0, gui.MACM_SIGNATURE, 0x20000, 36, 1, 220, 0, 0, 0, 0)
        struct.pack_into("<I", self.data, 36, 0x1800)
        for offset in (gui.CORE_OFFSET, gui.MEMORY_OFFSET):
            struct.pack_into("<4i", self.data, 36 + offset, 0, -400000, 1000000, 0)
        self.writes = []
        self.acknowledge = acknowledge

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def lock(self):
        return nullcontext()

    def read(self):
        return bytes(self.data)

    def write_int(self, offset, value):
        self.writes.append((offset, value))
        struct.pack_into("<i", self.data, offset, 0 if offset == 32 and self.acknowledge else value)


def make_app():
    """Build a CoolingApp with mocked hardware/config/threading and fresh GPU state."""
    import tkinter as tk
    from tkinter import ttk
    gui.tk, gui.ttk = tk, ttk
    root = tk.Tk()
    root.withdraw()
    with patch.object(gui.CoolingApp, "open_hardware"), \
            patch.object(gui.CoolingApp, "load_config"), \
            patch.object(gui.CoolingApp, "connect_cpu_backend"), \
            patch.object(gui.threading, "Thread"):
        app = gui.CoolingApp(root)
    app.gpu_backend = Mock()
    info = gui.decode_macm(FakeMemory().read())
    now = gui.time.monotonic()
    app.gpu_control = {"info": info, "updated_at": now}
    app.snapshot.update(cpu_temp=45, updated_at=now)
    app.gpu_data = {"temp": 55, "name": "GTX 1070", "uuid": "GPU-test", "load": 90, "updated_at": now}
    app.refresh_performance()
    return app, root, info


def teardown(app, root):
    root.update_idletasks()
    for callback in root.tk.splitlist(root.tk.call("after", "info")):
        root.after_cancel(callback)
    root.destroy()


class AutoDisableDuringPendingApply(unittest.TestCase):
    """Auto must not start a second write while an apply/reset is already in flight."""

    def setUp(self):
        self.app, self.root, self.info = make_app()
        self.preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_auto_preset = self.preset

    def tearDown(self):
        teardown(self.app, self.root)

    def test_auto_does_not_apply_while_busy(self):
        self.app.gpu_auto_enabled.set(True)
        self.app.gpu_busy = True
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        worker.assert_not_called()
        self.assertTrue(self.app.gpu_auto_enabled.get())

    def test_disable_request_while_busy_sets_stop_after_busy(self):
        self.app.gpu_owned = True
        self.app.gpu_busy = True
        self.app.gpu_auto_enabled.set(False)
        self.app.on_gpu_auto_toggle()
        self.assertTrue(self.app.gpu_stop_after_busy)

    def test_manual_reset_while_busy_does_not_start_second_write(self):
        self.app.gpu_busy = True
        with patch.object(gui.threading, "Thread") as worker:
            self.app.submit_gpu(reset=True)
        worker.assert_not_called()
        self.assertTrue(self.app.gpu_stop_after_busy)


class ApplyFailureDisarmsAuto(unittest.TestCase):
    """A failed apply must disarm Auto and trigger a reset attempt."""

    def setUp(self):
        self.app, self.root, self.info = make_app()
        self.preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_auto_preset = self.preset

    def tearDown(self):
        teardown(self.app, self.root)

    def test_apply_error_disarms_auto_and_requests_reset(self):
        self.app.gpu_auto_enabled.set(True)
        self.app.gpu_busy = True
        self.app.gpu_results.put((False, None, "Afterburner vanished"))
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        # failure path queues an automatic reset; busy stays True for the reset
        worker.assert_called_once()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        self.assertTrue(self.app.gpu_busy)

    def test_reset_error_marks_reset_failed_and_disarms_auto(self):
        self.app.gpu_auto_enabled.set(True)
        self.app.gpu_owned = True
        self.app.gpu_busy = True
        self.app.gpu_results.put((True, None, "Afterburner vanished"))
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        # reset failure must NOT start another reset attempt
        worker.assert_not_called()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        self.assertTrue(self.app.gpu_reset_failed)
        self.assertTrue(self.app.gpu_owned)
        self.assertEqual(self.app.gpu_switch_value.get(), -1)

    def test_reset_failure_blocks_subsequent_apply(self):
        self.app.gpu_reset_failed = True
        self.app.gpu_core_value.set(25)
        with patch("tkinter.messagebox.askyesno") as confirm, \
                patch.object(gui.threading, "Thread") as worker:
            self.app.submit_gpu()
        confirm.assert_not_called()
        worker.assert_not_called()
        self.assertIn("Reset clock offsets", self.app.gpu_notice.get())


class ExternalOffsetChangesDisarmAuto(unittest.TestCase):
    """If offsets were changed outside this app, Auto must disarm without writing."""

    def setUp(self):
        self.app, self.root, self.info = make_app()
        self.preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_auto_preset = self.preset
        self.app.gpu_auto_enabled.set(True)

    def tearDown(self):
        teardown(self.app, self.root)

    def test_owned_but_offsets_drift_disarms_without_write(self):
        self.app.gpu_owned = True
        self.app.gpu_control["info"] = dict(self.info, core=[80, -400, 1000, 0],
                                            memory=[50, -400, 1000, 0], is_default=False)
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        worker.assert_not_called()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        self.assertFalse(self.app.gpu_owned)

    def test_not_owned_but_non_default_disarms_without_write(self):
        self.app.gpu_owned = False
        self.app.gpu_control["info"] = dict(self.info, core=[40, -400, 1000, 0],
                                            memory=[0, -400, 1000, 0], is_default=False)
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        worker.assert_not_called()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        self.assertFalse(self.app.gpu_owned)

    def test_vf_curve_flag_disarms_auto_and_resets_when_owned(self):
        self.app.gpu_owned = True
        info = dict(self.info, flags=0x44000)
        self.app.gpu_control["info"] = info
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        worker.assert_called_once()
        self.assertFalse(self.app.gpu_auto_enabled.get())

    def test_vf_curve_flag_disarms_auto_without_write_when_not_owned(self):
        self.app.gpu_owned = False
        info = dict(self.info, flags=0x44000)
        self.app.gpu_control["info"] = info
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        worker.assert_not_called()
        self.assertFalse(self.app.gpu_auto_enabled.get())


class GpuIdentityChange(unittest.TestCase):
    """A different GPU must never receive the preset's offsets."""

    def setUp(self):
        self.app, self.root, self.info = make_app()
        self.preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_auto_preset = self.preset
        self.app.gpu_auto_enabled.set(True)

    def tearDown(self):
        teardown(self.app, self.root)

    def test_different_gpu_disarms_without_write(self):
        self.app.gpu_owned = True
        self.app.gpu_data["uuid"] = "GPU-different"
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        worker.assert_not_called()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        self.assertFalse(self.app.gpu_owned)

    def test_preset_uuid_mismatch_blocks_enable(self):
        self.app.gpu_auto_preset = dict(self.preset, uuid="GPU-other")
        self.app.gpu_auto_enabled.set(True)
        with patch("tkinter.messagebox.askyesno") as confirm:
            self.app.on_gpu_auto_toggle()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        confirm.assert_not_called()

    def test_automatic_apply_with_mismatched_uuid_does_not_write(self):
        self.app.gpu_auto_preset = dict(self.preset, uuid="GPU-other")
        self.app.gpu_auto_enabled.set(True)
        with patch.object(gui.threading, "Thread") as worker:
            self.app.submit_gpu(automatic=True)
        worker.assert_not_called()
        self.assertFalse(self.app.gpu_auto_enabled.get())


class StaleSensorDisarmsAuto(unittest.TestCase):
    """Stale GPU or CPU telemetry must disarm Auto and reset if owned."""

    def setUp(self):
        self.app, self.root, self.info = make_app()
        self.preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_auto_preset = self.preset
        self.app.gpu_auto_enabled.set(True)

    def tearDown(self):
        teardown(self.app, self.root)

    def test_stale_gpu_data_disarms_and_resets_if_owned(self):
        self.app.gpu_owned = True
        self.app.gpu_data["updated_at"] = 0
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        worker.assert_called_once()

    def test_stale_gpu_data_not_owned_disarms_without_write(self):
        self.app.gpu_owned = False
        self.app.gpu_data["updated_at"] = 0
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        worker.assert_not_called()

    def test_stale_cpu_snapshot_disarms_and_resets_if_owned(self):
        self.app.gpu_owned = True
        self.app.snapshot["updated_at"] = 0
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        worker.assert_called_once()


if __name__ == "__main__":
    unittest.main()
