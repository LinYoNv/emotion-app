from pathlib import Path
import tempfile
import unittest

from tools.sync_runtime import apply_plan, compare, make_plan, target


class SyncRuntimeTests(unittest.TestCase):
    def test_reports_missing_different_and_runtime_only_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, live = root / "source", root / "live"
            (source / "sub").mkdir(parents=True)
            live.mkdir(parents=True)
            (source / "same.txt").write_text("same\n", encoding="utf-8")
            (live / "same.txt").write_text("same\n", encoding="utf-8")
            (source / "changed.txt").write_text("after\n", encoding="utf-8")
            (live / "changed.txt").write_text("before\n", encoding="utf-8")
            (source / "sub" / "missing.txt").write_text("missing\n", encoding="utf-8")
            (live / "runtime-only.txt").write_text("generated\n", encoding="utf-8")
            (live / "data").mkdir()
            (live / "data" / "ignored.txt").write_text("runtime state\n", encoding="utf-8")

            report = compare(source, live, ["same.txt", "changed.txt", "sub/missing.txt"])

            self.assertEqual(report["missing"], ["sub/missing.txt"])
            self.assertEqual(report["different"], ["changed.txt"])
            self.assertEqual(report["extra"], ["runtime-only.txt"])

    def test_target_rejects_paths_outside_the_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                target(Path(temporary), "../outside.txt")

    def test_selected_plan_updates_only_included_files_and_verifies_that_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, live, backups = root / "source", root / "live", root / "backups"
            source.mkdir()
            live.mkdir()
            (source / "quota.py").write_text("fixed\n", encoding="utf-8")
            (live / "quota.py").write_text("old\n", encoding="utf-8")
            (source / "unrelated.py").write_text("source\n", encoding="utf-8")
            (live / "unrelated.py").write_text("live\n", encoding="utf-8")

            plan = make_plan(source, live, backups, ["quota.py"])
            apply_plan(plan)

            self.assertEqual((live / "quota.py").read_text(encoding="utf-8"), "fixed\n")
            self.assertEqual((live / "unrelated.py").read_text(encoding="utf-8"), "live\n")


if __name__ == "__main__":
    unittest.main()
