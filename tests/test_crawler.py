"""app.core.crawler(SellerCrawler) 오케스트레이션 회귀 테스트.

requests/bs4 는 tests/__init__.py 가 필요 시 스텁을 등록하므로 PyQt6/scrapling
없이도 임포트 가능하다. fetch_seller_info 는 항상 monkeypatch 하여 실제 HTTP를
전혀 사용하지 않는다.

이 파일은 CrawlPlan 기반 API(HIGH-4)를 사용한다 — SellerCrawler.crawl() 은
list[PrescanResult] 가 아니라 확정된 CrawlPlan 을 받는다.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from app.core import crawler as crawler_module
from app.core.base import Control
from app.core.crawler import (
    OUTCOME_FAIL,
    OUTCOME_MISS,
    OUTCOME_OK,
    OUTCOME_SKIP,
    FetchResult,
    SellerCrawler,
    reconcile_leftover_checkpoints,
)
from app.core.plan import CategoryPlan, CrawlPlan, build_crawl_plan
from app.core.storage import Storage
from app.models.records import (
    STATUS_BLOCKED,
    STATUS_COLLECTABLE,
    STATUS_COMPLETED,
    PrescanResult,
)


def _rec(goodscode: str) -> dict:
    return {
        "goodscode": goodscode, "url": "u", "store_name": "S" + goodscode,
        "company_name": "C", "ceo_name": "", "email": "e@x.co", "phone": "010",
        "business_number": "1", "address": "addr", "collected_at": "t",
    }


def _plan(
    categories: list[CategoryPlan],
    storage: Storage | None = None,
    output_dir: str | None = None,
    max_items: int = 1000,
) -> CrawlPlan:
    """CrawlPlan.output_dir 는 이제 crawl() 이 storage.output_dir 와 일치하는지
    검증한다(4차 리뷰 MEDIUM) — 실제로 crawl() 을 호출하는 테스트는 반드시
    storage= 를 넘겨 output_dir 를 storage.output_dir 와 맞춰야 한다."""
    if output_dir is None:
        output_dir = str(storage.output_dir) if storage is not None else "x"
    return CrawlPlan(tuple(categories), output_dir, max_items, "testhash")


class _FastCrawlTestCase(unittest.TestCase):
    """time.sleep 을 무력화해 요청 간격(delay+jitter) 없이 즉시 실행되게 한다."""

    def setUp(self) -> None:
        patcher = patch.object(crawler_module.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class NormalRunTest(_FastCrawlTestCase):
    def test_plan_target_codes_executed_exactly(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        storage.reset_state()

        codes_a = tuple(str(1000 + i) for i in range(100))  # 이미 max_items 캡 적용된 계획
        plan = _plan([CategoryPlan("가공식품", "superdeal", codes_a, capped=True)], storage=storage)

        collected: list[dict] = []
        crawler = SellerCrawler(storage, control=Control(), on_collected=collected.append, delay=0.0)

        def fake_fetch(goodscode: str) -> FetchResult:
            n = int(goodscode)
            if n % 3 == 0:
                return FetchResult(OUTCOME_SKIP)
            if n % 7 == 0:
                return FetchResult(OUTCOME_MISS)
            return FetchResult(OUTCOME_OK, _rec(goodscode))

        crawler.fetch_seller_info = fake_fetch  # type: ignore[assignment]
        summary = crawler.crawl(plan)

        stats = summary.per_category["가공식품"]
        self.assertEqual(stats.total, 100)
        expected_ok = sum(1 for c in codes_a if int(c) % 3 != 0 and int(c) % 7 != 0)
        self.assertEqual(stats.success, expected_ok)
        self.assertEqual(summary.total_success, stats.success)
        self.assertEqual(len(collected), summary.total_success)
        self.assertIsNone(summary.error)
        self.assertFalse(summary.cancelled)

        self.assertEqual(len(summary.files), 1)
        self.assertIsNotNone(summary.all_files)
        self.assertEqual(len(storage.load_collected_ids()), stats.success)
        self.assertEqual(
            len(json.loads(summary.all_files[0].read_text(encoding="utf-8"))), summary.total_success
        )
        self.assertEqual(storage.load_partial_results("가공식품"), [])

        # M4: capped=True 카테고리는 '완료' 처리하지 않는다.
        self.assertNotIn("가공식품", storage.load_state()["completed"])

    def test_non_capped_category_marked_completed(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        storage.reset_state()
        plan = _plan([CategoryPlan("신선식품", "best", ("1", "2", "3"), capped=False)], storage=storage)

        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]
        summary = crawler.crawl(plan)

        self.assertEqual(summary.total_success, 3)
        self.assertIn("신선식품", storage.load_state()["completed"])

    def test_empty_plan_returns_empty_summary(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        summary = crawler.crawl(_plan([], storage=storage))
        self.assertIsNone(summary.error)
        self.assertEqual(summary.total_success, 0)
        self.assertEqual(summary.per_category, {})


class ConsecutiveFailAbortTest(_FastCrawlTestCase):
    """연속 실패 조기 중단 (2026-09-09 검토 반영).

    사이트 측 제한(403/429 등)이 진행 중이면 나머지 대상을 끝까지 때리지 않고
    카테고리를 중단한다 — 실패 건은 collected_ids 에 커밋되지 않으므로 나중에
    '이어서 수집'으로 재시도된다.
    """

    def _make(self, n: int):
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        storage.reset_state()
        codes = tuple(str(2000 + i) for i in range(n))
        plan = _plan(
            [CategoryPlan("테스트", "superdeal", codes, capped=False)], storage=storage
        )
        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        return storage, plan, crawler

    def test_consecutive_fails_abort_category(self) -> None:
        _storage, plan, crawler = self._make(30)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_FAIL)  # type: ignore[assignment]
        records, stats, cancelled, error = crawler.crawl_category(
            plan.categories[0], set()
        )
        self.assertEqual(stats.fail, 10)  # config.CONSECUTIVE_FAIL_ABORT
        self.assertIsNotNone(error)
        self.assertIn("연속 10회", error)
        self.assertEqual(len(records), 0)
        self.assertFalse(cancelled)

    def test_recovered_streak_does_not_abort(self) -> None:
        # 9회 실패 후 성공이 끼어들면 카운터가 리셋된다 — 중단 없이 끝까지 진행
        _storage, plan, crawler = self._make(20)
        seq = {"i": 0}

        def fake_fetch(goodscode: str) -> FetchResult:
            seq["i"] += 1
            if seq["i"] % 10 == 0:  # 10번째, 20번째만 성공
                return FetchResult(OUTCOME_OK, _rec(goodscode))
            return FetchResult(OUTCOME_FAIL)

        crawler.fetch_seller_info = fake_fetch  # type: ignore[assignment]
        records, stats, cancelled, error = crawler.crawl_category(
            plan.categories[0], set()
        )
        self.assertIsNone(error)
        self.assertEqual(stats.success, 2)
        self.assertEqual(stats.fail, 18)
        self.assertEqual(len(records), 2)


class CancelTest(_FastCrawlTestCase):
    def test_cancel_mid_category_preserves_partial_results(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        control = Control()
        crawler = SellerCrawler(storage, control=control, delay=0.0)

        seen = {"n": 0}

        def fetch_then_cancel(goodscode: str) -> FetchResult:
            seen["n"] += 1
            if seen["n"] == 10:
                control.request_cancel()
            return FetchResult(OUTCOME_OK, _rec(goodscode))

        crawler.fetch_seller_info = fetch_then_cancel  # type: ignore[assignment]
        plan = _plan([CategoryPlan("cat", "best", tuple(str(i) for i in range(100)))], storage=storage)
        summary = crawler.crawl(plan)

        self.assertTrue(summary.cancelled)
        self.assertIsNone(summary.error)
        self.assertEqual(summary.total_success, 10)  # 유실 없이 부분 결과 보존
        self.assertIsNotNone(summary.all_files)
        self.assertEqual(len(json.loads(summary.all_files[0].read_text(encoding="utf-8"))), 10)
        self.assertEqual(storage.load_state()["completed"], [])
        self.assertEqual(len(storage.load_collected_ids()), 10)


class CheckpointFailureTest(_FastCrawlTestCase):
    """HIGH-1 회귀: '결과 없는 ID' 가 생기면 안 된다.

    핵심 불변식은 "체크포인트가 실패하면 무조건 ID 도 비어 있어야 한다"가
    아니라, "ID 가 커밋됐다면 반드시 그에 대응하는 레코드가 어딘가(체크포인트
    또는 최종 파일)에 durable 하게 존재해야 한다" 이다. crawl_category 내부의
    중간 체크포인트가 실패해도, crawl() 레벨의 최종 save_results 가 성공하면
    그 시점에 다시 한번 확정 커밋되는 것은 안전하고 의도된 동작이다(§ 아래
    test_checkpoint_and_final_save_both_fail... 가 진짜 최악의 케이스를 다룬다).
    """

    def test_checkpoint_failure_recovered_by_final_save_still_commits_consistently(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()

        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]

        # 카테고리 내부 체크포인트(중간/종료 시 save_partial_results)만 강제로 실패.
        storage.save_partial_results = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[assignment]
            OSError("checkpoint write failed")
        )

        plan = _plan([CategoryPlan("cat", "best", ("1",))], storage=storage)
        summary = crawler.crawl(plan)

        # crawl_category 자체는 체크포인트 실패를 error 로 보고한다.
        self.assertIsNotNone(summary.error)
        self.assertIn("checkpoint write failed", summary.error)

        # 하지만 crawl() 레벨의 최종 save_results(패치하지 않음, 정상 동작)가
        # 성공했으므로 레코드는 유실되지 않았고, 그 시점에 ID 도 커밋되었다.
        self.assertEqual(summary.total_success, 1)
        self.assertIsNotNone(summary.all_files)
        saved = json.loads(summary.all_files[0].read_text(encoding="utf-8"))
        self.assertEqual(len(saved), 1)
        committed_ids = storage.load_collected_ids()
        self.assertEqual(committed_ids, {"1"})

        # 불변식 검증: 커밋된 모든 ID 는 실제로 최종 파일 안에 존재해야 한다
        # (ID 만 있고 데이터가 어디에도 없는 상태가 없어야 한다).
        saved_ids = {r["goodscode"] for r in saved}
        self.assertTrue(committed_ids.issubset(saved_ids))

    def test_checkpoint_and_final_save_both_fail_ids_never_committed(self) -> None:
        """체크포인트와 최종 저장이 모두 실패하면, 데이터는 메모리에만 남고
        (요약에는 포함되어 사용자가 알 수 있음) ID 는 절대 커밋되지 않는다 —
        재수집(중복)만 발생할 뿐 '유령 ID' 는 생기지 않는다."""
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]

        storage.save_partial_results = lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full"))  # type: ignore[assignment]
        storage.save_results = lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full"))  # type: ignore[assignment]

        plan = _plan([CategoryPlan("cat", "best", ("1",))], storage=storage)
        summary = crawler.crawl(plan)

        self.assertEqual(storage.load_collected_ids(), set())
        self.assertIsNotNone(summary.error)
        self.assertIn("disk full", summary.error)


class LeftoverCheckpointReconciliationTest(_FastCrawlTestCase):
    """HIGH-3 회귀: 완료됨으로 판정되어 계획 밖인 카테고리의 체크포인트도 승격되어야 한다."""

    def test_leftover_checkpoint_for_category_not_in_plan_is_promoted(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()

        # 이전 실행이 체크포인트만 남기고 죽은 상황을 시뮬레이션. 체크포인트에
        # 있는 goodscode 는 이미 ID 로도 커밋되어 있다고 가정(정상 커밋 순서라면
        # 그래야 함) — 그래서 다음 Pre-scan 에서 이 카테고리는 '완료됨'으로
        # 보이고, CrawlPlan 에는 아예 포함되지 않는다.
        leftover = [_rec("1"), _rec("2")]
        storage.save_partial_results(leftover, "완료카테")
        storage.save_collected_ids({"1", "2"})

        # 이 카테고리는 계획에 전혀 등장하지 않는다(다른 카테고리만 수집).
        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]
        plan = _plan([CategoryPlan("다른카테", "best", ("100",))], storage=storage)
        crawler.crawl(plan)

        # 체크포인트가 최종 파일로 승격되고 정리되어야 한다 — 계획에 없었더라도.
        self.assertEqual(storage.load_partial_results("완료카테"), [])
        promoted_files = list(storage.output_dir.glob("gmarket_fast_완료카테_*.json"))
        self.assertEqual(len(promoted_files), 1)
        self.assertEqual(len(json.loads(promoted_files[0].read_text(encoding="utf-8"))), 2)

    def test_leftover_checkpoint_without_committed_ids_is_reconciled(self) -> None:
        """체크포인트는 썼지만 ID 커밋 전에 죽은 극단적 크래시 창(HIGH-1 의 남은
        틈)도, crawl() 시작 시 재조정(reconcile)으로 ID 가 뒤늦게 올바르게
        커밋된다."""
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        storage.save_partial_results([_rec("1"), _rec("2")], "cat")
        # 의도적으로 collected_ids 커밋을 생략(크래시 시뮬레이션).

        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]
        plan = _plan([CategoryPlan("cat", "best", ("3",))], storage=storage)
        summary = crawler.crawl(plan)

        # 복구된 2건 + 신규 1건(3) = 3건. "1","2" 는 재조정 단계에서 ID 커밋됨.
        self.assertEqual(summary.total_success, 1)  # crawl_category 자체는 신규 1건만
        self.assertEqual(storage.load_collected_ids(), {"1", "2", "3"})


class FailClosedPromotionTest(_FastCrawlTestCase):
    """3차 리뷰 HIGH-1 회귀: 승격 실패는 fail-closed 여야 한다.

    예전 버그: 승격 실패를 로그만 남기고 계획 실행을 계속하면, 같은 카테고리를
    다시 수집할 때 save_partial_results() 가 (라벨로 고정된) 동일한 체크포인트
    경로에 새 데이터를 덮어써 미승격 상태로 남아있던 old 레코드가 영구히
    사라졌다(ID 는 이미 커밋되어 있었으므로 "유령 ID/영구 유실" 발생). 이제는
    승격이 하나라도 실패하면 crawl() 이 어떤 카테고리도 실행하지 않고 즉시
    summary.error 를 반환해야 한다.
    """

    def test_promotion_failure_blocks_new_collection_and_preserves_checkpoint(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        # 이전 실행: 체크포인트 저장 + ID 커밋까지는 성공했지만 최종
        # save_results 를 마치기 전에 죽었다고 가정(정상적인 커밋 순서라면
        # 이렇게 ID 가 먼저 있고 체크포인트가 아직 안 지워진 상태가 가능하다).
        storage.save_partial_results([_rec("old")], "cat")
        storage.save_collected_ids({"old"})

        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]

        # 승격(reconcile) 단계에서 실제로 쓰이는 promote_partial() 만 일시적으로
        # 실패하게 만든다(save_results() 는 카테고리 최종 저장/ALL 저장에만
        # 쓰이므로 여기서 패치해도 승격 경로를 가로채지 못한다).
        original_promote_partial = storage.promote_partial
        storage.promote_partial = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[assignment]
            OSError("promotion transient failure")
        )

        plan = _plan([CategoryPlan("cat", "best", ("new",))], storage=storage)
        summary = crawler.crawl(plan)

        # fail-closed: 카테고리를 단 하나도 실행하지 않았어야 한다.
        self.assertIsNotNone(summary.error)
        self.assertEqual(summary.total_success, 0)
        self.assertEqual(summary.per_category, {})

        # 체크포인트가 새 수집으로 덮어써지지 않고 원본 그대로 보존됐어야 한다
        # — 예전 버그라면 여기서 [] 또는 다른 내용으로 바뀌어 있었을 것이다.
        self.assertEqual(storage.load_partial_results("cat"), [_rec("old")])
        self.assertEqual(storage.load_collected_ids(), {"old"})

        # 디스크 문제를 해결한 뒤 재시도하면 이번엔 정상적으로 승격되고,
        # 이어서 신규 수집도 진행돼야 한다 — 유실도 중복도 없어야 한다.
        storage.promote_partial = original_promote_partial  # type: ignore[assignment]
        summary2 = crawler.crawl(plan)
        self.assertIsNone(summary2.error)
        self.assertEqual(summary2.total_success, 1)
        self.assertEqual(storage.load_partial_results("cat"), [])
        self.assertEqual(storage.load_collected_ids(), {"old", "new"})

        promoted = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        # old(승격분) + new(신규 카테고리 최종 저장) = 서로 다른 두 파일.
        # old 레코드가 정확히 한 번만 나타나야 한다(중복 승격 없음).
        old_occurrences = sum(
            1
            for p in promoted
            for r in json.loads(p.read_text(encoding="utf-8"))
            if r.get("goodscode") == "old"
        )
        self.assertEqual(old_occurrences, 1)


class PromotionIdempotencyTest(_FastCrawlTestCase):
    """3차/5차/6차 리뷰 MEDIUM/HIGH 회귀: 결과 저장 성공 후 ID 커밋이
    실패해도, 재시도가 같은 데이터를 다시 승격(중복 파일 생성)하지 않는다.

    6차 리뷰 HIGH-1 이후 순서가 다시 바뀌었다: 결과 저장 → manifest 기록 →
    ID 커밋(성공해야만) → 체크포인트 삭제. ID 커밋이 실패하면 체크포인트를
    **지우지 않는다** — 예전(5차)에는 ID 커밋 실패와 무관하게 마지막에
    무조건 정리를 시도해, 재시작 후 collected_ids.json 에도 체크포인트에도
    없는 "완전히 사라진 상태"가 될 수 있었다. 이제는 체크포인트가 남아있는
    한 다음 재시도가 (이미 승격된 파일이 디스크에 있음을 직접 확인하고)
    재승격 없이 곧장 ID 커밋만 다시 시도한다."""

    def test_checkpoint_preserved_when_id_commit_fails_then_recovers_without_duplicate(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        storage.save_partial_results([_rec("x")], "cat")

        original_save_collected_ids = storage.save_collected_ids
        storage.save_collected_ids = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[assignment]
            OSError("id commit failed")
        )

        result = reconcile_leftover_checkpoints(storage, storage.load_collected_ids())

        # 결과 저장(promote_partial) 자체는 성공했으므로 fail-closed 대상이 아니다.
        self.assertFalse(result.blocking)
        self.assertEqual(result.failed_labels, [])
        self.assertEqual(result.commit_failed_labels, ["cat"])
        promoted = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        self.assertEqual(len(promoted), 1)
        # ID 커밋이 실패했으므로 체크포인트는 지워지지 않고 보존돼야 한다
        # (6차 리뷰 HIGH-1 회귀 방지) — collected_ids.json 에도 체크포인트
        # 에도 없는 "완전히 사라진 ID" 상태를 만들지 않는다.
        self.assertEqual(storage.load_partial_results("cat"), [_rec("x")])
        self.assertEqual(storage.load_collected_ids(), set())

        # ID 커밋을 복구하고 재시도 — 이미 승격된 파일이 디스크에 있으므로
        # 재승격(중복 파일 생성) 없이 곧장 ID 커밋 + 체크포인트 정리만 한다.
        storage.save_collected_ids = original_save_collected_ids  # type: ignore[assignment]
        result2 = reconcile_leftover_checkpoints(storage, storage.load_collected_ids())
        self.assertFalse(result2.blocking)
        self.assertEqual(storage.load_collected_ids(), {"x"})
        self.assertEqual(storage.load_partial_results("cat"), [])
        promoted_after = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        self.assertEqual(len(promoted_after), 1)  # 여전히 1개 — 중복 승격 없음

        # ID 커밋을 복구하고 재조정을 다시 실행해도, 이미 지워진 체크포인트가
        # 없으므로 추가로 승격되는 파일이 없어야 한다(중복 방지 확인).
        storage2 = Storage(storage.output_dir)
        result2 = reconcile_leftover_checkpoints(storage2, storage2.load_collected_ids())
        self.assertFalse(result2.blocking)
        promoted_after = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        self.assertEqual(len(promoted_after), 1)  # 여전히 1개 — 중복 없음

    def test_checkpoint_unlink_failure_does_not_cause_duplicate_promotion_on_retry(self) -> None:
        """4차 리뷰 HIGH 회귀: clear_partial() 이 삭제 실패를 삼키지 않고
        반환하므로, 삭제가 실패해도 다음 재시도에서 같은 체크포인트를 또
        승격해도(멱등한 결정적 파일명 덕분에) 중복 결과 파일이 생기지 않아야
        한다."""
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        storage.save_partial_results([_rec("x")], "cat")

        with patch.object(Path, "unlink", side_effect=OSError("simulated unlink failure")):
            result = reconcile_leftover_checkpoints(storage, storage.load_collected_ids())

        # 결과 저장 자체는 성공했으므로 fail-closed 대상이 아니다.
        self.assertFalse(result.blocking)
        self.assertEqual(result.cleanup_failed_labels, ["cat"])
        self.assertEqual(result.promoted_ids, {"x"})
        promoted = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        self.assertEqual(len(promoted), 1)
        first_promoted_path = promoted[0]
        # 삭제가 실패했으므로 체크포인트 파일이 여전히 남아 있어야 한다.
        self.assertTrue(storage._partial_path("cat").exists())

        # 재시도(체크포인트가 여전히 존재, 이번엔 unlink 정상 동작) — 같은
        # 내용을 다시 승격해도 결정적 파일명이므로 같은 경로에 덮어쓸 뿐,
        # 새 파일이 추가로 생기지 않아야 한다.
        ids_after = storage.load_collected_ids()
        self.assertIn("x", ids_after)
        result2 = reconcile_leftover_checkpoints(storage, ids_after)
        self.assertFalse(result2.blocking)
        promoted_after = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        self.assertEqual(len(promoted_after), 1)  # 여전히 1개 — 중복 승격 없음
        self.assertEqual(promoted_after[0], first_promoted_path)  # 같은 파일에 덮어씀
        self.assertFalse(storage._partial_path("cat").exists())  # 이번엔 삭제 성공

    def test_cleanup_failure_after_normal_save_does_not_duplicate_on_next_reconcile(self) -> None:
        """5차 리뷰 HIGH-2 회귀(정확한 재현): 정상 카테고리 최종 저장
        (save_results, 타임스탬프 파일명) 후 체크포인트 삭제만 실패하면,
        다음 실행의 reconcile 이 이 체크포인트를 "아직 승격 안 됨"으로
        오인해 promote_partial(결정적 파일명)로 별도의 _recovered_hash
        파일을 또 만들어 같은 goodscode 가 두 파일에 중복 출현했다.
        mark_promoted_hash manifest 가 이를 막아야 한다."""
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]

        with patch.object(Path, "unlink", side_effect=OSError("simulated unlink failure")):
            plan = _plan([CategoryPlan("cat", "best", ("A100",))], storage=storage)
            summary = crawler.crawl(plan)

        self.assertIsNone(summary.error)
        self.assertEqual(summary.total_success, 1)
        # 체크포인트 삭제가 실패했으므로 여전히 남아있어야 한다.
        self.assertTrue(storage._partial_path("cat").exists())
        first_finals = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        self.assertEqual(len(first_finals), 1)

        # 다음 실행(unlink 정상 동작) — reconcile 이 이 체크포인트를 발견해도
        # manifest 덕분에 재승격하지 않고 정리만 해야 한다.
        collected_ids = storage.load_collected_ids()
        result = reconcile_leftover_checkpoints(storage, collected_ids)
        self.assertFalse(result.blocking)

        all_finals = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        self.assertEqual(len(all_finals), 1)  # 여전히 1개 — _recovered_hash 중복 없음

        occurrences = sum(
            1
            for p in all_finals
            for r in json.loads(p.read_text(encoding="utf-8"))
            if r.get("goodscode") == "A100"
        )
        self.assertEqual(occurrences, 1)
        self.assertFalse(storage._partial_path("cat").exists())  # 이번엔 정리 성공

    def test_manifest_entry_pointing_to_missing_file_is_not_trusted(self) -> None:
        """6차 리뷰 HIGH-2 회귀: manifest 에 해시가 있어도 참조된 파일이 실제로
        존재하지 않으면 신뢰하지 않고 다시 승격해야 한다 — 예전에는 결과 파일
        경로·존재 여부·digest 확인 없이 해시 문자열만 믿어서, 체크포인트가
        지워지고도 실제 결과는 어디에도 없는 상태(유실)가 될 수 있었다."""
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        storage.save_partial_results([_rec("A100")], "cat")

        content_hash = crawler_module._checkpoint_content_hash([_rec("A100")])
        fake_json = storage.output_dir / "does_not_exist.json"
        fake_csv = storage.output_dir / "does_not_exist.csv"
        storage.mark_promoted(content_hash, fake_json, fake_csv)  # 존재하지 않는 파일을 가리킴

        result = reconcile_leftover_checkpoints(storage, storage.load_collected_ids())

        self.assertFalse(result.blocking)
        self.assertEqual(result.failed_labels, [])
        # manifest 의 거짓 힌트는 무시되고, 실제로 promote_partial() 이 실행돼
        # 결과 파일이 진짜로 생겨야 한다 — 그렇지 않으면 데이터가 유실된다.
        real_promoted = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        self.assertEqual(len(real_promoted), 1)
        self.assertEqual(
            json.loads(real_promoted[0].read_text(encoding="utf-8"))[0]["goodscode"],
            "A100",
        )
        self.assertEqual(storage.load_collected_ids(), {"A100"})
        self.assertEqual(storage.load_partial_results("cat"), [])  # 체크포인트는 정리됨

    def test_manifest_corruption_does_not_lose_data(self) -> None:
        """6차 리뷰 HIGH-3 회귀: manifest 손상은 fail-open 이지만, 이제는
        그래도 데이터 유실로 이어지지 않는다(설계 자체가 바뀌었다) — manifest
        를 잃으면 "정상 저장으로 이미 처리됨" 힌트만 잃을 뿐, promote_partial
        자신의 결정적 경로는 여전히 직접 확인하므로 이미 승격된 파일은 절대
        사라지지 않는다. 최악의 경우 정상 저장 건이 다시 승격돼 같은
        goodscode 가 두 파일에 나타날 수는 있지만(중복), 유실은 없다."""
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()

        # 정상 저장 경로(카테고리 최종 저장)를 흉내낸다: 이미 타임스탬프
        # 파일로 저장되고 ID 도 커밋됐지만, 체크포인트 삭제만 실패해 남아있다.
        record = _rec("A100")
        storage.save_results([record], label="cat")
        storage.save_collected_ids({"A100"})
        storage.save_partial_results([record], "cat")
        # manifest 는 기록된 적이 있었지만 이제 손상됐다고 가정.
        storage.promoted_manifest_path.write_text("{not valid json", encoding="utf-8")

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = reconcile_leftover_checkpoints(storage, storage.load_collected_ids())

        # 손상되어도 차단되지 않는다(manifest 는 참고용일 뿐이므로 — HIGH-3).
        self.assertFalse(result.blocking)
        # 핵심 불변식: 데이터는 유실되지 않았다 — goodscode 는 (중복이 생기더라도)
        # 최종 파일에서 최소 1회는 반드시 발견돼야 한다.
        all_files = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        total_occurrences = sum(
            1
            for p in all_files
            for r in json.loads(p.read_text(encoding="utf-8"))
            if r.get("goodscode") == "A100"
        )
        self.assertGreaterEqual(total_occurrences, 1)
        self.assertEqual(storage.load_collected_ids(), {"A100"})


class CollectedIdsQuarantineFailureTest(_FastCrawlTestCase):
    """6차 리뷰 HIGH-4 회귀: collected_ids.json 이 손상됐고 격리(백업)조차
    실패하면(원본이 위험한 상태로 남음), 빈 집합으로 조용히 진행하지 않고
    fail-closed 로 막아야 한다 — 그렇지 않으면 ①대량 재수집이 발생하고
    ②이후 save_collected_ids() 가 백업 없는 원본을 덮어써 영구히 잃는다."""

    def test_quarantine_failure_blocks_crawl_and_preserves_original(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        storage.collected_ids_path.write_text("{broken", encoding="utf-8")

        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]
        plan = _plan([CategoryPlan("cat", "best", ("1",))], storage=storage)

        with patch.object(Path, "replace", side_effect=OSError("simulated replace failure")), \
             patch.object(Path, "write_bytes", side_effect=OSError("simulated copy failure")), \
             warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            summary = crawler.crawl(plan)

        self.assertIsNotNone(summary.error)
        self.assertEqual(summary.total_success, 0)
        self.assertEqual(summary.per_category, {})
        # 원본이 그대로 보존됐어야 한다(격리 자체가 실패했으므로 건드리지 않는다).
        self.assertTrue(storage.collected_ids_path.exists())
        self.assertEqual(storage.collected_ids_path.read_text(encoding="utf-8"), "{broken")


class ContentHashFullRecordTest(unittest.TestCase):
    """5차 리뷰 HIGH-3 회귀: 콘텐츠 해시가 goodscode 목록만 반영하면 안 된다
    — 같은 상품이라도 판매자 정보(필드 값)가 달라지면 반드시 다른 해시가
    나와야 한다. 그렇지 않으면 promote_partial() 이 서로 다른 내용을 같은
    결정적 파일에 덮어써 이전 값을 유실시킨다."""

    def test_different_field_values_produce_different_hash(self) -> None:
        before = [_rec("A100")]
        after = [dict(_rec("A100"), store_name="변경된이름")]
        self.assertNotEqual(
            crawler_module._checkpoint_content_hash(before),
            crawler_module._checkpoint_content_hash(after),
        )

    def test_same_content_different_order_produces_same_hash(self) -> None:
        """레코드 나열 순서 차이는 해시에 영향을 주면 안 된다(같은 집합)."""
        a = [_rec("1"), _rec("2")]
        b = [_rec("2"), _rec("1")]
        self.assertEqual(
            crawler_module._checkpoint_content_hash(a),
            crawler_module._checkpoint_content_hash(b),
        )

    def test_promote_partial_does_not_overwrite_different_content_with_same_goodscodes(self) -> None:
        """실제 저장 계층까지 관통하는 회귀 테스트: goodscode 는 같지만
        내용이 다른 두 체크포인트를 승격하면 서로 다른 파일에 남아야 한다."""
        storage = Storage(tempfile.mkdtemp())
        before = [dict(_rec("A100"), store_name="before")]
        after = [dict(_rec("A100"), store_name="after")]

        h_before = crawler_module._checkpoint_content_hash(before)
        h_after = crawler_module._checkpoint_content_hash(after)
        self.assertNotEqual(h_before, h_after)

        jp1, _ = storage.promote_partial(before, "cat", h_before)
        jp2, _ = storage.promote_partial(after, "cat", h_after)
        self.assertNotEqual(jp1, jp2)
        self.assertEqual(
            json.loads(jp1.read_text(encoding="utf-8"))[0]["store_name"], "before"
        )
        self.assertEqual(
            json.loads(jp2.read_text(encoding="utf-8"))[0]["store_name"], "after"
        )


class ReplanRequiredTest(_FastCrawlTestCase):
    """4차 리뷰 아키텍처 BLOCK-1 회귀: crawl() 을 UI 를 거치지 않고 직접
    호출할 때, reconcile 이 collected_ids 를 바꿨는데 전달받은 CrawlPlan 이
    그 변경 이전 스냅샷으로 만들어진 낡은 계획이라면, 그대로 실행하면 같은
    goodscode 가 승격분 + 신규 재수집분으로 두 번 저장된다. 이런 겹침이
    발견되면 replan_required=True 로 표시하고 계획을 실행하지 않아야 한다."""

    def test_direct_call_with_stale_plan_overlapping_checkpoint_is_blocked(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        # 체크포인트에 "x" 가 남아있고(아직 승격 전), 계획도 같은 "x" 를
        # target_codes 로 담고 있다 — 계획이 이 체크포인트 존재를 몰랐던
        # (reconcile 보다 먼저 만들어진) 낡은 상태를 시뮬레이션한다.
        storage.save_partial_results([_rec("x")], "cat")

        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]

        plan = _plan([CategoryPlan("cat", "best", ("x",))], storage=storage)
        summary = crawler.crawl(plan)

        self.assertTrue(summary.replan_required)
        self.assertIsNotNone(summary.error)
        self.assertEqual(summary.total_success, 0)  # 계획을 실행하지 않았다

        # 체크포인트는 reconcile 단계에서 정상적으로 승격됐어야 한다(그 자체는
        # 막지 않는다 — 막는 것은 '낡은 계획으로 재수집'뿐이다).
        self.assertIn("x", storage.load_collected_ids())
        final_files = list(storage.output_dir.glob("gmarket_fast_cat_*.json"))
        x_occurrences = sum(
            1
            for p in final_files
            for r in json.loads(p.read_text(encoding="utf-8"))
            if r.get("goodscode") == "x"
        )
        self.assertEqual(x_occurrences, 1)  # 승격분에만 존재 — 재수집으로 중복되지 않음

    def test_ui_path_reconciles_before_building_plan_so_never_stale(self) -> None:
        """main_window.on_start() 가 실제로 하는 순서(reconcile → build_crawl_plan)
        를 그대로 재현하면 replan_required 가 발생하지 않아야 한다 — 정상 UI
        경로의 안전성을 회귀 테스트로 고정한다."""
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        storage.save_partial_results([_rec("x")], "cat")

        collected_ids = storage.load_collected_ids()
        recon = reconcile_leftover_checkpoints(storage, collected_ids)
        self.assertFalse(recon.blocking)

        prescan = [PrescanResult("cat", "best", 2, 2, 0, ["x", "y"], STATUS_COLLECTABLE)]
        plan = build_crawl_plan(prescan, collected_ids, max_items=10, output_dir=str(storage.output_dir))
        self.assertNotIn("x", plan.categories[0].target_codes)  # 이미 승격된 "x" 는 제외됨

        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]
        summary = crawler.crawl(plan)

        self.assertFalse(summary.replan_required)
        self.assertIsNone(summary.error)
        self.assertEqual(summary.total_success, 1)  # "y" 만 신규 수집


class CorruptCheckpointFailClosedTest(_FastCrawlTestCase):
    """4차 리뷰 아키텍처 BLOCK-2 회귀: 손상되어 격리된 체크포인트를 조용히
    넘기지 않는다 — fail-closed 로 막고 on_log 로 사용자에게 알려야 한다."""

    def test_corrupt_checkpoint_blocks_and_is_logged(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        storage.reset_collected_ids()
        storage.save_collected_ids({"old"})
        # 체크포인트가 list 가 아니라 dict(문법은 유효하지만 의미적으로 손상).
        storage._partial_path("cat").write_text('{"broken": "shape"}', encoding="utf-8")

        logs: list[str] = []
        crawler = SellerCrawler(storage, control=Control(), on_log=logs.append, delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]

        plan = _plan([CategoryPlan("other", "best", ("1",))], storage=storage)
        summary = crawler.crawl(plan)

        # fail-closed: "cat" 과 무관한 다른 카테고리조차 실행되지 않는다.
        self.assertIsNotNone(summary.error)
        self.assertEqual(summary.total_success, 0)
        self.assertEqual(summary.per_category, {})

        # 예전 버그라면 summary.error=None 으로 조용히 넘어갔다 — on_log 로
        # 사용자에게 반드시 알려야 한다(경고만 발생시키고 UI 로그에는 아무것도
        # 안 남기는 것은 사용자가 절대 보지 못하는 것과 같다).
        self.assertTrue(any("손상" in m for m in logs))

        # 원본은 격리(백업)되어 보존됐어야 한다 — 조용히 버려지지 않는다.
        backups = list(storage.output_dir.glob(".partial_gmarket_fast_cat.json.corrupt_*.bak"))
        self.assertEqual(len(backups), 1)
        # collected_ids 는 손상된 체크포인트와 무관하게 그대로다.
        self.assertEqual(storage.load_collected_ids(), {"old"})


class OutputDirMismatchTest(_FastCrawlTestCase):
    """4차 리뷰 MEDIUM 회귀: CrawlPlan.output_dir 와 실제 Storage.output_dir 가
    어긋나면(프로그래밍/설정 실수), 계획 경로가 아닌 storage 경로에 조용히
    저장되지 않도록 실행을 중단해야 한다."""

    def test_mismatched_output_dir_blocks_execution(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]

        mismatched_plan = CrawlPlan(
            (CategoryPlan("cat", "best", ("1",)),),
            output_dir="/some/completely/different/path",
            max_items=10,
            plan_hash="x",
        )
        summary = crawler.crawl(mismatched_plan)

        self.assertIsNotNone(summary.error)
        self.assertEqual(summary.total_success, 0)
        self.assertEqual(storage.load_collected_ids(), set())
        self.assertEqual(list(storage.output_dir.glob("gmarket_fast_*.json")), [])


class SessionCleanupTest(_FastCrawlTestCase):
    """3차 리뷰 MEDIUM 회귀: 세션 정리(close)는 최외곽 finally 에 있어야 한다.

    예전에는 crawl() 본문 끝에서만 self.close() 를 호출했다 — 그 앞의 예외
    처리 코드(collected_ids 저장 등) 자체가 실패해 예외가 다시 밖으로
    전파되면 close() 가 아예 실행되지 않아 requests.Session 이 새어나갔다.
    """

    def test_session_closed_even_when_unhandled_exception_escapes(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        crawler = SellerCrawler(storage, control=Control(), delay=0.0)

        close_calls = {"n": 0}
        original_close = crawler.close

        def spy_close() -> None:
            close_calls["n"] += 1
            original_close()

        crawler.close = spy_close  # type: ignore[assignment]

        # crawl() 의 try 블록 최초 진입점에서부터 실패시켜, 어떤 내부 except 도
        # 잡지 못하는 완전히 처리되지 않은 예외를 재현한다.
        storage.load_collected_ids_status = lambda: (_ for _ in ()).throw(  # type: ignore[assignment]
            RuntimeError("disk unreadable")
        )

        plan = _plan([], storage=storage)
        with self.assertRaises(RuntimeError):
            crawler.crawl(plan)

        self.assertEqual(close_calls["n"], 1)


class BuildCrawlPlanTest(unittest.TestCase):
    """HIGH-4 회귀: 계획은 확정 시점 스냅샷으로 고정되고, 이후 실행에서 재계산되지 않는다."""

    def test_no_duplicate_goodscode_reservation_across_categories(self) -> None:
        """3차 리뷰 HIGH-2 회귀: 같은 goodscode 가 여러 카테고리 리스팅에 함께
        노출되면(교차 태깅), 먼저 나온 카테고리만 그 코드를 가져가야 한다 —
        그렇지 않으면 같은 건이 두 번 수집되어 ALL 통합 파일에 중복 행이 남는다."""
        prescan = [
            PrescanResult("A", "best", 2, 2, 0, ["1", "2"], STATUS_COLLECTABLE),
            PrescanResult("B", "best", 2, 2, 0, ["1", "3"], STATUS_COLLECTABLE),
        ]
        plan = build_crawl_plan(prescan, collected_ids=set(), max_items=100, output_dir="out")

        all_codes = [c for cat in plan.categories for c in cat.target_codes]
        self.assertEqual(len(all_codes), len(set(all_codes)), "카테고리 간 goodscode 중복")

        cat_a = next(c for c in plan.categories if c.category_name == "A")
        cat_b = next(c for c in plan.categories if c.category_name == "B")
        self.assertIn("1", cat_a.target_codes)
        self.assertNotIn("1", cat_b.target_codes)  # A 가 먼저 예약했으므로 B 에서 제외
        self.assertIn("3", cat_b.target_codes)

    def test_reservation_respects_max_items_cap_not_full_candidate_list(self) -> None:
        """max_items 캡으로 잘려나간 코드는 예약되지 않아 다른 카테고리가
        가져갈 수 있어야 한다 — 캡을 넘어간 코드까지 예약해버리면 그 코드는
        이번 실행에서 어느 카테고리에서도 수집되지 않고 그냥 누락된다."""
        prescan = [
            PrescanResult("A", "best", 3, 3, 0, ["1", "2", "3"], STATUS_COLLECTABLE),
            PrescanResult("B", "best", 1, 1, 0, ["3"], STATUS_COLLECTABLE),
        ]
        plan = build_crawl_plan(prescan, collected_ids=set(), max_items=2, output_dir="out")
        cat_a = next(c for c in plan.categories if c.category_name == "A")
        cat_b = next(c for c in plan.categories if c.category_name == "B")
        self.assertEqual(cat_a.target_codes, ("1", "2"))  # "3" 은 캡에 밀려 제외
        self.assertTrue(cat_a.capped)
        self.assertEqual(cat_b.target_codes, ("3",))  # A 가 예약하지 않았으므로 B 가 가져감

    def test_excludes_already_collected_and_caps_at_max_items(self) -> None:
        prescan = [
            PrescanResult("가공식품", "superdeal", 10, 10, 0,
                          [str(i) for i in range(10)], STATUS_COLLECTABLE),
        ]
        plan = build_crawl_plan(prescan, collected_ids={"0", "1"}, max_items=3, output_dir="out")
        self.assertEqual(len(plan.categories), 1)
        cat = plan.categories[0]
        self.assertEqual(len(cat.target_codes), 3)
        self.assertNotIn("0", cat.target_codes)
        self.assertNotIn("1", cat.target_codes)
        self.assertTrue(cat.capped)  # 10-2=8개 신규 중 3개만 담김 -> 캡 적용됨
        self.assertEqual(plan.total_targets, 3)

    def test_not_capped_when_max_items_covers_all_new_codes(self) -> None:
        prescan = [PrescanResult("cat", "best", 3, 3, 0, ["1", "2", "3"], STATUS_COLLECTABLE)]
        plan = build_crawl_plan(prescan, collected_ids=set(), max_items=100, output_dir="out")
        self.assertFalse(plan.categories[0].capped)

    def test_excludes_non_collectable_statuses(self) -> None:
        prescan = [
            PrescanResult("완료", "best", 5, 0, 5, [], STATUS_COMPLETED),
            PrescanResult("차단", "best", 0, 0, 0, [], STATUS_BLOCKED),
        ]
        plan = build_crawl_plan(prescan, collected_ids=set(), max_items=10, output_dir="out")
        self.assertEqual(plan.categories, ())
        self.assertEqual(plan.total_targets, 0)

    def test_plan_frozen_against_later_collected_ids_changes(self) -> None:
        """계획 생성 후 collected_ids 를 아무리 바꿔도 이미 만들어진 CrawlPlan
        객체의 target_codes 는 절대 바뀌지 않는다(불변 dataclass)."""
        prescan = [PrescanResult("cat", "best", 2, 2, 0, ["1", "2"], STATUS_COLLECTABLE)]
        collected = set()
        plan = build_crawl_plan(prescan, collected, max_items=10, output_dir="out")
        self.assertEqual(plan.categories[0].target_codes, ("1", "2"))

        collected.add("1")  # 원본 set 을 나중에 바꿔도(다른 코드가 실수로 공유해도)
        self.assertEqual(plan.categories[0].target_codes, ("1", "2"))  # 계획은 불변

        with self.assertRaises(AttributeError):
            plan.categories[0].target_codes = ("x",)  # frozen dataclass

    def test_plan_hash_deterministic_and_sensitive_to_content(self) -> None:
        prescan = [PrescanResult("cat", "best", 2, 2, 0, ["1", "2"], STATUS_COLLECTABLE)]
        plan_a = build_crawl_plan(prescan, set(), max_items=10, output_dir="out")
        plan_b = build_crawl_plan(prescan, set(), max_items=10, output_dir="out")
        self.assertEqual(plan_a.plan_hash, plan_b.plan_hash)

        plan_c = build_crawl_plan(prescan, set(), max_items=1, output_dir="out")
        self.assertNotEqual(plan_a.plan_hash, plan_c.plan_hash)


class FinalSaveFailureTest(_FastCrawlTestCase):
    """MEDIUM-1 회귀: 전체 통합(ALL) 파일 저장 실패가 summary 반환 자체를 막으면 안 된다."""

    def test_all_file_save_failure_still_returns_summary_with_error(self) -> None:
        storage = Storage(tempfile.mkdtemp())
        crawler = SellerCrawler(storage, control=Control(), delay=0.0)
        crawler.fetch_seller_info = lambda gc: FetchResult(OUTCOME_OK, _rec(gc))  # type: ignore[assignment]

        original_save_results = storage.save_results
        call_count = {"n": 0}

        def flaky_save_results(records, label=""):
            call_count["n"] += 1
            if label == "ALL":
                raise OSError("disk full on ALL save")
            return original_save_results(records, label)

        storage.save_results = flaky_save_results  # type: ignore[assignment]

        plan = _plan([CategoryPlan("cat", "best", ("1", "2"))], storage=storage)
        summary = crawler.crawl(plan)

        # 카테고리별 저장은 성공했으므로 total_success 는 정상 반영.
        self.assertEqual(summary.total_success, 2)
        # ALL 저장 실패가 error 로 기록되되, summary 자체(그리고 category 파일들)는 살아있다.
        self.assertIsNotNone(summary.error)
        self.assertIn("disk full on ALL save", summary.error)
        self.assertIsNone(summary.all_files)
        self.assertEqual(len(summary.files), 1)  # 카테고리별 파일은 정상 저장됨


if __name__ == "__main__":
    unittest.main()
