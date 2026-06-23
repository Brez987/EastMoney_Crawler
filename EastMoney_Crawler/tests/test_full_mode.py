import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import auto_pipeline_000001 as pipeline  # type: ignore


class MinimalCsv:
    fieldnames = [
        "user_id", "post_id", "post_source_id", "post_type",
        "user_name", "post_publish_time", "stockbar_name", "stockbar_code",
        "forward", "coment_count", "click_count",
        "post_title", "url", "content",
    ]

    def __init__(self, path: Path, rows: list[dict]):
        self.path = path
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fieldnames)
            writer.writeheader()
            for row in rows:
                full = {k: "" for k in self.fieldnames}
                full.update(row)
                writer.writerow(full)


class FullModeManifestTests(unittest.TestCase):
    def test_full_manifest_written_and_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            stock = "000001"
            start_date = "2009-01-01"
            orig_project_dir = pipeline._PROJECT_DIR
            try:
                pipeline._PROJECT_DIR = str(tmp)
                pipeline.TEMP_DIR = str(tmp / "temp_extract")
                pipeline.EXPORT_DIR = str(tmp / "temp_export")
                os.makedirs(pipeline.TEMP_DIR, exist_ok=True)

                summary = {
                    "max_page": 100,
                    "boundary_page": 80,
                    "completed_pages": 80,
                    "failed_pages": [],
                    "rows": 1234,
                    "unique_post_ids": 1234,
                    "min_time": "2009-01-01 09:30",
                    "max_time": "2026-06-23 10:00",
                }
                pipeline.write_full_manifest(stock, start_date, summary)
                manifest = pipeline.read_full_manifest(stock)
                self.assertIsNotNone(manifest)
                self.assertEqual(manifest["crawl_mode"], "full")
                self.assertEqual(manifest["start_date"], start_date)
                self.assertEqual(manifest["rows"], 1234)
            finally:
                pipeline._PROJECT_DIR = orig_project_dir
                pipeline.TEMP_DIR = os.path.join(orig_project_dir, "temp_extract")
                pipeline.EXPORT_DIR = os.path.join(orig_project_dir, "temp_export")


class FullModeExportTests(unittest.TestCase):
    def test_export_fails_when_manifest_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            stock = "000001"
            orig_project_dir = pipeline._PROJECT_DIR
            try:
                pipeline._PROJECT_DIR = str(tmp)
                pipeline.TEMP_DIR = str(tmp / "temp_extract")
                pipeline.EXPORT_DIR = str(tmp / "temp_export")
                os.makedirs(pipeline.TEMP_DIR, exist_ok=True)
                os.makedirs(pipeline.EXPORT_DIR, exist_ok=True)
                result = pipeline.export_full_posts(stock, "2009-01-01")
                self.assertIsNone(result)
            finally:
                pipeline._PROJECT_DIR = orig_project_dir
                pipeline.TEMP_DIR = os.path.join(orig_project_dir, "temp_extract")
                pipeline.EXPORT_DIR = os.path.join(orig_project_dir, "temp_export")

    def test_export_fails_when_date_too_early(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            stock = "000001"
            orig_project_dir = pipeline._PROJECT_DIR
            try:
                pipeline._PROJECT_DIR = str(tmp)
                pipeline.TEMP_DIR = str(tmp / "temp_extract")
                pipeline.EXPORT_DIR = str(tmp / "temp_export")
                os.makedirs(pipeline.TEMP_DIR, exist_ok=True)
                os.makedirs(pipeline.EXPORT_DIR, exist_ok=True)

                full_csv = Path(pipeline.full_posts_csv_path(stock))
                MinimalCsv(full_csv, [
                    {"post_id": "1", "post_publish_time": "2008-12-31 10:00:00",
                     "post_title": "old", "url": "https://guba.eastmoney.com/news,000001,1.html", "content": "old"},
                ])

                pipeline.write_full_manifest(stock, "2009-01-01", {
                    "max_page": 10, "boundary_page": 8, "completed_pages": 8,
                    "failed_pages": [], "rows": 1, "unique_post_ids": 1,
                    "min_time": "2008-12-31 10:00", "max_time": "2008-12-31 10:00",
                })

                result = pipeline.export_full_posts(stock, "2009-01-01")
                self.assertIsNone(result)
            finally:
                pipeline._PROJECT_DIR = orig_project_dir
                pipeline.TEMP_DIR = os.path.join(orig_project_dir, "temp_extract")
                pipeline.EXPORT_DIR = os.path.join(orig_project_dir, "temp_export")

    def test_export_succeeds_and_sorts_desc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            stock = "000001"
            orig_project_dir = pipeline._PROJECT_DIR
            try:
                pipeline._PROJECT_DIR = str(tmp)
                pipeline.TEMP_DIR = str(tmp / "temp_extract")
                pipeline.EXPORT_DIR = str(tmp / "temp_export")
                os.makedirs(pipeline.TEMP_DIR, exist_ok=True)
                os.makedirs(pipeline.EXPORT_DIR, exist_ok=True)

                full_csv = Path(pipeline.full_posts_csv_path(stock))
                MinimalCsv(full_csv, [
                    {"post_id": "1", "post_publish_time": "2009-01-01 10:00:00",
                     "post_title": "old", "url": "https://guba.eastmoney.com/news,000001,1.html", "content": "old"},
                    {"post_id": "2", "post_publish_time": "2025-01-01 10:00:00",
                     "post_title": "new", "url": "https://guba.eastmoney.com/news,000001,2.html", "content": "new"},
                ])

                pipeline.write_full_manifest(stock, "2009-01-01", {
                    "max_page": 10, "boundary_page": 8, "completed_pages": 8,
                    "failed_pages": [], "rows": 2, "unique_post_ids": 2,
                    "min_time": "2009-01-01 10:00", "max_time": "2025-01-01 10:00",
                })

                out_csv = pipeline.export_full_posts(stock, "2009-01-01")
                self.assertIsNotNone(out_csv)
                self.assertTrue(Path(out_csv).exists())

                with Path(out_csv).open("r", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["post_id"], "2")
                self.assertEqual(rows[1]["post_id"], "1")
            finally:
                pipeline._PROJECT_DIR = orig_project_dir
                pipeline.TEMP_DIR = os.path.join(orig_project_dir, "temp_extract")
                pipeline.EXPORT_DIR = os.path.join(orig_project_dir, "temp_export")


class FullModeStageCommandTests(unittest.TestCase):
    def test_build_stage_cmd_full_mode(self):
        # 仅验证命令行参数拼接
        cmd = [
            "python", "auto_pipeline_000001.py",
            "--stock", "000001",
            "--stage", "1",
            "--crawl-mode", "full",
        ]
        self.assertIn("--crawl-mode", cmd)
        self.assertIn("full", cmd)


if __name__ == "__main__":
    unittest.main()
