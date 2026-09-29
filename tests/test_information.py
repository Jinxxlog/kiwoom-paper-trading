import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from config.settings import InformationSettings
from data.market_calendar import SEOUL_TIMEZONE
from db.database import Database
from information.scoring import classify, decayed_score
from information.service import InformationService
from screening.engine import StockUniverseService


class FakeDartClient:
    def companies(self):
        return [
            {
                "corp_code": "00126380",
                "corp_name": "삼성전자",
                "corp_eng_name": "Samsung Electronics",
                "stock_code": "005930",
                "modify_date": "20260601",
            }
        ]

    def disclosures(self, **kwargs):
        return [
            {
                "rcept_no": "202606100001",
                "rcept_dt": "20260610",
                "report_nm": "유상증자결정",
                "rm": "",
            },
            {
                "rcept_no": "202606110001",
                "rcept_dt": "20260611",
                "report_nm": "[정정] 유상증자결정",
                "rm": "",
            },
        ]


class FakeNaverClient:
    def search(self, query, **kwargs):
        return [
            {
                "title": "<b>삼성전자</b> 대규모 공급계약 체결",
                "description": "신규 수주 소식",
                "link": "https://news.example/item/1?utm_source=test",
                "originallink": "https://press.example/article/1?utm_source=test",
                "pubDate": "Mon, 15 Jun 2026 09:00:00 +0900",
            },
            {
                "title": "삼성전자 대규모 공급계약 체결!",
                "description": "같은 내용",
                "link": "https://news.example/item/2",
                "originallink": "https://press.example/article/1",
                "pubDate": "Mon, 15 Jun 2026 09:01:00 +0900",
            },
        ]


class FakeNoisyNaverClient:
    def search(self, query, **kwargs):
        return [
            {
                "title": "가평 자라섬 꽃 축제 성황",
                "description": "지역 행사 소식",
                "link": "https://news.example/noise/1",
                "originallink": "https://press.example/noise/1",
                "pubDate": "Mon, 15 Jun 2026 09:00:00 +0900",
            },
            {
                "title": "삼성전자 대규모 공급계약 체결",
                "description": "반도체 공급 확대",
                "link": "https://news.example/item/3",
                "originallink": "https://press.example/article/3",
                "pubDate": "Mon, 15 Jun 2026 09:01:00 +0900",
            },
        ]


class FakeNaverPlatformNoiseClient:
    def search(self, query, **kwargs):
        return [
            {
                "title": '"13만 명 몰렸다" 가평 자라섬 꽃 페스타 성황',
                "description": "지역 행사 소식 reporter@naver.example",
                "link": "https://news.example/noise/naver-email",
                "originallink": "https://press.example/noise/naver-email",
                "pubDate": "Wed, 17 Jun 2026 13:30:00 +0900",
            },
            {
                "title": "Registration for free summer concert opens in Seoul",
                "description": "How to apply: https://m.site.naver.com/2a3Zu",
                "link": "https://news.example/noise/naver-link",
                "originallink": "https://press.example/noise/naver-link",
                "pubDate": "Wed, 17 Jun 2026 13:36:00 +0900",
            },
            {
                "title": "Netflix drama based on popular Naver webtoon",
                "description": "popular Naver webtoon of the same name",
                "link": "https://news.example/noise/naver-webtoon",
                "originallink": "https://press.example/noise/naver-webtoon",
                "pubDate": "Wed, 17 Jun 2026 13:52:00 +0900",
            },
            {
                "title": "차이커뮤니케이션, 대학생 AI 광고 공모전 개최",
                "description": "한국광고산업협회, 네이버 등이 공식 후원사로 참여",
                "link": "https://news.example/noise/naver-sponsor",
                "originallink": "https://press.example/noise/naver-sponsor",
                "pubDate": "Wed, 17 Jun 2026 11:48:00 +0900",
            },
            {
                "title": "네이버, 광고 전문가 인증 프로그램 도입",
                "description": "네이버 광고 상품 공식 파트너사 역량 강화",
                "link": "https://news.example/item/naver-ads",
                "originallink": "https://press.example/item/naver-ads",
                "pubDate": "Wed, 17 Jun 2026 13:28:00 +0900",
            },
        ]


class FakeKakaoNoiseClient:
    def search(self, query, **kwargs):
        return [
            {
                "title": "청소년 상담센터, 카카오톡 채널 운영",
                "description": "지역 공지",
                "link": "https://news.example/noise/kakao-summary",
                "originallink": "https://press.example/noise/kakao-summary",
                "pubDate": "Wed, 17 Jun 2026 09:00:00 +0900",
            },
            {
                "title": "카카오, AI 서비스 개편 발표",
                "description": "카카오톡과 광고 사업 경쟁력 강화",
                "link": "https://news.example/item/kakao-ai",
                "originallink": "https://press.example/item/kakao-ai",
                "pubDate": "Wed, 17 Jun 2026 09:10:00 +0900",
            },
        ]


class InformationServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        StockUniverseService(self.database).upsert(
            "A005930",
            stock_name="삼성전자",
        )
        self.settings = InformationSettings(
            naver_client_id="id",
            naver_client_secret="secret",
            dart_api_key="key",
            news_half_life_hours=72,
            disclosure_half_life_hours=168,
            candidate_score_limit=20,
        )
        self.service = InformationService(
            self.database,
            self.settings,
            dart_client=FakeDartClient(),
            naver_client=FakeNaverClient(),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_classification_blocks_severe_negative_events(self):
        result = classify("횡령 발생 및 거래정지", "", source="dart")

        self.assertEqual(result.event_type, "embezzlement")
        self.assertEqual(result.base_score, -100)
        self.assertTrue(result.entry_blocked)
        self.assertEqual(result.confidence, 0.95)

    def test_time_decay_halves_score_at_half_life(self):
        published = datetime(2026, 6, 10, tzinfo=SEOUL_TIMEZONE)
        score = decayed_score(
            40,
            1,
            1,
            published,
            datetime(2026, 6, 13, tzinfo=SEOUL_TIMEZONE),
            half_life_hours=72,
        )

        self.assertAlmostEqual(score, 20)

    def test_entry_block_expires_after_event_window(self):
        self.service.sync_dart_companies()
        self.service.collect_dart(
            ["A005930"],
            begin_date=date(2026, 6, 1),
            end_date=date(2026, 6, 15),
        )

        score = self.service.scores(
            ["A005930"],
            as_of=datetime(2026, 7, 1, tzinfo=SEOUL_TIMEZONE),
        )["A005930"]

        self.assertFalse(score["entry_blocked"])

    def test_naver_url_and_similar_title_duplicates_are_collapsed(self):
        collected_at = datetime(2026, 6, 15, 10, tzinfo=SEOUL_TIMEZONE)
        with patch("information.service._now", return_value=collected_at):
            result = self.service.collect_naver(["A005930"])

        self.assertEqual(result, {"saved": 1, "duplicates": 1, "ignored": 0})
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT duplicate_count, first_collected_at, available_at "
                "FROM information_items WHERE source = 'naver'"
            ).fetchone()
        self.assertEqual(row["duplicate_count"], 1)
        self.assertEqual(row["first_collected_at"], collected_at.isoformat(timespec="seconds"))
        self.assertEqual(row["available_at"], collected_at.isoformat(timespec="seconds"))

    def test_same_source_item_id_for_another_stock_is_duplicate(self):
        StockUniverseService(self.database).upsert("A000660", stock_name="SK하이닉스")
        classification = classify("반도체 공급계약 체결", "", source="naver")

        first = self.service._save_item(
            source="naver",
            source_item_id="same-news-id",
            stock_code="A005930",
            corp_code=None,
            title="삼성전자 반도체 공급계약 체결",
            summary="",
            url="https://news.example/same",
            original_url="https://press.example/same",
            normalized="삼성전자 반도체 공급계약 체결",
            logical_key="same",
            published_at=datetime(2026, 6, 15, 9, tzinfo=SEOUL_TIMEZONE),
            available_at=datetime(2026, 6, 15, 9, tzinfo=SEOUL_TIMEZONE),
            classification=classification,
            raw={"id": "same-news-id"},
        )
        second = self.service._save_item(
            source="naver",
            source_item_id="same-news-id",
            stock_code="A000660",
            corp_code=None,
            title="SK하이닉스 반도체 공급계약 체결",
            summary="",
            url="https://news.example/same",
            original_url="https://press.example/same",
            normalized="SK하이닉스 반도체 공급계약 체결",
            logical_key="same",
            published_at=datetime(2026, 6, 15, 9, tzinfo=SEOUL_TIMEZONE),
            available_at=datetime(2026, 6, 15, 9, tzinfo=SEOUL_TIMEZONE),
            classification=classification,
            raw={"id": "same-news-id"},
        )

        self.assertTrue(first)
        self.assertFalse(second)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, MAX(duplicate_count) AS duplicates "
                "FROM information_items WHERE source = 'naver'"
            ).fetchone()
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["duplicates"], 1)

    def test_naver_noise_without_stock_name_is_ignored(self):
        service = InformationService(
            self.database,
            self.settings,
            dart_client=FakeDartClient(),
            naver_client=FakeNoisyNaverClient(),
        )

        result = service.collect_naver(["A005930"])

        self.assertEqual(result, {"saved": 1, "duplicates": 0, "ignored": 1})
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT title FROM information_items WHERE source = 'naver'"
            ).fetchall()
        self.assertEqual([row["title"] for row in rows], ["삼성전자 대규모 공급계약 체결"])

    def test_naver_platform_reference_noise_is_ignored_for_naver_stock(self):
        StockUniverseService(self.database).upsert("A035420", stock_name="NAVER")
        service = InformationService(
            self.database,
            self.settings,
            dart_client=FakeDartClient(),
            naver_client=FakeNaverPlatformNoiseClient(),
        )

        result = service.collect_naver(["A035420"])

        self.assertEqual(result, {"saved": 1, "duplicates": 0, "ignored": 4})
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT title FROM information_items "
                "WHERE source = 'naver' AND stock_code = 'A035420'"
            ).fetchall()
        self.assertEqual(
            [row["title"] for row in rows],
            ["네이버, 광고 전문가 인증 프로그램 도입"],
        )

    def test_ambiguous_stock_name_requires_title_match(self):
        StockUniverseService(self.database).upsert("A035720", stock_name="카카오")
        service = InformationService(
            self.database,
            self.settings,
            dart_client=FakeDartClient(),
            naver_client=FakeKakaoNoiseClient(),
        )

        result = service.collect_naver(["A035720"])

        self.assertEqual(result, {"saved": 1, "duplicates": 0, "ignored": 1})
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT title FROM information_items "
                "WHERE source = 'naver' AND stock_code = 'A035720'"
            ).fetchall()
        self.assertEqual([row["title"] for row in rows], ["카카오, AI 서비스 개편 발표"])

    def test_corrected_disclosure_uses_latest_logical_item_only(self):
        self.service.sync_dart_companies()
        self.service.collect_dart(
            ["A005930"],
            begin_date=date(2026, 6, 1),
            end_date=date(2026, 6, 15),
        )

        score = self.service.scores(
            ["A005930"],
            as_of=datetime(2026, 6, 12, tzinfo=SEOUL_TIMEZONE),
        )["A005930"]

        self.assertEqual(score["item_count"], 1)
        self.assertTrue(score["entry_blocked"])
        self.assertEqual(score["details"][0]["title"], "[정정] 유상증자결정")

    def test_as_of_excludes_information_not_yet_available(self):
        collected_at = datetime(2026, 6, 15, 10, tzinfo=SEOUL_TIMEZONE)
        with patch("information.service._now", return_value=collected_at):
            self.service.collect_naver(["A005930"])

        before = self.service.scores(
            ["A005930"],
            as_of=datetime(2026, 6, 15, 9, 30, tzinfo=SEOUL_TIMEZONE),
        )["A005930"]
        after = self.service.scores(
            ["A005930"],
            as_of=datetime(2026, 6, 15, 10, 1, tzinfo=SEOUL_TIMEZONE),
        )["A005930"]

        self.assertEqual(before["item_count"], 0)
        self.assertEqual(after["item_count"], 1)
        self.assertGreater(after["total_score"], 0)


if __name__ == "__main__":
    unittest.main()
