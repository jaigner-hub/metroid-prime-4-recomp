"""MP4: tools/build_game.py run_ninja, Ninja run again under Wine when a compiler failed to start (issue #11)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import build_game  # noqa: E402


class RunNinjaTest(unittest.TestCase):
    def setUp(self):
        self.real = build_game.run_measured, build_game.wine_crt.under_wine
        self.calls = 0

    def tearDown(self):
        build_game.run_measured, build_game.wine_crt.under_wine = self.real

    def fake(self, codes, wine):
        def run_measured(cmd, env):
            code = codes[self.calls]
            self.calls += 1
            return code, 1.5, 100 * self.calls, 200 * self.calls
        build_game.run_measured = run_measured
        build_game.wine_crt.under_wine = lambda: wine

    def test_a_failure_under_wine_runs_again(self):
        self.fake([1, 0], wine=True)
        self.assertEqual(build_game.run_ninja(['ninja'], {}), (0, 3.0, 200, 400))
        self.assertEqual(self.calls, 2)

    def test_under_wine_it_gives_up_after_the_retries(self):
        self.fake([1] * 10, wine=True)
        self.assertEqual(build_game.run_ninja(['ninja'], {})[0], 1)
        self.assertEqual(self.calls, build_game.WINE_RETRIES + 1)

    def test_off_wine_a_failure_is_final(self):
        self.fake([1, 0], wine=False)
        self.assertEqual(build_game.run_ninja(['ninja'], {})[0], 1)
        self.assertEqual(self.calls, 1)


if __name__ == '__main__':
    unittest.main()
