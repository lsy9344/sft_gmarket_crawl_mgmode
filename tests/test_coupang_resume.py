"""Coupang 카테고리 수집 중단 지점 저장·재개 테스트 (WORK_ORDER_CATEGORY_RESUME §5).

실제 Coupang·Decodo 연결 없이 가짜 페이지/가짜 크롤러로 검증한다:
- §5.1  페이지 1~3 저장 후 4페이지 차단 → 새 회선 시도에서 1페이지부터 다시
        읽지 않고 3페이지 경계를 확인한 뒤 4페이지부터 수집, 상품 중복 없이 합침
- §5.2  저장된 상태는 앱 재시작(새 ResumeStore 객체) 뒤에도 유지된다
- §5.3  빈 페이지 판정은 새 회선에서 다시 확인 — 2회 연속이 새 세션에서
        확정되기 전에는 목록 끝이 아니다
- §5.4  판매자 단계 실패 → 목록·매핑을 다시 읽지 않고 확인된 판매자만 건너뛰고
        미확인 판매자를 재처리, 오류 판매자는 완료 집계에 들어가지 않음
- §5.6  재개 뒤 누락 증명이 없으면(목록 변동·미확인 판매자) '완료'가 아니라
        일부 수집으로 표시
- 설정 불일치·처음부터 다시 수집(아카이브)·판매자 단계 차단의 회선 교체 금지

세션 구성은 실제 Decodo 경로와 같게 한다: 목록은 프록시 세션, 판매자 API 는
회선 IP 세션(_switch_to_direct_session) — 브라우저 풀로 2회 생성을 흉낸다.
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.core.base import Control
from app.core.coupang import blockguard
from app.core.coupang.crawler import COUPANG_HOME
from app.core.coupang.decodo_run import (
    category_run_dir,
    run_category_attempts,
    should_keep_attempt,
    should_retry,
)
from app.core.coupang.outcome import RunOutcome, determine_outcome
from app.core.coupang.resume_store import (
    ResumeStore,
    ResumeStoreError,
    peek_resume,
)
from app.core.coupang.search_crawler import SearchRunConfig
from app.core.coupang.search_parser import SearchProduct
from app.core.decodo import DecodoSettings
from app.models.coupang_records import CoupangRunSummary
from tests.test_coupang_search import (
    _BLOCKED_HTML,
    _OK_HTML,
    FakeBrowser,
    FakeSearchPage,
    FastSearchCrawler,
    _individual_response,
    _review_response,
    _row,
)

CATEGORY_ID = "195"
_ALL_VIIDS = ["11", "22", "33", "44", "55", "66"]
_PROXY = {
    "server": "http://gate.decodo.com:7000",
    "username": "user-test-session-x",
    "password": "pw",
}


def _prod(viid, title="상품"):
    """저장소 직접 호출용 SearchProduct (파이프라인과 동일한 타입)."""
    return SearchProduct(item_id=f"1{viid}", vendor_item_id=viid,
                         title=title, price=100)


class SeqPage(FakeSearchPage):
    """페이지(비-홈 goto)마다 다른 DOM 행·HTML을 순서대로 반환하는 fake page.

    rows_seq: PLP 페이지 로드 순서대로 소비되는 DOM 추출 결과 목록.
    html_seq: 비-홈 goto 순서대로 반환할 HTML (부족하면 content_html).
    """

    def __init__(self, rows_seq=(), html_seq=(), viids=None, menu_has_link=False,
                 content_html=_OK_HTML, review_ok=True, review_fail_from=None):
        super().__init__(sorter_rows=list(rows_seq), viids=list(viids or []),
                         menu_has_link=menu_has_link, content_html=content_html)
        self._html_seq = list(html_seq)
        self.review_calls = []
        self.review_ok = review_ok
        self.review_fail_from = review_fail_from  # 1-based — 이 순번부터 403
        self.individual_calls = 0

    def content(self):
        if self.url == COUPANG_HOME:
            return self._home_html
        idx = sum(1 for u in self.goto_urls if u != COUPANG_HOME) - 1
        if 0 <= idx < len(self._html_seq):
            return self._html_seq[idx]
        return self._content_html

    def evaluate(self, script, *args):
        if "individualInfo" in script:
            self.individual_calls += 1
            return _individual_response(self._viids)
        if "getStoreReview" in script:
            vid = args[0] if args else "?"
            self.review_calls.append(str(vid))
            if self.review_ok and (
                self.review_fail_from is None
                or len(self.review_calls) < self.review_fail_from
            ):
                return _review_response()
            return {"status": 403, "body": "Access Denied"}
        return super().evaluate(script, *args)


class PartialMapPage(SeqPage):
    """개별정보(individualInfo) 첫 호출을 실패시키고, 이후엔 요청 배치 분만 반환.

    실제 API처럼 요청된 viid 에 해당하는 판매자만 응답한다 — 배치 실패가
    다음 배치의 전체 응답으로 흡수되는 것을 막는다(검토 2026-09-16).
    """

    def __init__(self, *args, individual_fail_first=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.individual_fail_first = individual_fail_first

    def evaluate(self, script, *args):
        if "individualInfo" in script:
            self.individual_calls += 1
            if self.individual_fail_first and self.individual_calls == 1:
                return {"status": 500, "body": "err"}
            req = args[0] if args else {}
            batch = list(req.get("viids", [])) if isinstance(req, dict) else []
            return _individual_response(batch)
        return super().evaluate(script, *args)


class SharedSellerPage(SeqPage):
    """여러 상품이 같은 판매자를 가리키는 individualInfo 응답을 만든다."""

    def __init__(self, seller_by_viid, *args, drop_viids=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.seller_by_viid = dict(seller_by_viid)
        self.drop_viids = set(drop_viids)

    def evaluate(self, script, *args):
        if "individualInfo" in script:
            self.individual_calls += 1
            request = args[0] if args else {}
            viids = list(request.get("viids", [])) if isinstance(request, dict) else []
            products = []
            for viid in viids:
                viid = str(viid)
                if viid in self.drop_viids:
                    continue
                seller = self.seller_by_viid[viid]
                products.append({
                    "productId": f"P{viid}",
                    "itemId": f"I{viid}",
                    "vendorItemId": viid,
                    "storeInfoArea": {
                        "vendorId": seller,
                        "storeId": 109671,
                        "displayName": f"스토어{seller}",
                    },
                })
            return {
                "status": 200,
                "body": json.dumps({"code": 200, "data": {"products": products}}),
            }
        return super().evaluate(script, *args)


def _run_category(tmp_dir, pages, store=None, max_pages=6, proxy=_PROXY,
                  batch_size=10):
    """pages: 브라우저 풀 — 목록(1차 세션) → 판매자(2차 세션) 순서로 소비된다."""
    if isinstance(pages, SeqPage):
        pages = [pages]
    pool = [FakeBrowser(p) for p in pages]
    made = []

    def factory():
        browser = pool.pop(0) if pool else FakeBrowser(pages[-1])
        made.append(browser)
        return browser

    config = SearchRunConfig(
        output_dir=Path(tmp_dir),
        output_prefix="resume_test",
        keyword="",
        category_name="냉동과일",
        category_id=CATEGORY_ID,
        max_pages=max_pages,
        batch_size=batch_size,
        warmup_time=0,
        page_delay_min=0,
        page_delay_max=0,
        delay_min=0,
        delay_max=0,
        category_cooldown_min=0,
        category_cooldown_max=0,
        proxy=proxy,
        resume_store=store,
    )
    crawler = FastSearchCrawler(
        config=config,
        control=Control(),
        browser_factory=factory,
    )
    return crawler.run()


def _store_for(tmp_dir, max_pages=200):
    store = ResumeStore(Path(tmp_dir) / "resume.sqlite3")
    store.open()
    store.check_config(CATEGORY_ID, True, max_pages)
    return store


def _expire_cooldown(tmp_dir):
    """판매자 단계 차단의 12시간 쿨다운 경과를 시뮬레이션한다."""
    blockguard.block_state_path(Path(tmp_dir)).unlink(missing_ok=True)


class ListingResumeTest(unittest.TestCase):
    """§5.1 — 목록 단계 차단 뒤 저장된 경계부터 이어서 수집."""

    def test_block_mid_listing_resumes_from_boundary(self):
        rows_p1 = [_row("11"), _row("22")]
        rows_p2 = [_row("33")]
        rows_p3 = [_row("44")]
        with tempfile.TemporaryDirectory() as tmp:
            # 시도 1: 페이지 1~3 저장 후 4페이지에서 차단 (프록시 회선 거부)
            page1 = SeqPage(
                rows_seq=[rows_p1, rows_p2, rows_p3],
                html_seq=[_OK_HTML, _OK_HTML, _OK_HTML, _BLOCKED_HTML],
                viids=_ALL_VIIDS,
            )
            store = _store_for(tmp)
            summary1 = _run_category(tmp, page1, store, max_pages=6)
            self.assertEqual(summary1.termination_reason, "blocked")
            self.assertEqual(store.last_completed_page, 3)
            self.assertEqual(store.product_count, 4)  # 4페이지는 미완료 — 저장 없음
            plp1 = [u.split("page=")[-1]
                    for u in page1.goto_urls if "/np/categories/" in u]
            self.assertEqual(plp1, ["1", "2", "3", "4"])
            store.close()

            # 시도 2(새 회선): 1~2페이지 재요청 없이 경계 3 확인 → 4부터 수집
            page2 = SeqPage(
                rows_seq=[rows_p3, [_row("44"), _row("55")], [_row("66")], [], []],
                viids=_ALL_VIIDS,
            )
            # p3 경계확인 + p4~p7 (빈 페이지 2회로 목록 끝 확정)
            seller2 = SeqPage(viids=_ALL_VIIDS, review_ok=True)
            store2 = ResumeStore(Path(tmp) / "resume.sqlite3")  # 앱 재시작 재현
            store2.open()
            self.assertEqual(store2.last_completed_page, 3)
            summary2 = _run_category(tmp, [page2, seller2], store2, max_pages=7)
            plp2 = [u.split("page=")[-1]
                    for u in page2.goto_urls if "/np/categories/" in u]
            self.assertEqual(plp2, ["3", "4", "5", "6", "7"], f"재요청 페이지: {plp2}")
            self.assertEqual(summary2.termination_reason, "success")
            self.assertEqual(summary2.products_seen, 6)  # A~F 중복 없이 합쳐짐
            self.assertEqual(len(summary2.records), 6)
            self.assertEqual(store2.status, "finished")
            # Windows: 열린 sqlite 연결이 임시 폴더 삭제를 막는다 — 반드시 닫는다.
            store2.close()

    def test_empty_end_reconfirmed_in_new_session(self):
        """§5.3 — 첫 빈 페이지 저장 뒤 세션이 바뀌면 빈 페이지를 다시 확인한다."""
        rows_p1 = [_row("11")]
        with tempfile.TemporaryDirectory() as tmp:
            page1 = SeqPage(
                rows_seq=[rows_p1, [_row("22")], []],  # p1·p2 상품 / p3 빈
                html_seq=[_OK_HTML, _OK_HTML, _OK_HTML, _BLOCKED_HTML],
                viids=_ALL_VIIDS,
            )
            store = _store_for(tmp)
            _run_category(tmp, page1, store, max_pages=6)
            self.assertEqual(store.last_completed_page, 3)
            self.assertEqual(store.empty_streak, 1)
            store.close()

            # 새 회선: 경계 p2 재확인 → p3 빈(재확인) → p4 빈 → 2회 확정 종료
            page2 = SeqPage(rows_seq=[[_row("22")], [], []], viids=_ALL_VIIDS)
            seller2 = SeqPage(viids=_ALL_VIIDS, review_ok=True)
            store2 = ResumeStore(Path(tmp) / "resume.sqlite3")
            store2.open()
            summary = _run_category(tmp, [page2, seller2], store2, max_pages=6)
            self.assertEqual(summary.termination_reason, "success")
            self.assertEqual(summary.products_seen, 2)
            self.assertEqual(store2.status, "finished")
            plp2 = [u.split("page=")[-1]
                    for u in page2.goto_urls if "/np/categories/" in u]
            self.assertEqual(plp2, ["2", "3", "4"])
            # Windows: 열린 sqlite 연결이 임시 폴더 삭제를 막는다 — 반드시 닫는다.
            store2.close()

    def test_listing_shift_on_boundary_marks_partial(self):
        """§5.6 — 경계 페이지에서 목록 변동이 관측되면 '완료'가 아니다."""
        rows_p1 = [_row("11"), _row("22")]
        with tempfile.TemporaryDirectory() as tmp:
            page1 = SeqPage(
                rows_seq=[rows_p1],
                html_seq=[_OK_HTML, _BLOCKED_HTML],
                viids=_ALL_VIIDS,
            )
            store = _store_for(tmp)
            _run_category(tmp, page1, store, max_pages=6)
            self.assertEqual(store.last_completed_page, 1)
            store.close()

            # 새 회선: 경계 p1에서 신규 상품 발견(목록 변동) → 이어서 수집은
            # 되지만 누락 증명이 없으므로 일부 수집(재개·목록 변동 미확인)
            page2 = SeqPage(
                rows_seq=[rows_p1 + [_row("33", title="신규")], [_row("44")], [], []],
                viids=_ALL_VIIDS,
            )
            seller2 = SeqPage(viids=_ALL_VIIDS, review_ok=True)
            store2 = ResumeStore(Path(tmp) / "resume.sqlite3")
            store2.open()
            summary = _run_category(tmp, [page2, seller2], store2, max_pages=6)
            self.assertEqual(summary.termination_reason, "resume_shifted")
            self.assertEqual(determine_outcome(summary), RunOutcome.PARTIAL)
            self.assertTrue(should_keep_attempt(summary))
            self.assertTrue(summary.json_path and Path(summary.json_path).exists())
            store2.close()

    def test_saved_state_survives_app_restart(self):
        """§5.2 — 저장된 상태는 새 ResumeStore 객체(앱 재시작)에서도 동일하다."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_for(tmp)
            store.record_page(1, [_prod("11")])
            store.save_mapping({"11": {"vendorId": "V11"}})
            store.record_seller_confirmed("V11", {"vendor_id": "V11", "company_name": "가"})
            store.mark_listing_done("empty")
            store.close()

            store2 = ResumeStore(Path(tmp) / "resume.sqlite3")
            store2.open()
            self.assertEqual(store2.status, "listing_done")
            self.assertEqual(store2.product_count, 1)
            self.assertEqual(store2.confirmed_seller_count, 1)
            self.assertEqual(store2.load_mapping(), {"11": {"vendorId": "V11"}})
            self.assertIn("페이지 1", store2.preview_text())
            store2.close()


class SellerResumeTest(unittest.TestCase):
    """§5.4 — 판매자 단계 실패 뒤 목록·매핑 재요청 없이 미확인 판매자만 재처리."""

    def test_seller_failure_then_resume_skips_confirmed(self):
        rows_p1 = [_row(v) for v in _ALL_VIIDS]  # 6개 상품 → 6명 판매자
        with tempfile.TemporaryDirectory() as tmp:
            # 시도 1: 목록·매핑 성공, 판매자 전원 403 → 연속 5회 한도로 차단 중단
            page1 = SeqPage(rows_seq=[rows_p1, [], []], viids=_ALL_VIIDS)
            seller1 = SeqPage(viids=_ALL_VIIDS, review_ok=False)
            store = _store_for(tmp)
            summary1 = _run_category(tmp, [page1, seller1], store, max_pages=6)
            self.assertEqual(summary1.termination_reason, "blocked")
            self.assertEqual(store.status, "listing_done")
            self.assertEqual(len(store.load_mapping()), 6)
            self.assertEqual(store.confirmed_seller_count, 0)
            self.assertEqual(summary1.business_info_success, 0)
            store.close()
            _expire_cooldown(tmp)  # 12시간 쿨다운 경과 시뮬레이션

            # 시도 2: 목록·매핑 재요청 없이 판매자 6명 모두 확인
            page2 = SeqPage(viids=_ALL_VIIDS)
            seller2 = SeqPage(viids=_ALL_VIIDS, review_ok=True)
            store2 = ResumeStore(Path(tmp) / "resume.sqlite3")
            store2.open()
            summary2 = _run_category(tmp, [page2, seller2], store2, max_pages=6)
            self.assertEqual(summary2.termination_reason, "success")
            self.assertEqual(len(summary2.records), 6)
            self.assertEqual(seller2.individual_calls, 0)  # 매핑 저장본 사용
            self.assertEqual(len(seller2.review_calls), 6)  # 판매자만 재호출
            plp2 = [u for u in page2.goto_urls if "/np/categories/" in u]
            self.assertEqual(plp2, [])  # 목록 재요청 없음
            self.assertEqual(store2.status, "finished")
            store2.close()

    def test_partial_save_when_sellers_pending(self):
        """§5.6 — 미확인 판매자가 남으면 '완료'로 표시하지 않는다."""
        rows_p1 = [_row("11"), _row("22")]
        with tempfile.TemporaryDirectory() as tmp:
            # 1번째 판매자 확인 성공, 2번째부터 403 — 2명뿐이므로 연속 한도
            # (5회)까지 가지 않고 실행이 정상 종료 경로로 진행
            page1 = SeqPage(rows_seq=[rows_p1, []], viids=["11", "22"])
            seller1 = SeqPage(viids=["11", "22"], review_ok=True, review_fail_from=2)
            store = _store_for(tmp)
            summary = _run_category(tmp, [page1, seller1], store, max_pages=6)
            self.assertEqual(store.confirmed_seller_count, 1)
            self.assertEqual(summary.termination_reason, "resume_pending")
            self.assertEqual(determine_outcome(summary), RunOutcome.PARTIAL)
            self.assertTrue(Path(summary.json_path).exists())
            saved = json.loads(Path(summary.json_path).read_text(encoding="utf-8"))
            self.assertEqual(len(saved), 1)  # 확인된 판매자만 파일로
            store.close()

            # 재시작 — 남은 1명만 처리해 완료
            page2 = SeqPage(viids=["11", "22"])
            seller2 = SeqPage(viids=["11", "22"], review_ok=True)
            store2 = ResumeStore(Path(tmp) / "resume.sqlite3")
            store2.open()
            summary2 = _run_category(tmp, [page2, seller2], store2, max_pages=6)
            self.assertEqual(summary2.termination_reason, "success")
            self.assertEqual(len(summary2.records), 2)
            self.assertEqual(seller2.review_calls, ["V22"])  # 확인된 V11 은 스킵
            self.assertEqual(store2.status, "finished")
            store2.close()


class ResumeConfigGuardTest(unittest.TestCase):
    """설정 불일치·처음부터 다시 수집·차단 회선 교체 금지."""

    def test_config_mismatch_stops_before_crawling(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = category_run_dir(Path(tmp), CATEGORY_ID, "냉동과일")
            store = ResumeStore(run_dir / "resume.sqlite3")
            store.open()
            store.check_config(CATEGORY_ID, True, 200)
            store.record_page(1, [_prod("11")])
            store.close()
            calls = {"n": 0}

            def factory(cfg):
                calls["n"] += 1
                raise AssertionError("불일치 시 크롤러가 시작되면 안 됩니다")

            # 같은 카테고리 폴더에 저장된 진행은 최대 200페이지로 쌓였는데
            # 지금은 5페이지로 축소 — 재개하지 않고 이유를 알린다
            config = SearchRunConfig(
                output_dir=Path(tmp), output_prefix="x", keyword="",
                category_name="냉동과일", category_id=CATEGORY_ID, max_pages=5,
                warmup_time=0, page_delay_min=0, page_delay_max=0,
                delay_min=0, delay_max=0,
            )
            summary = run_category_attempts(
                config, DecodoSettings(username="u", password="p"), Control(),
                crawler_factory=factory, exit_ip_fn=None, on_log=lambda m: None,
            )
            self.assertEqual(calls["n"], 0)
            self.assertIn("일치하지 않", summary.error)
            self.assertEqual(summary.termination_reason, "error")

    def test_start_fresh_archives_and_starts_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = category_run_dir(Path(tmp), CATEGORY_ID, "냉동과일")
            store = ResumeStore(run_dir / "resume.sqlite3")
            store.open()
            store.check_config(CATEGORY_ID, True, 200)
            store.record_page(1, [_prod("11")])
            store.close()

            seen = {}

            class FreshCrawler:
                def __init__(self, cfg):
                    self.cfg = cfg

                def run(self):
                    st = self.cfg.resume_store
                    seen["clean"] = not st.has_state()
                    return _ok_summary()

            config = SearchRunConfig(
                output_dir=Path(tmp), output_prefix="x", keyword="",
                category_name="냉동과일", category_id=CATEGORY_ID, max_pages=5,
                warmup_time=0, page_delay_min=0, page_delay_max=0,
                delay_min=0, delay_max=0,
            )
            summary = run_category_attempts(
                config, DecodoSettings(username="u", password="p"), Control(),
                crawler_factory=lambda cfg: FreshCrawler(cfg),
                exit_ip_fn=lambda proxy: _kr(), retry_wait=0, on_log=lambda m: None,
                start_fresh=True,
            )
            self.assertEqual(summary.termination_reason, "success")
            self.assertTrue(seen["clean"], "처음부터 실행은 빈 진행 상태로 시작해야 합니다")
            archives = list(run_dir.glob("resume_archive_*.sqlite3"))
            self.assertEqual(len(archives), 1, "기존 진행 파일은 아카이브로 보존된다")

    def test_direct_session_block_does_not_rotate_line(self):
        """§3.3 — 판매자 단계(회선 IP) 차단은 Decodo 회선 교체를 하지 않는다."""
        summary = CoupangRunSummary()
        summary.termination_reason = "blocked"
        summary.blocked_direct = True
        self.assertFalse(should_retry(summary))

        listing_block = CoupangRunSummary()
        listing_block.termination_reason = "blocked"
        self.assertTrue(should_retry(listing_block))  # 목록(프록시) 차단은 회선 교체 대상

    def test_shared_store_across_attempts(self):
        """시도(회선) 간 같은 진행 저장소가 전달되고 지점이 이어진다."""
        with tempfile.TemporaryDirectory() as tmp:
            attempts = []

            class BlockedThenDone:
                def __init__(self, cfg):
                    self.cfg = cfg

                def run(self):
                    st = self.cfg.resume_store
                    attempts.append((self.cfg.proxy["username"], st.last_completed_page))
                    if len(attempts) == 1:
                        st.record_page(1, [_prod("11")])
                        st.record_page(2, [_prod("22")])
                        return _blocked_summary()
                    return _ok_summary()

            config = SearchRunConfig(
                output_dir=Path(tmp), output_prefix="x", keyword="",
                category_name="냉동과일", category_id=CATEGORY_ID, max_pages=5,
                warmup_time=0, page_delay_min=0, page_delay_max=0,
                delay_min=0, delay_max=0,
            )
            summary = run_category_attempts(
                config, DecodoSettings(username="u", password="p"), Control(),
                crawler_factory=lambda cfg: BlockedThenDone(cfg),
                exit_ip_fn=lambda proxy: _kr(), retry_wait=0, on_log=lambda m: None,
            )
            self.assertEqual(summary.termination_reason, "success")
            self.assertEqual(len(attempts), 2)
            # 회선(세션)이 바뀌었고, 2차 시도가 1차가 저장한 페이지 2부터 이어짐
            self.assertNotEqual(attempts[0][0], attempts[1][0])
            self.assertEqual(attempts[1][1], 2)


class MappingResumeTest(unittest.TestCase):
    """매핑 배치 부분 실패 — 누락을 봉인하지 않고 재개 대상에 남긴다(§3.3·§5.6)."""

    def test_same_seller_keeps_every_product_mapping(self):
        """같은 판매자의 상품도 배치 안팎에서 모두 매핑으로 저장한다."""
        viids = ["11", "22", "33", "44"]
        seller_by_viid = {viid: "V_SHARED" for viid in viids}
        with tempfile.TemporaryDirectory() as tmp:
            page = SharedSellerPage(
                seller_by_viid,
                rows_seq=[[_row(v) for v in viids], []],
                viids=viids,
            )
            store = _store_for(tmp)
            summary = _run_category(tmp, page, store, max_pages=6, batch_size=2)

            self.assertEqual(summary.termination_reason, "success")
            self.assertEqual(summary.products_seen, len(viids))
            self.assertEqual(summary.unique_vendors, 1)
            self.assertEqual(summary.business_info_success, 1)
            self.assertEqual(set(store.load_mapping()), set(viids))
            self.assertEqual(store.status, "finished")
            store.close()

    def test_resume_fills_missing_mappings_without_refetching_known_seller(self):
        """기존 판매자 정보는 유지하고 누락 상품 매핑만 채운다."""
        viids = ["11", "22", "33", "44"]
        seller_by_viid = {viid: "V_SHARED" for viid in viids}
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_for(tmp)
            store.record_page(1, [_prod(v) for v in viids])
            store.mark_listing_done("empty")
            store.save_mapping({
                "11": {
                    "vendorId": "V_SHARED",
                    "vendorItemId": "11",
                    "productId": "P11",
                    "itemId": "I11",
                },
            })
            store.record_seller_confirmed(
                "V_SHARED",
                {"vendor_id": "V_SHARED", "store_name": "기존 판매자", "power_seller": False},
            )
            store.close()

            page = SharedSellerPage(seller_by_viid, viids=viids)
            store2 = ResumeStore(Path(tmp) / "resume.sqlite3")
            store2.open()
            summary = _run_category(tmp, page, store2, max_pages=6, batch_size=2)

            self.assertEqual(summary.termination_reason, "success")
            self.assertEqual(summary.business_info_success, 1)
            self.assertEqual(page.individual_calls, 2)
            self.assertEqual(page.review_calls, [])
            self.assertEqual(set(store2.load_mapping()), set(viids))
            self.assertEqual(store2.status, "finished")
            store2.close()

    def test_response_missing_product_stays_resumable(self):
        """API가 상품 하나를 돌려주지 않으면 완료로 봉인하지 않는다."""
        viids = ["11", "22", "33", "44"]
        seller_by_viid = {viid: "V_SHARED" for viid in viids}
        with tempfile.TemporaryDirectory() as tmp:
            page = SharedSellerPage(
                seller_by_viid,
                drop_viids=["44"],
                rows_seq=[[_row(v) for v in viids], []],
                viids=viids,
            )
            store = _store_for(tmp)
            summary = _run_category(tmp, page, store, max_pages=6, batch_size=2)

            self.assertEqual(summary.termination_reason, "mapping_pending")
            self.assertEqual(determine_outcome(summary), RunOutcome.PARTIAL)
            self.assertEqual(set(store.load_mapping()), {"11", "22", "33"})
            self.assertNotEqual(store.status, "finished")
            store.close()

    def test_mapping_batch_failure_stays_resumable(self):
        rows_p1 = [_row(v) for v in _ALL_VIIDS]  # 상품 6개 → 배치 2개(batch_size=3)
        with tempfile.TemporaryDirectory() as tmp:
            # 배치 1(11,22,33) 실패, 배치 2(44,55,66) 성공 — 매핑 절반만 완료
            page1 = PartialMapPage(rows_seq=[rows_p1, []], viids=_ALL_VIIDS,
                                   individual_fail_first=True)
            seller1 = PartialMapPage(viids=_ALL_VIIDS, review_ok=True,
                                     individual_fail_first=True)
            store = _store_for(tmp)
            summary = _run_category(tmp, [page1, seller1], store, max_pages=6,
                                    batch_size=3)
            self.assertEqual(summary.termination_reason, "mapping_pending")
            self.assertEqual(determine_outcome(summary), RunOutcome.PARTIAL)
            self.assertEqual(len(summary.records), 3)  # 매핑된 3명만 저장
            self.assertEqual(len(store.load_mapping()), 3)
            self.assertNotEqual(store.status, "finished")  # 완료 봉인 금지
            self.assertTrue(should_keep_attempt(summary))  # 회선 재시도 없이 채택
            self.assertTrue(Path(summary.json_path).exists())
            store.close()

            # 재시작 — 누락 매핑 3건·미확인 판매자 3명만 처리하고 완료
            page2 = PartialMapPage(viids=_ALL_VIIDS)
            seller2 = PartialMapPage(viids=_ALL_VIIDS, review_ok=True)
            store2 = ResumeStore(Path(tmp) / "resume.sqlite3")
            store2.open()
            summary2 = _run_category(tmp, [page2, seller2], store2, max_pages=6,
                                     batch_size=3)
            self.assertEqual(summary2.termination_reason, "success")
            self.assertEqual(len(summary2.records), 6)
            self.assertEqual(len(store2.load_mapping()), 6)
            self.assertEqual(store2.status, "finished")
            store2.close()


class InterruptionGuardTest(unittest.TestCase):
    """§5.5 보강 — 손상·쓰기 실패·취소 보존·완료 봉인 재실행(검토 2026-09-16)."""

    def test_corrupt_progress_file_stops_before_crawling(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = category_run_dir(Path(tmp), CATEGORY_ID, "냉동과일")
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "resume.sqlite3").write_bytes(b"this is not a sqlite database" * 8)
            self.assertIsNone(peek_resume(run_dir))  # 시작 전 안내는 조용히 None
            calls = {"n": 0}

            def factory(cfg):
                calls["n"] += 1
                raise AssertionError("손상된 진행 파일로 크롤러가 시작되면 안 됩니다")

            config = SearchRunConfig(
                output_dir=Path(tmp), output_prefix="x", keyword="",
                category_name="냉동과일", category_id=CATEGORY_ID, max_pages=5,
                warmup_time=0, page_delay_min=0, page_delay_max=0,
                delay_min=0, delay_max=0,
            )
            summary = run_category_attempts(
                config, DecodoSettings(username="u", password="p"), Control(),
                crawler_factory=factory, exit_ip_fn=None, on_log=lambda m: None,
            )
            self.assertEqual(calls["n"], 0)
            self.assertIn("진행 파일", summary.error)
            self.assertEqual(summary.termination_reason, "error")

    def test_store_write_failure_surfaces_as_resume_store_error(self):
        rows_p1 = [_row("11")]
        with tempfile.TemporaryDirectory() as tmp:
            page1 = SeqPage(rows_seq=[rows_p1, []], viids=["11"])
            seller1 = SeqPage(viids=["11"], review_ok=True)

            class BrokenStore:
                status = "listing"
                category_id = CATEGORY_ID

                def load_products(self):
                    return []

                def has_state(self):
                    return False

                def record_page(self, page_no, items):
                    raise ResumeStoreError("디스크 쓰기 실패 재현")

            summary = _run_category(tmp, [page1, seller1], BrokenStore(),
                                    max_pages=6)
            # 조용한 건너뛰기 금지 — 구조화된 사유로 승격돼 재시도 대상에서 뺀다
            self.assertEqual(summary.termination_reason, "resume_store_error")
            self.assertIn("ResumeStoreError", summary.error)
            self.assertFalse(should_retry(summary))

    def test_resume_store_error_stops_retry_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempts = {"n": 0}

            class StoreFailCrawler:
                def __init__(self, cfg):
                    self.cfg = cfg

                def run(self):
                    attempts["n"] += 1
                    s = CoupangRunSummary()
                    s.termination_reason = "resume_store_error"
                    s.error = "ResumeStoreError: 페이지 2 저장 실패"
                    return s

            config = SearchRunConfig(
                output_dir=Path(tmp), output_prefix="x", keyword="",
                category_name="냉동과일", category_id=CATEGORY_ID, max_pages=5,
                warmup_time=0, page_delay_min=0, page_delay_max=0,
                delay_min=0, delay_max=0,
            )
            summary = run_category_attempts(
                config, DecodoSettings(username="u", password="p"), Control(),
                crawler_factory=lambda cfg: StoreFailCrawler(cfg),
                exit_ip_fn=lambda proxy: _kr(), retry_wait=0,
                on_log=lambda m: None,
            )
            self.assertEqual(attempts["n"], 1)  # 회선을 바꿔 재시도하지 않는다
            self.assertEqual(summary.termination_reason, "resume_store_error")

    def test_finished_store_archives_and_starts_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = category_run_dir(Path(tmp), CATEGORY_ID, "냉동과일")
            store = ResumeStore(run_dir / "resume.sqlite3")
            store.open()
            store.check_config(CATEGORY_ID, True, 200)
            store.record_page(1, [_prod("11")])
            store.mark_finished()
            store.close()
            self.assertIsNone(peek_resume(run_dir))  # 완료 기록은 재개 안내 대상 아님

            seen = {}

            class CleanCrawler:
                def __init__(self, cfg):
                    self.cfg = cfg

                def run(self):
                    seen["clean"] = not self.cfg.resume_store.has_state()
                    return _ok_summary()

            config = SearchRunConfig(
                output_dir=Path(tmp), output_prefix="x", keyword="",
                category_name="냉동과일", category_id=CATEGORY_ID, max_pages=5,
                warmup_time=0, page_delay_min=0, page_delay_max=0,
                delay_min=0, delay_max=0,
            )
            summary = run_category_attempts(
                config, DecodoSettings(username="u", password="p"), Control(),
                crawler_factory=lambda cfg: CleanCrawler(cfg),
                exit_ip_fn=lambda proxy: _kr(), retry_wait=0,
                on_log=lambda m: None,
            )
            self.assertEqual(summary.termination_reason, "success")
            self.assertTrue(
                seen["clean"], "완료된 기록은 보관 뒤 새 실행으로 시작해야 합니다"
            )
            archives = list(run_dir.glob("resume_archive_*.sqlite3"))
            self.assertEqual(len(archives), 1)

    def test_cancel_preserves_progress_for_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = category_run_dir(Path(tmp), CATEGORY_ID, "냉동과일")

            class CancelledCrawler:
                def __init__(self, cfg):
                    self.cfg = cfg

                def run(self):
                    self.cfg.resume_store.record_page(1, [_prod("11")])
                    self.cfg.resume_store.record_page(2, [_prod("22")])
                    s = CoupangRunSummary()
                    s.cancelled = True
                    s.termination_reason = "cancelled"
                    return s

            config = SearchRunConfig(
                output_dir=Path(tmp), output_prefix="x", keyword="",
                category_name="냉동과일", category_id=CATEGORY_ID, max_pages=5,
                warmup_time=0, page_delay_min=0, page_delay_max=0,
                delay_min=0, delay_max=0,
            )
            summary = run_category_attempts(
                config, DecodoSettings(username="u", password="p"), Control(),
                crawler_factory=lambda cfg: CancelledCrawler(cfg),
                exit_ip_fn=lambda proxy: _kr(), retry_wait=0,
                on_log=lambda m: None,
            )
            self.assertTrue(summary.cancelled)
            note = peek_resume(run_dir)  # 취소해도 진행 기록은 보존된다
            self.assertIsNotNone(note)
            self.assertIn("페이지 2", note)
            store2 = ResumeStore(run_dir / "resume.sqlite3")
            store2.open()
            self.assertEqual(store2.last_completed_page, 2)
            store2.close()


def _kr():
    from app.core.decodo import ExitIpInfo

    return ExitIpInfo(ip="2.2.2.2", country_code="KR")


def _blocked_summary():
    s = CoupangRunSummary()
    s.termination_reason = "blocked"
    s.error = "회선 차단"
    return s


def _ok_summary():
    s = CoupangRunSummary()
    s.termination_reason = "success"
    s.products_seen = 4
    s.unique_vendors = 2
    s.business_info_success = 2
    s.records = [{"vendor_id": "V1"}, {"vendor_id": "V2"}]
    s.json_path = "/tmp/fake.json"
    return s


if __name__ == "__main__":
    unittest.main()
