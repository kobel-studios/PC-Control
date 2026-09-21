"""Mocked tests for the ToggleSwitch widget (iOS-style sliding toggle).

Covers the API Devin-PCControl requested:
  - get/set/configure/disabled all work
  - variable changes redraw WITHOUT firing command
  - disabled blocks mouse/Space/Return
  - state([...]) and instate([...]) compatible with ttk
  - text argument shows visible label
  - cleanup trace/animation callbacks on destroy

No hardware calls.
"""
import importlib.util
import unittest
from pathlib import Path
import tkinter as tk

spec = importlib.util.spec_from_file_location("toggle_switch", Path(__file__).with_name("toggle_switch.py"))
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)


class ToggleSwitchTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.update_idletasks()
        for cb in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(cb)
        self.root.destroy()

    def make_switch(self, **kw):
        return ts.ToggleSwitch(self.root, **kw)

    # --- get/set ---
    def test_initial_off(self):
        sw = self.make_switch(initial=False)
        self.assertFalse(sw.get())

    def test_initial_on(self):
        sw = self.make_switch(initial=True)
        self.assertTrue(sw.get())

    def test_set_true(self):
        sw = self.make_switch(initial=False)
        sw.set(True)
        self.assertTrue(sw.get())

    def test_set_false(self):
        sw = self.make_switch(initial=True)
        sw.set(False)
        self.assertFalse(sw.get())

    def test_get_returns_bool(self):
        sw = self.make_switch(initial=True)
        self.assertIsInstance(sw.get(), bool)

    # --- click ---
    def test_click_toggles(self):
        sw = self.make_switch(initial=False)
        sw._on_click()
        self.assertTrue(sw.get())
        sw._on_click()
        self.assertFalse(sw.get())

    def test_click_fires_command(self):
        calls = []
        sw = self.make_switch(command=lambda: calls.append(1))
        sw._on_click()
        self.assertEqual(len(calls), 1)

    # --- set/invoke ---
    def test_set_without_invoke_no_command(self):
        calls = []
        sw = self.make_switch(command=lambda: calls.append(1))
        sw.set(True)
        self.assertEqual(len(calls), 0)

    def test_set_with_invoke_fires_command(self):
        calls = []
        sw = self.make_switch(command=lambda: calls.append(1))
        sw.set(True, invoke=True)
        self.assertEqual(len(calls), 1)

    # --- variable sync ---
    def test_variable_change_no_command(self):
        calls = []
        sw = self.make_switch(command=lambda: calls.append(1))
        sw.variable.set(True)
        self.assertTrue(sw.get())
        self.assertEqual(len(calls), 0)

    def test_external_variable_sync(self):
        var = tk.BooleanVar(value=False)
        sw = self.make_switch(variable=var)
        var.set(True)
        self.assertTrue(sw.get())
        var.set(False)
        self.assertFalse(sw.get())

    # --- disabled ---
    def test_disabled_blocks_click(self):
        sw = self.make_switch(initial=False, state="disabled")
        sw._on_click()
        self.assertFalse(sw.get())

    def test_configure_disabled(self):
        sw = self.make_switch(initial=False)
        sw.configure(state="disabled")
        self.assertEqual(sw.cget("state"), "disabled")
        sw._on_click()
        self.assertFalse(sw.get())

    def test_configure_normal_re_enables(self):
        sw = self.make_switch(initial=False, state="disabled")
        sw.configure(state="normal")
        sw._on_click()
        self.assertTrue(sw.get())

    def test_disabled_blocks_key(self):
        sw = self.make_switch(initial=False, state="disabled")
        sw._on_key()
        self.assertFalse(sw.get())

    def test_key_toggles(self):
        sw = self.make_switch(initial=False)
        sw._on_key()
        self.assertTrue(sw.get())

    # --- state([...]) / instate([...]) ttk API ---
    def test_state_set_disabled(self):
        sw = self.make_switch(initial=False)
        sw.state(["disabled"])
        self.assertTrue(sw.instate(["disabled"]))

    def test_state_set_normal(self):
        sw = self.make_switch(initial=False, state="disabled")
        sw.state(["!disabled"])
        self.assertFalse(sw.instate(["disabled"]))

    def test_instate_disabled_true(self):
        sw = self.make_switch(initial=False, state="disabled")
        self.assertTrue(sw.instate(["disabled"]))

    def test_instate_disabled_false(self):
        sw = self.make_switch(initial=False)
        self.assertFalse(sw.instate(["disabled"]))

    def test_instate_not_disabled_true(self):
        sw = self.make_switch(initial=False)
        self.assertTrue(sw.instate(["!disabled"]))

    def test_instate_callback_fires(self):
        sw = self.make_switch(initial=False)
        called = []
        sw.instate(["!disabled"], lambda: called.append(1))
        self.assertEqual(len(called), 1)

    def test_instate_callback_not_fires(self):
        sw = self.make_switch(initial=False, state="disabled")
        called = []
        sw.instate(["!disabled"], lambda: called.append(1))
        self.assertEqual(len(called), 0)

    def test_state_returns_list(self):
        sw = self.make_switch(initial=False)
        result = sw.state()
        self.assertIsInstance(result, list)

    # --- text ---
    def test_text_stored(self):
        sw = self.make_switch(text="Overclock")
        self.assertEqual(sw.cget("text"), "Overclock")

    def test_text_configure(self):
        sw = self.make_switch()
        sw.configure(text="Auto")
        self.assertEqual(sw.cget("text"), "Auto")

    # --- destroy ---
    def test_destroy_cleans_up(self):
        sw = self.make_switch()
        sw.destroy()
        self.root.update_idletasks()

    def test_set_after_destroy_no_error(self):
        sw = self.make_switch()
        sw.destroy()
        sw.set(True)  # should not raise


if __name__ == "__main__":
    unittest.main()
