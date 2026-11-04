import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("pm3_runner", Path(__file__).with_name("runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class AdapterFirewallTests(unittest.TestCase):
    def test_endpoint_accepts_full_chat_url(self):
        self.assertEqual(runner.endpoint_base("https://example.test/v1/chat/completions/"),
                         "https://example.test/v1")

    def test_history_read_is_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary).resolve() / "history"
            snapshot.mkdir()
            result = runner.scoped_arguments("read_file", {"path": "README.md"}, snapshot)
            self.assertEqual(result["path"], str(snapshot / "README.md"))
            with self.assertRaises(ValueError):
                runner.scoped_arguments("read_file", {"path": "../test.json"}, snapshot)
            with self.assertRaises(ValueError):
                runner.scoped_arguments("search_files", {"path": str(snapshot) + "-gold"}, snapshot)

    def test_symlink_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot = root / "history"
            snapshot.mkdir()
            (root / "secret.json").write_text("ground truth")
            (snapshot / "link.json").symlink_to(root / "secret.json")
            with self.assertRaises(ValueError):
                runner.scoped_arguments("read_file", {"path": "link.json"}, snapshot)

    def test_write_and_terminal_are_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            for tool in ("write_file", "patch", "terminal", "memory"):
                with self.assertRaises(ValueError):
                    runner.scoped_arguments(tool, {"path": "."}, Path(temporary))


if __name__ == "__main__":
    unittest.main()
