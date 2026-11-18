import unittest

from nsx.calc import _calc


class ClampTests(unittest.TestCase):

    def test_below(self):
        self.assertEqual(_calc.clamp(-5, 0, 10), 0)

    def test_inside(self):
        self.assertEqual(_calc.clamp(5, 0, 10), 5)
