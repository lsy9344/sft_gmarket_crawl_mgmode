"""쿠팡 병렬 수집 파이프라인 코어(parallel_pipeline) — 네트워크 없음.

병렬 확장 설계(PARALLEL_SCALE_OUT_DESIGN_20261001.md) §5.5와 2026-10-01
b01 조기 사망 사건 대응을 scaleout 프로토타입 테스트(test_coupang_
patchright_top_pipeline.py)에서 이식했다. 프로토타입은 스크립트 main()을
subprocess 스텁으로 돌렸지만, 이 테스트는 모듈 함수(choose_action/
check_and_rotate_session/build_all_finals)를 직접 검증한다 — 자식 프로세스
실행·명령줄 조립은 향후 매니저(M2) 책임이다.

- choose_action: 판매자 우선·부트스트랩·halted 경로(컨트롤·실패 매핑·가족
  혼용)를 dict 반환으로 고정한다.
- check_and_rotate_session: 출구 IP 점검 이벤트와 sid 자동 교체(시작점은
  상태 파일의 마지막 성공 sid, 교체 3회 상한, 계정 수준 오류는 교체 없음).
- build_all_finals: 가족 파일 유무로 루트별 final 건너뛰기.
- append_run_log: 실행 로그 JSONL(top_pipeline_runs.jsonl) 기록 헬퍼.

app/core/decodo 는 테스트마다 가짜 모듈로 대체해 오프라인으로 검증한다
(sys.modules 를 잠시 교체).
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core.coupang.parallel_pipeline import (
    MAX_SESSION_ROTATIONS,
    PROXY_STATE_FILENAME,
    ROTATABLE_ERROR_KINDS,
    RUN_LOG_FILENAME,
    append_run_log,
    build_all_finals,
    check_and_rotate_session,
    choose_action,
    load_categories_file,
    next_session_id,
    read_proxy_session_state,
)
from app.core.coupang.patchright_top_sellers import TopSellerStore
from app.core.coupang.patchright_top_thousand import (
    PRODUCT_FIELDS,
    TopThousandStore,
)


def _product_row(vendor_item_id: str) -> dict:
    return {
        "category_id": "194373",
        "category_name": "식품/견과/건과",
        "page_number": "1",
        "product_id": "10",
        "item_id": "20",
        "vendor_item_id": vendor_item_id,
        "title": f"상품 {vendor_item_id}",
        "price": "1000",
        "review_count": "100",
        "delivery_markers": "",
        "url": f"https://www.coupang.com/vp/products/10?vendorItemId={vendor_item_id}",
        "collected_at": "2026-10-01 00:00:00",
    }


def _prepare(output_dir: Path, product_rows: list[dict]) -> None:
    TopThousandStore(output_dir).ensure_files()
    TopSellerStore(output_dir).ensure_files()
    from app.core.coupang.patchright_full_fruit import _write_csv

    _write_csv(output_dir / "top_products.csv", PRODUCT_FIELDS, product_rows)


def _complete_sellers(output_dir: Path) -> None:
    """판매자 대기열이 비었음을 만드는 최소 파일 쌍(목록 단계로 넘어간다)."""
    from app.core.coupang.patchright_full_fruit import (
        PRODUCT_SELLER_FIELDS,
        SELLER_FIELDS,
        _write_csv,
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
    _write_csv(
        output_dir / "top_sellers.csv",
        SELLER_FIELDS,
        [
            {
                "status": "saved",
                "vendor_id": "A00001",
                "url": "u",
                "store_name": "스토어",
                "company_name": "상호1",
                "ceo_name": "대표1",
                "business_number": "1",
                "phone": "p",
                "email": "e",
                "address": "a",
                "ecommerce_report_number": "",
                "power_seller": "False",
                "power_seller_title": "",
                "rating_count": "100",
                "thumb_up_ratio": "90",
                "error": "",
                "updated_at": "2026-10-01 00:00:00",
            }
        ],
    )


def _fake_decodo(calls: list, *, info=None, error=None, proxy_none=False):
    """파이프라인이 늦게 import 하는 app.core.decodo 의 가짜 대체품.

    calls 에 (settings, session_id) / proxy 기록을 남겨 호출 횟수를 검증한다.
    실제 decodo 모듈 존재 여부와 무관하게 동작한다(sys.modules 교체).
    """
    module = types.ModuleType("app.core.decodo")

    class DecodoError(RuntimeError):
        def __init__(self, message: str, *, kind: str = "other") -> None:
            super().__init__(message)
            self.kind = kind

    def load_settings(path=None):
        calls.append(("load_settings", path))
        return SimpleNamespace(username="sp3lqmo64w", password="pw")

    def sticky_proxy_dict(settings=None, session_id=""):
        calls.append(("sticky_proxy_dict", session_id))
        if proxy_none:
            return None
        return {
            "server": "http://gate.decodo.com:7000",
            "username": f"user-sp3lqmo64w-session-{session_id}",
            "password": "pw",
        }

    def fetch_exit_ip(proxy, **_kwargs):
        calls.append(("fetch_exit_ip", proxy))
        if error is not None:
            raise error
        return SimpleNamespace(
            ip=info["ip"], country_code=info["country_code"], country_name="Korea"
        )

    module.DecodoError = DecodoError
    module.load_settings = load_settings
    module.sticky_proxy_dict = sticky_proxy_dict
    module.fetch_exit_ip = fetch_exit_ip
    return module


def _script_fetch_by_sid(module, calls: list, results: dict) -> None:
    """가짜 decodo 모듈의 fetch_exit_ip 을 sid별 결과로 교체한다.

    results 는 sid → 성공 SimpleNamespace 또는 raise 할 예외. "*" 는 그 외
    모든 sid에 적용하는 와일드카드다. 호출은 ("fetch_exit_ip", sid) 로
    기록해 교체 루프의 시도 순서를 그대로 검증한다.
    """

    def fetch_exit_ip(proxy, **_kwargs):
        sid = str(proxy.get("username", "")).split("-session-")[-1]
        calls.append(("fetch_exit_ip", sid))
        outcome = results.get(sid, results.get("*"))
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            raise AssertionError(f"스크립트에 없는 sid 점검: {sid}")
        return outcome

    module.fetch_exit_ip = fetch_exit_ip


def _run_lines(output_dir: Path) -> list[dict]:
    path = output_dir / RUN_LOG_FILENAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _family_b_path(root: Path) -> Path:
    """인스턴스 B 카나리 가족 정의 파일(주방용품 소형)을 임시로 만든다."""
    path = root / "coupang_categories_kitchen_b.json"
    path.write_text(
        json.dumps(
            {
                "instance": "b01",
                "description": "병렬 확장 B 카나리 — 주방용품 소형 가족 (설계 §8 Phase 1)",
                "categories": [
                    ["185671", "주방용품/냄비/프라이팬"],
                    ["185735", "주방용품/그릇/홈세트"],
                    ["185872", "주방용품/밀폐저장/도시락"],
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _b_product_row(vendor_item_id: str) -> dict:
    row = _product_row(vendor_item_id)
    row["category_id"] = "185671"
    row["category_name"] = "주방용품/냄비/프라이팬"
    return row


def _b_category_record(name: str, status: str = "exhausted") -> dict:
    return {
        "category_name": name,
        "status": status,
        "raw_seen": 60,
        "rocket_seen": 40,
        "jet_seen": 0,
        "tp_collected": 20,
        "pages_scanned": 2,
    }


def _b_done_state() -> dict:
    """B 가족 세 카테고리를 모두 마친 상태 파일 내용."""
    return {
        "version": 1,
        "status": "completed",
        "category_index": 3,
        "page_number": 1,
        "consecutive_empty_pages": 0,
        "categories": {
            "185671": _b_category_record("주방용품/냄비/프라이팬"),
            "185735": _b_category_record("주방용품/그릇/홈세트"),
            "185872": _b_category_record("주방용품/밀폐저장/도시락"),
        },
    }


def _b_running_state() -> dict:
    """B 가족 목록 단계가 진행 중인 상태 파일 내용(두 번째 카테고리 시작)."""
    state = _b_done_state()
    state["status"] = "running"
    state["category_index"] = 1
    state["categories"]["185735"]["status"] = "running"
    state["categories"]["185872"]["status"] = "pending"
    return state


def _alive(ip: str = "175.203.56.170") -> SimpleNamespace:
    return SimpleNamespace(ip=ip, country_code="KR", country_name="Korea")


def _proxy_state(output_dir: Path) -> dict:
    return json.loads(
        (output_dir / PROXY_STATE_FILENAME).read_text(encoding="utf-8")
    )


def _fetch_sids(calls: list) -> list[str]:
    return [call[1] for call in calls if call[0] == "fetch_exit_ip"]


class PipelineConstantsTest(unittest.TestCase):
    """상수 고정 — 프로토타입 실행기와 같은 값·파일명을 쓴다."""

    def test_constants_match_prototype_pipeline(self):
        self.assertEqual(MAX_SESSION_ROTATIONS, 3)
        self.assertEqual(
            ROTATABLE_ERROR_KINDS,
            frozenset({"response", "connection", "timeout", "other"}),
        )
        self.assertEqual(PROXY_STATE_FILENAME, "proxy_session_state.json")
        self.assertEqual(RUN_LOG_FILENAME, "top_pipeline_runs.jsonl")

    def test_load_categories_file_is_reexported(self):
        """가족 로더는 top_thousand 구현을 그대로 재수출한다."""
        self.assertIs(
            load_categories_file,
            __import__(
                "app.core.coupang.patchright_top_thousand",
                fromlist=["load_categories_file"],
            ).load_categories_file,
        )


class FreshInstanceBootstrapTest(unittest.TestCase):
    """신규 인스턴스 부트스트랩 — 상품 파일이 없으면 목록 단계부터 시작한다.

    2026-10-01 통합에서 발견: 빈 출력 폴더에서 _work가 top_products.csv
    읽기를 예외로 던져 halted(종료 23)·예약 자동 비활성으로 빠졌다.
    신규 인스턴스(B/C)의 판매자 대기열은 정의상 비어 있으므로 목록 단계가
    첫 동작이어야 한다.
    """

    def test_fresh_output_dir_starts_with_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            decision = choose_action(Path(tmp))
            self.assertEqual(decision["action"], "category")
            self.assertIn("목록", decision["reason"])

    def test_products_file_keeps_seller_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _prepare(output_dir, [_product_row("9001")])
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "sellers")

    def test_completed_listing_reports_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            _complete_sellers(output_dir)
            state = _b_done_state()
            # 기본 A 가족 상태로 완주 상태를 만든다(가족 파일 없음 경로).
            from app.core.coupang.patchright_top_thousand import TOP_CATEGORIES

            state["categories"] = {
                category_id: {
                    "category_name": name,
                    "status": "exhausted",
                    "raw_seen": 0,
                    "rocket_seen": 0,
                    "jet_seen": 0,
                    "tp_collected": 0,
                    "pages_scanned": 1,
                }
                for category_id, name in TOP_CATEGORIES
            }
            (output_dir / "top_state.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "complete")


class ChooseActionHaltTest(unittest.TestCase):
    """choose_action 의 halted 경로 — 예약이 안전하게 멈추는 조건들."""

    _TUNNEL_REASON = (
        "TimeoutError: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at "
        "https://www.coupang.com/. Try upgrading your browser."
    )

    def test_halted_seller_control_stops_with_its_reason(self):
        """터널 장애로 남은 halted 컨트롤의 이유를 그대로 돌려준다(2026-10-01
        17:45 b01 사건 고정 — 실행 중 세션 사망 시 예약은 자동 비활성)."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out_B"
            output_dir.mkdir()
            (output_dir / "top_seller_control.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "halted",
                        "reason": self._TUNNEL_REASON,
                        "updated_at": "2026-10-01 17:45:04",
                        "event": "failed",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "halted")
            self.assertEqual(decision["reason"], self._TUNNEL_REASON)
            self.assertIn("ERR_TUNNEL_CONNECTION_FAILED", decision["reason"])

    def test_failed_mappings_stop_progress(self):
        """판매자를 연결하지 못한 상품이 있으면 자동 진행을 멈춘다."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            _complete_sellers(output_dir)
            (output_dir / "top_failed_mappings.json").write_text(
                json.dumps([{"vendor_item_id": "viid-1", "error": "매핑 실패"}]),
                encoding="utf-8",
            )
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "halted")
            self.assertIn("연결하지 못한", decision["reason"])

    def test_foreign_family_state_halts_instead_of_raising(self):
        """가족 파일 없이 B 상태 폴더를 읽으면 halted — 예외 경계 흡수(프로토
        타입 main의 try/except와 같은 안전거동, 가족 혼용 방지)."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_b_product_row("viid-1")])
            _complete_sellers(output_dir)
            (output_dir / "top_state.json").write_text(
                json.dumps(_b_done_state(), ensure_ascii=False),
                encoding="utf-8",
            )
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "halted")
            self.assertIn("ValueError", decision["reason"])


class ChooseActionResumeTest(unittest.TestCase):
    """끊긴 실행(control cancelled/in_progress)은 이어서 수집한다.

    2026-10-06 검토 반영 — 사용자 정지(halt→cancelled)와 크래시 잔존
    (in_progress)이 예약을 영구 멈추던 halted 브릭을 고정한다. halted 는
    데이터 사유(실패 매핑·503·검증 실패)만 남긴다.
    """

    def _write_control(self, output_dir: Path, status: str) -> None:
        (output_dir / "top_seller_control.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "status": status,
                    "updated_at": "2026-10-06 09:00:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_cancelled_control_resumes_sellers(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            self._write_control(output_dir, "cancelled")
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "sellers")

    def test_stale_in_progress_control_resumes_sellers(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            self._write_control(output_dir, "in_progress")
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "sellers")

    def test_unconfirmed_marker_row_is_requeued_before_decision(self):
        """요청 표시만 남은 판매자는 대기열로 되돌아가 다시 수집 대상이 된다.

        크래시 직전 판매자의 결과 행이 '요청 시작' 표시뿐이면, 표시를
        되돌리지 않으면 completed 로 오판해 그 판매자를 영구 유실한다.
        """
        from app.core.coupang.patchright_full_sellers import _seller_row
        from app.core.coupang.store_files import UNCONFIRMED_SELLER_ERROR

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            # 크래시 직전 상태 — 매핑은 저장됐고 결과 행은 요청 표시만 있다.
            store = TopSellerStore(output_dir)
            store.merge_product_sellers(
                [
                    {
                        "product_id": "10",
                        "item_id": "20",
                        "vendor_item_id": "viid-1",
                        "vendor_id": "A00001",
                        "mapped_at": "2026-10-06 09:00:00",
                    }
                ]
            )
            store.merge_sellers(
                [
                    _seller_row(
                        "A00001",
                        {
                            "vendorId": "A00001",
                            "displayName": "스토어",
                            "productId": "10",
                            "itemId": "20",
                            "vendorItemId": "viid-1",
                        },
                        None,
                        UNCONFIRMED_SELLER_ERROR,
                    )
                ]
            )
            decision = choose_action(output_dir)
            self.assertEqual(decision["action"], "sellers")
            # 표시 행은 실제로 대기열로 되돌아갔다(파일에서 사라졌다).
            with (output_dir / "top_sellers.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])

    def test_family_state_with_categories_continues_or_completes(self):
        """같은 B 상태라도 가족을 함께 넘기면 정상 동작한다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_b_product_row("viid-1")])
            _complete_sellers(output_dir)
            family_path = _family_b_path(root)
            categories = load_categories_file(family_path)

            running = dict(_b_running_state())
            (output_dir / "top_state.json").write_text(
                json.dumps(running, ensure_ascii=False), encoding="utf-8"
            )
            decision = choose_action(output_dir, categories)
            self.assertEqual(decision["action"], "category")

            (output_dir / "top_state.json").write_text(
                json.dumps(_b_done_state(), ensure_ascii=False),
                encoding="utf-8",
            )
            decision = choose_action(output_dir, categories)
            self.assertEqual(decision["action"], "complete")


class BuildAllFinalsCategoriesTest(unittest.TestCase):
    """build_all_finals(categories_file=…) — 다른 가족 final 건너뛰기."""

    def _completed_b_dir(self, root: Path) -> Path:
        output_dir = root / "out"
        _prepare(output_dir, [_b_product_row("viid-1")])
        _complete_sellers(output_dir)
        (output_dir / "top_state.json").write_text(
            json.dumps(_b_done_state(), ensure_ascii=False), encoding="utf-8"
        )
        return output_dir

    def test_with_categories_file_skips_family_finals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = self._completed_b_dir(root)
            family_path = _family_b_path(root)
            finals = build_all_finals(output_dir, categories_file=family_path)
            self.assertEqual(set(finals), {"sellers", "all"})
            self.assertFalse((output_dir / "final_dataset_194373.csv").exists())
            self.assertFalse((output_dir / "final_dataset_194688.csv").exists())
            all_rows = _read_rows(output_dir / "final_dataset_all.csv")
            self.assertEqual(
                [row["vendor_item_id"] for row in all_rows], ["viid-1"]
            )

    def test_without_categories_file_makes_family_finals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = self._completed_b_dir(root)
            finals = build_all_finals(output_dir)
            self.assertEqual(set(finals), {"sellers", "194373", "194688", "all"})
            for name in (
                "final_dataset_194373.csv",
                "final_dataset_194688.csv",
                "final_dataset_all.csv",
            ):
                self.assertTrue((output_dir / name).exists(), name)


class AppendRunLogTest(unittest.TestCase):
    """append_run_log — 실행 로그 JSONL 기록 헬퍼(기록 시점은 호출자 책임)."""

    def test_appends_jsonl_lines_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "deep" / "out"
            append_run_log(
                output_dir, {"event": "exit_ip_check", "ok": True}
            )
            append_run_log(
                output_dir, {"event": "pipeline_slot_finished", "action": "sellers"}
            )
            lines = _run_lines(output_dir)
            self.assertEqual(
                [line["event"] for line in lines],
                ["exit_ip_check", "pipeline_slot_finished"],
            )
            # 한글은 그대로 저장된다(ensure_ascii=False).
            self.assertIn('"ok": true', (output_dir / RUN_LOG_FILENAME).read_text("utf-8"))


class ReadProxySessionStateTest(unittest.TestCase):
    """상태 파일 읽기 — 없거나 깨졌으면 조용히 빈 문자열."""

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_proxy_session_state(Path(tmp)), "")

    def test_corrupt_or_non_dict_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / PROXY_STATE_FILENAME).write_text("not-json", encoding="utf-8")
            self.assertEqual(read_proxy_session_state(output_dir), "")
            (output_dir / PROXY_STATE_FILENAME).write_text('["b01"]', encoding="utf-8")
            self.assertEqual(read_proxy_session_state(output_dir), "")
            (output_dir / PROXY_STATE_FILENAME).write_text(
                '{"session_id": null}', encoding="utf-8"
            )
            self.assertEqual(read_proxy_session_state(output_dir), "")

    def test_valid_state_returns_stripped_sid(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / PROXY_STATE_FILENAME).write_text(
                json.dumps({"session_id": " b07 ", "updated_at": "x"}),
                encoding="utf-8",
            )
            self.assertEqual(read_proxy_session_state(output_dir), "b07")


class NextSessionIdTest(unittest.TestCase):
    """sid 교체 규칙 — 끝자리 +1, 9에서 0으로 순환(자리올림 금지).

    자리올림(i29→i30)이 인스턴스 네임스페이스를 침범해 2026-10-04
    7-병렬 실측에서 동일 출구 IP 충돌을 일으켰다 — 교체는 십진
    네임스페이스(i{N}0~i{N}9) 안에서만 순환한다.
    """

    def test_numeric_tail_increments(self):
        self.assertEqual(next_session_id("b03"), "b04")
        self.assertEqual(next_session_id("i20"), "i21")

    def test_trailing_nine_wraps_without_carry(self):
        self.assertEqual(next_session_id("b09"), "b00")
        self.assertEqual(next_session_id("i29"), "i20")
        self.assertEqual(next_session_id("c99"), "c90")

    def test_wrap_keeps_rotation_inside_namespace(self):
        """누적 회전이 아무리 쌓여도 인스턴스 2의 sid는 i2x 에 머문다."""
        sid = "i20"
        seen = {sid}
        for _ in range(30):
            sid = next_session_id(sid)
            seen.add(sid)
        self.assertTrue(seen <= {f"i2{digit}" for digit in range(10)})
        self.assertEqual(len(seen), 10)

    def test_non_numeric_tail_appends_two(self):
        self.assertEqual(next_session_id("bx"), "bx2")

    def test_blank_sid_becomes_two(self):
        self.assertEqual(next_session_id(""), "2")
        self.assertEqual(next_session_id("   "), "2")


class CheckAndRotateSessionTest(unittest.TestCase):
    """출구 IP 점검 + 세션 자동 교체 — 2026-10-01 b01 조기 사망 사건 대응.

    시작 sid는 상태 파일(proxy_session_state.json)의 마지막 성공 sid,
    없으면 호출자 sid. 세션 수준 오류(response/connection/timeout/other)
    는 sid를 +1 해 재점검(최대 교체 3회), 계정 수준 오류(quota/auth/
    unknown_407)는 교체 없다. 성공 sid를 돌려주고 상태 파일에 저장하며,
    전부 실패하면 호출자 sid를 돌려준다(현행 안전거동).
    """

    def _rotate(self, fake, output_dir: Path, sid: str) -> tuple[dict, str]:
        with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
            return check_and_rotate_session(output_dir, sid)

    def test_success_returns_event_and_writes_state(self):
        calls: list = []
        fake = _fake_decodo(calls, info={"ip": "203.0.113.9", "country_code": "KR"})
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            event, effective_sid = self._rotate(fake, output_dir, "b01")
            self.assertEqual(effective_sid, "b01")
            self.assertEqual(event["event"], "exit_ip_check")
            self.assertEqual(event["proxy_session_id"], "b01")
            self.assertEqual(event["ip"], "203.0.113.9")
            self.assertEqual(event["country_code"], "KR")
            self.assertIs(event["ok"], True)
            self.assertIs(event["rotated"], False)
            self.assertEqual(event["tried"], ["b01"])
            # 프록시 dict는 sid로 조립되고 점검 호출은 1회뿐이다.
            self.assertEqual(
                [call[0] for call in calls],
                ["load_settings", "sticky_proxy_dict", "fetch_exit_ip"],
            )
            self.assertEqual(calls[1][1], "b01")
            self.assertEqual(
                calls[2][1]["username"],
                "user-sp3lqmo64w-session-b01",
            )
            # 성공 sid는 상태 파일에 남는다.
            state = _proxy_state(output_dir)
            self.assertEqual(state["session_id"], "b01")
            self.assertRegex(
                state["updated_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
            )

    def test_dead_session_rotates_and_returns_new_sid(self):
        """첫 sid 사망(response)→ 교체 성공: 반환·상태 파일 모두 교체 sid."""
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(
            fake,
            calls,
            {
                "b03": fake.DecodoError("HTTP 502 터널 실패", kind="response"),
                "*": _alive(),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            event, effective_sid = self._rotate(fake, output_dir, "b03")
            self.assertEqual(effective_sid, "b04")
            self.assertEqual(_fetch_sids(calls), ["b03", "b04"])
            self.assertIs(event["ok"], True)
            self.assertIs(event["rotated"], True)
            self.assertEqual(event["tried"], ["b03", "b04"])
            self.assertEqual(event["proxy_session_id"], "b04")
            self.assertEqual(event["ip"], "175.203.56.170")
            self.assertEqual(_proxy_state(output_dir)["session_id"], "b04")

    def test_state_file_sid_is_starting_point(self):
        """상태 파일의 마지막 성공 sid가 시작점 — 호출자 sid보다 우선."""
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(fake, calls, {"*": _alive("1.1.1.1")})
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            (output_dir / PROXY_STATE_FILENAME).write_text(
                json.dumps(
                    {"session_id": "b07", "updated_at": "2026-10-01 21:09:28"}
                ),
                encoding="utf-8",
            )
            event, effective_sid = self._rotate(fake, output_dir, "b03")
            self.assertEqual(_fetch_sids(calls), ["b07"])
            self.assertEqual(effective_sid, "b07")
            self.assertIs(event["ok"], True)
            self.assertIs(event["rotated"], False)
            self.assertEqual(event["tried"], ["b07"])
            self.assertEqual(_proxy_state(output_dir)["session_id"], "b07")

    def test_state_file_dead_sid_rotates_from_state_value(self):
        """상태 파일 sid가 죽었으면 그 값에서 이어 교체한다(b07→b08)."""
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(
            fake,
            calls,
            {
                "b07": fake.DecodoError("연결 불가", kind="connection"),
                "*": _alive(),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            (output_dir / PROXY_STATE_FILENAME).write_text(
                json.dumps({"session_id": "b07", "updated_at": "2026-10-01 21:09"}),
                encoding="utf-8",
            )
            _event, effective_sid = self._rotate(fake, output_dir, "b03")
            self.assertEqual(_fetch_sids(calls), ["b07", "b08"])
            self.assertEqual(effective_sid, "b08")
            self.assertEqual(_proxy_state(output_dir)["session_id"], "b08")

    def test_state_file_sid_from_other_namespace_is_not_inherited(self):
        """타 인스턴스 네임스페이스의 폴더 sid는 승계하지 않는다.

        조각/샤드 승계로 다른 인스턴스가 쓰던 폴더를 물려받아도 그 회선
        (출구 IP)까지 물려받지 않는다 — 두 인스턴스가 같은 IP를 공유하거나
        프로필(쿠키)-IP 불일치가 생기는 걸 막는다(2026-10-04 sid 충돌
        사고와 같은 실패 유형). 자기 네임스페이스 sid로 시작하고 상태
        파일도 자기 sid로 덮어쓴다.
        """
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(fake, calls, {"*": _alive("1.1.1.1")})
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            (output_dir / PROXY_STATE_FILENAME).write_text(
                json.dumps({"session_id": "i57", "updated_at": "x"}),
                encoding="utf-8",
            )
            event, effective_sid = self._rotate(fake, output_dir, "i20")
            self.assertEqual(_fetch_sids(calls), ["i20"])
            self.assertEqual(effective_sid, "i20")
            self.assertEqual(event["tried"], ["i20"])
            self.assertEqual(_proxy_state(output_dir)["session_id"], "i20")

    def test_state_file_sid_namespace_boundaries(self):
        """같은 인스턴스 네임스페이스(i{N}_)면 승계, 숫자 자리수가 다른
        타 인스턴스(i2x vs i20x)는 거부한다."""
        cases = [
            ("i23", "i20", "i23"),    # 같은 인스턴스 2 — 승계
            ("i207", "i200", "i207"),  # 같은 인스턴스 20 — 승계
            ("i207", "i20", "i20"),   # 인스턴스 20 → 2 침범 거부
            ("i2", "i20", "i20"),     # 구형 짧은 sid 거부(안전 방향)
        ]
        for stored, caller, expected in cases:
            with self.subTest(stored=stored, caller=caller):
                calls: list = []
                fake = _fake_decodo(calls)
                _script_fetch_by_sid(fake, calls, {"*": _alive()})
                with tempfile.TemporaryDirectory() as tmp:
                    output_dir = Path(tmp) / "out"
                    _prepare(output_dir, [_product_row("viid-1")])
                    (output_dir / PROXY_STATE_FILENAME).write_text(
                        json.dumps({"session_id": stored, "updated_at": "x"}),
                        encoding="utf-8",
                    )
                    _event, effective_sid = self._rotate(fake, output_dir, caller)
                    self.assertEqual(_fetch_sids(calls), [expected])
                    self.assertEqual(effective_sid, expected)

    def test_account_level_kinds_do_not_rotate(self):
        """quota/auth/unknown_407은 계정 수준 문제 — 교체 없이 원본 sid로."""
        for kind in ("quota", "auth", "unknown_407"):
            with self.subTest(kind=kind):
                calls: list = []
                fake = _fake_decodo(calls)
                _script_fetch_by_sid(
                    fake,
                    calls,
                    {"*": fake.DecodoError("계정 수준 문제", kind=kind)},
                )
                with tempfile.TemporaryDirectory() as tmp:
                    output_dir = Path(tmp) / "out"
                    _prepare(output_dir, [_product_row("viid-1")])
                    event, effective_sid = self._rotate(fake, output_dir, "b03")
                    # 점검 1회뿐 — sid를 바꿔 다시 보지 않는다.
                    self.assertEqual(_fetch_sids(calls), ["b03"])
                    self.assertEqual(effective_sid, "b03")
                    self.assertIs(event["ok"], False)
                    self.assertEqual(event["error_kind"], kind)
                    self.assertEqual(event["tried"], ["b03"])
                    self.assertFalse(
                        (output_dir / PROXY_STATE_FILENAME).exists()
                    )

    def test_rotation_caps_at_three_replacements(self):
        """교체 3회 상한 — 원본 포함 정확히 4회 점검, 5회째는 없다."""
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(
            fake, calls, {"*": fake.DecodoError("타임아웃", kind="timeout")}
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            event, effective_sid = self._rotate(fake, output_dir, "b03")
            self.assertEqual(effective_sid, "b03")
            self.assertEqual(_fetch_sids(calls), ["b03", "b04", "b05", "b06"])
            self.assertEqual(event["tried"], ["b03", "b04", "b05", "b06"])
            self.assertEqual(event["proxy_session_id"], "b03")
            self.assertIs(event["ok"], False)
            self.assertEqual(event["error_kind"], "timeout")
            # 실패 시 상태 파일은 갱신하지 않는다.
            self.assertFalse((output_dir / PROXY_STATE_FILENAME).exists())

    def test_non_numeric_sid_appends_two(self):
        """끝이 숫자가 아닌 sid는 2를 붙여 교체한다(bx→bx2)."""
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(
            fake,
            calls,
            {
                "bx": fake.DecodoError("HTTP 502 터널 실패", kind="response"),
                "*": _alive("2.2.2.2"),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            _event, effective_sid = self._rotate(fake, output_dir, "bx")
            self.assertEqual(_fetch_sids(calls), ["bx", "bx2"])
            self.assertEqual(effective_sid, "bx2")
            self.assertEqual(_proxy_state(output_dir)["session_id"], "bx2")

    def test_non_decodo_failure_records_exception_name(self):
        """DecodoError 외 예외는 종류 분류가 없어 교체 없이 실패로만 기록."""
        calls: list = []
        fake = _fake_decodo(
            calls, error=ConnectionError("네트워크 인터페이스 없음")
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            event, effective_sid = self._rotate(fake, output_dir, "c01")
            self.assertEqual(effective_sid, "c01")
            self.assertIs(event["ok"], False)
            self.assertEqual(event["error_kind"], "ConnectionError")
            self.assertEqual(event["tried"], ["c01"])

    def test_missing_decodo_module_is_reported_not_raised(self):
        # sys.modules 에 None 을 넣으면 import 가 실패한다(모듈 부재 시뮬레이션).
        # decodo 모듈이 없어도 점검 실패로만 기록하고 호출자 sid로 진행한다.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            with mock.patch.dict(sys.modules, {"app.core.decodo": None}):
                event, effective_sid = check_and_rotate_session(
                    output_dir, "b01"
                )
            self.assertEqual(effective_sid, "b01")
            self.assertIs(event["ok"], False)
            self.assertEqual(event["error_kind"], "ModuleNotFoundError")

    def test_success_event_feeds_append_run_log(self):
        """점검 이벤트는 append_run_log 로 그대로 JSONL 에 남는다(매니저 흐름)."""
        calls: list = []
        fake = _fake_decodo(
            calls, info={"ip": "203.0.113.9", "country_code": "KR"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            event, _sid = self._rotate(fake, output_dir, "b01")
            append_run_log(output_dir, event)
            append_run_log(output_dir, {"event": "pipeline_slot_finished"})
            checks = [
                line
                for line in _run_lines(output_dir)
                if line["event"] == "exit_ip_check"
            ]
            self.assertEqual(len(checks), 1)
            self.assertIs(checks[0]["ok"], True)
            self.assertEqual(checks[0]["proxy_session_id"], "b01")


if __name__ == "__main__":
    unittest.main()
