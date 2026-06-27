import csv
import contextlib
import io
import json
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
    def test_wap_caifuhao_api_extracts_content_from_csv_ids(self):
        class FakeResponse:
            def __init__(self, status_code, text):
                self.status_code = status_code
                self.text = text

        class FakeSession:
            def __init__(self):
                self.calls = []

            def get(self, url, params=None, headers=None, timeout=None, proxies=None):
                self.calls.append((url, params or {}, headers or {}))
                payload = {
                    "rc": 1,
                    "post": {
                        "post_title": "API title",
                        "post_content": "<p>Hello&nbsp;world</p><p>Second line</p>",
                        "post_publish_time": "2025-07-19 09:54:55",
                        "post_user": {"user_nickname": "api author"},
                    },
                }
                return FakeResponse(200, json.dumps(payload))

        session = FakeSession()
        result = PostParser()._try_wap_caifuhao(
            post_id="1573140480",
            source_id="20250719095455869493170",
            session=session,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["post_title"], "API title")
        self.assertEqual(result["post_date"], "2025-07-19")
        self.assertEqual(result["post_time"], "09:54")
        self.assertEqual(result["post_author"], "api author")
        self.assertIn("Hello", result["post_content"])
        self.assertIn("Second line", result["post_content"])
        self.assertEqual(len(session.calls), 1)
        self.assertIn("ArticleContent", session.calls[0][0])

    def test_caifuhao_crawl_prefers_wap_api_without_pc_fallback(self):
        callbacks = []
        crawler = PostCrawler("000001")
        api_detail = {
            "ok": True,
            "post_content": "api body",
            "post_title": "api title",
            "post_date": "2025-07-19",
            "post_time": "09:54",
            "post_author": "api author",
            "http_status": 200,
            "reason": "ok",
        }

        with mock.patch.object(crawler, "_new_caifuhao_session", return_value=mock.Mock(cookies=[])), \
             mock.patch.object(crawler, "_bootstrap_caifuhao_session_via_browser", side_effect=AssertionError("no browser")), \
             mock.patch.object(PostParser, "_try_wap_caifuhao", return_value=api_detail) as wap_api, \
             mock.patch.object(PostParser, "_try_requests_caifuhao", side_effect=AssertionError("no pc fallback")), \
             mock.patch("crawler.time.sleep", return_value=None):
            ok = crawler._crawl_caifuhao_posts(
                [
                    {
                        "_id": "1573140480",
                        "post_source_id": "20250719095455869493170",
                        "post_url": "https://caifuhao.eastmoney.com/news/20250719095455869493170",
                    }
                ],
                lambda post_id, update_data: callbacks.append((post_id, update_data)),
                max_workers=1,
            )

        self.assertTrue(ok)
        wap_api.assert_called_once()
        self.assertEqual(callbacks[0][0], "1573140480")
        self.assertEqual(callbacks[0][1]["post_content"], "api body")
        self.assertEqual(callbacks[0][1]["post_author"], "api author")

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
                pipeline._append_detail_failed("000001", "failed", reason="invalid_article")
                pipeline._append_detail_failed("000001", "retryable", reason="http_403")
                failed_ids = pipeline._load_detail_failed_ids("000001")
                with contextlib.redirect_stdout(io.StringIO()):
                    posts, title_fills = pipeline.load_posts_for_detail_crawl(
                        [str(csv_path)],
                        skip_failed_ids=failed_ids,
                    )

            self.assertEqual([p["_id"] for p in posts], ["todo"])
            self.assertEqual(title_fills, {"normal": "normal title"})
            self.assertNotIn("retryable", failed_ids)

    def test_full_csv_caifuhao_url_enters_detail_crawl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "full_posts.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "post_id", "post_source_id", "post_type",
                        "post_title", "url", "content",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "post_id": "guba_mirror_id",
                        "post_source_id": "source_id",
                        "post_type": "20",
                        "post_title": "caifuhao title",
                        "url": "https://caifuhao.eastmoney.com/news/source_id",
                        "content": "",
                    }
                )

            with contextlib.redirect_stdout(io.StringIO()):
                posts, title_fills = pipeline.load_posts_for_detail_crawl([str(csv_path)])

        self.assertEqual(title_fills, {})
        self.assertEqual(posts[0]["_id"], "guba_mirror_id")
        self.assertEqual(posts[0]["post_url"], "https://caifuhao.eastmoney.com/news/source_id")
        self.assertEqual(posts[0]["post_source_id"], "source_id")
        self.assertEqual(posts[0]["post_type"], "20")

    def test_permanent_caifuhao_result_marks_failed_without_selenium_fallback(self):
        callbacks = []

        def callback(post_id, update_data):
            callbacks.append((post_id, update_data))

        crawler = PostCrawler("000001")
        invalid_detail = {
            "ok": False,
            "post_content": "",
            "post_title": "",
            "post_date": "",
            "post_time": "",
            "post_author": "",
            "http_status": 404,
            "reason": "http_404",
        }
        with mock.patch.object(crawler, "_bootstrap_caifuhao_session_via_browser"), \
             mock.patch.object(crawler, "_new_caifuhao_session", return_value=mock.Mock(cookies=[])), \
             mock.patch.object(PostParser, "_try_requests_caifuhao", return_value=invalid_detail), \
             mock.patch.object(PostParser, "parse_post_detail", side_effect=AssertionError("no selenium fallback")):
            ok = crawler._crawl_caifuhao_posts(
                [{"_id": "p1", "post_url": "https://caifuhao.eastmoney.com/news/1"}],
                callback,
                max_workers=1,
            )

        self.assertTrue(ok)
        self.assertEqual(callbacks[0][0], "p1")
        self.assertTrue(callbacks[0][1]["_detail_failed"])
        self.assertEqual(callbacks[0][1]["reason"], "http_404")

    def test_http_403_pauses_without_persistent_failed_callback(self):
        callbacks = []
        crawler = PostCrawler("000001")
        blocked_detail = {
            "ok": False,
            "post_content": "",
            "http_status": 403,
            "reason": "http_403",
        }
        fake_browser = mock.Mock()
        fake_browser.execute_async_script.return_value = {
            "status": 403,
            "url": "https://caifuhao.eastmoney.com/news/1",
            "text": "",
        }

        with mock.patch.object(crawler, "_bootstrap_caifuhao_session_via_browser"), \
             mock.patch.object(crawler, "_new_caifuhao_session", return_value=mock.Mock(cookies=[])), \
             mock.patch.object(PostParser, "_try_requests_caifuhao", return_value=blocked_detail), \
             mock.patch("crawler.create_stealth_chrome", return_value=fake_browser), \
             mock.patch("crawler.time.sleep", return_value=None):
            ok = crawler._crawl_caifuhao_posts(
                [{"_id": "p1", "post_url": "https://caifuhao.eastmoney.com/news/1"}],
                lambda post_id, update_data: callbacks.append((post_id, update_data)),
                max_workers=1,
            )

        self.assertFalse(ok)
        self.assertEqual(callbacks, [])

    def test_crawl_post_detail_csv_records_failed_and_finishes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "full_posts.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "post_id", "post_source_id", "post_type",
                        "post_title", "url", "content",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "post_id": "failed",
                        "post_source_id": "source_id",
                        "post_type": "20",
                        "post_title": "failed title",
                        "url": "https://caifuhao.eastmoney.com/news/source_id",
                        "content": "",
                    }
                )

            def fake_crawl_detail(self, posts, update_callback, max_workers):
                update_callback(
                    "failed",
                    {
                        "post_content": "",
                        "_detail_failed": True,
                        "reason": "invalid_article",
                        "post_url": posts[0]["post_url"],
                    },
                )

            with mock.patch.object(pipeline, "TEMP_DIR", tmpdir), \
                 mock.patch.object(PostCrawler, "crawl_post_detail", fake_crawl_detail):
                with contextlib.redirect_stdout(io.StringIO()):
                    ok = pipeline.crawl_post_detail_csv(
                        "000001",
                        [str(csv_path)],
                        detail_workers=3,
                    )
                failed_ids = pipeline._load_detail_failed_ids("000001")

            self.assertTrue(ok)
            self.assertIn("failed", failed_ids)


if __name__ == "__main__":
    unittest.main()
