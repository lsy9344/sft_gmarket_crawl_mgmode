"""쿠팡 병렬 인스턴스 매니저(parallel_manager) — 네트워크 없음.

M2 검증: 인스턴스 구성(강등 포함)·가족 배분/승계·run_due 스케줄·프록시
경로(direct/decodo 회전)·차단 격리·가드 사전 점검·상태 영속/복원·완주
경로·구성 검증. 브라우저는 M1 러너 테스트(test_coupang_patchright_top_*)
의 가짜 팩토리 패턴을 그대로 쓰고, app.core.decodo 는 sys.modules 가짜
모듈로 교체한다(test_coupang_parallel_pipeline.py 패턴).

가드 소유권(모듈 docstring 참조): 매니저의 사전 점검은 장부 사본으로
판정만 하고 실장부 claim/settle 은 러너가 한다 — 아래
GuardProbeTest 가 이를 고정한다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core.base import Control
from app.core.coupang import parallel_manager as pm
from app.core.coupang.parallel_manager import (
    ParallelCoupangManager,
    ParallelRunConfig,
    STATE_FILENAME,
    build_instances,
)
from app.core.coupang.parallel_pipeline import (
    PROXY_STATE_FILENAME,
    RUN_LOG_FILENAME,
)
from app.core.coupang.patchright_canary import claim_live_attempt
from app.core.coupang.patchright_sample import (
    FETCH_STORE_REVIEW_JS,
    FETCH_VENDORS_JS,
)
from app.core.coupang.patchright_top_sellers import TopSellerStore
from app.core.coupang.patchright_top_thousand import (
    EXTRACT_CARDS_JS,
    PRODUCT_FIELDS,
    TopThousandStore,
)
from app.core.coupang.patchright_full_fruit import (
    PRODUCT_SELLER_FIELDS,
    SELLER_FIELDS,
    _write_csv,
)

INTERVAL_MINUTES = 90  # 세션 간격 — 가드 최소 간격(60분)보다 길게
INTERVAL_SECONDS = INTERVAL_MINUTES * 60


def _t0() -> float:
    """세션 테스트 기준 시각 — 러너의 실접속 기록(time.time)과 같은 시계.

    run_top_pages/run_top_seller_batch 의 가드 claim 은 내부에서
    time.time() 을 쓰므로, 매니저 테스트의 가상 시각도 실제 현재 시각
    기준으로 잡아야 사전 점검(프로브)과 러너 claim 이 같은 판정을 낸다.
    """
    return time.time()


T0 = _t0()  # 모듈 로드 시각 — 러너 가드 기록(실시간)과 같은 시계 기준


def _age_guard(state_root: Path) -> None:
    """가드 장부의 마지막 실접속 시각을 2시간 전으로 되돌린다.

    한 테스트 안에서 같은 인스턴스의 세션을 두 번 돌릴 때 쓰는 M1 러너
    테스트 패턴(test_coupang_patchright_top_thousand._age_last_attempt).
    """
    path = state_root / "canary_guard.json"
    guard = json.loads(path.read_text(encoding="utf-8"))
    guard["last_attempt_ts"] = time.time() - 2 * 60 * 60
    path.write_text(json.dumps(guard), encoding="utf-8")

# 주방용품 계열 가족 풀 — 기본 A 가족(TOP_CATEGORIES)과 겹치지 않아
# 가족 정의 파일 경로(루트별 final 생략)도 함께 검증된다.
_CATEGORY_POOL = [
    ("185671", "주방용품/냄비/프라이팬"),
    ("185735", "주방용품/그릇/홈세트"),
    ("185872", "주방용품/밀폐저장/도시락"),
    ("186176", "주방용품/주방소형/조리도구"),
    ("186250", "주방용품/주방소형/도마"),
    ("186320", "주방용품/주방소형/커터"),
]


def _families(count: int) -> list[list[tuple[str, str]]]:
    """가족 count개 — 각각 카테고리 1개짜리 소형 가족(트리 선택 순서 고정)."""
    return [[_CATEGORY_POOL[index]] for index in range(count)]


# ── 가짜 decodo 모듈 ──────────────────────────────────────────────────


def _fake_decodo(calls: list, *, ready=True, dead_sids=None):
    """parallel_manager 가 늦게 import 하는 app.core.decodo 가짜 대체품.

    calls 에 (함수, 인자) 기록을 남긴다. dead_sids 는 sid → raise 할 예외
    사전(생성 후에 항목을 채울 수 있게 참조를 그대로 쓴다). 해당 sid 는
    죽은 회선(세션 수준 오류)으로 응답해 회전을 유발한다.
    sticky_proxy_dict 사용자명은 M1 테스트와 같은 축약형이라 sid 추출이
    가능하다(user-<id>-session-<sid>).
    """
    module = types.ModuleType("app.core.decodo")

    class DecodoError(RuntimeError):
        def __init__(self, message: str, *, kind: str = "other") -> None:
            super().__init__(message)
            self.kind = kind

    dead = dead_sids if dead_sids is not None else {}

    def credentials_ready(settings=None):
        calls.append(("credentials_ready",))
        return ready

    def load_settings(path=None):
        calls.append(("load_settings", path))
        return SimpleNamespace(username="sp3lqmo64w", password="pw")

    def sticky_proxy_dict(settings=None, session_id=""):
        calls.append(("sticky_proxy_dict", session_id))
        return {
            "server": "http://gate.decodo.com:7000",
            "username": f"user-sp3lqmo64w-session-{session_id}",
            "password": "pw",
        }

    def fetch_exit_ip(proxy, **_kwargs):
        sid = str(proxy.get("username", "")).split("-session-")[-1]
        calls.append(("fetch_exit_ip", sid))
        failure = dead.get(sid)
        if failure is not None:
            raise failure
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
    """가짜 decodo 모듈을 잠시 심는다(구성·회전 모두 오프라인)."""
    with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
        yield


# ── 가짜 브라우저 ─────────────────────────────────────────────────────


class _Response:
    status = 200


class _CategoryPage:
    """페이지별 카드 목록을 주는 가짜 목록 페이지(M1 패턴 이식)."""

    def __init__(self, *, cards_by_page=None, default_cards=None, blocked_urls=()):
        self.url = "about:blank"
        self.cards_by_page = cards_by_page or {}
        self.default_cards = default_cards or []
        self.blocked_urls = set(blocked_urls)

    def goto(self, url, **_kwargs):
        self.url = str(url)
        return _Response()

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        if self.url in self.blocked_urls:
            return "Access Denied Reference #18.full"
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


class _SellerPage:
    """판매자 매핑·사업자정보 API 를 흉내내는 가짜 페이지(M1 패턴 이식)."""

    def __init__(self, *, vendors_by_item=None, sellers_by_id=None):
        self.url = "about:blank"
        self.vendors_by_item = vendors_by_item or {}
        self.sellers_by_id = sellers_by_id or {}

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


class _Context:
    def __init__(self, page) -> None:
        self.pages = [page]


class _RoutingFactory:
    """프록시 종류로 페이지를 고르고 (프로필, proxy) 인자를 기록하는 팩토리.

    direct 인스턴스는 proxy=None, decodo 인스턴스는 proxy dict 로 호출되므로
    한 번의 run_due 에서 인스턴스별로 다른 페이지를 내줄 수 있다.
    """

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


# ── 저장소 준비 헬퍼 ──────────────────────────────────────────────────


def _product_row(family: list[tuple[str, str]], vendor_item_id="viid-1") -> dict:
    category_id, category_name = family[0]
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
        "collected_at": "2026-10-01 00:00:00",
    }


def _prepare_complete_family(
    output_root: Path, family_index: int, family: list[tuple[str, str]]
) -> Path:
    """완주한 가족 출력 폴더를 만든다(상품 1·판매자 매핑 완료·상태 completed)."""
    output_dir = output_root / f"family_{family_index + 1:02d}"
    output_dir.mkdir(parents=True, exist_ok=True)
    TopThousandStore(output_dir, family).ensure_files()
    TopSellerStore(output_dir).ensure_files()
    _write_csv(
        output_dir / "top_products.csv", PRODUCT_FIELDS, [_product_row(family)]
    )
    _write_csv(
        output_dir / "top_product_seller.csv",
        PRODUCT_SELLER_FIELDS,
        [
            {
                "product_id": "10",
                "item_id": "20",
                "vendor_item_id": "viid-1",
                "vendor_id": "A00001",
                "mapped_at": "2026-10-01 00:00:00",
            }
        ],
    )
    seller_row = {field: "" for field in SELLER_FIELDS}
    seller_row.update(
        status="saved",
        vendor_id="A00001",
        url="https://www.coupang.com/vendors/A00001",
        store_name="테스트스토어",
        company_name="주식회사테스트",
        ceo_name="홍길동",
        business_number="123-45-67890",
        rating_count="4321",
        thumb_up_ratio="95",
        updated_at="2026-10-01 00:00:00",
    )
    _write_csv(output_dir / "top_sellers.csv", SELLER_FIELDS, [seller_row])
    state = {
        "version": 1,
        "status": "completed",
        "category_index": len(family),
        "page_number": 1,
        "consecutive_empty_pages": 0,
        "categories": {
            category_id: {
                "category_name": name,
                "status": "exhausted",
                "raw_seen": 60,
                "rocket_seen": 40,
                "jet_seen": 0,
                "tp_collected": 20,
                "pages_scanned": 2,
            }
            for category_id, name in family
        },
    }
    (output_dir / "top_state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    return output_dir


def _prepare_products_only_family(
    output_root: Path, family_index: int, family: list[tuple[str, str]]
) -> Path:
    """상품만 저장된 가족 폴더 — 다음 작업이 판매자 단계가 된다."""
    output_dir = output_root / f"family_{family_index + 1:02d}"
    output_dir.mkdir(parents=True, exist_ok=True)
    TopThousandStore(output_dir, family).ensure_files()
    TopSellerStore(output_dir).ensure_files()
    _write_csv(
        output_dir / "top_products.csv", PRODUCT_FIELDS, [_product_row(family)]
    )
    return output_dir


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _run_lines(output_dir: Path) -> list[dict]:
    path = output_dir / RUN_LOG_FILENAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _events_of(events: list[dict], type_name: str) -> list[dict]:
    return [event for event in events if event.get("type") == type_name]


def _make_manager(
    root: Path,
    families: list[list[tuple[str, str]]],
    events: list,
    *,
    factory=None,
    instance_count=1,
    control=None,
    interval_minutes=INTERVAL_MINUTES,
    listing_pages=1,
):
    """매니저 생성 — 인스턴스 state_root 를 테스트 폴더로 돌린다."""
    config = ParallelRunConfig(
        families=families,
        output_dir=root / "out",
        instance_count=instance_count,
        interval_minutes=interval_minutes,
        listing_pages=listing_pages,
    )
    with mock.patch.object(
        pm, "_state_root_for", lambda instance_id: root / f"state_{instance_id}"
    ):
        return ParallelCoupangManager(
            config,
            control if control is not None else Control(),
            events.append,
            browser_scope_factory=factory,
        )


# ── 1. 인스턴스 구성 ─────────────────────────────────────────────────


class BuildInstancesTest(unittest.TestCase):
    """정적 팩토리 build_instances — 1직접+N decodo, 자격 미비 강등."""

    def test_three_instances_are_one_direct_and_two_decodo(self):
        calls: list = []
        fake = _fake_decodo(calls, ready=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ParallelRunConfig(
                families=_families(3), output_dir=root / "out", instance_count=3
            )
            with mock.patch.object(
                pm,
                "_state_root_for",
                lambda instance_id: root / f"state_{instance_id}",
            ):
                with _decodo(fake):
                    instances = build_instances(config)
            self.assertEqual(
                [instance.instance_id for instance in instances],
                ["1", "2", "3"],
            )
            self.assertEqual(
                [instance.name for instance in instances],
                ["인스턴스 1", "인스턴스 2", "인스턴스 3"],
            )
            self.assertEqual(
                [instance.line for instance in instances],
                ["direct", "decodo", "decodo"],
            )
            # decodo 시작 sid 는 인스턴스 번호 기본값(예: "i2").
            self.assertEqual(
                [instance.session_id for instance in instances],
                ["", "i2", "i3"],
            )
            # state_root 는 인스턴스별 분리, 출력 루트는 실행 설정 공용.
            self.assertEqual(
                [instance.state_root for instance in instances],
                [root / "state_1", root / "state_2", root / "state_3"],
            )
            self.assertEqual(
                {instance.output_root for instance in instances},
                {root / "out"},
            )

    def test_decodo_not_ready_downgrades_to_single_direct(self):
        calls: list = []
        fake = _fake_decodo(calls, ready=False)
        events: list = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ParallelRunConfig(
                families=_families(3), output_dir=root / "out", instance_count=3
            )
            with mock.patch.object(
                pm,
                "_state_root_for",
                lambda instance_id: root / f"state_{instance_id}",
            ):
                with _decodo(fake):
                    instances = build_instances(config, on_event=events.append)
            # 1개(직접)로 강등 — decodo 인스턴스는 아예 만들지 않는다.
            self.assertEqual(len(instances), 1)
            self.assertEqual(instances[0].line, "direct")
            degraded = _events_of(events, "degraded")
            self.assertEqual(len(degraded), 1)
            self.assertIn("강등", degraded[0]["message"])

    def test_single_instance_never_checks_decodo(self):
        """인스턴스 1개 요청은 decodo 자격과 무관하게 직접 회선 1개다."""
        calls: list = []
        fake = _fake_decodo(calls, ready=False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ParallelRunConfig(
                families=_families(1), output_dir=root / "out", instance_count=1
            )
            with _decodo(fake):
                instances = build_instances(config)
            self.assertEqual(len(instances), 1)
            self.assertEqual(instances[0].line, "direct")
            # 자격 점검을 아예 호출하지 않는다(불필요한 경고 없음).
            self.assertNotIn(("credentials_ready",), calls)

    def test_state_root_follows_platform_layout(self):
        """Windows 는 %LOCALAPPDATA% 산하, 그 외는 PROJECT_ROOT 산하."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_os = SimpleNamespace(
                name="nt", environ={"LOCALAPPDATA": str(root / "local")}
            )
            config = ParallelRunConfig(
                families=_families(1), output_dir=root / "out", instance_count=1
            )
            with mock.patch.object(pm, "os", fake_os):
                windows = build_instances(config)[0].state_root
            self.assertEqual(
                windows,
                root / "local" / "SellerCollector" / "coupang_parallel" / "1",
            )
            # 비Windows — 실제 모듈 상수 기반 경로(cdnary state_dir 패턴).
            # Windows 빌드 머신에서도 posix 분기를 강제로 검증한다.
            with mock.patch("os.name", "posix"):
                linux = pm._state_root_for("2")
            self.assertEqual(
                linux,
                pm.PROJECT_ROOT
                / "runtime_profile"
                / "coupang_parallel"
                / "2",
            )


# ── 2. 가족 배분·승계 ────────────────────────────────────────────────


class FamilyPlanningTest(unittest.TestCase):
    """초기 배분(앞에서부터 1개씩)·완주 승계·큐 소진 시 done."""

    def test_initial_distribution_and_waiting_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            calls: list = []
            fake = _fake_decodo(calls, ready=True)
            with _decodo(fake):
                manager = _make_manager(
                    root, _families(5), events, instance_count=3
                )
            # 인스턴스 수만큼 앞에서부터 1개씩, 남은 가족은 대기 큐.
            self.assertEqual(
                [manager.instances[i].family_index for i in ("1", "2", "3")],
                [0, 1, 2],
            )
            self.assertEqual(manager.pending_families, [3, 4])
            # 배분 결과는 즉시 영속된다(재시작 재개 대비).
            saved = _read_json(root / "out" / STATE_FILENAME)
            self.assertEqual(saved["pending_families"], [3, 4])
            self.assertEqual(
                [record["family_index"] for record in saved["instances"]],
                [0, 1, 2],
            )

    def test_completion_inherits_next_family_then_done(self):
        """완주 → 대기 가족 승계 → 큐 소진 → done(1인스턴스·직접 회선)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            families = _families(2)
            _prepare_complete_family(root / "out", 0, families[0])
            _prepare_complete_family(root / "out", 1, families[1])
            manager = _make_manager(root, families, events, instance_count=1)
            self.assertTrue(manager.run_due(T0))
            state = manager.instances["1"]
            # 가족 1 완주 → 대기 큐의 가족 2 승계, 다음 세션 예약.
            self.assertEqual(state.family_index, 1)
            self.assertEqual(state.status, "waiting")
            self.assertEqual(state.next_run_at, T0 + INTERVAL_SECONDS)
            inherited = _events_of(events, "complete")
            self.assertEqual(len(inherited), 1)
            self.assertEqual(inherited[0]["family_index"], 0)
            self.assertEqual(inherited[0]["inherited_family"], 1)
            # 승계 직후 예약 시각 전에는 새 세션을 돌리지 않는다.
            self.assertFalse(manager.run_due(T0 + INTERVAL_SECONDS - 1))
            self.assertFalse(manager.all_done())
            # 예약 시각 도래 — 가족 2도 완주, 큐가 비었으므로 done.
            self.assertTrue(manager.run_due(T0 + INTERVAL_SECONDS))
            self.assertEqual(manager.instances["1"].status, "done")
            self.assertTrue(manager.all_done())
            self.assertEqual(len(_events_of(events, "complete")), 2)
            self.assertEqual(len(_events_of(events, "all_done")), 1)
            # 가족별 실행 로그에 완주 기록이 남는다.
            self.assertEqual(
                [
                    line["event"]
                    for line in _run_lines(root / "out" / "family_01")
                ],
                ["family_complete"],
            )
            self.assertEqual(
                [
                    line["event"]
                    for line in _run_lines(root / "out" / "family_02")
                ],
                ["family_complete"],
            )


# ── 3. run_due 스케줄 ────────────────────────────────────────────────


class RunDueSchedulingTest(unittest.TestCase):
    """도래한 인스턴스만 실행, 간격 미달 스킵, 실행 후 재예약."""

    def test_due_instance_runs_once_and_reschedules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            manager = _make_manager(
                root, _families(1), events, factory=factory, instance_count=1
            )
            self.assertTrue(manager.run_due(T0))
            self.assertEqual(len(factory.captured), 1)
            state = manager.instances["1"]
            self.assertEqual(state.status, "waiting")
            self.assertEqual(state.next_run_at, T0 + INTERVAL_SECONDS)
            # 간격 미달 — 같은 인스턴스는 다시 실행하지 않는다.
            self.assertFalse(manager.run_due(T0 + INTERVAL_SECONDS - 1))
            self.assertEqual(len(factory.captured), 1)
            # 예약 시각 도래 — 세션이 다시 실행된다(러너 가드 간격은 이미
            # 지난 것으로 되돌린다 — 테스트가 실시간으로 기다리지 않게).
            _age_guard(root / "state_1")
            self.assertTrue(manager.run_due(T0 + INTERVAL_SECONDS))
            self.assertEqual(len(factory.captured), 2)
            # 매 세션 뒤 진행 이벤트(인스턴스·상태·next_run_at)가 나간다.
            # (러너 중간 이벤트도 progress 로 브리지되므로 세션 요약만 센다.)
            progress = [
                event
                for event in _events_of(events, "progress")
                if "result" in event
            ]
            self.assertEqual(len(progress), 2)
            self.assertEqual(progress[0]["instance"], "1")
            self.assertEqual(progress[0]["status"], "waiting")
            self.assertEqual(
                progress[0]["next_run_at"], T0 + INTERVAL_SECONDS
            )

    def test_not_due_instance_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            manager = _make_manager(
                root, _families(1), events, factory=factory, instance_count=1
            )
            manager.instances["1"].next_run_at = T0 + 100
            self.assertFalse(manager.run_due(T0))
            self.assertEqual(len(factory.captured), 0)

    def test_blocked_and_error_instances_are_not_rescheduled(self):
        """종료 상태(blocked/error) 인스턴스는 run_due 가 건너뛴다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            manager = _make_manager(
                root, _families(1), events, factory=factory, instance_count=1
            )
            manager.instances["1"].status = "blocked"
            self.assertFalse(manager.run_due(T0))
            manager.instances["1"].status = "error"
            self.assertFalse(manager.run_due(T0))
            self.assertEqual(len(factory.captured), 0)


# ── 4. 프록시 경로 ────────────────────────────────────────────────────


class ProxyRoutingTest(unittest.TestCase):
    """direct 는 proxy=None, decodo 는 회전된 sid 의 스티키 프록시 dict."""

    def test_direct_category_session_receives_proxy_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            manager = _make_manager(
                root, _families(1), events, factory=factory, instance_count=1
            )
            self.assertTrue(manager.run_due(T0))
            profile, proxy = factory.captured[0]
            self.assertIsNone(proxy)
            # 프로필은 인스턴스 전용 state_root 안에 만들어진다.
            self.assertEqual(profile, root / "state_1" / "chrome_profile")

    def test_direct_seller_session_receives_proxy_none(self):
        """판매자 단계도 direct 인스턴스는 proxy=None 으로 호출된다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            families = _families(1)
            _prepare_products_only_family(root / "out", 0, families[0])
            seller_page = _SellerPage(
                vendors_by_item={"viid-1": "A00001"},
                sellers_by_id={
                    "A00001": {
                        "name": "주식회사테스트",
                        "repPersonName": "홍길동",
                        "businessNumber": "123-45-67890",
                    }
                },
            )
            factory = _RoutingFactory(direct_page=seller_page)
            manager = _make_manager(
                root, families, events, factory=factory, instance_count=1
            )
            self.assertTrue(manager.run_due(T0))
            _profile, proxy = factory.captured[0]
            self.assertIsNone(proxy)
            progress = _events_of(events, "progress")
            self.assertEqual(progress[0]["action"], "sellers")
            self.assertEqual(progress[0]["result"], "seller_collection_complete")
            # 확보 판매자 누적이 세션 후 집계된다.
            self.assertEqual(progress[0]["total_sellers"], 1)

    def test_decodo_session_uses_effective_sid_proxy(self):
        calls: list = []
        fake = _fake_decodo(calls, ready=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            families = _families(2)
            with _decodo(fake):
                manager = _make_manager(
                    root, families, events, factory=factory, instance_count=2
                )
                self.assertTrue(manager.run_due(T0))
            self.assertEqual(len(factory.captured), 2)
            direct_profile, direct_proxy = factory.captured[0]
            decodo_profile, decodo_proxy = factory.captured[1]
            self.assertIsNone(direct_proxy)
            self.assertEqual(direct_profile, root / "state_1" / "chrome_profile")
            self.assertEqual(decodo_profile, root / "state_2" / "chrome_profile")
            # 산 sid(i2)로 조립된 스티키 프록시 dict 가 전달된다.
            self.assertEqual(
                decodo_proxy["username"], "user-sp3lqmo64w-session-i2"
            )
            exit_ips = _events_of(events, "exit_ip")
            self.assertEqual(len(exit_ips), 1)
            self.assertEqual(exit_ips[0]["instance"], "2")
            self.assertIs(exit_ips[0]["ok"], True)
            self.assertEqual(exit_ips[0]["proxy_session_id"], "i2")
            # 가족 출력 폴더가 인스턴스별로 분리된다(출력·장부 격리).
            self.assertTrue(
                (root / "out" / "family_02" / PROXY_STATE_FILENAME).exists()
            )
            self.assertEqual(
                _read_json(root / "out" / "family_02" / PROXY_STATE_FILENAME)[
                    "session_id"
                ],
                "i2",
            )

    def test_dead_decodo_session_rotates_sid_in_proxy(self):
        """시작 sid 가 죽었으면 교체된 sid(i3)의 프록시로 세션을 돈다."""
        calls: list = []
        dead: dict = {}
        fake = _fake_decodo(calls, ready=True, dead_sids=dead)
        dead["i2"] = fake.DecodoError("HTTP 502 터널 실패", kind="response")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            families = _families(2)
            with _decodo(fake):
                manager = _make_manager(
                    root, families, events, factory=factory, instance_count=2
                )
                self.assertTrue(manager.run_due(T0))
            _direct_profile, direct_proxy = factory.captured[0]
            _decodo_profile, decodo_proxy = factory.captured[1]
            self.assertIsNone(direct_proxy)
            self.assertIn("session-i3", decodo_proxy["username"])
            exit_ips = _events_of(events, "exit_ip")
            self.assertIs(exit_ips[0]["rotated"], True)
            self.assertEqual(exit_ips[0]["tried"], ["i2", "i3"])
            # 교체된 sid 는 매니저 상태에도 남는다(재시작 seed).
            self.assertEqual(manager.instances["2"].effective_sid, "i3")

    def test_decodo_failure_marks_instance_error_without_browser(self):
        """회선 점검 실패(계정 수준)는 proxy=None 진행 대신 인스턴스 정지."""
        calls: list = []
        fake = _fake_decodo(calls, ready=True)
        fake.fetch_exit_ip = lambda proxy, **_kwargs: (
            _raise(fake.DecodoError("사용량 소진", kind="quota"))
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            families = _families(2)
            with _decodo(fake):
                manager = _make_manager(
                    root, families, events, factory=factory, instance_count=2
                )
                self.assertTrue(manager.run_due(T0))
            # direct 인스턴스만 브라우저를 열었다 — decodo 는 정지.
            self.assertEqual(len(factory.captured), 1)
            self.assertIsNone(factory.captured[0][1])
            self.assertEqual(manager.instances["2"].status, "error")
            errors = _events_of(events, "error")
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["instance"], "2")
            self.assertIn("Decodo", errors[0]["reason"])
            # 정지한 인스턴스는 이후 run_due 에도 세션을 받지 않는다
            # (direct 인스턴스 1의 다음 예약 시각 이전 기준).
            with _decodo(fake):
                self.assertFalse(
                    manager.run_due(T0 + INTERVAL_SECONDS - 1)
                )
            self.assertEqual(len(factory.captured), 1)


def _raise(error: Exception):
    raise error


# ── 5. 차단 격리 ──────────────────────────────────────────────────────


class BlockedIsolationTest(unittest.TestCase):
    """한 인스턴스 차단은 그 인스턴스만 중단시킨다(장부가 인스턴스별)."""

    def test_blocked_instance_stops_while_other_continues(self):
        calls: list = []
        fake = _fake_decodo(calls, ready=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            families = _families(2)
            blocked_page = _CategoryPage(
                blocked_urls=[
                    # 인스턴스 1(직접) 가족의 첫 카테고리 페이지.
                    "https://www.coupang.com/np/categories/185671?page=1"
                ]
            )
            good_page = _CategoryPage()
            factory = _RoutingFactory(
                direct_page=blocked_page, decodo_page=good_page
            )
            with _decodo(fake):
                manager = _make_manager(
                    root, families, events, factory=factory, instance_count=2
                )
                self.assertTrue(manager.run_due(T0))
            # 인스턴스 1 은 차단으로 스케줄이 중단, 인스턴스 2 는 정상 완료.
            self.assertEqual(manager.instances["1"].status, "blocked")
            self.assertEqual(manager.instances["2"].status, "waiting")
            blocked = _events_of(events, "blocked")
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0]["instance"], "1")
            self.assertIn("Reference #18", blocked[0]["reference"])
            # 차단 기록은 인스턴스 1 장부에만 남는다.
            guard_1 = _read_json(root / "state_1" / "canary_guard.json")
            self.assertTrue(guard_1["blocked"])
            self.assertFalse(
                _read_json(root / "state_2" / "canary_guard.json")["blocked"]
            )
            # 다음 틱 — 차단된 인스턴스는 건너뛰고 인스턴스 2만 실행
            # (인스턴스 2 장부의 실접속 간격은 지난 것으로 되돌린다).
            _age_guard(root / "state_2")
            with _decodo(fake):
                self.assertTrue(manager.run_due(T0 + INTERVAL_SECONDS))
            self.assertEqual(len(factory.captured), 3)  # 1회차 2 + 2회차 1
            self.assertEqual(
                factory.captured[2][0], root / "state_2" / "chrome_profile"
            )
            self.assertFalse(manager.all_done())

    def test_error_instance_does_not_block_manager_finish(self):
        """error/blocked 인스턴스가 있어도 남은 인스턴스 완주 후 all_done."""
        calls: list = []
        fake = _fake_decodo(calls, ready=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            families = _families(2)
            _prepare_complete_family(root / "out", 1, families[1])
            with _decodo(fake):
                manager = _make_manager(
                    root, families, events, instance_count=2
                )
                # 인스턴스 1 을 미리 종료 상태로 만든다(차단과 동등).
                manager.instances["1"].status = "blocked"
                self.assertTrue(manager.run_due(T0))
            self.assertEqual(manager.instances["2"].status, "done")
            self.assertTrue(manager.all_done())
            self.assertEqual(len(_events_of(events, "all_done")), 1)


# ── 6. 가드 사전 점검 ─────────────────────────────────────────────────


class GuardProbeTest(unittest.TestCase):
    """사전 점검은 장부 사본으로 판정 — 거부 시 세션 미실행, 실장부 무결."""

    def test_guard_refusal_postpones_without_running_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            manager = _make_manager(
                root, _families(1), events, factory=factory, instance_count=1
            )
            # 실제 인스턴스 장부에 최근 실접속 기록을 남긴다(간격 미달 유발).
            allowed, reason = claim_live_attempt(
                root / "state_1",
                now=T0,
                planned_items=60,
                planned_pages=1,
            )
            self.assertTrue(allowed, reason)
            later = T0 + 600  # 10분 뒤 — 가드 최소 간격(60분) 안쪽.
            self.assertTrue(manager.run_due(later))
            # 세션(브라우저)은 실행되지 않고 next_run_at 만 연기된다.
            self.assertEqual(len(factory.captured), 0)
            state = manager.instances["1"]
            self.assertEqual(state.status, "waiting")
            self.assertEqual(state.next_run_at, later + INTERVAL_SECONDS)
            logs = [
                event
                for event in _events_of(events, "log")
                if "연기" in event.get("message", "")
            ]
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0]["instance"], "1")
            # 사전 점검은 장부 사본으로 판정했다 — 실장부가 오염되지 않는다.
            guard = _read_json(root / "state_1" / "canary_guard.json")
            self.assertEqual(guard["daily_sessions"], 1)
            self.assertEqual(len(guard["attempt_history"]), 1)
            self.assertEqual(guard["last_attempt_ts"], T0)

    def test_normal_session_leaves_settled_attempt_in_real_ledger(self):
        """정상 세션 후 실장부의 예약은 settle 된다(러너 소유) + 프로브 무결."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            manager = _make_manager(
                root, _families(1), events, factory=factory, instance_count=1
            )
            self.assertTrue(manager.run_due(T0))
            self.assertEqual(len(factory.captured), 1)
            guard = _read_json(root / "state_1" / "canary_guard.json")
            # 세션 1회의 예약만 남고(프로브가 기록하지 않는다) 정상 반납됐다.
            self.assertEqual(len(guard["attempt_history"]), 1)
            self.assertIs(guard["attempt_history"][0]["settled"], True)


# ── 7. 상태 영속/복원 ─────────────────────────────────────────────────


class PersistRestoreTest(unittest.TestCase):
    """persist → 신규 매니저가 배분·예정 시각·sid 를 복원한다."""

    def test_state_roundtrip_restores_distribution_and_schedule(self):
        calls: list = []
        fake = _fake_decodo(calls, ready=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            families = _families(3)
            with _decodo(fake):
                first = _make_manager(
                    root, families, events, factory=factory, instance_count=2
                )
                self.assertTrue(first.run_due(T0))
            self.assertTrue((root / "out" / STATE_FILENAME).exists())

            restored_events: list = []
            with _decodo(fake):
                second = _make_manager(
                    root,
                    families,
                    restored_events,
                    factory=_RoutingFactory(),
                    instance_count=2,
                )
            # 배분·예정 시각·큐·유효 sid 가 그대로 복원된다.
            for instance_id, family_index in (("1", 0), ("2", 1)):
                self.assertEqual(
                    second.instances[instance_id].family_index, family_index
                )
                self.assertEqual(
                    second.instances[instance_id].next_run_at,
                    first.instances[instance_id].next_run_at,
                )
            self.assertEqual(second.pending_families, [2])
            self.assertEqual(second.instances["2"].effective_sid, "i2")
            self.assertIn("복원", restored_events[-1]["message"])
            # 복원된 예정 시각 이전에는 세션을 돌리지 않는다.
            restored_factory = second.browser_scope_factory
            self.assertFalse(second.run_due(T0 + INTERVAL_SECONDS - 1))
            self.assertEqual(len(restored_factory.captured), 0)

    def test_load_state_returns_none_for_missing_or_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "nope.json"
            self.assertIsNone(ParallelCoupangManager.load_state(missing))
            corrupt = root / "corrupt.json"
            corrupt.write_text("not-json", encoding="utf-8")
            self.assertIsNone(ParallelCoupangManager.load_state(corrupt))
            wrong_version = root / "wrong.json"
            wrong_version.write_text(
                json.dumps({"version": 2, "instances": []}), encoding="utf-8"
            )
            self.assertIsNone(
                ParallelCoupangManager.load_state(wrong_version)
            )

    def test_changed_instance_configuration_falls_back_to_fresh_plan(self):
        """인스턴스 구성이 달라지면(강등) 저장 상태를 버리고 새로 배분한다."""
        calls: list = []
        ready_fake = _fake_decodo(calls, ready=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            families = _families(3)
            with _decodo(ready_fake):
                _make_manager(
                    root, families, events, factory=factory, instance_count=2
                )
            # 자격이 사라진 채 재시작 — 1개(직접)로 강등되어 구성이 다르다.
            not_ready = _fake_decodo(calls, ready=False)
            restarted_events: list = []
            with _decodo(not_ready):
                restarted = _make_manager(
                    root,
                    families,
                    restarted_events,
                    factory=factory,
                    instance_count=2,
                )
            self.assertEqual(set(restarted.instances), {"1"})
            self.assertEqual(restarted.pending_families, [1, 2])
            self.assertEqual(restarted.instances["1"].family_index, 0)


# ── 8. 완주 경로 ──────────────────────────────────────────────────────


class CompletePathTest(unittest.TestCase):
    """complete → build_all_finals + 승계/done 이벤트."""

    def test_complete_family_builds_finals_and_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            families = _families(1)
            family_dir = _prepare_complete_family(root / "out", 0, families[0])
            manager = _make_manager(
                root, families, events, instance_count=1
            )
            self.assertTrue(manager.run_due(T0))
            complete = _events_of(events, "complete")
            self.assertEqual(len(complete), 1)
            self.assertEqual(complete[0]["instance"], "1")
            self.assertEqual(complete[0]["family_index"], 0)
            # 기본 A 가족이 아니므로 판매자+통합 final 만 만들어진다.
            self.assertEqual(
                set(complete[0]["finals"]), {"sellers", "all"}
            )
            for key in ("sellers", "all"):
                self.assertTrue(Path(complete[0]["finals"][key]).exists())
            self.assertTrue((family_dir / "final_dataset_all.csv").exists())
            self.assertTrue(
                (family_dir / "coupang_판매자_1명.csv").exists()
            )
            self.assertFalse(
                (family_dir / "final_dataset_194373.csv").exists()
            )
            # 가족 정의 파일이 출력 폴더에 남는다(M1 규약 전달용).
            self.assertTrue(
                (family_dir / "coupang_family.json").exists()
            )
            self.assertEqual(manager.instances["1"].status, "done")
            self.assertTrue(manager.all_done())


# ── 9. 구성 검증 ──────────────────────────────────────────────────────


class RunConfigValidationTest(unittest.TestCase):
    """ParallelRunConfig 검증 — 인스턴스 수 1~5, 가족/파라미터 형식."""

    def test_instance_count_out_of_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in (0, 6):
                with self.subTest(instance_count=bad):
                    with self.assertRaises(ValueError):
                        ParallelRunConfig(
                            families=_families(1),
                            output_dir=root / "out",
                            instance_count=bad,
                        )

    def test_instance_count_bounds_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for good in (1, 5):
                with self.subTest(instance_count=good):
                    config = ParallelRunConfig(
                        families=_families(good),
                        output_dir=root / "out",
                        instance_count=good,
                    )
                    self.assertEqual(config.instance_count, good)

    def test_empty_families_and_bad_parameters_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                ParallelRunConfig(
                    families=[], output_dir=root / "out", instance_count=1
                )
            with self.assertRaises(ValueError):
                ParallelRunConfig(
                    families=[[]], output_dir=root / "out", instance_count=1
                )
            with self.assertRaises(ValueError):
                ParallelRunConfig(
                    families=_families(1),
                    output_dir=root / "out",
                    listing_pages=0,
                )
            with self.assertRaises(ValueError):
                ParallelRunConfig(
                    families=_families(1),
                    output_dir=root / "out",
                    interval_minutes=0,
                )


# ── 부가: 취소·halted ─────────────────────────────────────────────────


class CancelAndHaltTest(unittest.TestCase):
    """사용자 취소는 대기 복귀, halted 예약은 인스턴스 error."""

    def test_cancelled_session_returns_to_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            control = Control()
            control.request_cancel()
            manager = _make_manager(
                root,
                _families(1),
                events,
                factory=factory,
                instance_count=1,
                control=control,
            )
            manager.run_due(T0)
            self.assertEqual(len(factory.captured), 0)
            state = manager.instances["1"]
            self.assertEqual(state.status, "waiting")
            # 취소 시각 이후에도 상태는 영속된다.
            saved = _read_json(root / "out" / STATE_FILENAME)
            self.assertEqual(saved["instances"][0]["status"], "waiting")

    def test_halted_reservation_marks_instance_error(self):
        """앞선 세션이 halted 로 남긴 판매자 기록은 예약을 정지시킨다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            factory = _RoutingFactory()
            families = _families(1)
            family_dir = root / "out" / "family_01"
            family_dir.mkdir(parents=True)
            (family_dir / "top_seller_control.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "halted",
                        "reason": "TimeoutError: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED",
                        "updated_at": "2026-10-01 17:45:04",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manager = _make_manager(
                root, families, events, factory=factory, instance_count=1
            )
            self.assertTrue(manager.run_due(T0))
            self.assertEqual(manager.instances["1"].status, "error")
            self.assertEqual(len(factory.captured), 0)
            errors = _events_of(events, "error")
            self.assertEqual(len(errors), 1)
            self.assertIn("ERR_TUNNEL_CONNECTION_FAILED", errors[0]["reason"])
            # halted 사유는 가족 실행 로그에도 남는다.
            self.assertEqual(
                _run_lines(family_dir)[-1]["result"], "halted"
            )


if __name__ == "__main__":
    unittest.main()
