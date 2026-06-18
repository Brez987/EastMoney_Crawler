import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
