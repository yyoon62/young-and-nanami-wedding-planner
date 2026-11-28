"""L29: Projection folded into Accounts. Fictional DBs only; the clock is pinned; no network."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import urlencode

import l29_golden
from l29_fixture import fetch, running, seed

GOLDEN = json.loads((Path(__file__).with_name("l29_golden.json")).read_text())


def projection_url(view, currency):
    """Where the Projection panel is served. Baseline 043c5a9: its own page."""
    return "/projections?" + urlencode({"view": view, "currency": currency})


class Base(unittest.TestCase):
    backdated = False

    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="fictional-l29-", dir=Path(tempfile.gettempdir()).resolve()))
        self.path = Path(tmp) / "accounts.sqlite3"
        self.ids = seed(self.path, backdated=self.backdated)
        self.srv = self.enterContext(running(self.path))

    def get(self, target, **kw):
        return fetch(self.srv, target, **kw)

    def post(self, target, data, **kw):
        return fetch(self.srv, target, data, **kw)

    def rows(self, sql, *args):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql, args).fetchall()


class Golden(Base):
    def test_example_9_numbers_equal_the_baseline_projections_page(self):
        """Pinned at 043c5a9 from GET /projections on this fixture: numbers, not layout."""
        got = l29_golden.collect(self.srv, fetch, projection_url)
        self.assertEqual(sorted(got), sorted(GOLDEN))
        for case in GOLDEN:
            for region in GOLDEN[case]:
                with self.subTest(case=case, region=region):
                    self.assertEqual(json.loads(json.dumps(got[case][region])), GOLDEN[case][region])

    def test_golden_is_not_vacuous(self):
        household = GOLDEN["Household-USD"]
        for region in ("cards", "annual", "budget", "notes", "suggest", "debt", "asset", "form"):
            self.assertTrue(household[region], region)
        self.assertIn(["Net worth", "Start: USD 980.00", "Year 10: USD 262,935.95"], household["cards"])


if __name__ == "__main__":
    unittest.main()
