"""Game boost tests: mocked process list and booster, no real priority changes."""
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import cooling_gui as gui


def make_app():
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
    app.game_booster = Mock()
    app.game_booster.timer_raised = False
    app.game_booster.denied = set()
    return app, root


def teardown(app, root):
    for callback in root.tk.splitlist(root.tk.call("after", "info")):
        root.after_cancel(callback)
    root.destroy()


class GameBoostTests(unittest.TestCase):
    def setUp(self):
        self.app, self.root = make_app()
        self.app.running_procs = {}
        self.app.game_exes_var.set("helldivers2.exe")

    def tearDown(self):
        teardown(self.app, self.root)

    def test_boosts_listed_game(self):
        self.app.game_boost_value.set(True)
        self.app.running_procs = {"helldivers2.exe": [1234]}
        with patch.object(self.app, "_apply_power_scheme") as scheme:
            self.app.tick_game(0)
        self.app.game_booster.apply.assert_called_once_with([1234])
        scheme.assert_called_once()
        self.assertIn("helldivers2.exe", self.app.boosting_games)

    def test_no_boost_when_disabled(self):
        self.app.running_procs = {"helldivers2.exe": [1234]}
        self.app.tick_game(0)
        self.app.game_booster.apply.assert_not_called()

    def test_no_match_keeps_watching(self):
        self.app.game_boost_value.set(True)
        self.app.running_procs = {"notepad.exe": [55]}
        self.app.tick_game(0)
        self.app.game_booster.apply.assert_not_called()
        self.assertEqual(self.app.boosting_games, set())

    def test_releases_when_game_exits(self):
        self.app.game_boost_value.set(True)
        self.app.running_procs = {"helldivers2.exe": [1234]}
        with patch.object(self.app, "_apply_power_scheme"):
            self.app.tick_game(0)
        self.app.running_procs = {}
        self.app.game_booster.timer_raised = True
        with patch.object(self.app, "_restore_power_scheme") as restore:
            self.app.tick_game(1)
        self.app.game_booster.release.assert_called_once()
        restore.assert_called_once()

    def test_toggle_off_releases(self):
        self.app.game_boost_value.set(True)
        self.app.tick_game(0)
        self.app.game_boost_value.set(False)
        self.app.on_game_boost_toggle()
        self.app.game_booster.release.assert_called()

    def test_denied_processes_reported(self):
        self.app.game_boost_value.set(True)
        self.app.running_procs = {"helldivers2.exe": [1234]}
        self.app.game_booster.denied = {1234}
        with patch.object(self.app, "_apply_power_scheme"):
            self.app.tick_game(0)
        self.assertIn("anti-cheat", self.app.game_boost_status.cget("text"))

    def test_booster_apply_raises_timer_and_priority(self):
        booster = gui.GameBooster()
        booster.k32 = Mock()
        booster.ntdll = Mock()
        booster.k32.OpenProcess.return_value = 99
        booster.apply([1234])
        booster.ntdll.NtSetTimerResolution.assert_called_once()
        booster.k32.SetPriorityClass.assert_called_once_with(99, gui.HIGH_PRIORITY_CLASS)
        booster.k32.CloseHandle.assert_called_with(99)

    def test_booster_denied_when_open_fails(self):
        booster = gui.GameBooster()
        booster.k32 = Mock()
        booster.ntdll = Mock()
        booster.k32.OpenProcess.return_value = 0
        booster.apply([1234])
        self.assertIn(1234, booster.denied)
        booster.k32.SetPriorityClass.assert_not_called()

    def test_booster_apply_purges_standby(self):
        booster = gui.GameBooster()
        booster.k32 = Mock()
        booster.ntdll = Mock()
        booster.k32.OpenProcess.return_value = 99
        booster.apply([1234])
        booster.ntdll.NtSetSystemInformation.assert_called_once()

    def test_ultimate_scheme_and_unparking(self):
        self.app.game_boost_value.set(True)
        self.app.running_procs = {"helldivers2.exe": [1234]}
        prev_scheme = "381b4222-f694-41f0-9685-ff5bb260df2e"

        def fake_run(cmd, **kw):
            out = Mock(stdout="")
            if cmd[1] == "/getactivescheme":
                out.stdout = f"Power Scheme GUID: {prev_scheme}  (Balanced)"
            elif cmd[1] == "/list":
                out.stdout = f"Power Scheme GUID: {gui.ULTIMATE_SCHEME}  (Ultimate Performance)"
            elif cmd[1] == "/query":
                out.stdout = "Current AC Power Setting Index: 0x0000000a"
            return out

        with patch.object(gui.subprocess, "run", side_effect=fake_run) as run:
            self.app.tick_game(0)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertIn(["powercfg", "/setactive", gui.ULTIMATE_SCHEME], calls)
        self.assertIn(["powercfg", "/setacvalueindex", "SCHEME_CURRENT",
                       gui.SUB_PROCESSOR, gui.CPMINCORES, "100"], calls)

        self.app.running_procs = {}
        with patch.object(gui.subprocess, "run", side_effect=fake_run) as run2:
            self.app.tick_game(1)
        calls2 = [c.args[0] for c in run2.call_args_list]
        self.assertIn(["powercfg", "/setacvalueindex", "SCHEME_CURRENT",
                       gui.SUB_PROCESSOR, gui.CPMINCORES, "10"], calls2)
        self.assertIn(["powercfg", "/setactive", prev_scheme], calls2)

    def test_game_dvr_disabled_and_restored(self):
        store = {}
        fake_key = Mock()
        fake_key.__enter__ = lambda s: s
        fake_key.__exit__ = lambda s, *a: False

        def query(key, name):
            if name not in store:
                raise OSError()
            return (store[name], 4)

        with patch("winreg.OpenKey", return_value=fake_key), \
                patch("winreg.QueryValueEx", side_effect=query), \
                patch("winreg.SetValueEx",
                      side_effect=lambda k, n, t, ty, v: store.__setitem__(n, v)), \
                patch("winreg.DeleteValue",
                      side_effect=lambda k, n: store.pop(n, None)), \
                patch("winreg.CreateKey", return_value=fake_key), \
                patch("winreg.CloseKey"):
            self.app._game_dvr(True)
            self.assertEqual(store.get("GameDVR_Enabled"), 0)
            self.assertEqual(store.get("AllowGameDVR"), 0)
            self.app._game_dvr(False)
            self.assertNotIn("GameDVR_Enabled", store)  # absent before -> deleted
            self.assertIsNone(self.app.dvr_saved)

    def test_dethrottle_called_for_boosted_pid(self):
        booster = gui.GameBooster()
        booster.k32 = Mock()
        booster.ntdll = Mock()
        booster.k32.OpenProcess.return_value = 99
        booster.apply([1234])
        booster.k32.SetProcessInformation.assert_called_once()
        booster.ntdll.NtSetInformationProcess.assert_called_once()

    def test_services_paused_and_resumed(self):
        self.app.game_boost_value.set(True)
        self.app.running_procs = {"helldivers2.exe": [1234]}
        self.app._apply_power_scheme = Mock()

        def fake_run(cmd, **kw):
            out = Mock(stdout="")
            if cmd[0] == "sc" and len(cmd) == 3:
                out.stdout = "        STATE              : 4  RUNNING\n"
            elif cmd[0] == "sc":  # full service enum
                out.stdout = ("SERVICE_NAME: BcastDVRUserService_abc\n"
                              "        STATE              : 4  RUNNING\n\n"
                              "SERVICE_NAME: someothersvc\n"
                              "        STATE              : 1  STOPPED\n")
            return out

        with patch.object(gui.subprocess, "run", side_effect=fake_run) as run:
            self.app.tick_game(0)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertIn(["net", "stop", "wuauserv"], calls)
        self.assertIn(["net", "stop", "BcastDVRUserService_abc"], calls)
        self.assertNotIn(["net", "stop", "someothersvc"], calls)

        self.app.running_procs = {}
        with patch.object(gui.subprocess, "run", side_effect=fake_run) as run2:
            self.app.tick_game(1)
        calls2 = [c.args[0] for c in run2.call_args_list]
        self.assertIn(["net", "start", "wuauserv"], calls2)
        self.assertIn(["net", "start", "BcastDVRUserService_abc"], calls2)

    def test_booster_release_restores(self):
        booster = gui.GameBooster()
        booster.k32 = Mock()
        booster.ntdll = Mock()
        booster.k32.OpenProcess.return_value = 99
        booster.apply([1234])
        booster.release()
        booster.k32.SetPriorityClass.assert_called_with(99, gui.NORMAL_PRIORITY_CLASS)
        self.assertFalse(booster.timer_raised)


class GpuFanTests(unittest.TestCase):
    def setUp(self):
        self.app, self.root = make_app()

    def tearDown(self):
        teardown(self.app, self.root)

    def use_nvapi(self):
        backend = Mock(spec=gui.NvApiControl)
        backend.gpu = 1
        backend.power_raised = False
        self.app.nvapi = backend
        return backend

    def test_fan_full_when_hot(self):
        backend = self.use_nvapi()
        self.app.gpu_data = {"temp": 80}
        self.app.tick_gpu_fan()
        backend.set_fan.assert_called_once_with(100)
        self.assertTrue(self.app.gpu_fan_manual)

    def test_fan_auto_when_cool_again(self):
        backend = self.use_nvapi()
        self.app.gpu_data = {"temp": 80}
        self.app.tick_gpu_fan()
        self.app.gpu_data = {"temp": 65}
        self.app.tick_gpu_fan()
        backend.set_fan.assert_called_with(None)
        self.assertFalse(self.app.gpu_fan_manual)

    def test_fan_hysteresis(self):
        backend = self.use_nvapi()
        self.app.gpu_data = {"temp": 80}
        self.app.tick_gpu_fan()
        self.app.gpu_data = {"temp": 73}
        self.app.tick_gpu_fan()
        self.assertEqual(backend.set_fan.call_count, 1)
        self.assertTrue(self.app.gpu_fan_manual)

    def test_fan_ignored_on_afterburner(self):
        self.app.nvapi = None
        self.app.gpu_data = {"temp": 80}
        self.app.tick_gpu_fan()
        self.assertFalse(self.app.gpu_fan_manual)

    def test_power_raised_with_offsets(self):
        backend = self.use_nvapi()
        self.app.gpu_managed_offsets = (55.0, 106.0)
        self.app.gpu_data = {"temp": 50}
        self.app.tick_gpu_fan()
        backend.set_power.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
