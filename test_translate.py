import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gui import AppState, read_process_output, update_language_status_from_line
from translate import (
    RunStats,
    get_language_result,
    item_key,
    log_final_results,
    log_language_results,
    translate_source_file,
)


class LanguageResultsTest(unittest.TestCase):
    def render(self, stats):
        output = io.StringIO()
        with redirect_stdout(output):
            log_language_results(stats)
        return output.getvalue()

    def test_reports_language_success_when_all_attempted_keys_succeed(self):
        stats = RunStats()
        result = get_language_result(stats, "vi")
        result["translated"].extend([
            {"resource": "app/src/main/res/values/strings.xml", "key": "string::app_name"},
            {"resource": "app/src/main/res/values/strings.xml", "key": "string::welcome"},
        ])

        output = self.render(stats)

        self.assertIn(
            "OK    [vi] Language completed: all 2 attempted key(s) "
            "translated successfully, skipped=0",
            output,
        )
        self.assertIn("OK   app/src/main/res/values/strings.xml | string::app_name", output)
        self.assertNotIn("Language failed", output)

    def test_reports_partial_language_result_with_failed_key_reason(self):
        stats = RunStats()
        result = get_language_result(stats, "te")
        result["translated"].append(
            {"resource": "app/src/main/res/values/strings.xml", "key": "string::app_name"}
        )
        result["failed"].append({
            "resource": "app/src/main/res/values/strings.xml",
            "key": "string::welcome",
            "error": "TRANSLATION_FAILED: unavailable",
        })

        output = self.render(stats)

        self.assertIn(
            "WARN  [te] Language partially completed: translated=1, failed=1, skipped=0",
            output,
        )
        self.assertIn("FAIL app/src/main/res/values/strings.xml | string::welcome", output)

    def test_reports_language_failure_when_all_attempted_keys_fail(self):
        stats = RunStats()
        result = get_language_result(stats, "ta")
        result["failed"].extend([
            {
                "resource": "app/src/main/res/values/strings.xml",
                "key": "string::app_name",
                "error": "TRANSLATION_FAILED: unavailable",
            },
            {
                "resource": "app/src/main/res/values/arrays.xml",
                "key": "string-array::options::0",
                "error": "TRANSLATION_FAILED: unavailable",
            },
        ])

        output = self.render(stats)

        self.assertIn("ERROR [ta] Language failed: all 2 attempted key(s) failed", output)
        self.assertNotIn("Language completed", output)

    def test_reports_no_pending_translations_separately(self):
        stats = RunStats()
        result = get_language_result(stats, "fil")
        result["skipped"] = 3

        output = self.render(stats)

        self.assertIn("OK    [fil] No pending translations: skipped=3", output)

    def test_language_results_are_the_final_output_section(self):
        stats = RunStats(errors=["[ta] string::app_name: unavailable"])
        result = get_language_result(stats, "ta")
        result["failed"].append({
            "resource": "app/src/main/res/values/strings.xml",
            "key": "string::app_name",
            "error": "TRANSLATION_FAILED: unavailable",
        })
        output = io.StringIO()

        with redirect_stdout(output):
            log_final_results(stats)

        rendered = output.getvalue()
        self.assertLess(
            rendered.index("ERROR Key errors: 1"),
            rendered.index("== Final translation summary =="),
        )
        self.assertTrue(
            rendered.rstrip().endswith(
                "string::app_name: TRANSLATION_FAILED: unavailable"
            )
        )

    def test_collects_key_results_in_source_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            source_file = project_root / "app/src/main/res/values/strings.xml"
            source_file.parent.mkdir(parents=True)
            source_file.write_text(
                '<resources><string name="first">First</string>'
                '<string name="second">Second</string></resources>',
                encoding="utf-8",
            )
            args = SimpleNamespace(
                project_root=project_root,
                skip_translated=False,
                word_by_word=False,
                workers=2,
                engine="google",
            )
            stats = RunStats()

            def fake_translate(task):
                index, item, *_ = task
                error = "TRANSLATION_FAILED: unavailable" if item["name"] == "second" else None
                return index, item_key(item), f"translated-{index}", error

            output = io.StringIO()
            with (
                patch("translate.translate_item", side_effect=fake_translate),
                redirect_stdout(output),
            ):
                translate_source_file(source_file, args, "en", ["vi"], set(), stats)

            result = stats.language_results["vi"]
            self.assertEqual(["string::first"], [item["key"] for item in result["translated"]])
            self.assertEqual(["string::second"], [item["key"] for item in result["failed"]])
            self.assertEqual(
                "app/src/main/res/values/strings.xml",
                result["translated"][0]["resource"],
            )
            self.assertNotIn("string::first", output.getvalue())
            self.assertNotIn("string::second", output.getvalue())
            self.assertIn("[2/2]", output.getvalue())

    def test_gui_does_not_append_log_after_successful_process_exit(self):
        state = AppState()
        process = SimpleNamespace(
            stdout=io.StringIO(
                "== Final translation summary ==\n"
                "  OK   strings.xml | string::app_name\n"
            ),
            wait=lambda: 0,
        )
        state.process = process

        with patch("gui.STATE", state):
            read_process_output(process)

        messages = [item["message"] for item in state.snapshot_logs(0)]
        self.assertEqual(
            [
                "== Final translation summary ==",
                "  OK   strings.xml | string::app_name",
            ],
            messages,
        )
        self.assertEqual("Finished successfully", state.get_status()["status"])

    def test_target_language_status_transitions_from_logs(self):
        state = AppState()
        state.reset_language_statuses(["vi", "te", "ta"])

        update_language_status_from_line("== Language [1/3] vi ==\n", state)
        self.assertEqual("running", state.get_status()["language_statuses"]["vi"]["status"])

        update_language_status_from_line(
            "OK    [vi] Language completed: all 2 attempted key(s) translated successfully\n",
            state,
        )
        update_language_status_from_line(
            "WARN  [te] Language partially completed: translated=1, failed=1, skipped=0\n",
            state,
        )
        update_language_status_from_line(
            "ERROR [ta] Language failed: all 2 attempted key(s) failed\n",
            state,
        )

        statuses = state.get_status()["language_statuses"]
        self.assertEqual("success", statuses["vi"]["status"])
        self.assertEqual("partial", statuses["te"]["status"])
        self.assertEqual("failed", statuses["ta"]["status"])

    def test_unfinished_target_languages_become_stopped(self):
        state = AppState()
        state.reset_language_statuses(["vi", "te"])
        process = SimpleNamespace(
            stdout=io.StringIO("== Language [1/2] vi ==\n"),
            wait=lambda: -15,
        )
        state.process = process

        with patch("gui.STATE", state):
            read_process_output(process)

        status = state.get_status()
        self.assertEqual("Stopped", status["status"])
        self.assertEqual("stopped", status["language_statuses"]["vi"]["status"])
        self.assertEqual("stopped", status["language_statuses"]["te"]["status"])


if __name__ == "__main__":
    unittest.main()
