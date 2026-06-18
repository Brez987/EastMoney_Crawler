import csv
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import auto_pipeline_000001 as pipeline  # type: ignore
from crawler import PostCrawler  # type: ignore
from parser import PostParser  # type: ignore


class Stage2FailedDetailTests(unittest.TestCase):
    def test_failed_caifuhao_ids_are_skipped_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "posts.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["post_id", "post_type", "post_title", "url", "content"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "post_id": "failed",
                        "post_type": "20",
                        "post_title": "failed title",
                        "url": "https://caifuhao.eastmoney.com/news/1",
                        "content": "",
                    }
                )
                writer.writerow(
                    {
                        "post_id": "todo",
                        "post_type": "20",
                        "post_title": "todo title",
                        "url": "https://caifuhao.eastmoney.com/news/2",
                        "content": "",
                    }
                )
                writer.writerow(
                    {
                        "post_id": "normal",
                        "post_type": "0",
                        "post_title": "normal title",
                        "url": "https://guba.eastmoney.com/news,000001,3.html",
                        "content": "",
                    }
                )

            with mock.patch.object(pipeline, "TEMP_DIR", tmpdir):
                pipeline._append_detail_failed("000001", "failed", reason="empty")
                failed_ids = pipeline._load_detail_failed_ids("000001")
                with contextlib.redirect_stdout(io.StringIO()):
                    posts, title_fills = pipeline.load_posts_for_detail_crawl(
                        [str(csv_path)],
                        skip_failed_ids=failed_ids,
                    )

            self.assertEqual([p["_id"] for p in posts], ["todo"])
            self.assertEqual(title_fills, {"normal": "normal title"})

    def test_empty_caifuhao_result_marks_failed_without_selenium_fallback(self):
        callbacks = []

        def callback(post_id, update_data):
            callbacks.append((post_id, update_data))

        crawler = PostCrawler("000001")
        empty_detail = {
            "post_content": "",
            "post_title": "",
            "post_date": "",
            "post_time": "",
            "post_author": "",
        }
        with mock.patch.object(PostParser, "_try_requests_caifuhao", return_value=empty_detail), \
             mock.patch.object(PostParser, "parse_post_detail", side_effect=AssertionError("no selenium fallback")):
            crawler._crawl_caifuhao_posts(
                [{"_id": "p1", "post_url": "https://caifuhao.eastmoney.com/news/1"}],
                callback,
                max_workers=1,
            )

        self.assertEqual(callbacks[0][0], "p1")
        self.assertTrue(callbacks[0][1]["_detail_failed"])
        self.assertEqual(callbacks[0][1]["reason"], "caifuhao_empty_or_invalid")


if __name__ == "__main__":
    unittest.main()
