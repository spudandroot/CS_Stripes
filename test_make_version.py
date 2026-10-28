"""Tests for make_version. Run: python -m unittest -v"""

import os
import tempfile
import unittest

import make_version


class MakeVersionTests(unittest.TestCase):
    def test_version_numbers(self):
        self.assertEqual(make_version.version_numbers("v1.10.2"), (1, 10, 2, 0))
        self.assertEqual(make_version.version_numbers("dev-abc1234"), (0, 0, 0, 0))

    def test_stamp(self):
        with tempfile.TemporaryDirectory() as folder:
            make_version.stamp("v1.2.0", folder)
            scope = {}
            with open(os.path.join(folder, "stripes_version.py"), encoding="utf-8") as f:
                exec(f.read(), scope)
            self.assertEqual(scope["VERSION"], "v1.2.0")
            with open(os.path.join(folder, "version_info.txt"), encoding="utf-8") as f:
                info = f.read()
            self.assertIn("filevers=(1, 2, 0, 0)", info)
            self.assertIn("StringStruct('ProductVersion', 'v1.2.0')", info)


if __name__ == "__main__":
    unittest.main()
