"""L06 contract tests. Shared route/navigation wiring is tested at integration."""
from pathlib import Path
import re
import tempfile
import unittest

from server import form, page
from servicers import servicer_get
from test_dashboard import running


def shortcut(target, token="example-token"):
    return servicer_get(target, page=page, form=form, token=token)


class StatementShortcuts(unittest.TestCase):
    def test_chooser_explains_shortcuts_and_record_ambiguity(self):
        _, guide = shortcut("/servicers")
        before_choices, choices = guide.split('<a href="/new?servicer=', 1)
        for message in ("local form, not a connection", "no servicer sign-in or automatic balance refresh",
                        "cannot upload or import a servicer statement"):
            self.assertIn(message, before_choices)
        self.assertIn('servicer=uas">UAS</a> — University Accounting Service', choices)
        for message in ("by file order, not by servicer",
                        "can't identify your loan in the file, use the dated statement",
                        "Don't guess which record is yours",
                        "or combine principal and interest with different as-of dates"):
            self.assertIn(message, choices)

    def test_guide_to_manual_forms_and_conditional_preview(self):
        status, guide = shortcut("/servicers")
        self.assertEqual(status, 200)
        self.assertIn('href="/">Find the loan', guide)
        self.assertIn("confirmed this specific loan", guide)
        self.assertIn('href="/federal-preview"', guide)
        self.assertNotIn('href="http', guide)
        links = re.findall(r'href="(/new[^"]+)">([^<]+)', guide)
        self.assertEqual([label for _, label in links], ["Aidvantage", "UAS", "Heartland ECSI"])
        for link, name in links:
            with self.subTest(provider=name):
                status, view = shortcut(link)
                self.assertEqual(status, 200)
                for expected in (f'name="institution" value="{name}"', 'value="Student loan"',
                                 'value="debt" selected', 'name="balance" value=""',
                                 'name="asof" value=""', 'name="nickname" value=""',
                                 'method="post" action="/new"', '>Save account<'):
                    self.assertIn(expected, view)

    def test_only_canonical_new_presets(self):
        for query in ("", "servicer", "servicer=", "servicer=other", "servicer=%",
                      "servicer=uas&servicer=", "servicer=uas&servicer=uas",
                      "servicer=uas&balance=500", "servicer=%3Cscript%3E", "side=asset"):
            with self.subTest(query=query):
                status, view = shortcut("/new?" + query)
                self.assertEqual(status, 400)
                self.assertNotIn("<form", view)
                self.assertEqual(view, shortcut("/new?invalid")[1])
        for target in ("/new", "/", "/federal-preview", "/edit/1?servicer=uas",
                       "/remove/1?servicer=uas"):
            self.assertIsNone(shortcut(target))

    def test_explicit_create_then_edit_one_of_two_same_servicer_loans(self):
        with tempfile.TemporaryDirectory() as tmp, running(Path(tmp) / "fictional.db") as request:
            original = request("/")[1]
            token = re.search(r'name="token" value="([^"]+)"', request("/new")[1])[1]
            status, view = shortcut("/new?servicer=uas", token)
            self.assertEqual(status, 200)
            values = dict(re.findall(r'name="(\w+)" value="([^"]*)"', view))
            values["side"] = re.search(r'<option value="(\w+)" selected>', view)[1]
            action = re.search(r'<form method="post" action="([^"]+)"', view)[1]
            self.assertEqual(request("/")[1], original)
            first = dict(values, balance="10.05", nickname="Larch", currency="USD")
            self.assertEqual(request(action, first)[0], 422)  # missing source date
            self.assertEqual(request("/")[1], original)
            first["asof"] = "2024-02-29"
            self.assertEqual(request(action, first)[0], 303)
            second = dict(first, nickname="Beech", balance="700", currency="JPY")
            self.assertEqual(request(action, second)[0], 303)
            before = request("/")[1]
            self.assertEqual(request("/federal-preview")[0], 200)
            self.assertIn('href="/edit/1"', before)
            self.assertEqual(request("/edit/1?servicer=aidvantage"), request("/edit/1"))
            self.assertEqual(request("/")[1], before)
            self.assertEqual(request("/edit/1", dict(first, balance="9.95", asof="2024-03-31"))[0], 303)
            after = request("/")[1]
            self.assertEqual(after.count('href="/edit/'), 2)
            self.assertIn("USD -9.95</strong>", after)
            self.assertIn("JPY -700</strong>", after)
            self.assertIn("2024-03-31", after)


if __name__ == "__main__":
    unittest.main()
