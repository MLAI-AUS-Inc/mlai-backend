"""F3: the prevention watch turns red on a red or skipped job. Recorded runs only; no live API call."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prevention_watch", ROOT / "scripts/prevention_watch.py")
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)

FIXTURE = Path(__file__).parent / "fixtures/prevention-watch-recorded-runs-v1.json"
RUNS = {key: value["workflow_runs"] for key, value in json.loads(FIXTURE.read_text()).items()}
CONFIG = json.loads((ROOT / ".github/prevention-watch.json").read_text())
NOW = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)
NIGHTLY = {"id": "nightly-schedule", "workflow": "nightly.yml", "events": ["schedule"], "cadence_days": 1}


def rows(report):
    return {row["id"]: row for row in report["entries"]}


class PreventionWatchTests(unittest.TestCase):
    def test_recorded_skipped_schedule_alerts(self):
        report = watch.evaluate({"workflows": [NIGHTLY]}, RUNS, NOW)
        row = rows(report)["nightly-schedule"]
        self.assertEqual(report["status"], "red")
        self.assertTrue(row["alert"])
        self.assertEqual(row["state"], "skipped")
        self.assertEqual(row["bad_runs"], 3)

    def test_main_checks_red_for_n_days_alerts_and_ignores_pull_requests(self):
        row = rows(watch.evaluate(CONFIG, RUNS, NOW))["main-checks"]
        self.assertTrue(row["alert"])
        self.assertEqual(row["state"], "red")
        self.assertEqual(row["bad_runs"], 3)
        self.assertTrue(row["last_success_at"].startswith("2026-11-16"))

    def test_a_red_streak_shorter_than_n_days_waits(self):
        row = rows(watch.evaluate(CONFIG, RUNS, NOW, stale_after_days=4))["main-checks"]
        self.assertFalse(row["alert"])
        self.assertEqual(row["state"], "red_recent")

    def test_unreadable_runs_are_red_and_an_empty_watch_list_is_an_error(self):
        report = watch.evaluate(CONFIG, {}, NOW)
        self.assertEqual(rows(report)["main-checks"]["state"], "unreadable")
        with self.assertRaises(ValueError):
            watch.evaluate({"workflows": []}, RUNS, NOW)

    def test_cli_replays_a_recorded_day_red_and_report_only(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            code = watch.main(["check", "--config", str(ROOT / ".github/prevention-watch.json"),
                               "--runs-fixture", str(FIXTURE), "--output", str(output),
                               "--now", "2026-11-20T22:00:00Z", "--mode", "slack-typo"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.read_text())["alerts"], ["main-checks"])

    def test_unknown_alert_mode_never_pages(self):
        self.assertEqual(watch.resolve_mode(""), "report-only")
        self.assertEqual(watch.resolve_mode("page-everyone"), "report-only")
        with self.assertRaises(RuntimeError):
            watch.page("slack", "title", "text", webhook="", summary_path=None)

    def test_workflow_is_scheduled_read_only_and_report_only_by_default(self):
        text = (ROOT / ".github/workflows/prevention-watch.yml").read_text()
        self.assertIn("schedule:", text)
        self.assertIn("actions: read", text)
        self.assertNotIn("write", text)
        self.assertIn("vars.PREVENTION_WATCH_ALERT_MODE || 'report-only'", text)


if __name__ == "__main__":
    unittest.main()
