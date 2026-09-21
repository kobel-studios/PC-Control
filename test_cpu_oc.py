"""CPU overclock tests: mocked mailbox backend, no real hardware access."""
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import cooling_gui as gui


def make_app():
    """Build a CoolingApp with mocked hardware and a connected fake CPU backend."""
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
    app.cpu_backend = Mock()
    app.cpu_connected = True
    app.cpu_oc_switch.state(["!disabled"])
    app.cpu_auto_switch.state(["!disabled"])
    now = gui.time.monotonic()
    app.snapshot.update(cpu_temp=45, cpu_load=20, updated_at=now)
    return app, root


def teardown(app, root):
    root.update_idletasks()
    for callback in root.tk.splitlist(root.tk.call("after", "info")):
        root.after_cancel(callback)
    root.destroy()


class BackendGuardTests(unittest.TestCase):
    def test_mailbox_rejects_non_vf_commands(self):
        backend = gui.CpuOcBackend()
        backend.lib = Mock()
        backend.handle = Mock()
        for cmd in (0x02, 0x16, 0x18, 0x19, 0x1A, 0x1B, 0xFF):
            with self.assertRaises(RuntimeError):
                backend.mailbox(cmd)

    def test_probe_decodes_capabilities(self):
        backend = gui.CpuOcBackend()
        backend.lib = Mock()
        backend.handle = Mock()
        backend.mailbox = Mock(return_value=0x750)
        backend._read = Mock(return_value=0x0000232326262626)
        info = backend.probe()
        self.assertTrue(info["ratio_oc"])
        self.assertEqual(info["max_ratio"], 80)
        self.assertEqual(info["stock_ratio"], 0x26)

    def test_probe_reports_no_ratio_support(self):
        backend = gui.CpuOcBackend()
        backend.lib = Mock()
        backend.handle = Mock()
        backend.mailbox = Mock(return_value=0x0)
        backend._read = Mock(return_value=0)
        info = backend.probe()
        self.assertFalse(info["ratio_oc"])
        self.assertIsNone(info["stock_ratio"])

    def test_write_rejects_other_registers(self):
        backend = gui.CpuOcBackend()
        backend.lib = Mock()
        backend.handle = Mock()
        for addr in (0x1AD, 0xCE, 0x64, 0x0):
            with self.assertRaises(RuntimeError):
                backend._write(addr, 1)

    def test_set_ratio_bounds(self):
        backend = gui.CpuOcBackend()
        backend.lib = Mock()
        backend.handle = Mock()
        for bad in (0, -1, 46, 100, 3.5, "40", None):
            with self.assertRaises(RuntimeError):
                backend.set_ratio(bad)


class CpuSwitchTests(unittest.TestCase):
    def setUp(self):
        self.app, self.root = make_app()

    def tearDown(self):
        teardown(self.app, self.root)

    def enable(self, auto=False):
        self.app.cpu_auto_value.set(auto)
        with patch("tkinter.messagebox.askyesno", return_value=True):
            self.app.cpu_oc_value.set(True)
            self.app.on_cpu_toggle()

    def test_enable_applies_verified_ratio(self):
        self.enable()
        self.app.cpu_backend.set_ratio.assert_called_once_with(self.app.cpu_ratio_preset)
        self.assertTrue(self.app.cpu_oc_active)
        self.assertIn("ON", self.app.component_widgets["cpu"]["status"].cget("text"))

    def test_decline_confirmation_leaves_off(self):
        with patch("tkinter.messagebox.askyesno", return_value=False):
            self.app.cpu_oc_value.set(True)
            self.app.on_cpu_toggle()
        self.assertFalse(self.app.cpu_oc_value.get())
        self.app.cpu_backend.set_ratio.assert_not_called()
        self.assertFalse(self.app.cpu_oc_active)

    def test_disable_restores_stock(self):
        self.enable()
        self.app.cpu_oc_value.set(False)
        self.app.on_cpu_toggle()
        self.app.cpu_backend.restore.assert_called_once()
        self.assertFalse(self.app.cpu_oc_active)

    def test_apply_failure_blocks_and_restores(self):
        self.app.cpu_backend.set_ratio.side_effect = RuntimeError("rejected")
        self.enable()
        self.assertFalse(self.app.cpu_oc_value.get())
        self.assertFalse(self.app.cpu_oc_active)
        self.assertTrue(self.app.cpu_oc_fault)
        self.app.cpu_backend.restore.assert_called_once()

    def test_hot_temp_resets_and_pauses(self):
        self.enable()
        self.app.snapshot.update(cpu_temp=gui.CPU_OC_RESET_TEMP + 1, updated_at=gui.time.monotonic())
        self.app.tick_cpu(gui.time.monotonic())
        self.app.cpu_backend.restore.assert_called_once()
        self.assertFalse(self.app.cpu_oc_active)
        self.assertTrue(self.app.cpu_oc_paused)

    def test_stale_temp_resets(self):
        self.enable()
        self.app.snapshot["updated_at"] = 0
        self.app.tick_cpu(gui.time.monotonic())
        self.app.cpu_backend.restore.assert_called_once()
        self.assertFalse(self.app.cpu_oc_active)

    def test_auto_waits_for_sustained_load(self):
        self.enable(auto=True)
        self.app.cpu_backend.set_ratio.assert_not_called()
        now = gui.time.monotonic()
        self.app.snapshot.update(cpu_load=80, updated_at=now)
        self.app.tick_cpu(now)
        self.app.cpu_backend.set_ratio.assert_not_called()
        self.app.snapshot["updated_at"] = now + 11
        self.app.tick_cpu(now + 11)
        self.app.cpu_backend.set_ratio.assert_called_once_with(self.app.cpu_ratio_preset)
        self.assertTrue(self.app.cpu_oc_active)

    def test_auto_releases_when_idle(self):
        self.enable(auto=True)
        now = gui.time.monotonic()
        self.app.snapshot.update(cpu_load=80, updated_at=now)
        self.app.tick_cpu(now)
        self.app.snapshot["updated_at"] = now + 11
        self.app.tick_cpu(now + 11)
        self.assertTrue(self.app.cpu_oc_active)
        self.app.snapshot.update(cpu_load=5, updated_at=now + 12)
        self.app.tick_cpu(now + 12)
        self.app.cpu_backend.restore.assert_not_called()
        self.app.snapshot["updated_at"] = now + 43
        self.app.tick_cpu(now + 43)
        self.app.cpu_backend.restore.assert_called_once()
        self.assertFalse(self.app.cpu_oc_active)
        self.assertEqual(self.app.cpu_oc_paused, "idle")

    def test_auto_reapplies_after_cooldown(self):
        self.enable(auto=True)
        now = gui.time.monotonic()
        self.app.snapshot.update(cpu_load=80, updated_at=now)
        self.app.tick_cpu(now)
        self.app.snapshot["updated_at"] = now + 11
        self.app.tick_cpu(now + 11)
        self.app.snapshot.update(cpu_temp=gui.CPU_OC_RESET_TEMP + 1, updated_at=now + 12)
        self.app.tick_cpu(now + 12)
        self.assertEqual(self.app.cpu_oc_paused, "thermal")
        self.app.cpu_backend.reset_mock()
        self.app.snapshot.update(cpu_temp=40, updated_at=now + 13)
        self.app.tick_cpu(now + 13)
        self.app.snapshot["updated_at"] = now + 24
        self.app.tick_cpu(now + 24)
        self.app.cpu_backend.set_ratio.assert_called_once_with(self.app.cpu_ratio_preset)
        self.assertTrue(self.app.cpu_oc_active)

    def test_no_reapply_when_auto_off(self):
        self.enable()
        self.app.snapshot.update(cpu_temp=gui.CPU_OC_RESET_TEMP + 1, updated_at=gui.time.monotonic())
        self.app.tick_cpu(gui.time.monotonic())
        self.app.cpu_backend.reset_mock()
        self.app.snapshot.update(cpu_temp=40, updated_at=gui.time.monotonic())
        self.app.tick_cpu(gui.time.monotonic())
        self.app.cpu_backend.set_ratio.assert_not_called()

    def test_enable_refused_when_hot(self):
        self.app.snapshot.update(cpu_temp=gui.CPU_OC_RESET_TEMP + 5, updated_at=gui.time.monotonic())
        self.enable()
        self.assertFalse(self.app.cpu_oc_value.get())
        self.app.cpu_backend.set_ratio.assert_not_called()

    def test_enable_refused_on_stale_temp(self):
        self.app.snapshot["updated_at"] = 0
        self.enable()
        self.assertFalse(self.app.cpu_oc_value.get())
        self.app.cpu_backend.set_ratio.assert_not_called()

    def test_fault_blocks_reenable(self):
        self.app.cpu_oc_fault = "previous reset failed"
        self.enable()
        self.assertFalse(self.app.cpu_oc_value.get())
        self.app.cpu_backend.set_ratio.assert_not_called()

    def test_restore_failure_sets_fault(self):
        self.enable()
        self.app.cpu_backend.restore.side_effect = RuntimeError("restore rejected")
        self.app.cpu_oc_value.set(False)
        self.app.on_cpu_toggle()
        self.assertTrue(self.app.cpu_oc_fault)
        self.assertIn("FAILED", self.app.component_widgets["cpu"]["status"].cget("text"))

    def test_close_restores_active_oc(self):
        self.enable()
        self.app.gpu_owned = False
        self.app.gpu_busy = False
        self.app.stop_flag = Mock()
        self.app.worker = Mock()
        self.app.restore_all = Mock()
        self.app.save_config = Mock()
        self.app.computer = None
        with patch.object(self.root, "destroy"):
            self.app.on_close()
        self.app.cpu_backend.restore.assert_called_once()
        self.app.cpu_backend.close.assert_called_once()


class RatioPresetConfigTests(unittest.TestCase):
    def setUp(self):
        self.app, self.root = make_app()

    def tearDown(self):
        teardown(self.app, self.root)

    def test_invalid_ratios_rejected(self):
        import json, tempfile, os
        for bad in (0, -5, 99, "40", 40.5):
            fd, path = tempfile.mkstemp(suffix=".json")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"cpu_ratio_preset": bad}, f)
                with patch.object(gui, "CONFIG_PATH", path):
                    self.app.cpu_ratio_preset = 33
                    self.app.load_config()
                self.assertEqual(self.app.cpu_ratio_preset, 33, f"accepted {bad!r}")
            finally:
                os.unlink(path)

    def test_valid_ratio_saved_and_loaded(self):
        import json, tempfile, os
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"cpu_ratio_preset": 42}, f)
            with patch.object(gui, "CONFIG_PATH", path):
                self.app.load_config()
            self.assertEqual(self.app.cpu_ratio_preset, 42)
            self.app.save_config()
            with open(path) as f:
                self.assertEqual(json.load(f)["cpu_ratio_preset"], 42)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
