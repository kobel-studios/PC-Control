"""FPS cleanup tests: classification, confirmation flow, protected lists."""
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import cooling_gui as gui
from test_game_boost import make_app, teardown


class FpsCleanupTests(unittest.TestCase):
    def setUp(self):
        self.app, self.root = make_app()
        self.app.fps_cleanup_value.set(True)
        self.app.boosting_games = {"helldivers2.exe"}
        self.app.game_exes_var.set("helldivers2.exe")
        self.app._cleanup_close = Mock()
        self.app._cleanup_next_ask = Mock()

    def tearDown(self):
        teardown(self.app, self.root)

    def scan(self, now=100.0):
        self.app.tick_cleanup(now)

    def test_safe_process_closed_without_asking(self):
        self.app.cleanup_candidates = [("spotify", 111, 60.0)]
        self.scan()
        self.app._cleanup_close.assert_called_once_with(111, "spotify")
        self.assertEqual(self.app.cleanup_queue, [])  # no confirmation needed

    def test_uncertain_process_asks_once(self):
        self.app.cleanup_candidates = [("someapp", 222, 40.0)]
        self.scan()
        self.app._cleanup_close.assert_not_called()
        self.assertEqual(self.app.cleanup_queue, [("someapp", 222, 40.0)])
        self.app.cleanup_queue.clear()  # simulate the dialog consuming it
        self.scan(101.0)
        self.assertEqual(self.app.cleanup_queue, [])  # asked once, not re-queued

    def test_protected_process_never_touched(self):
        self.app.cleanup_candidates = [("explorer", 333, 80.0), ("svchost", 334, 50.0),
                                       ("cooling_gui", 335, 90.0), ("code_verifier", 336, 60.0),
                                       ("vpnsvc", 337, 70.0), ("aswidsagent", 338, 70.0)]
        self.scan()
        self.app._cleanup_close.assert_not_called()
        self.assertEqual(self.app.cleanup_queue, [])

    def test_active_game_never_closed(self):
        self.app.cleanup_candidates = [("helldivers2", 444, 300.0)]
        self.scan()
        self.app._cleanup_close.assert_not_called()
        self.assertEqual(self.app.cleanup_queue, [])

    def test_own_pid_never_closed(self):
        import os
        self.app.cleanup_candidates = [("strangething", os.getpid(), 90.0)]
        self.scan()
        self.app._cleanup_close.assert_not_called()
        self.assertEqual(self.app.cleanup_queue, [])

    def test_low_cpu_ignored(self):
        self.app.cleanup_candidates = [("spotify", 555, 5.0)]
        self.scan()
        self.app._cleanup_close.assert_not_called()

    def test_nothing_happens_when_disabled(self):
        self.app.fps_cleanup_value.set(False)
        self.app.cleanup_candidates = [("spotify", 111, 60.0)]
        self.scan()
        self.app._cleanup_close.assert_not_called()
        self.assertFalse(self.app.fps_cleanup_active)

    def test_nothing_happens_without_game(self):
        self.app.boosting_games = set()
        self.app.cleanup_candidates = [("spotify", 111, 60.0)]
        self.scan()
        self.app._cleanup_close.assert_not_called()

    def test_always_close_remembered(self):
        self.app.cleanup_always.add("someapp")
        self.app.cleanup_candidates = [("someapp", 222, 40.0)]
        self.scan()
        self.app._cleanup_close.assert_called_once_with(222, "someapp")

    def test_never_ask_remembered(self):
        self.app.cleanup_never.add("someapp")
        self.app.cleanup_candidates = [("someapp", 222, 40.0)]
        self.scan()
        self.app._cleanup_close.assert_not_called()
        self.assertEqual(self.app.cleanup_queue, [])

    def test_choose_close_once(self):
        self.app._cleanup_choose("someapp", 222, "close")
        self.app._cleanup_close.assert_called_once_with(222, "someapp")
        self.assertNotIn("someapp", self.app.cleanup_always)

    def test_choose_always(self):
        with patch.object(self.app, "save_config"):
            self.app._cleanup_choose("someapp", 222, "always")
        self.app._cleanup_close.assert_called_once_with(222, "someapp")
        self.assertIn("someapp", self.app.cleanup_always)

    def test_choose_never(self):
        with patch.object(self.app, "save_config"):
            self.app._cleanup_choose("someapp", 222, "never")
        self.app._cleanup_close.assert_not_called()
        self.assertIn("someapp", self.app.cleanup_never)

    def test_choose_skip(self):
        self.app._cleanup_choose("someapp", 222, "skip")
        self.app._cleanup_close.assert_not_called()
        self.assertNotIn("someapp", self.app.cleanup_never)

    def test_asks_rearm_between_sessions(self):
        self.app.cleanup_candidates = [("someapp", 222, 40.0)]
        self.scan()
        self.assertIn("someapp", self.app.cleanup_asked)
        self.app.boosting_games = set()
        self.scan(101.0)  # game ended
        self.assertNotIn("someapp", self.app.cleanup_asked)

    def test_reclose_after_respawn_window(self):
        self.app.cleanup_closed["spotify"] = 0.0
        self.app.cleanup_candidates = [("spotify", 111, 60.0)]
        self.scan(now=60.0)
        self.app._cleanup_close.assert_not_called()  # within reclose window
        self.scan(now=gui.CLEANUP_RECLOSE_AFTER + 10)
        self.app._cleanup_close.assert_called_once_with(111, "spotify")

    def test_reset_choices_clears_memory(self):
        self.app.cleanup_always.add("a")
        self.app.cleanup_never.add("b")
        self.app.cleanup_asked.add("c")
        with patch.object(self.app, "save_config"):
            self.app.reset_cleanup_choices()
        self.assertEqual(self.app.cleanup_always, set())
        self.assertEqual(self.app.cleanup_never, set())
        self.assertEqual(self.app.cleanup_asked, set())


if __name__ == "__main__":
    unittest.main()
