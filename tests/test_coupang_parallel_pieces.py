"""볼륨 인지 조각 분할(작업 단위 whole/pages/sellers) — 매니저 통합 검증.

PARALLEL_VOLUME_AWARE_SPLIT_DESIGN_20261006 §8: 혼합 큐 배출 규칙(목록 완료
전 판매자 조각 미배출·완료 후 즉시 배출), 목록 조각 세션(페이지 범위 준수),
판매자 슬라이스 세션(서로소 매핑), plan_version 상태 재개(v2 왕복·구버전
이전 규약 재개). 가짜 브라우저 팩토리는 test_coupang_parallel_manager 의
패턴을 그대로 쓴다(네트워크 없음).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.base import Control  # noqa: E402
from app.core.coupang import parallel_manager as pm  # noqa: E402
from app.core.coupang.parallel_manager import (  # noqa: E402
    STATE_FILENAME,
    ParallelCoupangManager,
    ParallelRunConfig,
    split_family_into_shards,
)
from app.core.coupang.parallel_merge import MERGE_STATE_FILENAME  # noqa: E402
from app.core.coupang.patchright_full_fruit import (  # noqa: E402
    PRODUCT_SELLER_FIELDS,
    _read_csv,
    _write_csv,
)
from app.core.coupang.patchright_sample import (  # noqa: E402
    FETCH_STORE_REVIEW_JS,
    FETCH_VENDORS_JS,
)
from app.core.coupang.patchright_top_thousand import (  # noqa: E402
    EXTRACT_CARDS_JS,
    PRODUCT_FIELDS,
    TopThousandStore,
    _new_state,
)
from app.core.coupang.work_plan import (  # noqa: E402
    WORK_KIND_PAGES,
    WORK_KIND_SELLERS,
    WORK_KIND_WHOLE,
    WorkUnit,
    seller_slice_index,
    unit_to_dict,
)

INTERVAL_MINUTES = 90
INTERVAL_SECONDS = INTERVAL_MINUTES * 60
T0 = time.time()

_CATEGORY = ("185671", "주방용품/냄비/프라이팬")
_OTHER_CATEGORY = ("185735", "주방용품/그릇/홈세트")


# ── 가짜 브라우저(test_coupang_parallel_manager 패턴) ─────────────────


class _Response:
    status = 200


class _CategoryPage:
    """페이지별 카드 목록을 주는 가짜 목록 페이지."""

    def __init__(self, *, cards_by_page=None, default_cards=None):
        self.url = "about:blank"
        self.cards_by_page = cards_by_page or {}
        self.default_cards = default_cards or []
        self.visited_urls: list[str] = []

    def goto(self, url, **_kwargs):
        self.url = str(url)
        self.visited_urls.append(str(url))
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script != EXTRACT_CARDS_JS:
            raise AssertionError("unexpected script")
        assert int(argument) == 60
        page_number = 1
        if "?page=" in self.url:
            page_number = int(self.url.rsplit("?page=", 1)[1])
        category_id = ""
        if "/np/categories/" in self.url:
            category_id = (
                self.url.split("/np/categories/", 1)[1]
                .split("?")[0]
                .split("/")[0]
            )
        if not category_id:
            return self.default_cards
        return self.cards_by_page.get(
            (category_id, page_number), self.default_cards
        )


class _RecordingSellerPage:
    """매핑·사업자정보 API 를 흉내내며 요청된 vendor_item_id 를 기록하는 페이지."""

    def __init__(self, *, vendors_by_item=None, sellers_by_id=None):
        self.url = "about:blank"
        self.vendors_by_item = vendors_by_item or {}
        self.sellers_by_id = sellers_by_id or {}
        self.requested_item_ids: list[str] = []

    def goto(self, url, **_kwargs):
        self.url = str(url)
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        return "<html><body>normal page content</body></html>"

    def evaluate(self, script, argument=None):
        if script == FETCH_VENDORS_JS:
            products = []
            for vendor_item_id in argument:
                self.requested_item_ids.append(vendor_item_id)
                vendor_id = self.vendors_by_item.get(vendor_item_id)
                if vendor_id is None:
                    continue
                products.append(
                    {
                        "productId": 10,
                        "itemId": 20,
                        "vendorItemId": vendor_item_id,
                        "storeInfoArea": {
                            "vendorId": vendor_id,
                            "storeId": 99,
                            "displayName": "테스트스토어",
                        },
                    }
                )
            return {
                "status": 200,
                "body": json.dumps(
                    {"code": 200, "data": {"products": products}}
                ),
            }
        if script == FETCH_STORE_REVIEW_JS:
            payload = self.sellers_by_id.get(argument)
            if payload is None:
                return {"status": 200, "body": json.dumps({})}
            return {"status": 200, "body": json.dumps(payload)}
        raise AssertionError("unexpected script")


class _MixedPage:
    """목록 카드 추출·판매자 매핑 API 를 모두 처리하는 가짜 페이지.

    혼합 큐 시나리오(목록 조각 세션 → 판매자 조각 세션)에서 한 인스턴스가
    두 종류 세션을 모두 돌 때 쓴다(내구성 테스트 _MixedPage 패턴).
    """

    def __init__(self, *, cards_by_page=None, vendors_by_item=None, sellers_by_id=None):
        self.category = _CategoryPage(cards_by_page=cards_by_page)
        self.seller = _RecordingSellerPage(
            vendors_by_item=vendors_by_item, sellers_by_id=sellers_by_id
        )
        self._active = self.category

    @property
    def url(self):
        return self._active.url

    def goto(self, url, **kwargs):
        self._active.goto(url, **kwargs)
        # 홈/카테고리 주소는 목록 세션, shop 세션 주소는 판매자 세션이 쓴다.
        if "vendors" in str(url) or "shop" in str(url):
            self._active = self.seller
        return _Response()

    def wait_for_timeout(self, milliseconds):
        return self._active.wait_for_timeout(milliseconds)

    def content(self):
        return self._active.content()

    def evaluate(self, script, argument=None):
        if script == EXTRACT_CARDS_JS:
            return self.category.evaluate(script, argument)
        return self.seller.evaluate(script, argument)

    @property
    def visited_urls(self):
        return self.category.visited_urls

    @property
    def requested_item_ids(self):
        return self.seller.requested_item_ids


class _Context:
    def __init__(self, page) -> None:
        self.pages = [page]


class _RoutingFactory:
    def __init__(self, direct_page=None, decodo_page=None):
        self.direct_page = direct_page if direct_page is not None else _CategoryPage()
        self.decodo_page = decodo_page if decodo_page is not None else _CategoryPage()
        self.captured: list[tuple[Path, dict | None]] = []

    @contextmanager
    def _open(self, user_data_dir, proxy):
        self.captured.append((Path(user_data_dir), proxy))
        page = self.direct_page if proxy is None else self.decodo_page
        yield _Context(page)

    def __call__(self, user_data_dir, *, headless=False, proxy=None):
        return self._open(user_data_dir, proxy)


def _card(index: int, vendor_item_id: str) -> dict:
    return {
        "href": (
            f"/vp/products/{9000 + index}"
            f"?itemId={9100 + index}&vendorItemId={vendor_item_id}"
        ),
        "title": f"상품 {index}",
        "fullText": f"상품 {index} {index},000원 리뷰 {100 + index}",
        "imageSrcs": "",
    }


def _product_row(category: tuple[str, str], vendor_item_id: str) -> dict:
    category_id, category_name = category
    return {
        "category_id": category_id,
        "category_name": category_name,
        "page_number": "1",
        "product_id": "10",
        "item_id": "20",
        "vendor_item_id": vendor_item_id,
        "title": f"상품 {vendor_item_id}",
        "price": "1000",
        "review_count": "100",
        "delivery_markers": "",
        "url": (
            "https://www.coupang.com/vp/products/10"
            f"?itemId=20&vendorItemId={vendor_item_id}"
        ),
        "collected_at": "2026-10-06 00:00:00",
    }


def _seller_payload(vendor_id: str) -> dict:
    return {
        "name": f"주식회사{vendor_id}",
        "repPersonName": "홍길동",
        "businessNumber": "123-45-67890",
    }


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _age_guard(state_root: Path) -> None:
    """가드 장부의 마지막 실접속 시각을 2시간 전으로 되돌린다(연속 세션용)."""
    path = state_root / "canary_guard.json"
    guard = json.loads(path.read_text(encoding="utf-8"))
    guard["last_attempt_ts"] = time.time() - 2 * 60 * 60
    path.write_text(json.dumps(guard), encoding="utf-8")


def _write_piece_state(
    piece_dir: Path,
    family: list[tuple[str, str]],
    *,
    page_from: int,
    page_to: int,
    products: list[dict],
) -> None:
    """완주한 목록 조각 폴더를 만든다 — 조각 범위가 새겨진 완료 상태 + 상품."""
    piece_dir.mkdir(parents=True, exist_ok=True)
    TopThousandStore(piece_dir, family).ensure_files()
    if products:
        _write_csv(piece_dir / "top_products.csv", PRODUCT_FIELDS, products)
    state = _new_state(
        tuple(family), page_from=page_from, page_to=page_to
    )
    for record in state["categories"].values():
        record["status"] = "exhausted"
    state["status"] = "completed"
    state["category_index"] = len(family)
    (piece_dir / "top_state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )


def _specs(units: list[WorkUnit]) -> list[dict]:
    return [unit_to_dict(unit) for unit in units]


def _make_manager(
    root: Path,
    events: list,
    *,
    units: list[WorkUnit],
    root_family: list[tuple[str, str]] | None = None,
    factory=None,
    instance_count=1,
    listing_pages=2,
):
    # families 는 매번 새로 만든다 — 매니저가 배출로 config.families 를
    # 늘려도 이전 호출의 목록이 오염되지 않게(실행 중 상태 공유는 없다).
    families = [list(unit.categories) for unit in units]
    config = ParallelRunConfig(
        families=families,
        output_dir=root / "out",
        instance_count=instance_count,
        interval_minutes=INTERVAL_MINUTES,
        listing_pages=listing_pages,
        shard_mode=True,
        unit_specs=_specs(units),
        root_family=root_family,
    )
    with mock.patch.object(
        pm, "_state_root_for", lambda instance_id: root / f"state_{instance_id}"
    ):
        return ParallelCoupangManager(
            config,
            Control(),
            events.append,
            browser_scope_factory=factory,
        )


def _events_of(events: list[dict], type_name: str) -> list[dict]:
    return [event for event in events if event.get("type") == type_name]


def _logs_text(events: list[dict]) -> str:
    return " ".join(
        str(event.get("message") or "") for event in _events_of(events, "log")
    )


# ── 가짜 decodo 모듈(다중 인스턴스 구성용 — manager 테스트 패턴) ──────


def _fake_decodo(calls: list, *, ready=True):
    module = types.ModuleType("app.core.decodo")

    class DecodoError(RuntimeError):
        def __init__(self, message: str, *, kind: str = "other") -> None:
            super().__init__(message)
            self.kind = kind

    def credentials_ready(settings=None):
        calls.append(("credentials_ready",))
        return ready

    def load_settings(path=None):
        return SimpleNamespace(username="sp3lqmo64w", password="pw")

    def sticky_proxy_dict(settings=None, session_id=""):
        return {
            "server": "http://gate.decodo.com:7000",
            "username": f"user-sp3lqmo64w-session-{session_id}",
            "password": "pw",
        }

    def fetch_exit_ip(proxy, **_kwargs):
        return SimpleNamespace(
            ip="203.0.113.9", country_code="KR", country_name="Korea"
        )

    module.DecodoError = DecodoError
    module.credentials_ready = credentials_ready
    module.load_settings = load_settings
    module.sticky_proxy_dict = sticky_proxy_dict
    module.fetch_exit_ip = fetch_exit_ip
    return module


@contextmanager
def _decodo(fake):
    with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
        yield


# ── 1. 배출 규칙(§5.3, §7 위험 봉쇄) ─────────────────────────────────


class SellerEmissionRuleTest(unittest.TestCase):
    """목록 전 조각 완료 전에는 판매자 조각을 배출하지 않고, 완료 즉시 배출."""

    def _manager(self, root: Path, events: list) -> ParallelCoupangManager:
        units = [
            WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=1, page_to=2),
            WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=3, page_to=4),
        ]
        return _make_manager(
            root,
            events,
            units=units,
            root_family=[_CATEGORY, _OTHER_CATEGORY],
        )

    def test_no_emission_while_any_piece_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            manager = self._manager(root, events)
            _write_piece_state(
                root / "out" / "shard_01",
                [_CATEGORY],
                page_from=1,
                page_to=2,
                products=[_product_row(_CATEGORY, "viid-1")],
            )
            # 조각 2(shard_02)는 미시작 — 배출이 나와선 안 된다.
            self.assertEqual(manager._sweep_emit_seller_units(), 0)
            self.assertEqual(len(manager.units), 2)
            # 초기 배분 대기 큐(단위 2)는 그대로 — 배출이 새로 붙지 않았다.
            self.assertEqual(manager.pending_families, [1])

    def test_emits_immediately_once_all_pieces_complete_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            manager = self._manager(root, events)
            _write_piece_state(
                root / "out" / "shard_01",
                [_CATEGORY],
                page_from=1,
                page_to=2,
                products=[
                    _product_row(_CATEGORY, f"viid-{number}") for number in range(4)
                ],
            )
            _write_piece_state(
                root / "out" / "shard_02",
                [_CATEGORY],
                page_from=3,
                page_to=4,
                products=[
                    _product_row(_CATEGORY, f"viid-{number}")
                    for number in range(4, 6)
                ],
            )
            emitted = manager._sweep_emit_seller_units()
            # 상품 6개·세션 130 → K=1 (세션 분량보다 작은 조각은 만들지 않음).
            self.assertEqual(emitted, 1)
            self.assertEqual(len(manager.units), 3)
            seller = manager.units[2]
            self.assertEqual(seller.kind, WORK_KIND_SELLERS)
            self.assertEqual(seller.category_id, _CATEGORY[0])
            self.assertEqual(seller.slice_count, 1)
            self.assertEqual(seller.product_sources, ("shard_01", "shard_02"))
            self.assertEqual(manager.pending_families, [1, 2])
            self.assertIn("판매자 조각", _logs_text(events))
            # 상태 파일에 배출된 단위까지 남는다(재시작 복원 근거).
            saved = _read_json(root / "out" / STATE_FILENAME)
            self.assertEqual(saved["plan_version"], 2)
            self.assertEqual(len(saved["units"]), 3)
            # 멱등 — 다시 돌려도 중복 배출이 없다.
            self.assertEqual(manager._sweep_emit_seller_units(), 0)
            self.assertEqual(len(manager.units), 3)

    def test_slice_count_grows_with_volume_and_caps_at_instances(self):
        """상품이 많으면 K 가 늘고 인스턴스 수에서 막힌다(§10 권장 변형)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            units = [
                WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=1, page_to=8),
            ]
            calls: list = []
            with _decodo(_fake_decodo(calls)):
                manager = _make_manager(
                    root,
                    events,
                    units=units,
                    root_family=[_CATEGORY],
                    instance_count=3,
                )
            products = [
                _product_row(_CATEGORY, f"viid-{number}")
                for number in range(500)
            ]
            _write_piece_state(
                root / "out" / "shard_01",
                [_CATEGORY],
                page_from=1,
                page_to=8,
                products=products,
            )
            self.assertEqual(manager._sweep_emit_seller_units(), 3)  # 상한=인스턴스 수
            self.assertEqual(
                [unit.slice_index for unit in manager.units[1:]], [0, 1, 2]
            )
            self.assertTrue(
                all(unit.slice_count == 3 for unit in manager.units[1:])
            )


# ── 2. 목록 조각 세션 + 혼합 큐 완주(§5.2, §8-2) ─────────────────────


class PagesPieceSessionTest(unittest.TestCase):
    """가짜 브라우저로 목록 조각 수집 → 완주 → 판매자 조각 승계 → 병합까지."""

    def test_piece_collects_page_range_then_inherits_seller_unit_and_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            cards_by_page = {
                ("185671", 1): [_card(1, "viid-1"), _card(2, "viid-2")],
                ("185671", 2): [_card(3, "viid-3")],
                # 3쪽 이후는 조각 범위 밖 — 요청돼도 상태는 1~2쪽에서 끝난다.
                ("185671", 3): [_card(4, "viid-4")],
            }
            mixed_page = _MixedPage(
                cards_by_page=cards_by_page,
                vendors_by_item={
                    "viid-1": "A00001",
                    "viid-2": "A00002",
                    "viid-3": "A00003",
                },
                sellers_by_id={
                    vendor_id: _seller_payload(vendor_id)
                    for vendor_id in ("A00001", "A00002", "A00003")
                },
            )
            category_page = mixed_page.category
            seller_page = mixed_page.seller
            factory = _RoutingFactory(direct_page=mixed_page)
            units = [
                WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=1, page_to=2),
            ]
            manager = _make_manager(
                root,
                events,
                units=units,
                root_family=[_CATEGORY],
                factory=factory,
                instance_count=1,
                listing_pages=2,
            )
            # 세션 1 — 목록 1~2쪽. 2쪽까지 끝나면 조각 완주(범위 밖 미요청).
            self.assertTrue(manager.run_due(T0))
            category_urls = [
                url
                for url in category_page.visited_urls
                if "/np/categories/" in url
            ]
            self.assertEqual(
                category_urls,
                [
                    "https://www.coupang.com/np/categories/185671?page=1",
                    "https://www.coupang.com/np/categories/185671?page=2",
                ],
            )
            piece_state = _read_json(root / "out" / "shard_01" / "top_state.json")
            self.assertEqual(piece_state["status"], "completed")
            self.assertEqual(piece_state["page_range"], {"from": 1, "to": 2})
            # 세션 2 — 목록 완주 확인: 판매자 조각이 배출되고 이 인스턴스가
            # 곧바로 승계한다(혼합 큐 — 어떤 회선도 굶지 않는다).
            self.assertTrue(manager.run_due(T0 + INTERVAL_SECONDS))
            self.assertEqual(len(manager.units), 2)
            self.assertEqual(manager.units[1].kind, WORK_KIND_SELLERS)
            self.assertEqual(manager.instances["1"].family_index, 1)
            self.assertFalse(manager.all_done())
            # 세션 3 — 판매자 조각(상품 3개를 매핑·사업자정보 확보) 후 종료.
            # (가드 장부는 실제 시계를 쓰므로 세션 사이 시각을 되돌린다.)
            _age_guard(root / "state_1")
            self.assertTrue(manager.run_due(T0 + 2 * INTERVAL_SECONDS))
            self.assertEqual(
                sorted(seller_page.requested_item_ids),
                ["viid-1", "viid-2", "viid-3"],
            )
            # 세션 4 — 판매자 조각 완주 확인 → 남은 단위 없음 → 종료.
            _age_guard(root / "state_1")
            self.assertTrue(manager.run_due(T0 + 3 * INTERVAL_SECONDS))
            self.assertEqual(manager.instances["1"].status, "done")
            self.assertTrue(manager.all_done())
            # 병합 — 조각 폴더의 상품·판매자가 루트 완성형으로 합쳐진다.
            summary = manager.finalize()
            self.assertIsNotNone(summary)
            self.assertEqual(summary["products"], 3)
            self.assertEqual(summary["sellers"], 3)
            merged_products = _read_csv(
                root / "out" / "top_products.csv", PRODUCT_FIELDS
            )
            self.assertEqual(
                sorted(row["vendor_item_id"] for row in merged_products),
                ["viid-1", "viid-2", "viid-3"],
            )
            self.assertTrue((root / "out" / MERGE_STATE_FILENAME).exists())

    def test_incomplete_piece_resumes_from_saved_page(self):
        """조각 세션이 중간에 끊겨도 상태의 쪽 위치에서 이어서 진행한다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            cards_by_page = {
                ("185671", page): [_card(page, f"viid-{page}")]
                for page in range(1, 5)
            }
            category_page = _CategoryPage(cards_by_page=cards_by_page)
            factory = _RoutingFactory(direct_page=category_page)
            units = [
                WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=2, page_to=4),
            ]
            manager = _make_manager(
                root,
                events,
                units=units,
                root_family=[_CATEGORY],
                factory=factory,
                instance_count=1,
                listing_pages=1,  # 세션당 1쪽 — 3세션에 걸쳐 2~4쪽 진행
            )
            self.assertTrue(manager.run_due(T0))
            self.assertEqual(
                [
                    url
                    for url in category_page.visited_urls
                    if "/np/categories/" in url
                ],
                ["https://www.coupang.com/np/categories/185671?page=2"],
            )
            state = manager.instances["1"]
            self.assertEqual(state.status, "waiting")
            # 이어하기 — 저장된 3쪽부터 재개해 조각을 마저 완주한다.
            for tick in range(2):
                _age_guard(root / "state_1")
                self.assertTrue(
                    manager.run_due(T0 + (tick + 1) * INTERVAL_SECONDS)
                )
            piece_state = _read_json(root / "out" / "shard_01" / "top_state.json")
            self.assertEqual(piece_state["status"], "completed")
            self.assertEqual(
                [
                    url.rsplit("?page=", 1)[1]
                    for url in category_page.visited_urls
                    if "/np/categories/" in url
                ],
                ["2", "3", "4"],
            )


# ── 3. 판매자 슬라이스 서로소 매핑(§5.2, §8-2) ────────────────────────


class SellerSliceSessionTest(unittest.TestCase):
    """서로소 슬라이스 — 각 조각이 자기 슬라이스 상품만 매핑한다."""

    def test_two_slices_map_disjoint_products_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            item_ids = [f"viid-{number:03d}" for number in range(1, 13)]
            vendors_by_item = {
                item_id: f"A{index:05d}"
                for index, item_id in enumerate(item_ids, start=1)
            }
            seller_page = _RecordingSellerPage(
                vendors_by_item=vendors_by_item,
                sellers_by_id={
                    vendor_id: _seller_payload(vendor_id)
                    for vendor_id in vendors_by_item.values()
                },
            )
            factory = _RoutingFactory(
                direct_page=seller_page, decodo_page=seller_page
            )
            # 목록 조각(shard_01)은 이미 완주해 상품 12개가 있다고 준비.
            _write_piece_state(
                root / "out" / "shard_01",
                [_CATEGORY],
                page_from=1,
                page_to=4,
                products=[
                    _product_row(_CATEGORY, item_id) for item_id in item_ids
                ],
            )
            units = [
                WorkUnit(
                    WORK_KIND_SELLERS,
                    (_CATEGORY,),
                    slice_index=0,
                    slice_count=2,
                    product_sources=("shard_01",),
                ),
                WorkUnit(
                    WORK_KIND_SELLERS,
                    (_CATEGORY,),
                    slice_index=1,
                    slice_count=2,
                    product_sources=("shard_01",),
                ),
            ]
            calls: list = []
            with _decodo(_fake_decodo(calls)):
                manager = _make_manager(
                    root,
                    events,
                    units=units,
                    root_family=[_CATEGORY],
                    factory=factory,
                    instance_count=2,
                )
                # 두 인스턴스가 각자 슬라이스를 한 세션에 완주한다.
                self.assertTrue(manager.run_due(T0))
                # 다음 예약에서 완주 확인(complete 판정) → 종료.
                self.assertTrue(manager.run_due(T0 + INTERVAL_SECONDS))
            self.assertTrue(manager.all_done())
            requested = seller_page.requested_item_ids
            expected_even = {
                item_id
                for item_id in item_ids
                if seller_slice_index(item_id, 2) == 0
            }
            expected_odd = set(item_ids) - expected_even
            self.assertEqual(set(requested), set(item_ids))  # 빠짐없이
            # 조각 폴더별 매핑은 서로소 — 같은 상품을 두 조각이 요청하지 않는다.
            slice0_items = {
                row["vendor_item_id"]
                for row in _read_csv(
                    root / "out" / "shard_01" / "top_product_seller.csv",
                    PRODUCT_SELLER_FIELDS,
                )
            }
            slice1_items = {
                row["vendor_item_id"]
                for row in _read_csv(
                    root / "out" / "shard_02" / "top_product_seller.csv",
                    PRODUCT_SELLER_FIELDS,
                )
            }
            self.assertEqual(slice0_items, expected_even)
            self.assertEqual(slice1_items, expected_odd)
            self.assertFalse(slice0_items & slice1_items)
            # 병합 — 전 상품 매핑·판매자가 루트에 모인다.
            summary = manager.finalize()
            self.assertIsNotNone(summary)
            self.assertEqual(summary["products"], 12)
            self.assertEqual(summary["sellers"], 12)
            root_mappings = _read_csv(
                root / "out" / "top_product_seller.csv", PRODUCT_SELLER_FIELDS
            )
            self.assertEqual(len(root_mappings), 12)

    def test_empty_slice_completes_without_browser(self):
        """슬라이스에 상품이 없으면 브라우저 없이 즉시 완주한다(빈 조각)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            # 상품이 전부 슬라이스 0에 속하도록 1개만 준비.
            _write_piece_state(
                root / "out" / "shard_01",
                [_CATEGORY],
                page_from=1,
                page_to=2,
                products=[_product_row(_CATEGORY, "viid-1")],
            )
            occupied = seller_slice_index("viid-1", 2)
            units = [
                WorkUnit(
                    WORK_KIND_SELLERS,
                    (_CATEGORY,),
                    slice_index=1 - occupied,  # viid-1 이 속하지 않은 빈 슬라이스
                    slice_count=2,
                    product_sources=("shard_01",),
                ),
            ]
            manager = _make_manager(
                root,
                events,
                units=units,
                root_family=[_CATEGORY],
                factory=factory,
                instance_count=1,
            )
            self.assertTrue(manager.run_due(T0))
            self.assertEqual(len(factory.captured), 0)  # 브라우저 미기동
            self.assertEqual(manager.instances["1"].status, "done")
            self.assertTrue(manager.all_done())


# ── 4. plan_version 상태 재개(§5.5, §8-1) ────────────────────────────


class PlanVersionStateTest(unittest.TestCase):
    """v2 왕복 복원·구버전 상태는 이전 규약(통짜 샤드)으로 재개."""

    def test_v2_state_roundtrip_restores_emitted_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            units = [
                WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=1, page_to=2),
                WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=3, page_to=4),
            ]
            manager = _make_manager(
                root,
                events,
                units=units,
                root_family=[_CATEGORY, _OTHER_CATEGORY],
                instance_count=1,
            )
            # 두 조각 모두 완주 + 배출(상품 2개 → 판매자 조각 1개).
            for name, page_from, page_to in (
                ("shard_01", 1, 2),
                ("shard_02", 3, 4),
            ):
                _write_piece_state(
                    root / "out" / name,
                    [_CATEGORY],
                    page_from=page_from,
                    page_to=page_to,
                    products=[_product_row(_CATEGORY, f"viid-{page_from}")],
                )
            manager._sweep_emit_seller_units()
            self.assertEqual(len(manager.units), 3)

            # 같은 출력 폴더로 재시작 — 배출된 판매자 조각까지 그대로 복원.
            restarted_events: list = []
            restarted = _make_manager(
                root,
                restarted_events,
                units=units,
                root_family=[_CATEGORY, _OTHER_CATEGORY],
                instance_count=1,
            )
            self.assertEqual(restarted.plan_version, 2)
            self.assertEqual(len(restarted.units), 3)
            self.assertEqual(restarted.units[2].kind, WORK_KIND_SELLERS)
            self.assertEqual(
                restarted.units[2].product_sources, ("shard_01", "shard_02")
            )
            self.assertEqual(len(restarted.config.families), 3)
            # 복원 직후 배출 스윕을 돌려도 중복 배출이 없다(멱등).
            self.assertEqual(restarted._sweep_emit_seller_units(), 0)
            self.assertEqual(len(restarted.units), 3)

    def test_legacy_state_resumes_with_whole_shard_protocol(self):
        """오늘 형식(version 1, plan_version 없음) 상태는 통짜 샤드로 재개."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            root_family = [_CATEGORY, _OTHER_CATEGORY, ("185872", "밀폐저장")]
            # 구버전 상태 파일 — 카테고리 3개를 2샤드 라운드로빈으로 배분했던 흔적.
            legacy_families = split_family_into_shards(root_family, 2)
            (root / "out").mkdir(parents=True, exist_ok=True)
            (root / "out" / STATE_FILENAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "updated_at": "2026-10-06 18:00:00",
                        "interval_minutes": INTERVAL_MINUTES,
                        "shard_mode": True,
                        "family_count": len(legacy_families),
                        "pending_families": [],
                        "instances": [
                            {
                                "instance_id": "1",
                                "name": "인스턴스 1",
                                "line": "direct",
                                "session_id": "",
                                "state_root": str(root / "state_1"),
                                "output_root": str(root / "out"),
                                "status": "waiting",
                                "family_index": 0,
                                "next_run_at": 0.0,
                                "total_products": 0,
                                "total_sellers": 0,
                                "effective_sid": "",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            # 새 계획(unit_specs — 조각 포함)으로 시작해도 구버전 상태면
            # 이전 규약 분할로 되돌려 재개한다(§5.5 같은 폴더 이어받기).
            new_units = [
                WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=1, page_to=2),
                WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=3, page_to=4),
                WorkUnit(
                    WORK_KIND_WHOLE, (_OTHER_CATEGORY, ("185872", "밀폐저장"))
                ),
            ]
            manager = _make_manager(
                root,
                events,
                units=new_units,
                root_family=root_family,
                instance_count=1,
            )
            self.assertEqual(manager.plan_version, 1)
            self.assertEqual(manager.config.families, legacy_families)
            self.assertIsNone(manager.config.unit_specs)
            self.assertTrue(
                all(unit.kind == WORK_KIND_WHOLE for unit in manager.units)
            )
            self.assertEqual(manager.instances["1"].family_index, 0)
            self.assertIn("구버전", _logs_text(events))
            self.assertIn("이전 규약", _logs_text(events))

    def test_legacy_state_without_root_family_replans_fresh(self):
        """root_family 없이는 구버전 분할을 재구성할 수 없다 — 새 배분 폴백."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            (root / "out").mkdir(parents=True, exist_ok=True)
            (root / "out" / STATE_FILENAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "interval_minutes": INTERVAL_MINUTES,
                        "shard_mode": True,
                        "family_count": 2,
                        "pending_families": [],
                        "instances": [
                            {
                                "instance_id": "1",
                                "name": "인스턴스 1",
                                "line": "direct",
                                "session_id": "",
                                "state_root": str(root / "state_1"),
                                "output_root": str(root / "out"),
                                "status": "waiting",
                                "family_index": 0,
                                "next_run_at": 0.0,
                                "total_products": 0,
                                "total_sellers": 0,
                                "effective_sid": "",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            units = [
                WorkUnit(WORK_KIND_PAGES, (_CATEGORY,), page_from=1, page_to=2),
                WorkUnit(WORK_KIND_PAGES, (_OTHER_CATEGORY,), 1, 2),
            ]
            manager = _make_manager(
                root, events, units=units, instance_count=1
            )
            # 새 배분(v2) — 폴백 후에도 조각 규약으로 새로 시작한다.
            self.assertEqual(manager.plan_version, 2)
            self.assertEqual(
                [unit.kind for unit in manager.units],
                [WORK_KIND_PAGES, WORK_KIND_PAGES],
            )
            self.assertEqual(manager.instances["1"].family_index, 0)
            self.assertEqual(manager.pending_families, [1])


if __name__ == "__main__":
    unittest.main()
