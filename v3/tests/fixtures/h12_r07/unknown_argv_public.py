import unittest

from program import parse


class PublicArgvBoundary(unittest.TestCase):
    def test_unknown_arguments_keep_order_and_values(self):
        enabled, remaining = parse(["--debug", "--foreign", "X", "tail"])
        self.assertTrue(enabled)
        self.assertEqual(remaining, ["--foreign", "X", "tail"])

    def test_empty_remaining_argv(self):
        self.assertEqual(parse(["--debug"]), (True, []))
