import csv
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

selenium_module = types.ModuleType("selenium")
webdriver_module = types.ModuleType("selenium.webdriver")
chrome_module = types.ModuleType("selenium.webdriver.chrome")
service_module = types.ModuleType("selenium.webdriver.chrome.service")
webdriver_module.Chrome = object
webdriver_module.ChromeOptions = object
service_module.Service = object
selenium_module.webdriver = webdriver_module
sys.modules.setdefault("selenium", selenium_module)
sys.modules.setdefault("selenium.webdriver", webdriver_module)
sys.modules.setdefault("selenium.webdriver.chrome", chrome_module)
sys.modules.setdefault("selenium.webdriver.chrome.service", service_module)

from missing_year_backfill import (  # type: ignore
    DEFAULT_OUTPUT_BASE_DIR,
    OUTPUT_FIELDS,
    append_rows,
    build_stock_jobs,
    classify_page_years,
    build_output_row,
    build_pending_stock_jobs,
    convert_article_to_post,
    extract_page_posts,
    extract_article_list_data,
    filter_posts_by_years,
    format_post_publish_time,
    get_target_year_bounds,
    load_existing_stock_summaries,
    load_missing_year_targets,
    run_backfill,
    run_with_auto_restart,
    sanitize_text,
    save_summary,
    seek_target_start_page,
    should_restart_run,
    should_stop_for_min_year,
    validate_article_page_payload,
)


class MissingYearBackfillTests(unittest.TestCase):
    def test_output_fields_match_expected_schema(self):
        self.assertEqual(
            OUTPUT_FIELDS,
            [
                "user_id",
                "post_id",
                "post_type",
                "user_name",
                "post_publish_time",
                "post_title",
                "stockbar_name",
                "stockbar_code",
                "forward_count",
                "comment_count",
                "click_count",
                "page",
                "total_pages",
                "stock_code",
                "post_url",
            ],
        )

    def test_load_missing_year_targets_collects_requested_stock_years(self):
        with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8-sig", delete=False) as handle:
            writer = csv.DictWriter(handle, fieldnames=["code", "year"])
            writer.writeheader()
            writer.writerow({"code": "9", "year": "2008"})
            writer.writerow({"code": "000009", "year": "2023"})
            writer.writerow({"code": "600000", "year": "2010"})
            csv_path = handle.name

        targets = load_missing_year_targets(csv_path, selected_stocks=["000009"])

        self.assertEqual(targets, {"000009": {2008, 2023}})

    def test_load_missing_year_targets_supports_missing_years_column(self):
        with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8-sig", delete=False) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "code",
                    "first_year",
                    "last_year",
                    "n_years_data",
                    "n_missing",
                    "missing_years",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "code": "301372",
                    "first_year": "2009",
                    "last_year": "2025",
                    "n_years_data": "8",
                    "n_missing": "9",
                    "missing_years": "2010, 2011, 2012, 2013, 2015, 2016, 2017, 2018, 2019",
                }
            )
            writer.writerow(
                {
                    "code": "2947",
                    "first_year": "2007",
                    "last_year": "2025",
                    "n_years_data": "10",
                    "n_missing": "8",
                    "missing_years": "2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016",
                }
            )
            writer.writerow(
                {
                    "code": "688631",
                    "first_year": "2012",
                    "last_year": "2025",
                    "n_years_data": "6",
                    "n_missing": "0",
                    "missing_years": "",
                }
            )
            csv_path = handle.name

        targets = load_missing_year_targets(csv_path, selected_stocks=["301372", "002947", "688631"])

        self.assertEqual(
            targets,
            {
                "301372": {2010, 2011, 2012, 2013, 2015, 2016, 2017, 2018, 2019},
                "002947": {2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016},
            },
        )

    def test_format_post_publish_time_keeps_minute_precision(self):
        post = {"post_date": "2023-08-21", "post_time": "10:03:59"}

        self.assertEqual(format_post_publish_time(post), "2023-08-21 10:03")

    def test_filter_posts_by_years_returns_matches_and_page_years(self):
        posts = [
            {"_id": 1, "post_date": "2023-01-01", "post_time": "10:00:00"},
            {"_id": 2, "post_date": "2007-12-31", "post_time": "09:00:00"},
            {"_id": 3, "post_date": None, "post_time": None},
        ]

        matched_posts, page_years, invalid_count = filter_posts_by_years(posts, {2023})

        self.assertEqual([post["_id"] for post in matched_posts], [1])
        self.assertEqual(page_years, {2023, 2007})
        self.assertEqual(invalid_count, 1)

    def test_should_stop_for_min_year_only_when_page_is_older(self):
        self.assertTrue(should_stop_for_min_year({2007, 2006}, 2008))
        self.assertFalse(should_stop_for_min_year({2008, 2007}, 2008))
        self.assertFalse(should_stop_for_min_year(set(), 2008))

    def test_build_output_row_projects_expected_columns(self):
        row = build_output_row(
            {
                "_id": 123,
                "user_id": "456",
                "post_type": 2,
                "post_title": "标题",
                "post_view": "4567",
                "forward_count": 12,
                "comment_num": 89,
                "stockbar_code": "000009",
                "post_url": "http://example.com/post",
                "post_date": "2021-06-18",
                "post_time": "08:30:45",
                "post_author": "作者A",
            },
            page_num=77,
            total_pages=1888,
        )

        self.assertEqual(
            row,
            {
                "user_id": "456",
                "post_id": 123,
                "post_type": 2,
                "user_name": "作者A",
                "post_publish_time": "2021-06-18 08:30",
                "post_title": "标题",
                "stockbar_name": "",
                "stockbar_code": "000009",
                "forward_count": 12,
                "comment_count": 89,
                "click_count": "4567",
                "page": 77,
                "total_pages": 1888,
                "stock_code": "000009",
                "post_url": "http://example.com/post",
            },
        )

    def test_extract_article_list_data_parses_embedded_json(self):
        html = """
        <html><script>
        var article_list = {"count": 1, "re": [{"post_id": 1001, "post_publish_time": "2023-05-06 09:30:00"}]};
        </script></html>
        """

        data = extract_article_list_data(html)

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["re"][0]["post_id"], 1001)

    def test_convert_article_to_post_maps_http_payload(self):
        post = convert_article_to_post(
            {
                "post_id": 1688733593,
                "user_id": 99887766,
                "post_type": 1,
                "post_title": "标题B",
                "post_click_count": 47,
                "post_forward_count": 6,
                "post_comment_count": 1,
                "post_publish_time": "2023-05-06 09:30:00",
                "user_nickname": "作者B",
                "stockbar_code": "000009",
            }
        )

        self.assertEqual(
            post,
            {
                "_id": 1688733593,
                "user_id": "99887766",
                "post_type": 1,
                "post_title": "标题B",
                "post_view": "47",
                "forward_count": 6,
                "comment_num": 1,
                "stockbar_code": "000009",
                "post_url": "https://guba.eastmoney.com/news,000009,1688733593.html",
                "post_date": "2023-05-06",
                "post_time": "09:30:00",
                "post_author": "作者B",
            },
        )

    def test_validate_article_page_payload_rejects_empty_nonzero_page(self):
        with self.assertRaisesRegex(ValueError, "空帖子列表"):
            validate_article_page_payload({"count": 80, "re": []})

        payload = validate_article_page_payload({"count": 0, "re": []})
        self.assertEqual(payload, {"count": 0, "re": []})

    def test_extract_page_posts_rejects_empty_page(self):
        with self.assertRaisesRegex(ValueError, "解析结果为空"):
            extract_page_posts({"re": []}, page_num=3038)

    def test_append_rows_handles_special_characters_in_titles(self):
        with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8-sig", delete=False) as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            csv_path = handle.name

        append_rows(
            csv_path,
            [
                {
                    "user_id": "2",
                    "post_id": 1,
                    "post_type": 0,
                    "user_name": "作者",
                    "post_publish_time": "2023-01-01 10:00",
                    "post_title": '标题, 含有 "引号"',
                    "stockbar_name": "",
                    "stockbar_code": "000009",
                    "forward_count": 3,
                    "comment_count": 0,
                    "click_count": "10",
                    "page": 1,
                    "total_pages": 99,
                    "stock_code": "000009",
                    "post_url": "http://example.com/post",
                }
            ],
        )

        with open(csv_path, "r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(rows[0]["post_title"], '标题, 含有 "引号"')
        self.assertEqual(rows[0]["user_id"], "2")
        self.assertEqual(rows[0]["post_type"], "0")
        self.assertEqual(rows[0]["user_name"], "作者")
        self.assertEqual(rows[0]["stockbar_code"], "000009")
        self.assertEqual(rows[0]["forward_count"], "3")
        self.assertEqual(rows[0]["comment_count"], "0")
        self.assertEqual(rows[0]["click_count"], "10")
        self.assertEqual(rows[0]["total_pages"], "99")
        self.assertEqual(rows[0]["stock_code"], "000009")

    def test_sanitize_text_removes_control_characters(self):
        self.assertEqual(sanitize_text("A\x00B\r\nC\tD"), "A B C D")

    def test_build_stock_jobs_sorts_stock_codes(self):
        jobs = build_stock_jobs(
            {
                "600000": {2010},
                "000009": {2008, 2023},
                "300001": {2015},
            }
        )

        self.assertEqual(
            jobs,
            [
                ("000009", {2008, 2023}),
                ("300001", {2015}),
                ("600000", {2010}),
            ],
        )

    def test_get_target_year_bounds_closes_gaps(self):
        self.assertEqual(get_target_year_bounds({2008, 2010, 2012, 2018}), (2008, 2018))

    def test_classify_page_years_marks_pages_newer_than_target(self):
        self.assertEqual(classify_page_years({2021, 2022}, 2008, 2020), "newer")
        self.assertEqual(classify_page_years({2019, 2020}, 2008, 2020), "target_or_older")
        self.assertEqual(classify_page_years({2006, 2007}, 2008, 2020), "target_or_older")

    def test_seek_target_start_page_finds_first_target_window_page(self):
        def lookup(page_num: int):
            if page_num <= 40:
                return {2026}
            if page_num <= 80:
                return {2025}
            if page_num <= 120:
                return {2024}
            if page_num <= 160:
                return {2023}
            if page_num <= 200:
                return {2022}
            if page_num <= 240:
                return {2021}
            if page_num <= 280:
                return {2020}
            if page_num <= 320:
                return {2019}
            if page_num <= 360:
                return {2018}
            return {2017}

        start_page, probed_pages = seek_target_start_page(
            total_pages=400,
            target_min_year=2008,
            target_max_year=2018,
            first_page_years={2026},
            lookup_page_years=lookup,
        )

        self.assertEqual(start_page, 321)
        self.assertIn(321, probed_pages)

    def test_seek_target_start_page_handles_mixed_boundary_page(self):
        def lookup(page_num: int):
            if page_num <= 40:
                return {2020}
            if page_num <= 50:
                return {2019, 2020}
            if page_num <= 60:
                return {2019}
            return {2018}

        start_page, _ = seek_target_start_page(
            total_pages=80,
            target_min_year=2008,
            target_max_year=2019,
            first_page_years={2020},
            lookup_page_years=lookup,
        )

        self.assertEqual(start_page, 41)

    def test_load_existing_stock_summaries_reads_saved_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_summary(
                tmpdir,
                [
                    {
                        "stock_code": "000009",
                        "target_years": [2008, 2023],
                        "scanned_pages": 10,
                        "matched_rows": 20,
                        "matched_years": [2008, 2023],
                        "invalid_timestamp_count": 0,
                        "failed_pages": [],
                        "stop_reason": "year_boundary_reached",
                        "output_file": "000009.csv",
                    }
                ],
            )

            summaries = load_existing_stock_summaries(tmpdir)

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["stock_code"], "000009")

    def test_build_pending_stock_jobs_skips_completed_stocks(self):
        jobs = build_pending_stock_jobs(
            [
                ("000009", {2008, 2023}),
                ("000014", {2008, 2023}),
                ("600000", {2010}),
            ],
            completed_stock_codes={"000009", "600000"},
        )

        self.assertEqual(jobs, [("000014", {2008, 2023})])

    def test_should_restart_run_detects_browser_session_failures(self):
        self.assertTrue(
            should_restart_run(
                RuntimeError("Message: session not created from chrome not reachable")
            )
        )
        self.assertTrue(
            should_restart_run(
                RuntimeError("disconnected: not connected to DevTools")
            )
        )
        self.assertFalse(should_restart_run(RuntimeError("缺失年份清单中没有可处理的股票")))

    def test_run_with_auto_restart_retries_browser_failures_with_same_output_dir(self):
        recovered_summary = {
            "stock_code": "000009",
            "target_years": [2008, 2023],
            "scanned_pages": 5,
            "matched_rows": 9,
            "matched_years": [2008],
            "invalid_timestamp_count": 0,
            "failed_pages": [],
            "stop_reason": "year_boundary_reached",
            "output_file": "/tmp/recovered.csv",
        }

        with mock.patch(
            "missing_year_backfill.run_backfill",
            side_effect=[
                RuntimeError("session not created from chrome not reachable"),
                ("/tmp/fixed-output", [recovered_summary]),
            ],
        ) as run_mock, mock.patch("missing_year_backfill.time.sleep") as sleep_mock:
            output_dir, summaries = run_with_auto_restart(
                missing_years_file="missing.csv",
                selected_stocks=["000009"],
                output_dir="/tmp/fixed-output",
                max_pages=100,
                show_progress=False,
                max_run_restarts=2,
                restart_delay_seconds=1.5,
            )

        self.assertEqual(output_dir, "/tmp/fixed-output")
        self.assertEqual(summaries, [recovered_summary])
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(run_mock.call_args_list[0].kwargs["output_dir"], "/tmp/fixed-output")
        self.assertEqual(run_mock.call_args_list[1].kwargs["output_dir"], "/tmp/fixed-output")
        sleep_mock.assert_called_once_with(1.5)

    def test_run_with_auto_restart_reuses_generated_output_dir(self):
        recovered_summary = {
            "stock_code": "000009",
            "target_years": [2023],
            "scanned_pages": 1,
            "matched_rows": 1,
            "matched_years": [2023],
            "invalid_timestamp_count": 0,
            "failed_pages": [],
            "stop_reason": "exhausted_pages",
            "output_file": "unused.csv",
        }

        with mock.patch("missing_year_backfill.datetime") as datetime_mock, mock.patch(
            "missing_year_backfill.run_backfill",
            side_effect=[
                RuntimeError("invalid session id"),
                (os.path.join(DEFAULT_OUTPUT_BASE_DIR, "20260406_120000"), [recovered_summary]),
            ],
        ) as run_mock, mock.patch("missing_year_backfill.time.sleep"):
            datetime_mock.now.return_value.strftime.return_value = "20260406_120000"

            output_dir, summaries = run_with_auto_restart(
                missing_years_file="missing.csv",
                selected_stocks=None,
                output_dir=None,
                max_pages=None,
                show_progress=False,
                max_run_restarts=2,
                restart_delay_seconds=0.1,
            )

        expected_output_dir = os.path.join(DEFAULT_OUTPUT_BASE_DIR, "20260406_120000")
        self.assertEqual(output_dir, expected_output_dir)
        self.assertEqual(summaries, [recovered_summary])
        self.assertEqual(run_mock.call_args_list[0].kwargs["output_dir"], expected_output_dir)
        self.assertEqual(run_mock.call_args_list[1].kwargs["output_dir"], expected_output_dir)

    def test_run_with_auto_restart_does_not_retry_non_browser_errors(self):
        with mock.patch(
            "missing_year_backfill.run_backfill",
            side_effect=ValueError("缺失年份清单中没有可处理的股票"),
        ) as run_mock, mock.patch("missing_year_backfill.time.sleep") as sleep_mock:
            with self.assertRaisesRegex(ValueError, "缺失年份清单"):
                run_with_auto_restart(
                    missing_years_file="missing.csv",
                    output_dir="/tmp/fixed-output",
                    show_progress=False,
                    max_run_restarts=3,
                    restart_delay_seconds=1.0,
                )

        run_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_run_backfill_resumes_and_skips_completed_stocks(self):
        existing_summary = {
            "stock_code": "000009",
            "target_years": [2008, 2023],
            "scanned_pages": 10,
            "matched_rows": 20,
            "matched_years": [2008, 2023],
            "invalid_timestamp_count": 0,
            "failed_pages": [],
            "stop_reason": "year_boundary_reached",
            "output_file": "000009.csv",
        }
        new_summary = {
            "stock_code": "000014",
            "target_years": [2008, 2023],
            "scanned_pages": 11,
            "matched_rows": 21,
            "matched_years": [2008, 2023],
            "invalid_timestamp_count": 0,
            "failed_pages": [],
            "stop_reason": "year_boundary_reached",
            "output_file": "000014.csv",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            save_summary(tmpdir, [existing_summary])

            with mock.patch(
                "missing_year_backfill.load_missing_year_targets",
                return_value={"000009": {2008, 2023}, "000014": {2008, 2023}},
            ), mock.patch(
                "missing_year_backfill.crawl_missing_year_posts",
                return_value=new_summary,
            ) as crawl_mock:
                _, summaries = run_backfill(
                    missing_years_file="unused.csv",
                    output_dir=tmpdir,
                    show_progress=False,
                )

        crawl_mock.assert_called_once()
        self.assertEqual(crawl_mock.call_args.kwargs["stock_code"], "000014")
        self.assertEqual([item["stock_code"] for item in summaries], ["000009", "000014"])


if __name__ == "__main__":
    unittest.main()
