"""쿠팡 병렬 확장 — 다중 인스턴스 오프라인 검증 테스트(네트워크 없음).

사용자 결정(2026-10-02): 인스턴스 간 격리·세션 교체·가드 독립 같은 병렬
확장 속성은 라이브 실행이 아니라 오프라인 테스트로 갖춘다. 지금까지 실제
예약 실행으로만 확인했던 아래 내용을 가짜 자식 프로세스·가짜 decodo 로
고정한다(실접속·네트워크 호출 0회).

- 가족 파일 스키마 2종(주방 B·반려동물 C)의 카테고리 id 정확성
- 3인스턴스(A/B/C) 동시 부트스트랩 — 가족·출력 폴더·가드·회선 상태 분리
- B 의 세션 자동 교체가 C 의 세션 상태에 영향을 주지 않음
- 서로 다른 state_root 가드는 같은 시각에 서로를 막지 않음(간격 제한은
  장부 단위)
- 터널 장애(ERR_TUNNEL_CONNECTION_FAILED) halted 컨트롤의 현행 동작 고정
- 회전이 일어난 실행의 자식 명령에 교체 sid 와 --categories-file 조합
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core.coupang.patchright_canary import claim_live_attempt, profile_dir
from app.core.coupang.patchright_top_thousand import (
    MAX_LISTING_ITEMS,
    MAX_SESSION_PRODUCTS,
    TOP_CATEGORIES,
    TopThousandStore,
    load_categories_file,
    read_state,
)


def _load_pipeline():
    spec = importlib.util.spec_from_file_location(
        "coupang_top_pipeline_parallel_under_test",
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "prototypes"
        / "coupang_patchright_top_pipeline.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 워크트리에 함께 배포된 가족 정의 파일(§8 등록 명령이 이 경로를 쓴다).
_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
_KITCHEN_B_PATH = (
    _WORKTREE_ROOT / "scripts" / "prototypes" / "coupang_categories_kitchen_b.json"
)
_PET_C_PATH = (
    _WORKTREE_ROOT / "scripts" / "prototypes" / "coupang_categories_pet_c.json"
)

_KITCHEN_B_IDS = ["185671", "185735", "185872"]
_PET_C_IDS = ["118876", "118878", "381522"]


def _fake_decodo(calls: list, *, info=None, error=None):
    """파이프라인이 늦게 import 하는 app.core.decodo 의 가짜 대체품.

    calls 에 (settings, session_id) / proxy 기록을 남겨 호출 횟수를 검증한다.
    실제 decodo 모듈 존재 여부와 무관하게 동작한다(sys.modules 교체).
    info 를 생략하면 모든 sid 가 산 회선(정상 KR)으로 응답한다.
    """
    module = types.ModuleType("app.core.decodo")

    class DecodoError(RuntimeError):
        def __init__(self, message: str, *, kind: str = "other") -> None:
            super().__init__(message)
            self.kind = kind

    alive = info or {"ip": "203.0.113.9", "country_code": "KR"}

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
        calls.append(("fetch_exit_ip", proxy))
        if error is not None:
            raise error
        return SimpleNamespace(
            ip=alive["ip"], country_code=alive["country_code"], country_name="Korea"
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


class _CategoryChildStub:
    """subprocess.run 대체 — 목록 자식의 오프라인 부분을 실제 코드로 재현.

    실제 자식(coupang_patchright_top_thousand.py → run_top_pages)은 브라우저를
    열기 전에 ① TopThousandStore.ensure_files() 로 top_state.json /
    top_products.csv / top_summary.json 을 만들고 ② read_state 로 상태 파일이
    이 인스턴스의 가족과 일치하는지 검증한 뒤 ③ claim_live_attempt 로
    state_root 의 canary_guard.json 을 쓰고 chrome_profile 폴더를 만든다.
    이 스텁은 그 세 단계를 실제 라이브러리 함수로 수행해 인스턴스별 파일
    격리를 있는 그대로 검증하고, 브라우저·네트워크 단계만 생략해
    top_pages_completed(0) 를 돌려준다. now_fn 을 주면 가드 claim 시각을
    스텁 시계로 조정한다(예약 간격 시뮬레이션).
    """

    def __init__(self, *, now_fn=None) -> None:
        self.commands: list[list[str]] = []
        self.claims: list[tuple[Path, bool, str]] = []
        self._now_fn = now_fn

    def __call__(self, command, **_kwargs):
        self.commands.append(list(command))

        def value(flag: str) -> str | None:
            return command[command.index(flag) + 1] if flag in command else None

        output_dir = Path(value("--output-dir"))
        state_root = Path(value("--state-root"))
        categories_file = value("--categories-file")
        categories = (
            load_categories_file(Path(categories_file))
            if categories_file
            else None
        )
        store = TopThousandStore(output_dir, categories)
        store.ensure_files()
        state = read_state(output_dir, categories)
        pages = int(value("--pages") or 8)
        planned_items = min(pages * MAX_LISTING_ITEMS, MAX_SESSION_PRODUCTS)
        claim_kwargs = {"now": self._now_fn()} if self._now_fn else {}
        allowed, reason = claim_live_attempt(
            state_root,
            planned_items=planned_items,
            planned_pages=pages,
            **claim_kwargs,
        )
        self.claims.append((state_root, allowed, reason))
        # 브라우저 프로필도 자식이 만드는 그대로 state_root 아래에 둔다.
        profile_dir(state_root).mkdir(parents=True, exist_ok=True)
        if not allowed:
            return SimpleNamespace(
                returncode=21,
                stdout=json.dumps({"event": "guard_refused", "reason": reason}),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "event": "top_pages_completed",
                    "category_id": store.family[state["category_index"]][0],
                }
            ),
            stderr="",
        )


def _run_pipeline_main(
    module, argv: list[str], *, child, decodo=None
) -> int:
    """파이프라인 main 을 1회 돌린다. 자식은 스텁으로, decodo 는 가짜로."""
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(sys, "argv", ["pipeline", *argv]))
        stack.enter_context(mock.patch.object(subprocess, "run", child))
        stack.enter_context(mock.patch("sys.stdout", new_callable=io.StringIO))
        if decodo is not None:
            stack.enter_context(
                mock.patch.dict(sys.modules, {"app.core.decodo": decodo})
            )
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


def _state_category_ids(output_dir: Path) -> list[str]:
    state = json.loads(
        (output_dir / "top_state.json").read_text(encoding="utf-8")
    )
    return list(state["categories"])


# 파이프라인·자식이 출력 폴더에 만드는 파일 — 인스턴스 소유 검증 대상.
_TRACKED_OUTPUT_FILES = {
    "top_state.json",
    "top_products.csv",
    "top_summary.json",
    "top_sellers.csv",
    "top_product_seller.csv",
    "top_failed_sellers.json",
    "top_failed_mappings.json",
    "top_pipeline_runs.jsonl",
    "proxy_session_state.json",
}


class FamilyFileSchemaTest(unittest.TestCase):
    """가족 파일 스키마 2종 — 배포된 실제 파일의 카테고리 id 를 정확히 고정.

    기존 kitchen 테스트(test_coupang_patchright_top_thousand.py)는 로더
    형식 위주로 검증하므로 여기서는 두 파일의 id 3개씩을 정확히 단정하고,
    세 가족(A 기본·B 주방·C 반려동물)이 서로 겹치지 않음을 확인한다.
    카테고리가 겹치면 인스턴스 간 작업 분할(설계 §3.1)이 성립하지 않는다.
    """

    def test_pet_family_file_loads_exact_ids_and_names(self):
        pairs = load_categories_file(_PET_C_PATH)
        self.assertEqual(
            [category_id for category_id, _ in pairs],
            ["118876", "118878", "381522"],
        )
        self.assertEqual(
            [name for _, name in pairs],
            [
                "반려동물용품/강아지 용품",
                "반려동물용품/고양이 용품",
                "반려동물용품/펫티켓 산책용품",
            ],
        )
        value = json.loads(_PET_C_PATH.read_text(encoding="utf-8"))
        # instance 는 C 회선의 살아있는 세션 id 를 따라간다(교체마다 갱신).
        # 고정값 단정은 깨지므로 형식(c+숫자)만 검증한다.
        self.assertRegex(value["instance"], r"^c\d+$")
        self.assertIn("병렬 확장 C", value["description"])

    def test_kitchen_family_file_loads_exact_ids_and_names(self):
        pairs = load_categories_file(_KITCHEN_B_PATH)
        self.assertEqual(
            [category_id for category_id, _ in pairs],
            ["185671", "185735", "185872"],
        )
        self.assertEqual(
            [name for _, name in pairs],
            [
                "주방용품/냄비/프라이팬",
                "주방용품/그릇/홈세트",
                "주방용품/밀폐저장/도시락",
            ],
        )

    def test_deployed_families_are_mutually_disjoint(self):
        pet = {category_id for category_id, _ in load_categories_file(_PET_C_PATH)}
        kitchen = {
            category_id for category_id, _ in load_categories_file(_KITCHEN_B_PATH)
        }
        default_a = {category_id for category_id, _ in TOP_CATEGORIES}
        self.assertFalse(pet & kitchen)
        self.assertFalse(pet & default_a)
        self.assertFalse(kitchen & default_a)


class ThreeInstanceIsolationTest(unittest.TestCase):
    """3인스턴스(A/B/C) 동시 부트스트랩 — 파일·가드·프로필·회선 상태 분리.

    A 는 기본 가족(견과/축산 19개)에 sid 없음, B 는 주방 가족 + sid b04,
    C 는 반려동물 가족 + sid c01. 세 파이프라인을 나란히 1회씩 돌려 각자
    가족 상태만 만들고 남의 출력 폴더·가드 폴더에 한 건도 안 쓰이는지
    확인한다(실제 예약은 시각이 겹치므로 같은 틱에서 실행한다).
    """

    def test_each_instance_writes_only_its_own_family_and_files(self):
        module = _load_pipeline()
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dirs = {
                "A": (root / "out_A", root / "guard_A"),
                "B": (root / "out_B", root / "guard_B"),
                "C": (root / "out_C", root / "guard_C"),
            }
            plans = {
                "A": {"categories_file": None, "sid": ""},
                "B": {"categories_file": _KITCHEN_B_PATH, "sid": "b04"},
                "C": {"categories_file": _PET_C_PATH, "sid": "c01"},
            }
            stubs: dict[str, _CategoryChildStub] = {}
            for name, (output_dir, state_root) in dirs.items():
                stubs[name] = _CategoryChildStub()
                argv = [
                    "--output-dir",
                    str(output_dir),
                    "--state-root",
                    str(state_root),
                ]
                if plans[name]["categories_file"] is not None:
                    argv += ["--categories-file", str(plans[name]["categories_file"])]
                if plans[name]["sid"]:
                    argv += ["--proxy-session-id", plans[name]["sid"]]
                code = _run_pipeline_main(
                    module,
                    argv,
                    child=stubs[name],
                    decodo=(
                        _fake_decodo(calls) if plans[name]["sid"] else None
                    ),
                )
                self.assertEqual(code, 0, name)

            # 각 인스턴스 정확히 자식 1회(신규 폴더는 판매자 대기열이 비어
            # 목록 단계가 첫 동작)이고, 자식은 자기 인스턴스의 디렉을 받는다.
            for name, stub in stubs.items():
                self.assertEqual(len(stub.commands), 1, name)
                command = stub.commands[0]
                self.assertIn("coupang_patchright_top_thousand.py", command[1])
                output_dir, state_root = dirs[name]
                self.assertEqual(
                    Path(command[command.index("--output-dir") + 1]), output_dir
                )
                self.assertEqual(
                    Path(command[command.index("--state-root") + 1]), state_root
                )
            self.assertNotIn("--proxy-session-id", stubs["A"].commands[0])
            self.assertNotIn("--categories-file", stubs["A"].commands[0])
            self.assertEqual(_child_sid(stubs["B"].commands[0]), "b04")
            self.assertEqual(_child_sid(stubs["C"].commands[0]), "c01")
            self.assertEqual(
                stubs["B"].commands[0][
                    stubs["B"].commands[0].index("--categories-file") + 1
                ],
                str(_KITCHEN_B_PATH),
            )
            self.assertEqual(
                stubs["C"].commands[0][
                    stubs["C"].commands[0].index("--categories-file") + 1
                ],
                str(_PET_C_PATH),
            )

            # 각 출력 디렉의 top_state.json 카테고리 id가 각자 가족과 정확히
            # 일치한다(A 는 기본 견과/축산 19개, B/C 는 파일의 3개).
            expected_ids = {
                "A": [category_id for category_id, _ in TOP_CATEGORIES],
                "B": _KITCHEN_B_IDS,
                "C": _PET_C_IDS,
            }
            self.assertEqual(len(expected_ids["A"]), 19)
            for name, (output_dir, _state_root) in dirs.items():
                self.assertEqual(
                    _state_category_ids(output_dir), expected_ids[name], name
                )
                # 다른 인스턴스 가족 id가 섞여 들어오지 않는다.
                for other, other_ids in expected_ids.items():
                    if other != name:
                        self.assertFalse(
                            set(expected_ids[name]) & set(other_ids), (name, other)
                        )

            # 상품/상태 파일이 다른 인스턴스 디렉에 한 건도 안 쓰였다 —
            # 임시 워크트리 전체를 훨쳐 해당 파일은 세 출력 디렉 안에만 있다.
            output_dirs = {output_dir for output_dir, _state_root in dirs.values()}
            for path in root.rglob("*"):
                if path.is_file() and path.name in _TRACKED_OUTPUT_FILES:
                    self.assertIn(path.parent, output_dirs, path)
            # 각 출력 디렉에는 자기 파일만 정확히 있다(A 는 sid 없음).
            base_files = {
                "top_state.json",
                "top_products.csv",
                "top_summary.json",
                "top_sellers.csv",
                "top_product_seller.csv",
                "top_failed_sellers.json",
                "top_failed_mappings.json",
                "top_pipeline_runs.jsonl",
            }
            for name, (output_dir, _state_root) in dirs.items():
                names = {p.name for p in output_dir.iterdir() if p.is_file()}
                self.assertEqual(
                    names,
                    base_files | ({"proxy_session_state.json"} if name != "A" else set()),
                    name,
                )

            # 실행 로그도 각자 디렉에 남는다(sid 없는 A 는 점검 이벤트 없음).
            self.assertEqual(
                [line["event"] for line in _run_lines(dirs["A"][0])],
                ["pipeline_slot_finished"],
            )
            for name in ("B", "C"):
                self.assertEqual(
                    [line["event"] for line in _run_lines(dirs[name][0])],
                    ["exit_ip_check", "pipeline_slot_finished"],
                    name,
                )

            # 세 인스턴스의 canary_guard.json(state_root)은 전부 별개 파일.
            guards = [
                state_root / "canary_guard.json" for _out, state_root in dirs.values()
            ]
            self.assertTrue(all(guard.exists() for guard in guards))
            self.assertEqual(len({str(guard) for guard in guards}), 3)
            for guard in guards:
                ledger = json.loads(guard.read_text(encoding="utf-8"))
                self.assertEqual(ledger["daily_sessions"], 1)
            # 가드 claim 도 전부 허용 — 인스턴스별 장부라 서로를 막지 않는다.
            for name, stub in stubs.items():
                self.assertEqual(
                    [allowed for _root, allowed, _reason in stub.claims], [True], name
                )

            # 세 인스턴스의 proxy_session_state.json 도 전부 별개 파일(B/C 에만).
            state_b = dirs["B"][0] / "proxy_session_state.json"
            state_c = dirs["C"][0] / "proxy_session_state.json"
            self.assertNotEqual(state_b, state_c)
            self.assertFalse(
                (dirs["A"][0] / "proxy_session_state.json").exists()
            )
            self.assertEqual(_proxy_state(dirs["B"][0])["session_id"], "b04")
            self.assertEqual(_proxy_state(dirs["C"][0])["session_id"], "c01")

            # 브라우저 프로필 폴더도 인스턴스별로 분리된다.
            profiles = [
                profile_dir(state_root) for _out, state_root in dirs.values()
            ]
            self.assertTrue(all(profile.is_dir() for profile in profiles))
            self.assertEqual(len({str(profile) for profile in profiles}), 3)


class SessionRotationIsolationTest(unittest.TestCase):
    """회전 상태 인스턴스 격리 — B 의 세션 교체가 C 상태에 무영향."""

    def test_b_rotation_leaves_c_session_state_untouched(self):
        module = _load_pipeline()
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_b, guard_b = root / "out_B", root / "guard_B"
            out_c, guard_c = root / "out_C", root / "guard_C"
            # 스텁 시계: 1회차 예약 06:00, 2회차 예약 80분 뒤(실제 예약 간격과
            # 같아 가드 간격 60분을 지난다).
            base = time.mktime((2026, 10, 2, 6, 0, 0, 0, 0, -1))
            clock = {"tick": base}

            def now_fn() -> float:
                return clock["tick"]

            # 1회차: B(b04)·C(c01) 모두 살아 있는 회선으로 부트스트랩.
            first_b = _CategoryChildStub(now_fn=now_fn)
            first_c = _CategoryChildStub(now_fn=now_fn)
            for output_dir, state_root, sid, family, stub in (
                (out_b, guard_b, "b04", _KITCHEN_B_PATH, first_b),
                (out_c, guard_c, "c01", _PET_C_PATH, first_c),
            ):
                code = _run_pipeline_main(
                    module,
                    [
                        "--output-dir",
                        str(output_dir),
                        "--state-root",
                        str(state_root),
                        "--proxy-session-id",
                        sid,
                        "--categories-file",
                        str(family),
                    ],
                    child=stub,
                    decodo=_fake_decodo(calls),
                )
                self.assertEqual(code, 0)
            self.assertEqual(_proxy_state(out_b)["session_id"], "b04")
            c_state_before = (
                out_c / "proxy_session_state.json"
            ).read_text(encoding="utf-8")
            c_runs_before = (
                out_c / "top_pipeline_runs.jsonl"
            ).read_text(encoding="utf-8")
            c_guard_before = (guard_c / "canary_guard.json").read_text(
                encoding="utf-8"
            )

            # 2회차: B 회선(b04)만 죽고 b05 는 산다 — 가짜 decodo 로 B 만
            # 교체를 유발한다.
            clock["tick"] = base + (80 * 60)
            rotation_calls: list = []
            fake = _fake_decodo(rotation_calls)
            _script_fetch_by_sid(
                fake,
                rotation_calls,
                {
                    "b04": fake.DecodoError("HTTP 502 터널 실패", kind="response"),
                    "*": _alive(),
                },
            )
            second_b = _CategoryChildStub(now_fn=now_fn)
            code = _run_pipeline_main(
                module,
                [
                    "--output-dir",
                    str(out_b),
                    "--state-root",
                    str(guard_b),
                    "--proxy-session-id",
                    "b04",
                    "--categories-file",
                    str(_KITCHEN_B_PATH),
                ],
                child=second_b,
                decodo=fake,
            )
            self.assertEqual(code, 0)

            # B 의 세션 상태는 교체된 sid(b05)로 바뀐다.
            self.assertEqual(_proxy_state(out_b)["session_id"], "b05")
            self.assertEqual(_child_sid(second_b.commands[0]), "b05")
            check = _exit_ip_checks(out_b)[-1]
            self.assertIs(check["rotated"], True)
            self.assertEqual(check["tried"], ["b04", "b05"])
            self.assertEqual(check["proxy_session_id"], "b05")

            # C 의 세션 상태 파일은 c01 그대로, 실행 로그·가드 장부도
            # 한 글자도 바뀌지 않는다(B 교체가 C 로 새어들지 않는다).
            self.assertEqual(
                json.loads(c_state_before)["session_id"], "c01"
            )
            self.assertEqual(
                (out_c / "proxy_session_state.json").read_text(encoding="utf-8"),
                c_state_before,
            )
            self.assertEqual(
                (out_c / "top_pipeline_runs.jsonl").read_text(encoding="utf-8"),
                c_runs_before,
            )
            self.assertEqual(
                (guard_c / "canary_guard.json").read_text(encoding="utf-8"),
                c_guard_before,
            )


class GuardIndependenceTest(unittest.TestCase):
    """가드 독립성 — 간격 제한은 장부(canary_guard.json) 단위로만 적용.

    인스턴스마다 state_root 가드가 별개 파일이므로 같은 시각에 두 인스턴스가
    실접속을 claim 해도 서로를 막지 않는다(설계 §3.1 안전 장치 분리).
    """

    def test_same_instant_claims_on_separate_state_roots_are_both_allowed(self):
        moment = time.mktime((2026, 10, 2, 9, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard_b = root / "guard_B"
            guard_c = root / "guard_C"
            allowed_b, reason_b = claim_live_attempt(
                guard_b, now=moment, planned_items=480, planned_pages=8
            )
            allowed_c, reason_c = claim_live_attempt(
                guard_c, now=moment, planned_items=480, planned_pages=8
            )
            # 서로 다른 state_root 장부라 같은 시각에 둘 다 허용된다.
            self.assertTrue(allowed_b, reason_b)
            self.assertTrue(allowed_c, reason_c)

            # 대조: 같은 장부를 같은 시각에 다시 claim 하면 간격 제한에 걸린다
            # (제한이 장부 단위로만 적용됨의 반증).
            again, reason = claim_live_attempt(
                guard_b, now=moment, planned_items=480, planned_pages=8
            )
            self.assertFalse(again)
            self.assertIn("61분", reason)

            # 가드 파일 2개는 물리적으로 별개다.
            path_b = guard_b / "canary_guard.json"
            path_c = guard_c / "canary_guard.json"
            self.assertNotEqual(path_b, path_c)
            self.assertTrue(path_b.exists())
            self.assertTrue(path_c.exists())
            for path in (path_b, path_c):
                ledger = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(ledger["daily_sessions"], 1)
                self.assertEqual(ledger["last_attempt_ts"], moment)


class TunnelFailureHaltPinTest(unittest.TestCase):
    """터널 장애 시나리오 고정 — 현행 동작 변경이 아니라 규격 고정.

    2026-10-01 17:45 b01 사건: 실행 중 세션이 죽어(판매자 단계 home 진입 시
    net::ERR_TUNNEL_CONNECTION_FAILED) 판매자 컨트롤이 halted 로 남고
    파이프라인이 종료 23으로 끝나 예약이 자동 비활성됐다. 이 테스트는 그
    현행 동작(choose_action → ("halted", 이유), main → 23, 자식 0회)을
    있는 그대로 고정한다.

    세션 사망은 사전 점검(자동 교체)으로 예방되며, 실행 중 사망으로 이
    상태에 빠지면 운영 절차(기록문서 §8)대로 컨트롤 리셋 후 재개한다.
    """

    _TUNNEL_REASON = (
        "TimeoutError: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at "
        "https://www.coupang.com/. Try upgrading your browser."
    )

    def test_halted_tunnel_control_stops_pipeline_with_exit_23(self):
        module = _load_pipeline()
        child = _CategoryChildStub()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out_B"
            output_dir.mkdir()
            # 판매자 자식이 실행 중 사망해 남긴 컨트롤과 같은 형태(halt 가
            # "{type(error).__name__}: {error}" 를 reason 으로 저장).
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

            # choose_action 은 컨트롤의 이유를 그대로 ("halted", 이유) 로
            # 돌려준다.
            action, reason = module.choose_action(output_dir)
            self.assertEqual(action, "halted")
            self.assertEqual(reason, self._TUNNEL_REASON)
            self.assertIn("ERR_TUNNEL_CONNECTION_FAILED", reason)

            # main 은 종료 23으로 끝나고 자식은 한 번도 실행되지 않는다.
            code = _run_pipeline_main(
                module,
                [
                    "--output-dir",
                    str(output_dir),
                    "--state-root",
                    str(Path(tmp) / "guard_B"),
                ],
                child=child,
            )
            self.assertEqual(code, 23)
            self.assertEqual(child.commands, [])
            lines = _run_lines(output_dir)
            self.assertEqual([line["event"] for line in lines], ["pipeline_halted"])
            self.assertEqual(lines[0]["action"], "halted")
            self.assertEqual(lines[0]["reason"], self._TUNNEL_REASON)


class RotationWithCategoriesFileTest(unittest.TestCase):
    """자동 교체 + 가족 파일 통합 — 회전이 일어난 실행의 자식 명령 조합.

    기존 테스트(SessionRotationTest·CategoriesFilePassThroughTest)는 교체 sid
    통과와 --categories-file 통과를 각각 따로 검증한다. 여기서는 둘이 한
    자식 명령에 함께 전달되는 조합을 고정한다(B 인스턴스의 실제 형태).
    """

    def test_rotated_child_command_carries_both_new_sid_and_categories_file(self):
        module = _load_pipeline()
        calls: list = []
        fake = _fake_decodo(calls)
        _script_fetch_by_sid(
            fake,
            calls,
            {
                "b04": fake.DecodoError("HTTP 502 터널 실패", kind="response"),
                "*": _alive(),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out_B"
            child = _CategoryChildStub()
            code = _run_pipeline_main(
                module,
                [
                    "--output-dir",
                    str(output_dir),
                    "--state-root",
                    str(root / "guard_B"),
                    "--proxy-session-id",
                    "b04",
                    "--categories-file",
                    str(_KITCHEN_B_PATH),
                ],
                child=child,
                decodo=fake,
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(child.commands), 1)
            command = child.commands[0]
            self.assertIn("coupang_patchright_top_thousand.py", command[1])
            # 교체된 sid(b05)와 가족 파일이 같은 자식 명령에 함께 전달된다.
            self.assertEqual(command[command.index("--proxy-session-id") + 1], "b05")
            self.assertEqual(
                command[command.index("--categories-file") + 1],
                str(_KITCHEN_B_PATH),
            )
            # 상태 파일·이벤트도 교체 sid 를 가리킨다.
            self.assertEqual(_proxy_state(output_dir)["session_id"], "b05")
            check = _exit_ip_checks(output_dir)[0]
            self.assertIs(check["rotated"], True)
            self.assertEqual(check["tried"], ["b04", "b05"])
            # 자식이 만든 상태 파일도 여전히 B 가족이다.
            self.assertEqual(_state_category_ids(output_dir), _KITCHEN_B_IDS)


if __name__ == "__main__":
    unittest.main()
