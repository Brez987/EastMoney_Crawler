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


class Stage1ManifestTests(unittest.TestCase):
    def test_manifest_written_after_stage1_inputs_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            stock = "000001"
            source_csv = tmp / f"{stock}.csv"
            MinimalCsv(source_csv, [
                {"post_id": "1", "post_publish_time": "2025-01-01 10:00:00",
                 "post_title": "old", "url": "https://guba.eastmoney.com/news,000001,1.html", "content": "old"},
            ])

            # monkey-patch global paths to temp dir
            orig_project_dir = pipeline._PROJECT_DIR
            orig_stock = pipeline.STOCK_CODE
            try:
                pipeline._PROJECT_DIR = str(tmp)
                pipeline.STOCK_CODE = stock
                pipeline.TEMP_DIR = str(tmp / "temp_extract")
                pipeline.EXPORT_DIR = str(tmp / "temp_export")
                pipeline.PIPELINE_FLAG_DIR = str(tmp / ".pipeline_flags")
                os.makedirs(pipeline.TEMP_DIR, exist_ok=True)
                os.makedirs(pipeline.PIPELINE_FLAG_DIR, exist_ok=True)

                pipeline.write_stage1_manifest(
                    stock, str(source_csv), source_rows=1,
                    source_sha256="abc", source_latest_date="2025-01-01", new_rows=0
                )

                manifest_path = Path(pipeline.stage1_manifest_path(stock))
                self.assertTrue(manifest_path.exists())
                with manifest_path.open("r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
                self.assertEqual(manifest["stock"], stock)
                self.assertEqual(manifest["source_rows"], 1)
                self.assertEqual(manifest["new_rows"], 0)
                self.assertIn("source_sha256", manifest)
            finally:
                pipeline._PROJECT_DIR = orig_project_dir
                pipeline.STOCK_CODE = orig_stock
                pipeline.TEMP_DIR = os.path.join(orig_project_dir, "temp_extract")
                pipeline.EXPORT_DIR = os.path.join(orig_project_dir, "temp_export")
                pipeline.PIPELINE_FLAG_DIR = os.path.join(orig_project_dir, ".pipeline_flags")


class Stage3MergeValidationTests(unittest.TestCase):
    def test_merge_fails_when_base_missing(self):
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

                result = pipeline.merge_csv_files(stock, require_manifest=False)
                self.assertIsNone(result)
            finally:
                pipeline._PROJECT_DIR = orig_project_dir
                pipeline.TEMP_DIR = os.path.join(orig_project_dir, "temp_extract")
                pipeline.EXPORT_DIR = os.path.join(orig_project_dir, "temp_export")

    def test_merge_fails_when_manifest_expects_new_posts_but_csv_missing(self):
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

                base_csv = pipeline.base_csv_path(stock)
                MinimalCsv(Path(base_csv), [
                    {"post_id": "1", "post_publish_time": "2025-01-01 10:00:00",
                     "post_title": "old", "url": "https://guba.eastmoney.com/news,000001,1.html", "content": "old"},
                ])

                pipeline.write_stage1_manifest(
                    stock, source_csv=None, source_rows=1,
                    source_sha256="", source_latest_date="2025-01-01", new_rows=10
                )

                result = pipeline.merge_csv_files(stock, require_manifest=True)
                self.assertIsNone(result)
            finally:
                pipeline._PROJECT_DIR = orig_project_dir
                pipeline.TEMP_DIR = os.path.join(orig_project_dir, "temp_extract")
                pipeline.EXPORT_DIR = os.path.join(orig_project_dir, "temp_export")

    def test_merge_succeeds_and_validates_row_count(self):
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

                base_csv = Path(pipeline.base_csv_path(stock))
                MinimalCsv(base_csv, [
                    {"post_id": "1", "post_publish_time": "2025-01-01 10:00:00",
                     "post_title": "old", "url": "https://guba.eastmoney.com/news,000001,1.html", "content": "old"},
                ])

                new_csv = Path(pipeline.new_posts_csv_path(stock))
                MinimalCsv(new_csv, [
                    {"post_id": "2", "post_publish_time": "2025-01-02 10:00:00",
                     "post_title": "new", "url": "https://guba.eastmoney.com/news,000001,2.html", "content": "new"},
                ])

                pipeline.write_stage1_manifest(
                    stock, source_csv=str(base_csv), source_rows=1,
                    source_sha256=pipeline.file_sha256(str(base_csv)),
                    source_latest_date="2025-01-01", new_rows=1
                )

                out_csv = pipeline.merge_csv_files(stock, require_manifest=True)
                self.assertIsNotNone(out_csv)
                self.assertTrue(Path(out_csv).exists())

                with Path(out_csv).open("r", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                # 按时间降序，新帖在前
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["post_id"], "2")
                self.assertEqual(rows[1]["post_id"], "1")
            finally:
                pipeline._PROJECT_DIR = orig_project_dir
                pipeline.TEMP_DIR = os.path.join(orig_project_dir, "temp_extract")
                pipeline.EXPORT_DIR = os.path.join(orig_project_dir, "temp_export")


if __name__ == "__main__":
    unittest.main()
