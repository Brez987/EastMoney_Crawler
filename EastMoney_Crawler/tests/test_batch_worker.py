import argparse
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import batch_worker  # type: ignore


def write_stock_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["post_id", "post_publish_time", "post_title", "url", "content"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "post_id": "1",
                "post_publish_time": "2026-06-18 10:00:00",
                "post_title": "title",
                "url": "https://guba.eastmoney.com/news,000001,1.html",
                "content": "title",
            }
        )


class BatchWorkerTests(unittest.TestCase):
    def test_discovers_stock_csvs_without_list_file_dependency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir)
            write_stock_csv(source_dir / "000001.csv")
            write_stock_csv(source_dir / "000002.csv")
            (source_dir / "数据_list.csv").write_text("000003\n", encoding="utf-8")
            (source_dir / "notes.csv").write_text("x\n", encoding="utf-8")

            stocks = batch_worker.discover_stock_csvs([source_dir])

            self.assertEqual(sorted(stocks), ["000001", "000002"])

    def test_lock_claim_is_atomic_and_skips_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            source_csv = Path(tmpdir) / "000001.csv"
            write_stock_csv(source_csv)

            first = batch_worker.claim_stock(
                "000001",
                source_csv,
                progress_dir,
                "worker_1",
                stale_seconds=3600,
            )
            second = batch_worker.claim_stock(
                "000001",
                source_csv,
                progress_dir,
                "worker_2",
                stale_seconds=3600,
            )

            self.assertIsNotNone(first)
            self.assertIsNone(second)

            first.lock_path.unlink()
            batch_worker.mark_state(progress_dir, "000001", ".done", {})
            third = batch_worker.claim_stock(
                "000001",
                source_csv,
                progress_dir,
                "worker_3",
                stale_seconds=3600,
                retry_failed=True,
            )

            self.assertIsNone(third)

    def test_mark_state_skips_terminal_write_when_lock_owned_by_other_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            progress_dir.mkdir()
            lock_path = progress_dir / "000001.lock"
            lock_path.write_text(
                json.dumps({"worker_id": "worker_2"}),
                encoding="utf-8",
            )

            wrote = batch_worker.mark_state(
                progress_dir,
                "000001",
                ".done",
                {"worker_id": "worker_1"},
            )

            self.assertFalse(wrote)
            self.assertFalse((progress_dir / "000001.done").exists())

    def test_find_next_claim_retries_busy_stock_then_claims_when_lock_released(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            progress_dir.mkdir()
            source_csv = Path(tmpdir) / "000001.csv"
            write_stock_csv(source_csv)
            lock_path = progress_dir / "000001.lock"
            lock_path.write_text(
                json.dumps({"worker_id": "worker_1"}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                progress_dir=progress_dir,
                worker_id="worker_2",
                stale_lock_hours=1.0,
                retry_failed=False,
            )

            def release_lock(_seconds):
                lock_path.unlink()

            with mock.patch.object(batch_worker.time, "sleep", side_effect=release_lock):
                claim = batch_worker.find_next_claim(
                    {"000001": source_csv},
                    args,
                    max_wait_seconds=10,
                    retry_seconds=0,
                )

            self.assertIsNotNone(claim)
            self.assertEqual(claim.stock, "000001")
            claim.lock_path.unlink()

    def test_find_next_claim_does_not_wait_when_all_tasks_terminal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            batch_worker.mark_state(progress_dir, "000001", ".done", {})
            args = argparse.Namespace(
                progress_dir=progress_dir,
                worker_id="worker_1",
                stale_lock_hours=1.0,
                retry_failed=False,
            )

            with mock.patch.object(batch_worker.time, "sleep", side_effect=AssertionError("should not wait")):
                claim = batch_worker.find_next_claim(
                    {"000001": None},
                    args,
                    max_wait_seconds=10,
                    retry_seconds=0,
                )

            self.assertIsNone(claim)

    def test_claim_stock_skips_active_deferred_and_reclaims_expired_deferred(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            progress_dir.mkdir()
            source_csv = Path(tmpdir) / "000001.csv"
            write_stock_csv(source_csv)
            future = batch_worker.time.time() + 3600
            batch_worker.write_json(
                progress_dir / "000001.deferred",
                {"retry_after_epoch": future, "reason": "stage1_network"},
            )

            active = batch_worker.claim_stock(
                "000001",
                source_csv,
                progress_dir,
                "worker_1",
                stale_seconds=3600,
            )
            self.assertIsNone(active)

            batch_worker.write_json(
                progress_dir / "000001.deferred",
                {"retry_after_epoch": batch_worker.time.time() - 1, "reason": "stage1_network"},
            )
            expired = batch_worker.claim_stock(
                "000001",
                source_csv,
                progress_dir,
                "worker_1",
                stale_seconds=3600,
            )

            self.assertIsNotNone(expired)
            self.assertFalse((progress_dir / "000001.deferred").exists())
            expired.lock_path.unlink()

    def test_find_next_claim_returns_without_waiting_for_active_deferred_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            progress_dir.mkdir()
            source_csv = Path(tmpdir) / "000001.csv"
            write_stock_csv(source_csv)
            batch_worker.write_json(
                progress_dir / "000001.deferred",
                {
                    "retry_after_epoch": batch_worker.time.time() + 3600,
                    "reason": "stage1_network",
                },
            )
            args = argparse.Namespace(
                progress_dir=progress_dir,
                worker_id="worker_1",
                stale_lock_hours=1.0,
                retry_failed=False,
            )

            with mock.patch.object(batch_worker.time, "sleep", side_effect=AssertionError("should not wait")):
                claim = batch_worker.find_next_claim(
                    {"000001": source_csv},
                    args,
                    max_wait_seconds=10,
                    retry_seconds=1,
                )

            self.assertIsNone(claim)

    def test_process_claim_defers_retryable_failure_without_failed_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            progress_dir.mkdir()
            lock_path = progress_dir / "000001.lock"
            lock_path.write_text(
                json.dumps({"worker_id": "worker_1"}),
                encoding="utf-8",
            )
            claim = batch_worker.ClaimedStock(
                stock="000001",
                source_csv=Path(tmpdir) / "000001.csv",
                lock_path=lock_path,
            )
            args = argparse.Namespace(
                progress_dir=progress_dir,
                worker_id="worker_1",
                heartbeat_seconds=60.0,
                deferred_retry_seconds=30.0,
            )
            summary = {
                "stock": "000001",
                "worker_id": "worker_1",
                "attempts": 3,
                "failed_reason": "stage1_network",
            }

            with mock.patch.object(
                batch_worker,
                "run_stock_pipeline",
                return_value=(False, summary, "stage1_network"),
            ):
                status = batch_worker.process_claim(claim, args, source_dirs=[])

            self.assertEqual(status, "deferred")
            self.assertTrue((progress_dir / "000001.deferred").exists())
            self.assertFalse((progress_dir / "000001.failed").exists())
            self.assertFalse(lock_path.exists())

    def test_worker_loop_waits_when_only_deferred_stocks_remain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            progress_dir.mkdir()
            source_csv = Path(tmpdir) / "000001.csv"
            write_stock_csv(source_csv)
            batch_worker.write_json(
                progress_dir / "000001.deferred",
                {
                    "retry_after_epoch": batch_worker.time.time() + 60,
                    "reason": "stage1_network",
                },
            )
            claim = batch_worker.ClaimedStock(
                stock="000001",
                source_csv=source_csv,
                lock_path=progress_dir / "000001.lock",
            )
            args = argparse.Namespace(
                progress_dir=progress_dir,
                worker_id="worker_1",
                retry_failed=False,
                limit=1,
                min_free_gb=1.0,
                disk_wait_seconds=5.0,
            )

            with mock.patch.object(batch_worker, "disk_free_gb", return_value=100.0), \
                 mock.patch.object(batch_worker, "enough_disk_for_stock", return_value=(True, 100.0, 1.0)), \
                 mock.patch.object(batch_worker, "find_next_claim", side_effect=[None, claim]), \
                 mock.patch.object(batch_worker, "process_claim", return_value="success"), \
                 mock.patch.object(batch_worker.time, "sleep") as sleep:
                exit_code = batch_worker.worker_loop(args, {"000001": source_csv}, source_dirs=[])

            self.assertEqual(exit_code, 0)
            sleep.assert_called_once()

    def test_worker_loop_does_not_treat_deferred_as_failure_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            progress_dir.mkdir()
            claim = batch_worker.ClaimedStock(
                stock="000001",
                source_csv=Path(tmpdir) / "000001.csv",
                lock_path=progress_dir / "000001.lock",
            )
            args = argparse.Namespace(
                progress_dir=progress_dir,
                worker_id="worker_1",
                limit=1,
                min_free_gb=1.0,
                disk_wait_seconds=1.0,
            )

            with mock.patch.object(batch_worker, "disk_free_gb", return_value=100.0), \
                 mock.patch.object(batch_worker, "enough_disk_for_stock", return_value=(True, 100.0, 1.0)), \
                 mock.patch.object(batch_worker, "find_next_claim", return_value=claim), \
                 mock.patch.object(batch_worker, "process_claim", return_value="deferred"):
                exit_code = batch_worker.worker_loop(args, {"000001": claim.source_csv}, source_dirs=[])

            self.assertEqual(exit_code, 0)

    def test_clean_per_stock_temp_preserves_progress_lock_and_resume_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            temp_dir = project_dir / "temp_extract"
            progress_dir = project_dir / "batch_progress_full_20090101"
            page_cache = temp_dir / "000001_full_pages"
            page_cache.mkdir(parents=True)
            progress_dir.mkdir(parents=True)
            temp_dir.mkdir(parents=True, exist_ok=True)
            lock_path = progress_dir / "000001.lock"
            lock_path.write_text("{}", encoding="utf-8")
            retrying_path = progress_dir / "000001.retrying"
            retrying_path.write_text("{}", encoding="utf-8")
            detail_failed = temp_dir / "000001_detail_failed.jsonl"
            detail_failed.write_text("{}\n", encoding="utf-8")
            checkpoint = temp_dir / "000001_stage2_checkpoint.json"
            checkpoint.write_text("{}", encoding="utf-8")
            content_delta = temp_dir / "000001_content_delta.jsonl"
            content_delta.write_text("{}\n", encoding="utf-8")
            stale_output = temp_dir / "000001_full_posts.csv"
            stale_output.write_text("old", encoding="utf-8")

            with mock.patch.object(batch_worker, "PROJECT_DIR", project_dir), \
                 mock.patch.object(batch_worker, "DEFAULT_TEMP_DIR", temp_dir):
                batch_worker.clean_per_stock_temp("000001", crawl_mode="full")

            self.assertTrue(lock_path.exists())
            self.assertTrue(retrying_path.exists())
            self.assertTrue(detail_failed.exists())
            self.assertTrue(checkpoint.exists())
            self.assertTrue(content_delta.exists())
            self.assertTrue(page_cache.exists())
            self.assertFalse(stale_output.exists())

    def test_stream_subprocess_captures_output_tail(self):
        result = batch_worker.stream_subprocess(
            [
                sys.executable,
                "-c",
                "print('hello from child')",
            ],
            stage=1,
            stock="000001",
            progress_dir=None,
            idle_timeout=5,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("hello from child", result.output_tail)

    def test_run_stock_pipeline_retries_from_failed_stage_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_dir = Path(tmpdir) / "progress"
            progress_dir.mkdir()
            args = argparse.Namespace(
                worker_id="worker_1",
                progress_dir=progress_dir,
                crawl_mode="full",
                start_date="2009-01-01",
                list_workers=6,
                list_page_limit=0,
                list_source="html",
                detail_workers=3,
                python=sys.executable,
                max_retries=1,
                stock_timeout_minutes=60.0,
            )
            stages = []
            clean_from = []

            def fake_stream(cmd, stage, stock, progress_dir=None, idle_timeout=0):
                stages.append(stage)
                if stages == [1, 2]:
                    return batch_worker.StageResult(
                        returncode=1,
                        seconds=1.0,
                        output_tail="ConnectionError",
                    )
                return batch_worker.StageResult(returncode=0, seconds=1.0, output_tail="")

            def fake_clean(stock, crawl_mode="incremental", from_stage=1):
                clean_from.append(from_stage)

            with mock.patch.object(batch_worker, "stream_subprocess", side_effect=fake_stream), \
                 mock.patch.object(batch_worker, "clean_per_stock_temp", side_effect=fake_clean):
                success, summary, reason = batch_worker.run_stock_pipeline(
                    "000001",
                    None,
                    args,
                    source_dirs=[],
                )

            self.assertTrue(success)
            self.assertEqual(reason, "")
            self.assertEqual(stages, [1, 2, 2, 3])
            self.assertEqual(clean_from, [1, 2])
            self.assertEqual(summary["attempts"], 2)

    def test_classify_stage_failure_reports_network_and_resource_failures(self):
        network = batch_worker.StageResult(
            returncode=1,
            seconds=1.0,
            output_tail="requests.exceptions.ConnectionError: read timed out",
        )
        resource = batch_worker.StageResult(
            returncode=1,
            seconds=1.0,
            output_tail="OSError: [WinError 112] There is not enough space on the disk",
        )

        self.assertEqual(batch_worker.classify_stage_failure(1, network), "stage1_network")
        self.assertEqual(batch_worker.classify_stage_failure(3, resource), "stage3_resource")

    def test_dry_run_does_not_create_done_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "source"
            progress_dir = Path(tmpdir) / "progress"
            source_dir.mkdir()
            write_stock_csv(source_dir / "000001.csv")

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "batch_worker.py"),
                    "--dry-run",
                    "--limit",
                    "1",
                    "--source-dir",
                    str(source_dir),
                    "--progress-dir",
                    str(progress_dir),
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))
            self.assertIn("[dry-run] 000001", result.stdout)
            self.assertFalse((progress_dir / "000001.done").exists())

    def test_full_mode_default_stock_list_uses_glob_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "custom_list.csv").write_text("000001\n", encoding="utf-8")
            progress_dir = project_dir / "progress"

            with mock.patch.object(batch_worker, "PROJECT_DIR", project_dir):
                with mock.patch("sys.stdout"):
                    exit_code = batch_worker.main([
                        "--dry-run",
                        "--crawl-mode",
                        "full",
                        "--limit",
                        "1",
                        "--progress-dir",
                        str(progress_dir),
                    ])

            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
