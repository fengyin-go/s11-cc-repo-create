"""Summary output must not invalidate the source snapshot it reports on."""
import argparse
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_pipeline as pipeline


class SummarySafetyTests(unittest.TestCase):
    def test_refuse_output_inside_prepared_or_generated_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            for prepared in ([str(repo)], None):
                with self.subTest(prepared=prepared):
                    args = argparse.Namespace(json_out=str(repo / "summary.json"), prepared_repo=prepared, spec=None)
                    report = {"ok": True, "repos": [{"repo_root": str(repo)}]}
                    with contextlib.redirect_stdout(io.StringIO()):
                        pipeline._emit_report(report, args)
                    self.assertFalse(report["ok"])
                    self.assertIn("summary_write_error", report)
                    self.assertFalse((repo / "summary.json").exists())

    def test_refuse_overwriting_input_or_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / "input.json"
            spec.write_text("original")
            alias = root / "alias.json"
            alias.symlink_to(spec)
            for output in (spec, alias):
                args = argparse.Namespace(json_out=str(output), prepared_repo=None, spec=[str(spec)])
                report = {"ok": True}
                with contextlib.redirect_stdout(io.StringIO()):
                    pipeline._emit_report(report, args)
                self.assertFalse(report["ok"])
                self.assertEqual(spec.read_text(), "original")

    def test_parent_summary_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "container" / "repo"
            repo.mkdir(parents=True)
            out = root / "summary.json"
            args = argparse.Namespace(json_out=str(out), prepared_repo=[str(repo)], spec=None)
            report = {"ok": True, "repos": [{"repo_root": str(repo)}]}
            with contextlib.redirect_stdout(io.StringIO()):
                pipeline._emit_report(report, args)
            self.assertTrue(report["ok"])
            self.assertTrue(out.is_file())
