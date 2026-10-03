"""쿠팡 Patchright 상위 파이프라인 — proxy-session-id 통과·출구 IP 로깅(네트워크 없음).

병렬 확장 설계(PARALLEL_SCALE_OUT_DESIGN_20261001.md) §5.5: 파이프라인은
--proxy-session-id 를 자식 CLI(판매자/목록)에 통과시키고, 실행 시작 시
회선 출구 IP 확인 결과를 top_pipeline_runs.jsonl 에 1회 기록한다.
app/core/decodo 는 테스트마다 가짜 모듈로 대체해 오프라인으로 검증한다
(실제 모듈이 없어도 통과, 있으면 sys.modules 를 잠시 교체).

2026-10-01 b01 조기 사망 사건 대응: 세션이 죽었으면(오류 종류가 세션
수준) sid를 교체해 다시 점검하고, 성공한 sid를 자식에 전달하며 상태 파일
proxy_session_state.json 에 마지막 성공 sid를 남긴다. 전부 실패하면 CLI
인자 sid로 진행한다(현행 안전거동).
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core.coupang.patchright_top_sellers import TopSellerStore
from app.core.coupang.patchright_top_thousand import (
    PRODUCT_FIELDS,
    TopThousandStore,
)


def _load_pipeline():
    spec = importlib.util.spec_from_file_location(
        "coupang_top_pipeline_under_test",
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "prototypes"
        / "coupang_patchright_top_pipeline.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _run_main(module, argv: list[str]) -> int:
    with mock.patch.object(sys, "argv", ["pipeline", *argv]):
        return module.main()


def _run_lines(output_dir: Path) -> list[dict]:
    path = output_dir / "top_pipeline_runs.jsonl"
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


class _ChildStub:
    """subprocess.run 대체 — 자식 명령을 기록하고 지정 종료 코드를 돌려준다."""

    def __init__(self, returncode: int = 0) -> None:
        self.commands: list[list[str]] = []
        self._returncode = returncode

    def __call__(self, command, **_kwargs):
        self.commands.append(list(command))
        return SimpleNamespace(returncode=self._returncode, stdout='{"event": "stub_done"}', stderr="")


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


class ProxySessionIdPassThroughTest(unittest.TestCase):
    def test_sellers_child_receives_proxy_session_id(self):
        module = _load_pipeline()
        child = _ChildStub()
        calls: list = []
        fake = _fake_decodo(
            calls, info={"ip": "1.2.3.4", "country_code": "KR"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
                with mock.patch.object(subprocess, "run", child):
                    code = _run_main(
                        module,
                        [
                            "--output-dir",
                            str(output_dir),
                            "--state-root",
                            str(Path(tmp) / "guard"),
                            "--proxy-session-id",
                            "b01",
                        ],
                    )
            self.assertEqual(code, 0)
            self.assertEqual(len(child.commands), 1)
            command = child.commands[0]
            self.assertIn("coupang_patchright_top_sellers.py", command[1])
            self.assertIn("--proxy-session-id", command)
            self.assertEqual(
                command[command.index("--proxy-session-id") + 1], "b01"
            )

    def test_category_child_receives_proxy_session_id(self):
        module = _load_pipeline()
        child = _ChildStub()
        calls: list = []
        fake = _fake_decodo(
            calls, info={"ip": "5.6.7.8", "country_code": "KR"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            _complete_sellers(output_dir)
            with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
                with mock.patch.object(subprocess, "run", child):
                    code = _run_main(
                        module,
                        [
                            "--output-dir",
                            str(output_dir),
                            "--state-root",
                            str(Path(tmp) / "guard"),
                            "--proxy-session-id",
                            "c01",
                        ],
                    )
            self.assertEqual(code, 0)
            self.assertEqual(len(child.commands), 1)
            command = child.commands[0]
            self.assertIn("coupang_patchright_top_thousand.py", command[1])
            self.assertEqual(
                command[command.index("--proxy-session-id") + 1], "c01"
            )

    def test_empty_session_id_is_not_passed_and_never_checks_ip(self):
        module = _load_pipeline()
        child = _ChildStub()
        calls: list = []
        fake = _fake_decodo(calls)
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
                with mock.patch.object(subprocess, "run", child):
                    code = _run_main(
                        module,
                        [
                            "--output-dir",
                            str(output_dir),
                            "--state-root",
                            str(Path(tmp) / "guard"),
                            "--proxy-session-id",
                            "",
                        ],
                    )
            self.assertEqual(code, 0)
            self.assertEqual(len(child.commands), 1)
            self.assertNotIn("--proxy-session-id", child.commands[0])
            # sid가 비어 있으면 네트워크 호출 0회 + exit_ip_check 로그도 없음.
            self.assertEqual(
                [call[0] for call in calls],
                [],
            )
            self.assertEqual(
                [line for line in _run_lines(output_dir) if line["event"] == "exit_ip_check"],
                [],
            )


class ExitIpCheckTest(unittest.TestCase):
    def _run_with_sid(self, fake, output_dir: Path, state_root: Path) -> int:
        module = _load_pipeline()
        child = _ChildStub()
        with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
            with mock.patch.object(subprocess, "run", child):
                code = _run_main(
                    module,
                    [
                        "--output-dir",
                        str(output_dir),
                        "--state-root",
                        str(state_root),
                        "--proxy-session-id",
                        "b01",
                    ],
                )
        return code

    def test_success_logs_ip_once_and_continues(self):
        calls: list = []
        fake = _fake_decodo(calls, info={"ip": "203.0.113.9", "country_code": "KR"})
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            code = self._run_with_sid(fake, output_dir, Path(tmp) / "guard")
            self.assertEqual(code, 0)
            checks = [
                line
                for line in _run_lines(output_dir)
                if line["event"] == "exit_ip_check"
            ]
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]["proxy_session_id"], "b01")
            self.assertEqual(checks[0]["ip"], "203.0.113.9")
            self.assertEqual(checks[0]["country_code"], "KR")
            self.assertIs(checks[0]["ok"], True)
            # 이벤트는 실행 로그의 첫 줄(choose_action/자식 실행보다 앞서므로).
            self.assertEqual(_run_lines(output_dir)[0]["event"], "exit_ip_check")
            # 프록시 dict는 sid로 조립되고 호출은 1회뿐이다.
            self.assertEqual(
                [call[0] for call in calls],
                ["load_settings", "sticky_proxy_dict", "fetch_exit_ip"],
            )
            self.assertEqual(calls[1][1], "b01")
            self.assertEqual(
                calls[2][1]["username"],
                "user-sp3lqmo64w-session-b01",
            )

    def test_decodo_failure_is_logged_and_run_continues(self):
        calls: list = []
        fake = _fake_decodo(calls, error=None)
        # 자격 미완비(sticky_proxy_dict → None)처럼 점검만 실패하는 회선 상황.
        fake.sticky_proxy_dict = lambda settings=None, session_id="": None
        fake.fetch_exit_ip = lambda proxy, **_kwargs: (_ for _ in ()).throw(
            fake.DecodoError("프록시 정보가 없습니다", kind="other")
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            module = _load_pipeline()
            child = _ChildStub()
            with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
                with mock.patch.object(subprocess, "run", child):
                    code = _run_main(
                        module,
                        [
                            "--output-dir",
                            str(output_dir),
                            "--state-root",
                            str(Path(tmp) / "guard"),
                            "--proxy-session-id",
                            "b01",
                        ],
                    )
            self.assertEqual(code, 0)
            # 실패해도 자식 실행은 계속된다.
            self.assertEqual(len(child.commands), 1)
            checks = [
                line
                for line in _run_lines(output_dir)
                if line["event"] == "exit_ip_check"
            ]
            self.assertEqual(len(checks), 1)
            self.assertIs(checks[0]["ok"], False)
            self.assertEqual(checks[0]["error_kind"], "other")
            self.assertNotIn("ip", checks[0])

    def test_non_decodo_failure_records_exception_name(self):
        module = _load_pipeline()
        child = _ChildStub()
        calls: list = []
        fake = _fake_decodo(
            calls, error=ConnectionError("네트워크 인터페이스 없음")
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
                with mock.patch.object(subprocess, "run", child):
                    code = _run_main(
                        module,
                        [
                            "--output-dir",
                            str(output_dir),
                            "--state-root",
                            str(Path(tmp) / "guard"),
                            "--proxy-session-id",
                            "c01",
                        ],
                    )
            self.assertEqual(code, 0)
            self.assertEqual(len(child.commands), 1)
            checks = [
                line
                for line in _run_lines(output_dir)
                if line["event"] == "exit_ip_check"
            ]
            self.assertEqual(checks[0]["error_kind"], "ConnectionError")
            self.assertIs(checks[0]["ok"], False)

    def test_decodo_error_kind_is_reported(self):
        module = _load_pipeline()
        child = _ChildStub()
        calls: list = []
        fake = _fake_decodo(calls)
        fake.fetch_exit_ip = lambda proxy, **_kwargs: (_ for _ in ()).throw(
            fake.DecodoError("Decodo 데이터 사용량이 소진됐습니다", kind="quota")
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
                with mock.patch.object(subprocess, "run", child):
                    code = _run_main(
                        module,
                        [
                            "--output-dir",
                            str(output_dir),
                            "--state-root",
                            str(Path(tmp) / "guard"),
                            "--proxy-session-id",
                            "b01",
                        ],
                    )
            self.assertEqual(code, 0)
            checks = [
                line
                for line in _run_lines(output_dir)
                if line["event"] == "exit_ip_check"
            ]
            self.assertEqual(checks[0]["error_kind"], "quota")
            self.assertIs(checks[0]["ok"], False)

    def test_status_only_never_calls_fetch_exit_ip(self):
        module = _load_pipeline()
        child = _ChildStub()
        calls: list = []
        fake = _fake_decodo(
            calls, info={"ip": "203.0.113.9", "country_code": "KR"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
                with mock.patch.object(subprocess, "run", child):
                    code = _run_main(
                        module,
                        [
                            "--output-dir",
                            str(output_dir),
                            "--state-root",
                            str(Path(tmp) / "guard"),
                            "--proxy-session-id",
                            "b01",
                            "--status-only",
                        ],
                    )
            self.assertEqual(code, 0)
            # status-only은 네트워크 호출 0회(무네트워크 원칙) + 자식 미실행.
            self.assertEqual(calls, [])
            self.assertEqual(child.commands, [])
            self.assertEqual(_run_lines(output_dir), [])

    def test_missing_decodo_module_is_logged_not_raised(self):
        # sys.modules 에 None 을 넣으면 import 가 실패한다(모듈 부재 시뮬레이션).
        # decodo.py 가 아직 워크트리에 없어도 점검 실패로만 기록하고 진행해야 한다.
        module = _load_pipeline()
        child = _ChildStub()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            with mock.patch.dict(sys.modules, {"app.core.decodo": None}):
                with mock.patch.object(subprocess, "run", child):
                    code = _run_main(
                        module,
                        [
                            "--output-dir",
                            str(output_dir),
                            "--state-root",
                            str(Path(tmp) / "guard"),
                            "--proxy-session-id",
                            "b01",
                        ],
                    )
            self.assertEqual(code, 0)
            self.assertEqual(len(child.commands), 1)
            checks = [
                line
                for line in _run_lines(output_dir)
                if line["event"] == "exit_ip_check"
            ]
            self.assertEqual(len(checks), 1)
            self.assertIs(checks[0]["ok"], False)
            self.assertEqual(checks[0]["error_kind"], "ModuleNotFoundError")


if __name__ == "__main__":
    unittest.main()


class FreshInstanceBootstrapTest(unittest.TestCase):
    """신규 인스턴스 부트스트랩 — 상품 파일이 없으면 목록 단계부터 시작한다.

    2026-10-01 통합에서 발견: 빈 출력 폴더에서 _work가 top_products.csv
    읽기를 예외로 던져 halted(종료 23)·예약 자동 비활성으로 빠졌다.
    신규 인스턴스(B/C)의 판매자 대기열은 정의상 비어 있으므로 목록 단계가
    첫 동작이어야 한다.
    """

    def test_fresh_output_dir_starts_with_category(self) -> None:
        pipeline = _load_pipeline()
        with tempfile.TemporaryDirectory() as tmp:
            action, _reason = pipeline.choose_action(Path(tmp))
            self.assertEqual(action, "category")

    def test_products_file_keeps_seller_priority(self) -> None:
        pipeline = _load_pipeline()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _prepare(output_dir, [_product_row("9001")])
            action, _reason = pipeline.choose_action(output_dir)
            self.assertEqual(action, "sellers")


class CategoriesFilePassThroughTest(unittest.TestCase):
    """--categories-file 통과 — 목록 자식에만 전달(설계 §3.1 작업 분할)."""

    def test_category_child_receives_categories_file(self):
        module = _load_pipeline()
        child = _ChildStub()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_b_product_row("viid-1")])
            _complete_sellers(output_dir)
            # _prepare 가 만드는 상태 파일은 기본 A 가족이므로 B 진행 상태로
            # 덮어쓴다(실제 B 인스턴스 폴더는 목록 자식이 B 상태를 만든다).
            (output_dir / "top_state.json").write_text(
                json.dumps(_b_running_state(), ensure_ascii=False),
                encoding="utf-8",
            )
            family_path = _family_b_path(root)
            with mock.patch.object(subprocess, "run", child):
                code = _run_main(
                    module,
                    [
                        "--output-dir",
                        str(output_dir),
                        "--state-root",
                        str(root / "guard"),
                        "--categories-file",
                        str(family_path),
                    ],
                )
        self.assertEqual(code, 0)
        self.assertEqual(len(child.commands), 1)
        command = child.commands[0]
        self.assertIn("coupang_patchright_top_thousand.py", command[1])
        self.assertIn("--categories-file", command)
        self.assertEqual(
            command[command.index("--categories-file") + 1], str(family_path)
        )

    def test_sellers_child_does_not_receive_categories_file(self):
        """판매자 단계는 상품 CSV 기반이라 가족 파일을 받지 않는다."""
        module = _load_pipeline()
        child = _ChildStub()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_b_product_row("viid-1")])
            family_path = _family_b_path(root)
            with mock.patch.object(subprocess, "run", child):
                code = _run_main(
                    module,
                    [
                        "--output-dir",
                        str(output_dir),
                        "--state-root",
                        str(root / "guard"),
                        "--categories-file",
                        str(family_path),
                    ],
                )
        self.assertEqual(code, 0)
        command = child.commands[0]
        self.assertIn("coupang_patchright_top_sellers.py", command[1])
        self.assertNotIn("--categories-file", command)

    def test_category_child_without_file_passes_nothing(self):
        module = _load_pipeline()
        child = _ChildStub()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            _complete_sellers(output_dir)
            with mock.patch.object(subprocess, "run", child):
                code = _run_main(
                    module,
                    [
                        "--output-dir",
                        str(output_dir),
                        "--state-root",
                        str(Path(tmp) / "guard"),
                    ],
                )
        self.assertEqual(code, 0)
        command = child.commands[0]
        self.assertIn("coupang_patchright_top_thousand.py", command[1])
        self.assertNotIn("--categories-file", command)

    def test_invalid_categories_file_exits_before_any_child(self):
        module = _load_pipeline()
        child = _ChildStub()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_b_product_row("viid-1")])
            bad_path = root / "bad_family.json"
            bad_path.write_text(
                json.dumps({"categories": []}), encoding="utf-8"
            )
            with mock.patch.object(subprocess, "run", child), mock.patch(
                "sys.stderr", new_callable=lambda: io.StringIO()
            ):
                with self.assertRaises(SystemExit) as caught:
                    _run_main(
                        module,
                        [
                            "--output-dir",
                            str(output_dir),
                            "--state-root",
                            str(root / "guard"),
                            "--categories-file",
                            str(bad_path),
                        ],
                    )
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(child.commands, [])


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
        module = _load_pipeline()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = self._completed_b_dir(root)
            family_path = _family_b_path(root)
            finals = module.build_all_finals(
                output_dir, categories_file=family_path
            )
            self.assertEqual(set(finals), {"sellers", "all"})
            self.assertFalse((output_dir / "final_dataset_194373.csv").exists())
            self.assertFalse((output_dir / "final_dataset_194688.csv").exists())
            all_rows = _read_rows(output_dir / "final_dataset_all.csv")
            self.assertEqual(
                [row["vendor_item_id"] for row in all_rows], ["viid-1"]
            )

    def test_without_categories_file_makes_family_finals(self):
        module = _load_pipeline()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = self._completed_b_dir(root)
            finals = module.build_all_finals(output_dir)
            self.assertEqual(set(finals), {"sellers", "194373", "194688", "all"})
            for name in (
                "final_dataset_194373.csv",
                "final_dataset_194688.csv",
                "final_dataset_all.csv",
            ):
                self.assertTrue((output_dir / name).exists(), name)


class FamilyBCompletePipelineTest(unittest.TestCase):
    """B 가족 완주 출력 폴더의 complete 경로 — 가족 파일 유무로 갈린다."""

    def test_complete_path_builds_sellers_and_all_only(self):
        module = _load_pipeline()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_b_product_row("viid-1")])
            _complete_sellers(output_dir)
            (output_dir / "top_state.json").write_text(
                json.dumps(_b_done_state(), ensure_ascii=False),
                encoding="utf-8",
            )
            family_path = _family_b_path(root)
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                code = _run_main(
                    module,
                    [
                        "--output-dir",
                        str(output_dir),
                        "--state-root",
                        str(root / "guard"),
                        "--categories-file",
                        str(family_path),
                    ],
                )
            self.assertEqual(module.PIPELINE_COMPLETE, 30)
            self.assertEqual(code, 30)
            self.assertFalse(
                (output_dir / "final_dataset_194373.csv").exists()
            )
            self.assertTrue((output_dir / "final_dataset_all.csv").exists())
            completes = [
                line
                for line in _run_lines(output_dir)
                if line["event"] == "pipeline_complete"
            ]
            self.assertEqual(len(completes), 1)
            self.assertEqual(
                set(completes[0]["finals"]), {"sellers", "all"}
            )

    def test_family_b_state_without_file_halts(self):
        """가족 파일 없이 B 상태 폴더를 돌면 halted(23) — 가족 혼용 방지."""
        module = _load_pipeline()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            _prepare(output_dir, [_b_product_row("viid-1")])
            _complete_sellers(output_dir)
            (output_dir / "top_state.json").write_text(
                json.dumps(_b_done_state(), ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                code = _run_main(
                    module,
                    [
                        "--output-dir",
                        str(output_dir),
                        "--state-root",
                        str(root / "guard"),
                    ],
                )
            self.assertEqual(code, 23)
            self.assertEqual(
                [line["event"] for line in _run_lines(output_dir)],
                ["pipeline_halted"],
            )


def _alive(ip: str = "175.203.56.170") -> SimpleNamespace:
    return SimpleNamespace(ip=ip, country_code="KR", country_name="Korea")


def _proxy_state(output_dir: Path) -> dict:
    return json.loads(
        (output_dir / "proxy_session_state.json").read_text(encoding="utf-8")
    )


def _exit_ip_checks(output_dir: Path) -> list[dict]:
    return [
        line
        for line in _run_lines(output_dir)
        if line["event"] == "exit_ip_check"
    ]


def _child_sid(command: list[str]) -> str:
    return command[command.index("--proxy-session-id") + 1]


def _fetch_sids(calls: list) -> list[str]:
    return [call[1] for call in calls if call[0] == "fetch_exit_ip"]


class SessionRotationTest(unittest.TestCase):
    """Decodo 세션 사망 시 자동 sid 교체 — 2026-10-01 b01 조기 사망 사건 대응.

    시작 sid는 상태 파일(proxy_session_state.json)의 마지막 성공 sid,
    없으면 CLI 인자 sid. 세션 수준 오류(response/connection/timeout/other)
    는 sid를 +1 해 재점검(최대 교체 3회), 계정 수준 오류(quota/auth/
    unknown_407)는 교체 없다. 성공 sid를 자식에 전달하고 상태 파일에 저장,
    전부 실패하면 CLI 인자 sid로 진행한다(현행 안전거동).
    """

    def _run_pipeline(self, fake, output_dir: Path, state_root: Path, sid: str,
                      *, status_only: bool = False,
                      child: _ChildStub | None = None) -> tuple[int, _ChildStub]:
        module = _load_pipeline()
        child = child if child is not None else _ChildStub()
        argv = [
            "--output-dir", str(output_dir),
            "--state-root", str(state_root),
            "--proxy-session-id", sid,
        ]
        if status_only:
            argv.append("--status-only")
        with mock.patch.dict(sys.modules, {"app.core.decodo": fake}), \
                mock.patch.object(subprocess, "run", child):
            code = _run_main(module, argv)
        return code, child

    def test_dead_session_rotates_and_child_uses_new_sid(self):
        """첫 sid 사망(response)→ 교체 성공: 자식·상태 파일·이벤트 모두 교체 sid."""
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
            code, child = self._run_pipeline(
                fake, output_dir, Path(tmp) / "guard", "b03"
            )
            self.assertEqual(code, 0)
            # 자식은 교체된 sid를 받는다(CLI 인자 b03이 아님). 자릿수 보존.
            self.assertEqual(_child_sid(child.commands[0]), "b04")
            # 상태 파일에는 마지막 성공 sid가 남는다.
            state = _proxy_state(output_dir)
            self.assertEqual(state["session_id"], "b04")
            self.assertRegex(
                state["updated_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
            )
            # 이벤트 1회에 rotated/tried 가 기록된다.
            self.assertEqual(len(_exit_ip_checks(output_dir)), 1)
            check = _exit_ip_checks(output_dir)[0]
            self.assertIs(check["ok"], True)
            self.assertIs(check["rotated"], True)
            self.assertEqual(check["tried"], ["b03", "b04"])
            self.assertEqual(check["proxy_session_id"], "b04")
            self.assertEqual(check["ip"], "175.203.56.170")
            self.assertEqual(_fetch_sids(calls), ["b03", "b04"])

    def test_state_file_sid_is_starting_point(self):
        """상태 파일의 마지막 성공 sid가 시작점 — CLI 인자 sid보다 우선."""
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(fake, calls, {"*": _alive("1.1.1.1")})
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            (output_dir / "proxy_session_state.json").write_text(
                json.dumps(
                    {"session_id": "b07", "updated_at": "2026-10-01 21:09:28"}
                ),
                encoding="utf-8",
            )
            code, child = self._run_pipeline(
                fake, output_dir, Path(tmp) / "guard", "b03"
            )
            self.assertEqual(code, 0)
            self.assertEqual(_fetch_sids(calls), ["b07"])
            self.assertEqual(_child_sid(child.commands[0]), "b07")
            check = _exit_ip_checks(output_dir)[0]
            self.assertIs(check["ok"], True)
            self.assertIs(check["rotated"], False)
            self.assertEqual(check["tried"], ["b07"])
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
            (output_dir / "proxy_session_state.json").write_text(
                json.dumps({"session_id": "b07", "updated_at": "2026-10-01 21:09"}),
                encoding="utf-8",
            )
            code, child = self._run_pipeline(
                fake, output_dir, Path(tmp) / "guard", "b03"
            )
            self.assertEqual(code, 0)
            self.assertEqual(_fetch_sids(calls), ["b07", "b08"])
            self.assertEqual(_child_sid(child.commands[0]), "b08")
            self.assertEqual(_proxy_state(output_dir)["session_id"], "b08")

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
                    code, child = self._run_pipeline(
                        fake, output_dir, Path(tmp) / "guard", "b03"
                    )
                    self.assertEqual(code, 0)
                    # 점검 1회뿐 — sid를 바꿔 다시 보지 않는다.
                    self.assertEqual(_fetch_sids(calls), ["b03"])
                    self.assertEqual(_child_sid(child.commands[0]), "b03")
                    check = _exit_ip_checks(output_dir)[0]
                    self.assertIs(check["ok"], False)
                    self.assertEqual(check["error_kind"], kind)
                    self.assertEqual(check["tried"], ["b03"])
                    self.assertFalse(
                        (output_dir / "proxy_session_state.json").exists()
                    )

    def test_all_dead_runs_child_with_cli_sid_and_keeps_exit_code(self):
        """전부 사망 → CLI 인자 sid로 자식 실행, 자식 종료 코드 그대로 통과."""
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(
            fake,
            calls,
            {"*": fake.DecodoError("HTTP 502 터널 실패", kind="response")},
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            code, child = self._run_pipeline(
                fake,
                output_dir,
                Path(tmp) / "guard",
                "b03",
                child=_ChildStub(returncode=17),
            )
            # 자식 실패(17)가 파이프라인 종료 코드로 그대로 흐른다
            # (실행기가 예약을 끄는 현행 경로 유지).
            self.assertEqual(code, 17)
            self.assertEqual(_child_sid(child.commands[0]), "b03")
            check = _exit_ip_checks(output_dir)[0]
            self.assertIs(check["ok"], False)
            self.assertEqual(check["error_kind"], "response")
            self.assertIs(check["rotated"], False)
            # 실패 시 상태 파일은 갱신하지 않는다.
            self.assertFalse(
                (output_dir / "proxy_session_state.json").exists()
            )

    def test_rotation_caps_at_three_replacements(self):
        """교체 3회 상한 — 원본 포함 정확히 4회 점검, 5회째는 없다."""
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(
            fake,
            calls,
            {"*": fake.DecodoError("타임아웃", kind="timeout")},
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            code, _child = self._run_pipeline(
                fake, output_dir, Path(tmp) / "guard", "b03"
            )
            self.assertEqual(code, 0)
            self.assertEqual(_fetch_sids(calls), ["b03", "b04", "b05", "b06"])
            check = _exit_ip_checks(output_dir)[0]
            self.assertEqual(check["tried"], ["b03", "b04", "b05", "b06"])
            self.assertEqual(check["proxy_session_id"], "b03")

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
            code, child = self._run_pipeline(
                fake, output_dir, Path(tmp) / "guard", "bx"
            )
            self.assertEqual(code, 0)
            self.assertEqual(_fetch_sids(calls), ["bx", "bx2"])
            self.assertEqual(_child_sid(child.commands[0]), "bx2")
            self.assertEqual(_proxy_state(output_dir)["session_id"], "bx2")

    def test_status_only_skips_check_even_with_state_file(self):
        """status-only은 상태 파일이 있어도 점검·자식·로그 전부 0회."""
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(fake, calls, {"*": _alive()})
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            _prepare(output_dir, [_product_row("viid-1")])
            (output_dir / "proxy_session_state.json").write_text(
                json.dumps({"session_id": "b09", "updated_at": "2026-10-01 22:00:00"}),
                encoding="utf-8",
            )
            code, child = self._run_pipeline(
                fake, output_dir, Path(tmp) / "guard", "b03", status_only=True
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls, [])
            self.assertEqual(child.commands, [])
            self.assertEqual(_run_lines(output_dir), [])
            # 상태 파일도 그대로다.
            self.assertEqual(_proxy_state(output_dir)["session_id"], "b09")
