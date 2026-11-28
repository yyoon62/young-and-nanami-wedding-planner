"""L30b: deploy/set_anthropic_key.py writes the key privately, exactly where the classifier reads it."""
from contextlib import redirect_stderr, redirect_stdout
import getpass
from io import StringIO
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "deploy"))
import set_anthropic_key as setter
from tx_classifier import KEY_FILE, load_key

KEY = "sk-ant-fictional-" + "a1B2c3D4" * 4       # right shape, not a real key


class SetKey(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve() / "data"       # created by save() as 0700

    def test_saved_file_is_private_and_the_classifier_loads_it(self):
        target = setter.save(self.dir, KEY + "\n")
        self.assertEqual(target, self.dir / KEY_FILE)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(load_key(self.dir, environ={}), (KEY, "file", ""))
        self.assertEqual([p.name for p in self.dir.iterdir()], [KEY_FILE])          # no staging file left behind

    def test_never_overwrites_without_replace_and_replace_is_atomic(self):
        setter.save(self.dir, KEY)
        with self.assertRaises(setter.Refused):
            setter.save(self.dir, KEY.replace("a1B2", "zzzz"))
        self.assertEqual(load_key(self.dir, environ={})[0], KEY)
        other = KEY.replace("a1B2", "zzzz")
        setter.save(self.dir, other, replace=True)
        self.assertEqual(load_key(self.dir, environ={})[0], other)

    def test_wrong_shapes_are_refused_and_nothing_is_written(self):
        for bad in ("", "short", "has spaces " * 5, KEY + "\nEXTRA=1", "x" * 201, "key with/slash" * 3):
            with self.subTest(bad=bad[:12]), self.assertRaises(setter.Refused):
                setter.save(self.dir, bad)
        self.assertFalse((self.dir / KEY_FILE).exists())

    def test_a_loose_or_foreign_data_directory_is_refused(self):
        self.dir.mkdir(mode=0o700)
        os.chmod(self.dir, 0o755)
        with self.assertRaises(setter.Refused):
            setter.save(self.dir, KEY)
        self.assertFalse((self.dir / KEY_FILE).exists())

    def test_main_prints_a_confirmation_but_never_the_key(self):
        out, err = StringIO(), StringIO()
        with patch("getpass.getpass", return_value=KEY), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(setter.main(["--dir", str(self.dir)]), 0)
        self.assertIn("not printed", out.getvalue())
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())
        self.assertNotIn(KEY[10:], out.getvalue() + err.getvalue())

    def test_main_fails_instead_of_echoing_when_there_is_no_terminal(self):
        out, err = StringIO(), StringIO()
        with patch("getpass.getpass", side_effect=getpass.GetPassWarning("echo")), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(setter.main(["--dir", str(self.dir)]), 1)
        self.assertIn("Key not saved", err.getvalue())
        self.assertFalse((self.dir / KEY_FILE).exists())

    def test_the_key_cannot_be_given_as_an_argument(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            setter.main([KEY])


if __name__ == "__main__":
    unittest.main()
