import json
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler import CommentCrawler, PostCrawler  # type: ignore


class FastCrawlerPathTests(unittest.TestCase):
    def test_extract_article_payload_handles_nested_json(self):
        html = """
        <script>
        var article_list = {"count": 1, "re": [{"post_id": 1001, "post_user": {"user_id": "u1"}}]};
        var other_list = {};
        </script>
        """

        payload = PostCrawler._extract_article_payload(html)

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["re"][0]["post_user"]["user_id"], "u1")

    def test_fast_list_parser_uses_visible_rows_and_preserves_caifuhao_ids(self):
        article_payload = {
            "count": 3,
            "bar_name": "PingAn",
            "re": [
                {
                    "post_id": 1,
                    "post_title": "pinned",
                    "stockbar_code": "000001",
                    "user_id": "u0",
                    "user_nickname": "a0",
                    "post_click_count": 1,
                    "post_forward_count": 0,
                    "post_comment_count": 0,
                    "post_publish_time": "2026-06-17 09:00:00",
                    "post_type": 0,
                },
                {
                    "post_id": 2,
                    "post_title": "normal",
                    "stockbar_code": "000001",
                    "user_id": "u1",
                    "user_nickname": "a1",
                    "post_click_count": 10,
                    "post_forward_count": 1,
                    "post_comment_count": 3,
                    "post_publish_time": "2026-06-17 10:11:12",
                    "post_type": 0,
                },
                {
                    "post_id": 3,
                    "post_source_id": "20260617111213000000000",
                    "post_title": "caifuhao",
                    "stockbar_code": "000001",
                    "user_id": "u2",
                    "user_nickname": "a2",
                    "post_click_count": 20,
                    "post_forward_count": 2,
                    "post_comment_count": 4,
                    "post_publish_time": "2026-06-17 11:12:13",
                    "post_type": 20,
                },
            ],
        }
        html = (
            "<html><body><table>"
            '<tr class="listitem"><td></td><td></td><td><div><a href="/news,000001,1.html"></a></div></td></tr>'
            '<tr class="listitem"><td></td><td></td><td><div><a href="/news,000001,2.html"></a></div></td></tr>'
            '<tr class="listitem"><td></td><td></td><td><div><a href="//caifuhao.eastmoney.com/news/20260617111213000000000"></a></div></td></tr>'
            "</table><script>var article_list="
            + json.dumps(article_payload, ensure_ascii=False)
            + "; var other_list={};</script></body></html>"
        )
        crawler = PostCrawler("000001")
        crawler._fetch_article_payload_api_with_bootstrap = lambda page_num, session=None: article_payload
        crawler._fetch_list_html = lambda page_num, session=None: html

        posts = crawler._fetch_post_page_fast(1)

        self.assertEqual([p["_id"] for p in posts], ["2", "3"])
        self.assertEqual(posts[1]["post_source_id"], "20260617111213000000000")
        self.assertEqual(posts[1]["post_url"], "https://caifuhao.eastmoney.com/news/20260617111213000000000")
        self.assertEqual(posts[0]["comment_num"], 3)

    def test_fast_page_retry_treats_validation_as_blocked(self):
        crawler = PostCrawler("000001")
        crawler._fetch_article_payload_api_with_bootstrap = mock.Mock(
            side_effect=RuntimeError("list page redirected to validation")
        )

        with self.assertRaisesRegex(RuntimeError, "blocked by validation"):
            crawler._fetch_page_with_retry(1, list_source="html", retries=0)

    def test_list_parser_uses_selenium_rows_and_preserves_caifuhao_rows(self):
        class FakeBrowser:
            current_url = "https://guba.eastmoney.com/list,000001,f_1.html"
            title = "平安银行(000001)股吧_平安银行吧_东方财富网股吧"
            page_source = "<html></html>"

            def __init__(self):
                self.loaded_url = ""
                self.rows = ["pinned", "normal", "caifuhao"]

            def get(self, url):
                self.loaded_url = url

            def find_elements(self, selector_type, selector):
                return self.rows

        parser = mock.Mock()
        parser.parse_post_info.side_effect = [
            {
                "_id": "2",
                "post_url": "https://guba.eastmoney.com/news,000001,2.html",
                "post_date": "2026-06-17",
            },
            {
                "_id": "3",
                "post_url": "https://caifuhao.eastmoney.com/news/20260617111213000000000",
                "post_source_id": "20260617111213000000000",
                "post_date": "2026-06-17",
            },
        ]

        crawler = PostCrawler("000001")
        crawler.browser = FakeBrowser()

        posts = crawler.fetch_post_page(1, parser, stockbar_name="平安银行吧")

        self.assertEqual(crawler.browser.loaded_url, "http://guba.eastmoney.com/list,000001,f_1.html")
        self.assertEqual([p["_id"] for p in posts], ["2", "3"])
        self.assertEqual(posts[1]["post_source_id"], "20260617111213000000000")
        parser.parse_post_info.assert_has_calls([
            mock.call("normal", stockbar_name="平安银行吧"),
            mock.call("caifuhao", stockbar_name="平安银行吧"),
        ])

    def test_comment_jsonp_parser_flattens_main_and_child_replies(self):
        crawler = CommentCrawler("000001")
        payload = {
            "re": [
                {
                    "reply_id": 11,
                    "reply_text": "main",
                    "reply_like_count": 2,
                    "reply_publish_time": "2026-06-17 22:03:43",
                    "child_replys": [
                        {
                            "reply_id": 12,
                            "reply_text": "child",
                            "reply_like_count": 0,
                            "reply_publish_time": "2026-06-17 22:04:43",
                        }
                    ],
                }
            ],
            "reply_total_count": 2,
            "rc": 1,
        }

        data = crawler._parse_jsonp("callback(" + json.dumps(payload, ensure_ascii=False) + ")")
        comments = []
        for reply in data["re"]:
            comments.append(crawler._comment_from_api_reply(reply, "1728375394", False))
            comments.extend(
                crawler._comment_from_api_reply(child, "1728375394", True)
                for child in reply.get("child_replys", [])
            )

        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0]["comment_content"], "main")
        self.assertEqual(comments[0]["comment_date"], "2026-06-17")
        self.assertEqual(comments[1]["sub_comment"], 1)


if __name__ == "__main__":
    unittest.main()
