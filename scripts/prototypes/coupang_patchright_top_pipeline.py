"""예약 1회마다 판매자 사업자정보를 우선 처리하고, 남으면 목록을 이어간다.

사용자 결정(2026-10-01): 축산 목록은 이미 확인한 만큼으로 중단하고 완성형
판매자 데이터셋을 먼저 만든다. 그래서 이 파이프라인은 판매자 대기열이
남아 있으면 항상 판매자 단계를 돌고, 판매자가 끝난 뒤에만 목록 단계를
다시 확인한다. 둘 다 끝나면 완성형 파일을 만들고 종료 코드 30으로
실행기가 남은 예약을 자동으로 끄게 한다.

실행 시작 시 Decodo 스티키 세션 출구 IP를 점검해 세션이 죽었으면 sid를
자동 교체한다(2026-10-01 b01 조기 사망 사건 — 유휴 간격에 게이트가 세션을
버려 밤마다 예약이 자동 비활성됐다). 마지막 성공 sid는 상태 파일
proxy_session_state.json 에, 시도 내역은 exit_ip_check 이벤트에 남는다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.coupang.patchright_top_sellers import (
    _work,
    build_seller_final,
    read_seller_control,
)
from app.core.coupang.patchright_top_thousand import load_categories_file

PIPELINE_COMPLETE = 30

# Decodo 스티키 세션 자동 교체(2026-10-01 b01 조기 사망 사건 대응).
PROXY_STATE_FILENAME = "proxy_session_state.json"
MAX_SESSION_ROTATIONS = 3  # 원본 sid 포함 총 4회 점검
# 세션 수준 사망(게이트 502 터널 실패 등) — sid 교체로 회복되는 오류 종류.
# quota/auth/unknown_407 은 계정 수준 문제라 sid를 바꿔도 소용없다.
ROTATABLE_ERROR_KINDS = frozenset({"response", "connection", "timeout", "other"})


def choose_action(
    output_dir: Path,
    categories: list[tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """네트워크 요청 없이 다음 예약 작업을 고른다. 판매자가 항상 우선이다.

    categories는 이 인스턴스의 카테고리 가족(병렬 확장 설계 §3.1). 다른
    가족 상태 파일을 기본 목록으로 읽으면 검증이 실패하므로 함께 받는다.
    """
    store = _build_store(output_dir)
    store.ensure_files()

    control = read_seller_control(output_dir)
    if control["status"] in ("halted", "in_progress"):
        reason = str(control.get("reason") or "앞선 판매자 작업이 끝나지 않았습니다.")
        return "halted", reason

    # 신규 인스턴스는 상품 파일이 없어 판매자 대기열도 비어 있으므로
    # 목록 단계부터 시작한다. 파일이 있을 때만 대기열을 확인한다.
    if store.products_path.exists():
        pending, unmapped = _work(store, 1)
        if pending or unmapped:
            return "sellers", "남은 상품의 판매자 사업자정보를 수집합니다."

    failed_mappings = json.loads(
        store.failed_mappings_path.read_text(encoding="utf-8")
    )
    if failed_mappings:
        return "halted", "판매자를 연결하지 못한 상품이 있어 자동 진행을 멈춥니다."

    from app.core.coupang.patchright_top_thousand import read_state

    state = read_state(output_dir, categories)
    if state["status"] == "running":
        return "category", "이어서 상품 목록을 확인합니다."
    return "complete", "상품과 판매자 데이터 수집이 모두 끝났습니다."


def _build_store(output_dir: Path):
    from app.core.coupang.patchright_top_sellers import TopSellerStore

    return TopSellerStore(output_dir)


def _last_json(text: str) -> dict:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _append_log(output_dir: Path, value: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "top_pipeline_runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def read_proxy_session_state(output_dir: Path) -> str:
    """상태 파일에서 마지막 성공 sid를 읽는다. 없거나 깨졌으면 빈 문자열."""
    try:
        raw = json.loads(
            (output_dir / PROXY_STATE_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("session_id") or "").strip()


def _write_proxy_session_state(output_dir: Path, session_id: str) -> None:
    """마지막 성공 sid를 남겨 다음 실행의 시작점으로 쓴다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (output_dir / PROXY_STATE_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def next_session_id(session_id: str) -> str:
    """죽은 sid의 다음 후보 — 끝의 연속 숫자를 +1 한다(b03→b04).

    끝이 숫자가 아니면 2를 붙인다(bx→bx2, bx를 1회차로 세는 방식).
    자릿수는 보존한다(b09→b10처럼 자리가 넘어갈 때만 늘어난다).
    """
    sid = str(session_id or "").strip()
    index = len(sid)
    while index > 0 and sid[index - 1].isdigit():
        index -= 1
    digits = sid[index:]
    if digits:
        return sid[:index] + str(int(digits) + 1).zfill(len(digits))
    return sid + "2"


def check_and_rotate_session(
    output_dir: Path, cli_session_id: str
) -> tuple[dict, str]:
    """출구 IP 점검 + 세션 사망 시 sid 자동 교체(병렬 확장 설계 §3.2-2).

    2026-10-01 사건: 스티키 세션이 실행 사이 유휴 간격에 확률적으로 죽어
    (게이트 502 터널 실패, 쿠팡 접촉 0건) 예약이 밤마다 자동 비활성됐다.
    실증된 수동 복구 절차(사망 sid 진단 → sid 교체, 쿠팡 트래픽 0)를
    실행 시작 시 자동화한다.

    시작 sid는 상태 파일(proxy_session_state.json)의 마지막 성공 sid,
    없으면 CLI 인자 sid. 점검에 실패하고 원인이 세션 수준
    (ROTATABLE_ERROR_KINDS)이면 sid를 교체해 다시 점검한다 — 최대 교체
    3회, 원본 포함 총 4회. 원인이 계정 수준(quota/auth/unknown_407)이면
    sid와 무관하므로 교체 없이 원본으로 진행한다. 점검은 Decodo
    엔드포인트만 본다(쿠팡 트래픽 없음).

    반환은 (exit_ip_check 이벤트, 자식 전달 sid). 성공한 sid는 상태 파일에
    저장하고 자식에도 전달한다. 전부 실패하면 CLI 인자 sid를 돌려줘 자식이
    스스로 실패하게 한다(예약 자동 비활성 = 현행 안전거동 유지).
    app/core/decodo.py 는 실행 시점에 없을 수 있어 늦은 import 로 가져온다.
    """
    start_sid = read_proxy_session_state(output_dir) or cli_session_id
    tried = [start_sid]
    failure = {
        "event": "exit_ip_check",
        "proxy_session_id": start_sid,
        "ok": False,
        "rotated": False,
        "tried": tried,
    }
    try:
        from app.core.decodo import (
            DecodoError,
            fetch_exit_ip,
            load_settings,
            sticky_proxy_dict,
        )
    except Exception as error:  # noqa: BLE001 - 모듈 부재도 점검 실패로만 기록
        return {**failure, "error_kind": type(error).__name__}, cli_session_id
    settings = load_settings()
    sid = start_sid
    kind = ""
    while True:
        try:
            info = fetch_exit_ip(sticky_proxy_dict(settings, sid))
        except DecodoError as error:
            kind = str(error.kind or "") or type(error).__name__
            if kind not in ROTATABLE_ERROR_KINDS:
                # 계정 수준 문제 — sid 교체로는 회복되지 않는다.
                return {**failure, "error_kind": kind}, cli_session_id
        except Exception as error:  # noqa: BLE001 - 점검 실패는 실행을 막지 않는다
            # DecodoError 외 예외는 종류 분류가 없어 교체 판단을 할 수 없다.
            return (
                {**failure, "error_kind": type(error).__name__},
                cli_session_id,
            )
        else:
            _write_proxy_session_state(output_dir, sid)
            return (
                {
                    "event": "exit_ip_check",
                    "proxy_session_id": sid,
                    "ip": info.ip,
                    "country_code": info.country_code,
                    "ok": True,
                    "rotated": sid != start_sid,
                    "tried": tried,
                },
                sid,
            )
        if len(tried) > MAX_SESSION_ROTATIONS:
            break  # 교체 3회 상한 — 이 이상 점검하지 않는다.
        sid = next_session_id(sid)
        tried.append(sid)
    return {**failure, "error_kind": kind}, cli_session_id


def _run(command: list[str]) -> tuple[int, dict]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr, flush=True)
    return completed.returncode, _last_json(completed.stdout)


def build_all_finals(
    output_dir: Path,
    categories_file: Path | None = None,
) -> dict:
    """완성형 파일 전부(판매자·상품)를 만든다.

    categories_file이 제공되면 이 출력 폴더는 다른 카테고리 가족을 수집한
    것이므로(병렬 확장 설계 §3.1 작업 분할) FINAL_FAMILY_CATEGORIES 루트별
    final(final_dataset_{root}.csv)은 만들지 않는다. 판매자 완성형과 통합
    final_dataset_all만 만든다.
    """
    from app.core.coupang.patchright_top_thousand import (
        FINAL_FAMILY_CATEGORIES,
        TopThousandStore,
        build_combined_final_dataset,
        build_final_dataset,
    )

    finals = {"sellers": str(build_seller_final(output_dir))}
    store = TopThousandStore(output_dir)
    if categories_file is None:
        for root in FINAL_FAMILY_CATEGORIES:
            finals[root] = str(build_final_dataset(store, root)[0])
    finals["all"] = str(build_combined_final_dataset(store)[0])
    return finals


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patchright 판매자 우선 상위 데이터셋 예약 파이프라인"
    )
    parser.add_argument("--seller-limit", type=int, default=130)
    parser.add_argument("--listing-pages", type=int, default=8)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop" / "PatchrightTop1000",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        required=True,
        help="목록과 판매자 단계가 함께 쓰는 안전 기록 폴더",
    )
    parser.add_argument(
        "--proxy-session-id",
        default="",
        help=(
            "인스턴스 B/C 회선(Decodo 스티키 세션) ID. "
            "비어 있으면 프록시 없음(A 집 회선 직접)과 같은 현행 동작"
        ),
    )
    parser.add_argument(
        "--categories-file",
        type=Path,
        default=None,
        help=(
            "이 인스턴스가 수집할 카테고리 가족 정의 JSON(병렬 확장 설계 "
            "§3.1). 목록 단계 자식에만 전달한다(판매자 단계는 상품 CSV "
            "기반이라 불필요). 없으면 기본 A 가족(현행 동작)"
        ),
    )
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()

    # 가족 파일은 작업 선택·자식 실행·완성형 빌드 전부가 같은 목록을 쓰게
    # 여기서 한 번만 읽는다. 형식이 틀리면 예약 실행이 시작되기 전에 끝낸다.
    try:
        categories = (
            load_categories_file(args.categories_file)
            if args.categories_file
            else None
        )
    except ValueError as error:
        parser.error(str(error))

    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    # 자식에 전달할 sid: 점검에서 살아 있음이 확인된 회선(교체 포함).
    effective_sid = args.proxy_session_id
    if args.proxy_session_id and not args.status_only:
        # 세션이 죽었으면 sid를 교체해 산 회선으로 자식을 돌린다(2026-10-01
        # 사건 대응). 전부 실패하면 CLI 인자 sid로 진행해 자식이 실패하게
        # 한다(예약 자동 비활성 = 현행 안전거동). status-only 는 네트워크
        # 호출 0회 원칙을 유지한다.
        event, effective_sid = check_and_rotate_session(
            args.output_dir, args.proxy_session_id
        )
        _append_log(args.output_dir, event)
    try:
        action, reason = choose_action(args.output_dir, categories)
    except Exception as error:  # noqa: BLE001 - 예약 경계에서 안전하게 중단
        action, reason = "halted", f"{type(error).__name__}: {error}"

    status = {"event": "pipeline_status", "action": action, "reason": reason}
    print(json.dumps(status, ensure_ascii=True), flush=True)
    if args.status_only:
        return 0

    if action == "complete":
        finals = build_all_finals(
            args.output_dir, categories_file=args.categories_file
        )
        result = {**status, "event": "pipeline_complete", "finals": finals}
        _append_log(args.output_dir, {**result, "started_at": started_at})
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return PIPELINE_COMPLETE
    if action == "halted":
        result = {**status, "event": "pipeline_halted"}
        _append_log(args.output_dir, {**result, "started_at": started_at})
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return 23

    if action == "sellers":
        script = Path(__file__).with_name("coupang_patchright_top_sellers.py")
        command = [
            sys.executable,
            str(script),
            "--limit",
            str(args.seller_limit),
            "--output-dir",
            str(args.output_dir),
            "--state-root",
            str(args.state_root),
            "--profile-root",
            str(args.state_root),
        ]
    else:
        script = Path(__file__).with_name("coupang_patchright_top_thousand.py")
        command = [
            sys.executable,
            str(script),
            "--pages",
            str(args.listing_pages),
            "--output-dir",
            str(args.output_dir),
            "--state-root",
            str(args.state_root),
        ]

    if effective_sid:
        # 자식 CLI(판매자/목록)도 같은 인스턴스 회선을 쓰도록 세션 ID를 통과.
        # 점검에서 교체됐으면 교체된 sid를 전달한다(CLI 인자 값이 아님).
        command += ["--proxy-session-id", effective_sid]

    if args.categories_file and action == "category":
        # 가족 목록은 목록 단계 자식만 받는다(판매자 단계는 상품 CSV 기반).
        command += ["--categories-file", str(args.categories_file)]

    exit_code, child_result = _run(command)
    result = {
        # 완성형 완료(30)도 정상 종료 이벤트로 기록한다.
        "event": (
            "pipeline_slot_finished"
            if exit_code in (0, PIPELINE_COMPLETE)
            else "pipeline_slot_failed"
        ),
        "action": action,
        "exit_code": exit_code,
        "child_event": child_result.get("event", ""),
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _append_log(args.output_dir, result)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
