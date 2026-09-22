"""Mocked tests for the component-switch UI (GPU core / GPU memory / CPU).

Covers the cases Devin-PCControl requested:
  - Startup: all OC switches OFF, Auto selected by default
  - No OC sliders or separate tabs (switches only)
  - Independent core/memory ON/OFF
  - Missing preset blocks enabling a switch
  - tick_components disarm paths (stale sensors, external offsets, GPU change)

All tests are mocked - no hardware calls, no Afterburner process.
"""
import importlib.util
import math
import struct
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import Mock, patch, mock_open

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


class StartupStateTests(unittest.TestCase):
    """At startup all OC switches are OFF and Auto is selected by default."""

    def setUp(self):
        self.app, self.root, self.info = make_app()

    def tearDown(self):
        teardown(self.app, self.root)

    def test_gpu_switches_start_off(self):
        for key in ("core", "memory"):
            self.assertFalse(self.app.gpu_components[key]["enabled"].get())

    def test_auto_selected_by_default(self):
        for key in ("core", "memory"):
            self.assertTrue(self.app.gpu_components[key]["auto"].get())
        self.assertTrue(self.app.cpu_auto_value.get())

    def test_cpu_switch_disabled(self):
        self.assertTrue(self.app.cpu_oc_switch.instate(["disabled"]))

    def test_no_sliders_in_performance_tab(self):
        self.assertFalse(hasattr(self.app, "gpu_sliders") and self.app.gpu_sliders)

    def test_single_page_no_tabs(self):
        # The UI is a single scrollable page, not a tabbed notebook.
        self.assertFalse(hasattr(self.app, "notebook"))

    def test_component_session_false_at_startup(self):
        self.assertFalse(self.app.component_session)


class AutoPresetCreation(unittest.TestCase):
    """Missing/mismatched presets are replaced by a conservative auto preset -
    enabling a switch never requires the user to enter values."""

    def setUp(self):
        self.app, self.root, self.info = make_app()

    def tearDown(self):
        teardown(self.app, self.root)

    def _assert_auto_preset(self):
        preset = self.app.gpu_auto_preset
        self.assertTrue(gui.valid_auto_preset(preset))
        self.assertTrue(preset.get("auto_default"))
        self.assertEqual(preset["uuid"], "GPU-test")
        self.assertGreater(preset["core"], 0)
        self.assertGreater(preset["memory"], 0)

    def test_enable_without_preset_autocreates(self):
        for key in ("core", "memory"):
            self.app.gpu_auto_preset = None
            self.app.gpu_components[key]["enabled"].set(True)
            with patch("tkinter.messagebox.askyesno", return_value=False):
                self.app.on_component_toggle(key)
            self._assert_auto_preset()
            self.assertFalse(self.app.gpu_components[key]["enabled"].get())

    def test_enable_with_wrong_uuid_preset_autocreates(self):
        self.app.gpu_auto_preset = {"core": 25, "memory": 50, "uuid": "GPU-other", "user_tested": True}
        for key in ("core", "memory"):
            self.app.gpu_components[key]["enabled"].set(True)
            with patch("tkinter.messagebox.askyesno", return_value=False):
                self.app.on_component_toggle(key)
            self._assert_auto_preset()
            self.assertFalse(self.app.gpu_components[key]["enabled"].get())

    def test_enable_with_default_preset_autocreates(self):
        self.app.gpu_auto_preset = {"core": 0, "memory": 0, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_components["core"]["enabled"].set(True)
        with patch("tkinter.messagebox.askyesno", return_value=False):
            self.app.on_component_toggle("core")
        self._assert_auto_preset()
        self.assertFalse(self.app.gpu_components["core"]["enabled"].get())


class IndependentComponentToggle(unittest.TestCase):
    """Core and memory can be toggled independently."""

    def setUp(self):
        self.app, self.root, self.info = make_app()
        self.preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_auto_preset = self.preset

    def tearDown(self):
        teardown(self.app, self.root)

    def test_enable_core_only(self):
        self.app.gpu_components["core"]["enabled"].set(True)
        with patch("tkinter.messagebox.askyesno", return_value=True), \
                patch.object(gui.threading, "Thread") as worker:
            self.app.on_component_toggle("core")
        self.assertTrue(self.app.gpu_components["core"]["enabled"].get())
        self.assertFalse(self.app.gpu_components["memory"]["enabled"].get())

    def test_enable_memory_only(self):
        self.app.gpu_components["memory"]["enabled"].set(True)
        with patch("tkinter.messagebox.askyesno", return_value=True), \
                patch.object(gui.threading, "Thread") as worker:
            self.app.on_component_toggle("memory")
        self.assertTrue(self.app.gpu_components["memory"]["enabled"].get())
        self.assertFalse(self.app.gpu_components["core"]["enabled"].get())

    def test_disable_core_keeps_memory(self):
        for key in ("core", "memory"):
            self.app.gpu_components[key]["enabled"].set(True)
            with patch("tkinter.messagebox.askyesno", return_value=True), \
                    patch.object(gui.threading, "Thread") as worker:
                self.app.on_component_toggle(key)
        self.app.gpu_components["core"]["enabled"].set(False)
        self.app.on_component_toggle("core")
        self.assertFalse(self.app.gpu_components["core"]["enabled"].get())
        self.assertTrue(self.app.gpu_components["memory"]["enabled"].get())

    def test_cancel_enable_does_not_turn_on(self):
        self.app.gpu_components["core"]["enabled"].set(True)
        with patch("tkinter.messagebox.askyesno", return_value=False), \
                patch.object(gui.threading, "Thread") as worker:
            self.app.on_component_toggle("core")
        self.assertFalse(self.app.gpu_components["core"]["enabled"].get())
        worker.assert_not_called()

    def test_disable_all_resets_gpu(self):
        for key in ("core", "memory"):
            self.app.gpu_components[key]["enabled"].set(True)
            with patch("tkinter.messagebox.askyesno", return_value=True), \
                    patch.object(gui.threading, "Thread") as worker:
                self.app.on_component_toggle(key)
        self.app.gpu_owned = True
        self.app.gpu_components["core"]["enabled"].set(False)
        self.app.gpu_components["memory"]["enabled"].set(False)
        with patch.object(self.app, "submit_gpu") as submit:
            self.app.on_component_toggle("memory")
        submit.assert_called_once_with(reset=True, automatic=True)


class AutoChangeTests(unittest.TestCase):
    """Auto toggle behavior."""

    def setUp(self):
        self.app, self.root, self.info = make_app()
        self.preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_auto_preset = self.preset

    def tearDown(self):
        teardown(self.app, self.root)

    def test_auto_change_no_effect_when_disabled(self):
        self.app.gpu_components["core"]["auto"].set(False)
        self.app.on_component_auto_change("core")
        self.assertFalse(self.app.component_session)

    def test_auto_off_requires_confirmation(self):
        self.app.gpu_components["core"]["enabled"].set(True)
        with patch("tkinter.messagebox.askyesno", return_value=True), \
                patch.object(gui.threading, "Thread"):
            self.app.on_component_toggle("core")
        self.app.gpu_components["core"]["auto"].set(False)
        with patch("tkinter.messagebox.askyesno", return_value=False):
            self.app.on_component_auto_change("core")
        self.assertTrue(self.app.gpu_components["core"]["auto"].get())


class TickComponentsTests(unittest.TestCase):
    """tick_components disarm and reset paths."""

    def setUp(self):
        self.app, self.root, self.info = make_app()
        self.preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_auto_preset = self.preset
        self.app.component_session = True
        for key in ("core", "memory"):
            self.app.gpu_components[key]["enabled"].set(True)

    def tearDown(self):
        teardown(self.app, self.root)

    def test_stale_gpu_disarms_and_resets(self):
        self.app.gpu_owned = True
        self.app.gpu_data["updated_at"] = 0
        with patch.object(gui.threading, "Thread") as worker:
            self.app.tick_components(gui.time.monotonic(), self.app.component_connection_ready())
        self.assertFalse(self.app.component_session)
        worker.assert_called_once()

    def test_different_gpu_disarms_without_write(self):
        self.app.gpu_owned = True
        self.app.gpu_data["uuid"] = "GPU-different"
        with patch.object(gui.threading, "Thread") as worker:
            self.app.tick_components(gui.time.monotonic(), self.app.component_connection_ready())
        self.assertFalse(self.app.component_session)
        worker.assert_not_called()

    def test_external_offset_change_disarms(self):
        self.app.gpu_owned = True
        self.app.gpu_managed_offsets = [25, 50]
        self.app.gpu_control["info"] = dict(self.info, core=[80, -400, 1000, 0], is_default=False)
        with patch.object(gui.threading, "Thread") as worker:
            self.app.tick_components(gui.time.monotonic(), self.app.component_connection_ready())
        self.assertFalse(self.app.component_session)
        worker.assert_not_called()

    def test_hot_temp_resets_to_defaults(self):
        # Simulate the app owning non-default offsets that need to be reset when hot.
        self.app.gpu_owned = True
        self.app.gpu_managed_offsets = [25, 50]  # what the app thinks it set
        # Make info show non-default actual offsets so desired != actual when hot
        info = self.app.gpu_control["info"]
        info["core"] = (25, -400, 1000, 0)
        info["memory"] = (50, -400, 1000, 0)
        self.app.gpu_data.update(temp=85, updated_at=gui.time.monotonic())
        self.app.snapshot.update(updated_at=gui.time.monotonic())
        with patch.object(self.app, "submit_gpu") as submit:
            self.app.tick_components(gui.time.monotonic(), self.app.component_connection_ready())
        submit.assert_called_once()

    def test_busy_does_not_tick(self):
        self.app.gpu_busy = True
        with patch.object(gui.threading, "Thread") as worker:
            self.app.tick_components(gui.time.monotonic(), True)
        worker.assert_not_called()

    def test_no_session_does_not_tick(self):
        self.app.component_session = False
        with patch.object(gui.threading, "Thread") as worker:
            self.app.tick_components(gui.time.monotonic(), True)
        worker.assert_not_called()


class ScaledOffsetTests(unittest.TestCase):
    """Auto governor scales the tested preset by GPU temperature:
    full at <=65C, linear taper to zero at 80C, defaults when hotter.
    desired must never exceed the user-tested preset."""

    def setUp(self):
        self.app, self.root, self.info = make_app()
        self.preset = {"core": 60, "memory": 120, "uuid": "GPU-test", "user_tested": True}
        self.app.gpu_auto_preset = self.preset
        self.app.component_session = True
        for key in ("core", "memory"):
            self.app.gpu_components[key]["enabled"].set(True)

    def tearDown(self):
        teardown(self.app, self.root)

    def set_offsets(self, core, memory):
        """Pretend the app already applied these offsets."""
        self.app.gpu_owned = True
        self.app.gpu_managed_offsets = [core, memory]
        info = self.app.gpu_control["info"]
        info["core"] = (core, -400, 1000, 0)
        info["memory"] = (memory, -400, 1000, 0)
        info["is_default"] = core == 0 and memory == 0
        for key in ("core", "memory"):
            self.app.gpu_components[key]["applied"] = True

    def test_oc_scale_bounds(self):
        self.assertEqual(gui.oc_scale(50), 1.0)
        self.assertEqual(gui.oc_scale(65), 1.0)
        self.assertAlmostEqual(gui.oc_scale(72.5), 0.5)
        self.assertEqual(gui.oc_scale(80), 0.0)
        self.assertEqual(gui.oc_scale(90), 0.0)

    def test_cool_temp_full_preset(self):
        self.set_offsets(0, 0)
        self.app.gpu_data.update(temp=60, load=90)
        with patch.object(self.app, "submit_gpu") as submit:
            self.app.tick_components(gui.time.monotonic(), True)
        submit.assert_called_once_with(reset=False, automatic=True,
                                       desired_offsets=[60, 120])

    def test_warm_temp_scales_preset(self):
        # temp 70 -> scale 10/15 -> round(60*0.667)=40, round(120*0.667)=80
        self.set_offsets(0, 0)
        self.app.gpu_data.update(temp=70, load=90)
        with patch.object(self.app, "submit_gpu") as submit:
            self.app.tick_components(gui.time.monotonic(), True)
        submit.assert_called_once_with(reset=False, automatic=True,
                                       desired_offsets=[40, 80])

    def test_scaled_never_above_preset(self):
        for temp in (65, 68, 70, 72, 75, 78, 80):
            with self.subTest(temp=temp):
                self.set_offsets(0, 0)
                self.app.gpu_data.update(temp=temp, load=90)
                with patch.object(self.app, "submit_gpu") as submit:
                    self.app.tick_components(gui.time.monotonic(), True)
                for call in submit.call_args_list:
                    desired = call.kwargs.get("desired_offsets")
                    if desired is not None:
                        self.assertLessEqual(desired[0], 60)
                        self.assertLessEqual(desired[1], 120)
                        self.assertGreaterEqual(desired[0], 0)
                        self.assertGreaterEqual(desired[1], 0)

    def test_scale_zero_at_80_resets(self):
        self.set_offsets(40, 80)
        self.app.gpu_data.update(temp=80, load=90)
        with patch.object(self.app, "submit_gpu") as submit:
            self.app.tick_components(gui.time.monotonic(), True)
        submit.assert_called_once_with(reset=True, automatic=True, desired_offsets=[0, 0])

    def test_small_change_does_not_submit(self):
        # preset 39/75 at temp 70 -> desired [26,50]; actual [24,50] diff <3 -> skip
        self.app.gpu_auto_preset = {"core": 39, "memory": 75,
                                    "uuid": "GPU-test", "user_tested": True}
        self.set_offsets(24, 50)
        self.app.gpu_data.update(temp=70, load=90)
        with patch.object(self.app, "submit_gpu") as submit:
            self.app.tick_components(gui.time.monotonic(), True)
        submit.assert_not_called()

    def test_scaled_status_message(self):
        self.set_offsets(40, 80)
        self.app.gpu_data.update(temp=70, load=90)
        self.app.tick_components(gui.time.monotonic(), True)
        self.assertIn("scaled to", self.app.gpu_auto_status.get())


if __name__ == "__main__":
    unittest.main()
