"""쿠팡 병렬 확장 — 다중 인스턴스 코어 격리 검증 테스트(네트워크 없음).

사용자 결정(2026-10-02): 인스턴스 간 격리·세션 교체·가드 독립 같은 병렬
확장 속성은 라이브 실행이 아니라 오프라인 테스트로 갖춘다. scaleout 통합
테스트(test_parallel_instances_integration.py)에서 코어 관련 테스트를
이식했다 — 프로토타입 파이프라인 main()을 subprocess 스텁으로 돌리던
부분(3인스턴스 부트스트랩 실행·자식 명령 조립·배포 가족 JSON 파일 스키마
고정)은 제외했다. 자식 실행은 향후 매니저(M2) 책임이다.

- 서로 다른 state_root 가드는 같은 시각에 서로를 막지 않음(간격 제한은
  장부 단위)
- B 의 세션 자동 교체가 C 의 세션 상태·로그에 영향을 주지 않음
- 터널 장애(ERR_TUNNEL_CONNECTION_FAILED) halted 컨트롤의 현행 동작 고정은
  test_coupang_parallel_pipeline.py(ChooseActionHaltTest)로 이식했다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core.coupang.parallel_pipeline import (
    PROXY_STATE_FILENAME,
    RUN_LOG_FILENAME,
    append_run_log,
    check_and_rotate_session,
)
from app.core.coupang.patchright_canary import claim_live_attempt, profile_dir


def _fake_decodo(calls: list, *, info=None):
    """파이프라인이 늦게 import 하는 app.core.decodo 의 가짜 대체품.

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
    모든 sid에 적용하는 와일드카드다.
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


def _alive(ip: str = "175.203.56.170") -> SimpleNamespace:
    return SimpleNamespace(ip=ip, country_code="KR", country_name="Korea")


def _rotate(fake, output_dir: Path, sid: str) -> tuple[dict, str]:
    with mock.patch.dict(sys.modules, {"app.core.decodo": fake}):
        return check_and_rotate_session(output_dir, sid)


def _proxy_state(output_dir: Path) -> dict:
    return json.loads(
        (output_dir / PROXY_STATE_FILENAME).read_text(encoding="utf-8")
    )


def _run_lines(output_dir: Path) -> list[dict]:
    path = output_dir / RUN_LOG_FILENAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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

    def test_three_instance_claims_and_profiles_are_all_separate(self):
        """A/B/C 세 인스턴스가 같은 틱에 출발해도 가드·프로필이 전부 분리된다."""
        moment = time.mktime((2026, 10, 2, 6, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guards = {
                name: root / f"guard_{name}" for name in ("A", "B", "C")
            }
            for state_root in guards.values():
                allowed, _reason = claim_live_attempt(
                    state_root, now=moment, planned_items=480, planned_pages=8
                )
                self.assertTrue(allowed)
            # 세 장부는 각자 1회씩만 기록돼 있다(서로에 합쳐지지 않는다).
            for state_root in guards.values():
                ledger = json.loads(
                    (state_root / "canary_guard.json").read_text("utf-8")
                )
                self.assertEqual(ledger["daily_sessions"], 1)
            # 브라우저 프로필 폴더도 인스턴스(state_root)별로 분리된다.
            profiles = [profile_dir(state_root) for state_root in guards.values()]
            for profile in profiles:
                profile.mkdir(parents=True, exist_ok=True)
            self.assertEqual(len({str(profile) for profile in profiles}), 3)


class SessionRotationIsolationTest(unittest.TestCase):
    """회전 상태 인스턴스 격리 — B 의 세션 교체가 C 상태에 무영향."""

    def test_b_rotation_leaves_c_session_state_untouched(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_b, out_c = root / "out_B", root / "out_C"

            # 1회차: B(b04)·C(c01) 모두 살아 있는 회선으로 점검 + 로그 기록.
            first_fake = _fake_decodo(calls)
            for output_dir, sid in ((out_b, "b04"), (out_c, "c01")):
                event, effective_sid = _rotate(first_fake, output_dir, sid)
                self.assertEqual(effective_sid, sid)
                append_run_log(output_dir, event)
            self.assertEqual(_proxy_state(out_b)["session_id"], "b04")
            self.assertEqual(_proxy_state(out_c)["session_id"], "c01")
            c_state_before = (
                out_c / PROXY_STATE_FILENAME
            ).read_text(encoding="utf-8")
            c_runs_before = (out_c / RUN_LOG_FILENAME).read_text(encoding="utf-8")

            # 2회차: B 회선(b04)만 죽고 b05 는 산다 — B 만 교체를 유발한다.
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
            event, effective_sid = _rotate(fake, out_b, "b04")
            append_run_log(out_b, event)

            # B 의 세션 상태는 교체된 sid(b05)로 바뀐다.
            self.assertEqual(effective_sid, "b05")
            self.assertEqual(_proxy_state(out_b)["session_id"], "b05")
            checks = [
                line
                for line in _run_lines(out_b)
                if line["event"] == "exit_ip_check"
            ]
            self.assertIs(checks[-1]["rotated"], True)
            self.assertEqual(checks[-1]["tried"], ["b04", "b05"])
            self.assertEqual(checks[-1]["proxy_session_id"], "b05")

            # C 의 세션 상태 파일은 c01 그대로, 실행 로그도 한 글자도 바뀌지
            # 않는다(B 교체가 C 로 새어들지 않는다).
            self.assertEqual(_proxy_state(out_c)["session_id"], "c01")
            self.assertEqual(
                (out_c / PROXY_STATE_FILENAME).read_text(encoding="utf-8"),
                c_state_before,
            )
            self.assertEqual(
                (out_c / RUN_LOG_FILENAME).read_text(encoding="utf-8"),
                c_runs_before,
            )

    def test_instance_state_files_live_in_their_own_output_dirs(self):
        """세션 상태 파일은 각 출력 폴더 안에만 생긴다(인스턴스 소유 분리)."""
        calls: list = []
        fake = _fake_decodo(calls)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dirs = [root / "out_A", root / "out_B", root / "out_C"]
            for output_dir, sid in (
                (dirs[1], "b04"),
                (dirs[2], "c01"),
            ):
                _event, effective_sid = _rotate(fake, output_dir, sid)
                self.assertEqual(effective_sid, sid)
            # sid 없는 A 는 점검 자체를 돌리지 않는다(매니저가 호출 안 함) —
            # 상태 파일이 없는 것이 곧 분리의 증거다.
            states = list(root.rglob(PROXY_STATE_FILENAME))
            self.assertEqual(
                {path.parent for path in states}, {dirs[1], dirs[2]}
            )
            self.assertFalse((dirs[0] / PROXY_STATE_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
