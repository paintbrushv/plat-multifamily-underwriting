import unittest

from engine.modules.time_grid import TimeGrid


class TestTimeGrid(unittest.TestCase):
    def test_build_accepts_month_or_date(self):
        g1 = TimeGrid.build("2026-01", "2026-03")
        self.assertEqual(g1.month_ids, ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(g1.year_ids, ["2026"])

        g2 = TimeGrid.build("2026-01-01", "2026-03-01")
        self.assertEqual(g2.month_ids, ["2026-01", "2026-02", "2026-03"])


if __name__ == "__main__":
    unittest.main()

