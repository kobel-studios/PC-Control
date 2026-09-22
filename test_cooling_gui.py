import importlib.util
import math
import struct
from contextlib import nullcontext
from pathlib import Path
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch, mock_open

spec = importlib.util.spec_from_file_location("cooling_gui", Path(__file__).with_name("cooling_gui.py"))
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)


class Var:
    def __init__(self, value):
        self.value = value
        self._card_label = Mock()

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class TargetTests(unittest.TestCase):
    def test_workload_increases_cooling_without_fixed_target(self):
        self.assertEqual(gui.target_duty(50, 35, 5, load=100), 60)
        self.assertEqual(gui.target_duty(50, 35, 5, gpu_temp=80), 60)
        self.assertEqual(gui.target_duty(50, 35, 5), 50)

    def test_above_target_increases(self):
        self.assertGreater(gui.target_duty(60, 50, 5), 60)

    def test_below_target_decreases_slowly(self):
        self.assertEqual(gui.target_duty(80, 40, 5), 78)

    def test_curve_does_not_integrate_to_max_just_above_113f(self):
        value = 50
        for _ in range(100):
            value = gui.target_duty(value, 46, 5)
        self.assertEqual(value, 59)

    def test_bounds_and_slew_limit(self):
        value = 50
        for _ in range(100):
            value = gui.target_duty(value, 70, 5)
        self.assertEqual(value, 100)
        self.assertLess(gui.target_duty(value, 40, 5), 100)
        self.assertEqual(gui.target_duty(50, 30, 5), 50)
        self.assertEqual(gui.target_duty(60, 70, 1000), 70)

    def test_bad_sensor_and_overheat(self):
        for temp in (None, math.nan, math.inf, -10, 150, 90):
            self.assertEqual(gui.target_duty(50, temp, 1), 100)

    def make_app(self, temp=50, auto=True):
        app = gui.CoolingApp.__new__(gui.CoolingApp)
        app.snapshot = {"cpu_temp": temp, "updated_at": 100.0}
        app.hardware_lock = threading.RLock()
        app.auto_mode = Var(auto)
        app.liquid_val = Var(50)
        app.case_val = Var(50)
        app.manual_values = [50, 50]
        app.auto_boost = [50, 50]
        app.auto_duty = 50
        app.last_control_time = 95.0
        app.last_applied = (50, 50)
        app._programmatic = False
        app.roles = {0: Var("Liquid Cooling"), 1: Var("Case Fans")}
        app.controls = {i: SimpleNamespace(Control=Mock()) for i in (0, 1)}
        app.test_channel = None
        app.test_until = 0
        app.save_config = Mock()
        return app

    def test_auto_moves_sliders_and_writes(self):
        app = self.make_app()
        with patch.object(gui.time, "monotonic", return_value=100.0):
            app.apply_speeds()
        self.assertEqual(app.liquid_val.get(), 60)
        self.assertEqual(app.case_val.get(), 60)
        self.assertEqual(app.auto_boost, [50, 50])
        for sensor in app.controls.values():
            sensor.Control.SetSoftware.assert_called_with(60)

    def test_stale_sensor_fails_safe_even_manual(self):
        app = self.make_app(auto=False)
        app.snapshot["updated_at"] = 90.0
        with patch.object(gui.time, "monotonic", return_value=100.0):
            app.apply_speeds()
        self.assertEqual(app.last_applied, (100, 100))

    def test_test_button_does_not_suspend_other_channels(self):
        app = self.make_app(temp=95)
        app.test_channel = 0
        app.test_until = 104.0
        with patch.object(gui.time, "monotonic", return_value=100.0):
            app.apply_speeds()
        for sensor in app.controls.values():
            sensor.Control.SetSoftware.assert_called_with(100)

    def test_programmatic_slider_does_not_become_boost(self):
        app = self.make_app()
        app._programmatic = True
        app.liquid_val.set(100)
        app.on_slider(app.liquid_val)
        self.assertEqual(app.auto_boost, [50, 50])
        app.save_config.assert_not_called()

    def test_user_can_boost_but_not_undercut_target(self):
        app = self.make_app()
        with patch.object(gui.time, "monotonic", return_value=100.0):
            app.liquid_val.set(90)
            app.on_slider(app.liquid_val)
            self.assertEqual(app.last_applied, (90, 60))
            app.liquid_val.set(50)
            app.on_slider(app.liquid_val)
            self.assertEqual(app.last_applied, (60, 60))

    def test_enabling_auto_discards_old_manual_100(self):
        app = self.make_app()
        app.manual_values = [100, 100]
        app.auto_boost = [100, 100]
        with patch.object(gui.time, "monotonic", return_value=100.0):
            app.on_mode_change()
        self.assertEqual(app.last_applied, (60, 60))


class WidgetTests(unittest.TestCase):
    def test_real_widgets_move_without_latching_boost(self):
        import tkinter as tk
        from tkinter import ttk
        gui.tk, gui.ttk = tk, ttk
        root = tk.Tk()
        root.withdraw()
        try:
            with patch.object(gui.CoolingApp, "open_hardware"), \
                    patch.object(gui.CoolingApp, "load_config"), \
                    patch.object(gui.CoolingApp, "connect_cpu_backend"), \
                    patch.object(gui.threading, "Thread"):
                app = gui.CoolingApp(root)
            app.controls = {0: SimpleNamespace(Control=Mock())}
            app.roles[0].set("Liquid Cooling")
            app.snapshot = {"cpu_temp": 50, "cpu_load": 20, "updated_at": 100.0,
                            "duty": {0: 60}, "rpm": {0: 1200}}
            app.last_control_time = 95
            with patch.object(gui.time, "monotonic", return_value=100.0):
                app.tick()
                root.update_idletasks()
            self.assertEqual(app.liquid_val.get(), 60)
            self.assertEqual(app.case_val.get(), 60)
            self.assertEqual(app.auto_boost, [50, 50])
            self.assertEqual(app.temp_label.cget("text"), "122 F")
            self.assertIn("Load-aware", app.status_label.cget("text"))
            self.assertEqual(root.title(), "Gaming FPS Control")
            self.assertFalse(hasattr(app, "notebook"))
            self.assertTrue(app.cpu_oc_switch.instate(["disabled"]))
            self.assertTrue(app.cpu_auto_value.get())
            for component in app.gpu_components.values():
                self.assertTrue(component["auto"].get())
                self.assertFalse(component["enabled"].get())
            self.assertEqual(app.gpu_sliders, [])
            for widgets in app.component_widgets.values():
                self.assertIsInstance(widgets["auto"], gui.ToggleSwitch)
                self.assertIsInstance(widgets["switch"], gui.ToggleSwitch)
            self.assertIn("1200 RPM", app.chan_rows[0][1].cget("text"))
            app.snapshot.update(cpu_temp=40, updated_at=105.0)
            with patch.object(gui.time, "monotonic", return_value=105.0):
                app.tick()
                root.update_idletasks()
            self.assertEqual(app.liquid_val.get(), 58)
            self.assertEqual(app.auto_boost, [50, 50])
        finally:
            for callback in root.tk.splitlist(root.tk.call("after", "info")):
                root.after_cancel(callback)
            root.destroy()


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


class GpuBackendTests(unittest.TestCase):
    def test_read_is_read_only(self):
        memory = FakeMemory()
        control = gui.AfterburnerControl(lambda **_: memory, notify=Mock())
        self.assertTrue(control.read()["is_default"])
        self.assertEqual(memory.writes, [])

    def test_apply_writes_only_offsets_and_command_and_resets(self):
        memory = FakeMemory()
        notify = Mock()
        control = gui.AfterburnerControl(lambda **_: memory, notify=notify)
        before = bytes(memory.data)
        with patch.object(gui.time, "sleep"):
            applied = control.apply(25, 50, expected=[0, 0])
            self.assertEqual(applied["core"][0], 25)
            self.assertFalse(applied["is_default"])
            self.assertEqual(memory.writes, [(36 + gui.CORE_OFFSET, 25000),
                                             (36 + gui.MEMORY_OFFSET, 50000), (32, gui.MACM_FLUSH)])
            reset = control.apply(reset=True)
        self.assertTrue(reset["is_default"])
        self.assertEqual(bytes(memory.data), before)
        self.assertEqual(notify.call_count, 2)

    def test_invalid_requests_do_not_write(self):
        for core, mem in ((101, 0), (0, 251), (math.nan, 0), (math.inf, 0), (-1, 0)):
            memory = FakeMemory()
            with self.assertRaises(ValueError):
                gui.AfterburnerControl(lambda **_: memory, notify=Mock()).apply(core, mem)
            self.assertEqual(memory.writes, [])

    def test_external_change_rejected_before_writing(self):
        memory = FakeMemory()
        with self.assertRaisesRegex(RuntimeError, "changed"):
            gui.AfterburnerControl(lambda **_: memory, notify=Mock()).apply(25, 0, expected=[50, 0])
        self.assertEqual(memory.writes, [])

    def test_vf_curve_and_pending_command_rejected(self):
        for offset, value in ((36, 0x41800), (32, gui.MACM_FLUSH)):
            memory = FakeMemory()
            struct.pack_into("<I", memory.data, offset, value)
            with self.assertRaises(RuntimeError):
                gui.AfterburnerControl(lambda **_: memory, notify=Mock()).apply(25, 0)
            self.assertEqual(memory.writes, [])

    def test_bad_headers_are_rejected(self):
        for offset, value in ((0, 0xDEAD), (4, 0x30000), (8, 1), (12, 2), (16, 20), (20, 1)):
            memory = FakeMemory()
            struct.pack_into("<I", memory.data, offset, value)
            with self.assertRaises(RuntimeError):
                gui.decode_macm(memory.read())
        with self.assertRaises(RuntimeError):
            gui.decode_macm(b"x")

    def test_timeout_is_not_success(self):
        memory = FakeMemory(acknowledge=False)
        with patch.object(gui.time, "monotonic", side_effect=[0, 0, 7]), patch.object(gui.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                gui.AfterburnerControl(lambda **_: memory, notify=Mock()).apply(25, 0)

    def test_mismatching_readback_is_not_success(self):
        memory = FakeMemory()
        def notify():
            struct.pack_into("<i", memory.data, 36 + gui.CORE_OFFSET, 0)
        with patch.object(gui.time, "sleep"), self.assertRaisesRegex(RuntimeError, "differs"):
            gui.AfterburnerControl(lambda **_: memory, notify=notify).apply(25, 0)

    def test_telemetry_parser_and_safety_gate(self):
        gpu = gui.parse_gpu_telemetry("GPU-1, NVIDIA GeForce GTX 1070, 55, 40, [N/A], 1809, 4006, 100")
        self.assertIsNone(gpu["fan"])
        self.assertEqual(gpu["temp"], 55)
        now = gpu["updated_at"]
        cpu = {"cpu_temp": 45, "updated_at": now}
        info = gui.decode_macm(FakeMemory().read())
        self.assertIsNone(gui.gpu_apply_problem(cpu, gpu, info, now))
        self.assertIsNotNone(gui.gpu_apply_problem(cpu, gpu, info, now + 10))
        for temp in (80, math.nan, math.inf, -1):
            gpu["temp"] = temp
            self.assertIsNotNone(gui.gpu_apply_problem(cpu, gpu, info, now))


class GpuWidgetTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tkinter import ttk
        gui.tk, gui.ttk = tk, ttk
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(gui.CoolingApp, "open_hardware"), patch.object(gui.CoolingApp, "load_config"), \
                patch.object(gui.CoolingApp, "connect_cpu_backend"), \
                patch.object(gui.threading, "Thread"):
            self.app = gui.CoolingApp(self.root)
        self.app.gpu_backend = Mock()
        self.info = gui.decode_macm(FakeMemory().read())
        now = gui.time.monotonic()
        self.app.gpu_control = {"info": self.info, "updated_at": now}
        self.app.snapshot.update(cpu_temp=45, updated_at=now)
        self.app.gpu_data = {"temp": 55, "name": "GTX 1070", "updated_at": now}
        self.app.refresh_performance()

    def tearDown(self):
        self.root.update_idletasks()
        for callback in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(callback)
        self.root.destroy()

    def test_startup_and_slider_changes_do_not_apply(self):
        self.app.gpu_core_value.set(50)
        self.root.update_idletasks()
        self.app.gpu_backend.apply.assert_not_called()
        self.assertEqual(self.app.gpu_switch_value.get(), 0)
        self.assertFalse(self.app.gpu_owned)

    def test_cancel_does_not_start_write(self):
        self.app.gpu_core_value.set(25)
        with patch("tkinter.messagebox.askyesno", return_value=False), patch.object(gui.threading, "Thread") as worker:
            self.app.submit_gpu()
        worker.assert_not_called()
        self.assertFalse(self.app.gpu_owned)

    def test_conflict_during_apply_does_not_reset_external_settings(self):
        self.app.gpu_core_value.set(25)
        self.app.gpu_backend.apply.side_effect = gui.GpuSettingsChangedError("external change")
        with patch("tkinter.messagebox.askyesno", return_value=True), patch.object(gui.threading, "Thread") as worker:
            self.app.submit_gpu()
            worker.call_args.kwargs["target"]()
        self.app.gpu_control["info"] = dict(self.info, core=[80, -400, 1000, 0], is_default=False)
        with patch.object(gui.threading, "Thread") as reset_worker:
            self.app.refresh_performance()
        reset_worker.assert_not_called()
        self.assertFalse(self.app.gpu_owned)
        self.assertFalse(self.app.gpu_auto_enabled.get())

    def test_hot_or_stale_gpu_does_not_apply(self):
        for temp, stamp in ((85, gui.time.monotonic()), (50, 0)):
            self.app.gpu_data.update(temp=temp, updated_at=stamp)
            self.app.gpu_core_value.set(25)
            with patch("tkinter.messagebox.askyesno") as confirm, patch.object(gui.threading, "Thread") as worker:
                self.app.submit_gpu()
            confirm.assert_not_called()
            worker.assert_not_called()

    def test_reset_stays_unconfirmed_on_failure(self):
        self.app.gpu_owned = True
        self.app.gpu_busy = True
        self.app.gpu_results.put((True, None, "Disconnected"))
        self.app.refresh_performance()
        self.assertTrue(self.app.gpu_owned)
        self.assertTrue(self.app.gpu_reset_failed)
        self.assertEqual(self.app.gpu_switch_value.get(), -1)

    def test_temperature_loss_requests_reset_only_if_owned(self):
        self.app.gpu_data = {}
        with patch.object(self.app, "submit_gpu") as submit:
            self.app.refresh_performance()
            submit.assert_not_called()
            self.app.gpu_owned = True
            self.app.refresh_performance()
            submit.assert_called_once_with(reset=True, automatic=True)

    def test_confirmed_write_then_off_uses_backend_defaults(self):
        self.app.gpu_core_value.set(25)
        result = dict(self.info, core=[25, -400, 1000, 0], is_default=False)
        self.app.gpu_backend.apply.return_value = result
        with patch("tkinter.messagebox.askyesno", return_value=True), patch.object(gui.threading, "Thread") as worker:
            self.app.submit_gpu()
            worker.call_args.kwargs["target"]()
        self.app.refresh_performance()
        self.assertEqual(self.app.gpu_switch_value.get(), 1)
        self.app.gpu_backend.apply.assert_called_once_with(25, 0, reset=False, expected=[0, 0])
        self.app.gpu_backend.apply.return_value = self.info
        with patch.object(gui.threading, "Thread") as worker:
            self.app.submit_gpu(reset=True)
            worker.call_args.kwargs["target"]()
        self.app.refresh_performance()
        self.assertFalse(self.app.gpu_owned)
        self.assertEqual(self.app.gpu_switch_value.get(), 0)
        self.app.gpu_backend.apply.assert_called_with(None, None, reset=True, expected=None)


class AutoPolicyTests(unittest.TestCase):
    def test_apply_after_short_stable_cool(self):
        policy = gui.AutoGpuPolicy()
        for now in range(3):
            self.assertIsNone(policy.decide(now, 10, 60, False))
        self.assertEqual(policy.decide(3, 10, 60, False), "apply")

    def test_idle_never_releases(self):
        policy = gui.AutoGpuPolicy()
        for now in range(60):
            self.assertIsNone(policy.decide(now, 10, 60, True))

    def test_heat_and_missing_data_reset_immediately(self):
        for temp in (83, math.nan, None):
            self.assertEqual(gui.AutoGpuPolicy().decide(0, 90, temp, True), "reset")
        self.assertEqual(gui.AutoGpuPolicy().decide(0, None, 60, True), "reset")

    def test_cooldown_and_temperature_hysteresis(self):
        policy = gui.AutoGpuPolicy()
        self.assertEqual(policy.decide(0, 90, 83, True), "reset")
        for now in range(1, 60):
            self.assertIsNone(policy.decide(now, 90, 60, False))
        for now in range(60, 75):
            self.assertIsNone(policy.decide(now, 90, 76, False))
        for now in range(75, 78):
            self.assertIsNone(policy.decide(now, 90, 60, False))
        self.assertEqual(policy.decide(78, 90, 60, False), "apply")

    def test_polling_gap_does_not_count_as_sustained_load(self):
        policy = gui.AutoGpuPolicy()
        policy.decide(0, 90, 60, False)
        self.assertIsNone(policy.decide(30, 90, 60, False))

    def test_preset_validation(self):
        valid = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.assertTrue(gui.valid_auto_preset(valid))
        for patch_value in ({"core": 101}, {"memory": math.nan}, {"uuid": ""}, {"user_tested": False},
                            {"core": 0, "memory": 0}, {"core": True}):
            self.assertFalse(gui.valid_auto_preset(dict(valid, **patch_value)))
        self.assertFalse(gui.valid_auto_preset(None))


class AutoWidgetTests(GpuWidgetTests):
    def setUp(self):
        super().setUp()
        self.app.gpu_data.update(uuid="GPU-test", load=90)
        self.preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}

    def test_auto_requires_matching_tested_preset(self):
        for preset in (None, dict(self.preset, uuid="GPU-other")):
            self.app.gpu_auto_preset = preset
            self.app.gpu_auto_enabled.set(True)
            with patch("tkinter.messagebox.askyesno") as confirm:
                self.app.on_gpu_auto_toggle()
            self.assertFalse(self.app.gpu_auto_enabled.get())
            confirm.assert_not_called()

    def test_enabling_auto_requires_confirmation_and_does_not_apply_yet(self):
        self.app.gpu_auto_preset = self.preset
        for accepted in (False, True):
            self.app.gpu_auto_enabled.set(True)
            with patch("tkinter.messagebox.askyesno", return_value=accepted):
                self.app.on_gpu_auto_toggle()
            self.assertEqual(self.app.gpu_auto_enabled.get(), accepted)
        self.app.gpu_backend.apply.assert_not_called()

    def test_auto_uses_saved_values_not_moved_sliders(self):
        self.app.gpu_auto_preset = self.preset
        self.app.gpu_auto_enabled.set(True)
        self.app.gpu_core_value.set(99)
        self.app.gpu_memory_value.set(249)
        with patch("tkinter.messagebox.askyesno") as confirm, patch.object(gui.threading, "Thread") as worker:
            for now in range(11):
                self.app.gpu_data["updated_at"] = now
                self.app.gpu_control["updated_at"] = now
                self.app.snapshot["updated_at"] = now
                with patch.object(gui.time, "monotonic", return_value=now):
                    self.app.refresh_performance()
            worker.assert_called_once()
            self.app.gpu_backend.apply.return_value = dict(self.info, core=[25, -400, 1000, 0],
                                                         memory=[50, -400, 1000, 0], is_default=False)
            worker.call_args.kwargs["target"]()
        confirm.assert_not_called()
        self.app.gpu_backend.apply.assert_called_once_with(25, 50, reset=False, expected=[0, 0])

    def test_disable_while_apply_pending_resets_after_completion(self):
        self.app.gpu_owned = self.app.gpu_busy = True
        self.app.gpu_auto_enabled.set(False)
        self.app.on_gpu_auto_toggle()
        self.assertTrue(self.app.gpu_stop_after_busy)
        result = dict(self.info, core=[25, -400, 1000, 0], is_default=False)
        self.app.gpu_results.put((False, result, None))
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
            worker.assert_called_once()
            self.app.gpu_backend.apply.return_value = self.info
            worker.call_args.kwargs["target"]()
        self.app.refresh_performance()
        self.assertFalse(self.app.gpu_owned)
        self.assertFalse(self.app.gpu_auto_enabled.get())
        self.assertFalse(self.app.gpu_stop_after_busy)

    def test_external_offset_changes_disarm_auto_without_writing(self):
        self.app.gpu_auto_preset = self.preset
        self.app.gpu_auto_enabled.set(True)
        self.app.gpu_control["info"] = dict(self.info, core=[80, -400, 1000, 0], is_default=False)
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        worker.assert_not_called()

    def test_lost_sensor_disarms_and_requests_reset(self):
        self.app.gpu_auto_preset = self.preset
        self.app.gpu_auto_enabled.set(True)
        self.app.gpu_owned = True
        self.app.gpu_data = {}
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        self.assertFalse(self.app.gpu_auto_enabled.get())
        worker.assert_called_once()

    def test_different_gpu_is_not_modified(self):
        self.app.gpu_auto_preset = self.preset
        self.app.gpu_auto_enabled.set(True)
        self.app.gpu_owned = True
        self.app.gpu_data["uuid"] = "GPU-different"
        with patch.object(gui.threading, "Thread") as worker:
            self.app.refresh_performance()
        worker.assert_not_called()
        self.assertFalse(self.app.gpu_auto_enabled.get())

    def test_save_requires_matching_active_offsets_and_user_attestation(self):
        self.app.gpu_core_value.set(25)
        self.app.gpu_memory_value.set(50)
        with patch("tkinter.messagebox.askyesno") as confirm:
            self.app.save_gpu_preset()
        confirm.assert_not_called()
        self.assertIsNone(self.app.gpu_auto_preset)
        self.app.gpu_control["info"] = dict(self.info, core=[25, -400, 1000, 0],
                                            memory=[50, -400, 1000, 0], is_default=False)
        with patch("tkinter.messagebox.askyesno", return_value=True), patch.object(self.app, "save_config"):
            self.app.save_gpu_preset()
        self.assertEqual(self.app.gpu_auto_preset, self.preset)
        self.app.gpu_backend.apply.assert_not_called()

    def test_loading_preset_never_enables_auto(self):
        config = gui.json.dumps({"gpu_auto_preset": self.preset, "gpu_auto_enabled": True})
        with patch("builtins.open", mock_open(read_data=config)):
            self.app.load_config()
        self.assertEqual(self.app.gpu_auto_preset, self.preset)
        self.assertFalse(self.app.gpu_auto_enabled.get())
        self.app.gpu_auto_enabled.set(True)
        opened = mock_open()
        with patch("builtins.open", opened):
            self.app.save_config()
        saved = gui.json.loads("".join(c.args[0] for c in opened().write.call_args_list))
        self.assertNotIn("gpu_auto_enabled", saved)


class ComponentApplicationTests(unittest.TestCase):
    def setUp(self):
        GpuWidgetTests.setUp(self)
        self.app.gpu_data.update(uuid="GPU-test", load=90)
        self.app.gpu_auto_preset = {"core": 25, "memory": 50, "uuid": "GPU-test", "user_tested": True}
        self.app.component_session = True
        self.app.gpu_auto_enabled.set(True)

    def tearDown(self):
        GpuWidgetTests.tearDown(self)

    def test_core_off_reduces_only_core_even_above_apply_temperature(self):
        self.app.gpu_owned = True
        self.app.gpu_managed_offsets = (25, 50)
        self.app.gpu_control["info"] = dict(self.info, core=[25, -400, 1000, 0],
                                           memory=[50, -400, 1000, 0], is_default=False)
        self.app.gpu_data["temp"] = 81
        self.app.gpu_components["memory"]["enabled"].set(True)
        self.app.gpu_components["memory"]["auto"].set(False)
        with patch.object(gui.threading, "Thread") as worker:
            self.app.tick_components(gui.time.monotonic(), True)
            worker.assert_called_once()
            self.app.gpu_backend.apply.return_value = dict(self.info, memory=[50, -400, 1000, 0], is_default=False)
            worker.call_args.kwargs["target"]()
        self.app.gpu_backend.apply.assert_called_once_with(0, 50, reset=False, expected=[25, 50])

    def test_auto_core_does_not_enable_memory(self):
        self.app.gpu_components["core"]["enabled"].set(True)
        with patch.object(gui.threading, "Thread") as worker:
            for now in range(11):
                self.app.gpu_data["updated_at"] = now
                self.app.gpu_control["updated_at"] = now
                self.app.snapshot["updated_at"] = now
                with patch.object(gui.time, "monotonic", return_value=now):
                    self.app.tick_components(now, True)
            worker.assert_called_once()
            self.app.gpu_backend.apply.return_value = dict(self.info, core=[25, -400, 1000, 0], is_default=False)
            worker.call_args.kwargs["target"]()
        self.app.gpu_backend.apply.assert_called_once_with(25, 0, reset=False, expected=[0, 0])
        self.assertFalse(self.app.gpu_components["memory"]["enabled"].get())


class RestartTests(unittest.TestCase):
    def test_no_old_window_requires_no_prompt(self):
        user = Mock()
        user.FindWindowW.return_value = 0
        gui.close_previous_controller(user)
        user.MessageBoxW.assert_not_called()
        user.PostMessageW.assert_not_called()

    def test_declining_does_not_close_existing_controller(self):
        user = Mock()
        user.FindWindowW.return_value = 123
        user.MessageBoxW.return_value = 7
        with self.assertRaisesRegex(RuntimeError, "canceled"):
            gui.close_previous_controller(user)
        user.PostMessageW.assert_not_called()

    def test_accepting_requests_normal_close_only(self):
        user = Mock()
        user.FindWindowW.return_value = 123
        user.MessageBoxW.return_value = 6
        user.IsWindow.side_effect = [True, False]
        with patch.object(gui.time, "sleep"):
            gui.close_previous_controller(user)
        user.PostMessageW.assert_called_once_with(123, 0x0010, 0, 0)

    def test_hung_window_prevents_takeover(self):
        user = Mock()
        user.FindWindowW.return_value = 123
        user.MessageBoxW.return_value = 6
        user.IsWindow.return_value = True
        with patch.object(gui.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(RuntimeError, "No second controller"):
                gui.close_previous_controller(user)


if __name__ == "__main__":
    unittest.main()
