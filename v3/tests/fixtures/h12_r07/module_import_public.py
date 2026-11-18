import importlib
import unittest


class PublicModuleBoundary(unittest.TestCase):
    def test_importing_module_entry_does_not_launch_it(self):
        module = importlib.import_module("package.__main__")
        self.assertTrue(callable(module.main))

    def test_lightweight_package_export_import(self):
        from package import format_result

        self.assertEqual(format_result(7), "value=7")
