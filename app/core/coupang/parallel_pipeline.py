"""쿠팡 병렬 수집 파이프라인 코어 — 판매자 우선 예약 선택·세션 교체·완성형.

scripts/prototypes/coupang_patchright_top_pipeline.py(scaleout 검증본)의
순수 로직을 앱 모듈로 포팅했다. 종료 코드·argparse·subprocess 경계는
제거하고 함수가 결과 dict를 반환한다. 실행(자식 프로세스·스레드)과
로그 JSONL 기록 시점은 호출자(향후 매니저)의 책임이며, 기록 자체는
append_run_log 헬퍼로 제공한다.

사용자 결정(2026-10-01): 판매자 대기열이 남아 있으면 항상 판매자 단계를
돌고, 판매자가 끝난 뒤에만 목록 단계를 다시 확인한다. 둘 다 끝나면
완성형 파일을 만들어 호출자에게 완료를 알린다.

실행 시작 시 Decodo 스티키 세션 출구 IP를 점검해 세션이 죽었으면 sid를
자동 교체한다(2026-10-01 b01 조기 사망 사건 — 유휴 간격에 게이트가 세션을
버려 밤마다 예약이 자동 비활성됐다). 마지막 성공 sid는 상태 파일
proxy_session_state.json 에, 시도 내역은 exit_ip_check 이벤트에 남는다.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.core.coupang.patchright_top_sellers import (
    _work,
    build_seller_final,
    read_seller_control,
)
from app.core.coupang.patchright_top_thousand import load_categories_file

__all__ = [
    "MAX_SESSION_ROTATIONS",
    "PROXY_STATE_FILENAME",
    "ROTATABLE_ERROR_KINDS",
    "RUN_LOG_FILENAME",
    "append_run_log",
    "build_all_finals",
    "check_and_rotate_session",
    "choose_action",
    "load_categories_file",
    "next_session_id",
    "read_proxy_session_state",
]

# 실행 로그 JSONL 파일명 — 프로토타입 파이프라인과 동일한 이름을 쓴다.
RUN_LOG_FILENAME = "top_pipeline_runs.jsonl"

# Decodo 스티키 세션 자동 교체(2026-10-01 b01 조기 사망 사건 대응).
PROXY_STATE_FILENAME = "proxy_session_state.json"
MAX_SESSION_ROTATIONS = 3  # 원본 sid 포함 총 4회 점검
# 세션 수준 사망(게이트 502 터널 실패 등) — sid 교체로 회복되는 오류 종류.
# quota/auth/unknown_407 은 계정 수준 문제라 sid를 바꿔도 소용없다.
ROTATABLE_ERROR_KINDS = frozenset({"response", "connection", "timeout", "other"})


def choose_action(
    output_dir: Path,
    categories: list[tuple[str, str]] | None = None,
    *,
    page_from: int | None = None,
    page_to: int | None = None,
    products_paths: list[Path] | None = None,
    slice_index: int | None = None,
    slice_count: int = 1,
) -> dict:
    """네트워크 요청 없이 다음 예약 작업을 고른다.

    반환은 {"action": …, "reason": …} 이고 action 은 sellers/category/
    complete/halted 중 하나다. 프로토타입 실행기의 예외 경계(main 의
    try/except → halted)를 그대로 흡수해, 상태 파일 검증 실패 등의 예외도
    예약이 안전하게 멈추는 halted 로 돌아온다.

    categories는 이 인스턴스의 카테고리 가족(병렬 확장 설계 §3.1). 다른
    가족 상태 파일을 기본 목록으로 읽으면 검증이 실패하므로 함께 받는다.

    단계 판정은 작업 단위 종류를 따른다(볼륨 인지 분할 §5.3 — 단계 혼합
    큐). page_to 를 주면 목록 조각(목록만 확인, 판매자는 이 폴더에서 안
    돈다), products_paths/slice_* 를 주면 판매자 조각(매핑 대기열만 확인),
    둘 다 없으면 현행 통짜 규약(가족 안에서 목록→판매자 순서 고정).
    """
    try:
        if page_to is not None:
            return _choose_pages_action(
                output_dir, categories, page_from, page_to
            )
        if products_paths is not None or slice_count > 1:
            return _choose_sellers_action(
                output_dir, products_paths, slice_index, slice_count
            )
        return _choose_action(output_dir, categories)
    except Exception as error:  # noqa: BLE001 - 예약 경계에서 안전하게 중단
        return {
            "action": "halted",
            "reason": f"{type(error).__name__}: {error}",
        }


def _choose_pages_action(
    output_dir: Path,
    categories: list[tuple[str, str]] | None,
    page_from: int | None,
    page_to: int,
) -> dict:
    """목록 조각(work pages)의 다음 작업 — 목록만 보고 판정한다.

    판매자는 이 카테고리의 목록 전 조각이 완료된 뒤 별도 판매자 조각으로
    배출되므로(§5.3 배출 규칙), 이 폴더에서는 상품 목록 상태만 본다.
    완주 판정 시점에 판매자 저장소 파일이 없으면 매니저의 완성형 생성
    (build_all_finals)이 실패하므로 여기서 빈 파일을 만들어 둔다.
    """
    from app.core.coupang.patchright_top_sellers import TopSellerStore
    from app.core.coupang.patchright_top_thousand import read_state

    TopSellerStore(output_dir).ensure_files()
    state = read_state(output_dir, categories, page_from=page_from, page_to=page_to)
    if state["status"] == "running":
        return {"action": "category", "reason": "이어서 상품 목록을 확인합니다."}
    return {"action": "complete", "reason": "목록 조각 구간을 모두 확인했습니다."}


def _choose_sellers_action(
    output_dir: Path,
    products_paths: list[Path] | None,
    slice_index: int | None,
    slice_count: int,
) -> dict:
    """판매자 조각(work sellers)의 다음 작업 — 매핑 대기열만 판정한다."""
    from app.core.coupang.patchright_top_sellers import TopSellerStore, _work

    store = TopSellerStore(output_dir)
    store.ensure_files()

    control = read_seller_control(output_dir)
    if control["status"] == "halted":
        reason = str(control.get("reason") or "앞선 판매자 작업이 끝나지 않았습니다.")
        return {"action": "halted", "reason": reason}
    store.requeue_unconfirmed_sellers()

    pending, unmapped = _work(
        store,
        1,
        products_paths=products_paths,
        slice_index=slice_index,
        slice_count=slice_count,
    )
    if pending or unmapped:
        return {
            "action": "sellers",
            "reason": "남은 상품의 판매자 사업자정보를 수집합니다.",
        }
    failed_mappings = json.loads(
        store.failed_mappings_path.read_text(encoding="utf-8")
    )
    if failed_mappings:
        return {
            "action": "halted",
            "reason": "판매자를 연결하지 못한 상품이 있어 자동 진행을 멈춥니다.",
        }
    return {"action": "complete", "reason": "이 조각의 판매자 수집이 끝났습니다."}


def _choose_action(
    output_dir: Path,
    categories: list[tuple[str, str]] | None = None,
) -> dict:
    from app.core.coupang.patchright_top_sellers import TopSellerStore

    store = TopSellerStore(output_dir)
    store.ensure_files()

    control = read_seller_control(output_dir)
    if control["status"] == "halted":
        reason = str(control.get("reason") or "앞선 판매자 작업이 끝나지 않았습니다.")
        return {"action": "halted", "reason": reason}
    # cancelled(사용자 정지)·in_progress(크래시 잔존)는 끊긴 실행이다 —
    # 요청 표시만 남은 판매자를 되돌리고 대기열 확인부터 이어서 간다.
    store.requeue_unconfirmed_sellers()

    # 신규 인스턴스는 상품 파일이 없어 판매자 대기열도 비어 있으므로
    # 목록 단계부터 시작한다. 파일이 있을 때만 대기열을 확인한다.
    if store.products_path.exists():
        pending, unmapped = _work(store, 1)
        if pending or unmapped:
            return {
                "action": "sellers",
                "reason": "남은 상품의 판매자 사업자정보를 수집합니다.",
            }

    failed_mappings = json.loads(
        store.failed_mappings_path.read_text(encoding="utf-8")
    )
    if failed_mappings:
        return {
            "action": "halted",
            "reason": "판매자를 연결하지 못한 상품이 있어 자동 진행을 멈춥니다.",
        }

    from app.core.coupang.patchright_top_thousand import read_state

    state = read_state(output_dir, categories)
    if state["status"] == "running":
        return {"action": "category", "reason": "이어서 상품 목록을 확인합니다."}
    return {"action": "complete", "reason": "상품과 판매자 데이터 수집이 모두 끝났습니다."}


def append_run_log(output_dir: Path, event: dict) -> None:
    """실행 로그 JSONL(top_pipeline_runs.jsonl)에 이벤트 1건을 붙인다.

    무엇을 어느 시점에 기록할지는 호출자(매니저)가 정한다. 기록 형식만
    프로토타입 실행기와 동일하게 유지한다.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / RUN_LOG_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


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
    """죽은 sid의 다음 후보 — 끝자리 숫자를 +1 하되 자리올림하지 않는다.

    끝이 숫자가 아니면 2를 붙인다(bx→bx2, bx를 1회차로 세는 방식). 끝자리가
    9면 0으로 되감는다(i29→i20) — 십진 네임스페이스(i{N}0~i{N}9) 안에서만
    순환하므로 누적 회전이 인스턴스 수십 회에 걸쳐도 다른 인스턴스의
    sid와 절대 겹치지 않는다(b09→b10 같은 자리올림이 i29→i30 침범 사고를
    만들었던 2026-10-04 사건의 재발 방지).
    """
    sid = str(session_id or "").strip()
    index = len(sid)
    while index > 0 and sid[index - 1].isdigit():
        index -= 1
    digits = sid[index:]
    if digits:
        last = digits[-1]
        if last == "9":
            return sid[:index] + digits[:-1] + "0"
        return sid[:index] + digits[:-1] + str(int(last) + 1)
    return sid + "2"


def _sid_namespace(session_id: str) -> str:
    """sid 의 인스턴스 네임스페이스 — 마지막 숫자 1자리를 뺀 접두사.

    인스턴스 N 은 i{N}0~i{N}9 를 쓰므로 같은 인스턴스의 sid 들은 이 접두사가
    같다(i20·i23 → "i2", i200·i207 → "i20"). 끝자리가 숫자가 아니면 통째로
    반환한다(구형 단일 인스턴스 sid b01 계열끼리는 "b0" 로 같다).
    """
    sid = str(session_id or "").strip()
    if sid and sid[-1].isdigit():
        return sid[:-1]
    return sid


def check_and_rotate_session(
    output_dir: Path, cli_session_id: str
) -> tuple[dict, str]:
    """출구 IP 점검 + 세션 사망 시 sid 자동 교체(병렬 확장 설계 §3.2-2).

    2026-10-01 사건: 스티키 세션이 실행 사이 유휴 간격에 확률적으로 죽어
    (게이트 502 터널 실패, 쿠팡 접촉 0건) 예약이 밤마다 자동 비활성됐다.
    실증된 수동 복구 절차(사망 sid 진단 → sid 교체, 쿠팡 트래픽 0)를
    실행 시작 시 자동화한다.

    시작 sid는 상태 파일(proxy_session_state.json)의 마지막 성공 sid,
    없으면 호출자가 넘긴 sid. 단 폴더에 남은 sid가 호출자 인스턴스의
    네임스페이스(i{N}_) 소속이 아니면 승계하지 않는다 — 조각/샤드 승계로
    다른 인스턴스가 쓰던 폴더를 물려받을 때 그 회선(출구 IP)까지 물려받으면
    두 인스턴스가 같은 IP를 공유하거나 프로필(쿠키)-IP 불일치 조합이
    생긴다(2026-10-04 sid 충돌 사고와 같은 실패 유형). 이때는 자기
    네임스페이스의 sid로 시작한다. 점검에 실패하고 원인이 세션 수준
    (ROTATABLE_ERROR_KINDS)이면 sid를 교체해 다시 점검한다 — 최대 교체
    3회, 원본 포함 총 4회. 원인이 계정 수준(quota/auth/unknown_407)이면
    sid와 무관하므로 교체 없이 원본으로 진행한다. 점검은 Decodo
    엔드포인트만 본다(쿠팡 트래픽 없음).

    반환은 (exit_ip_check 이벤트, 유효 sid). 성공한 sid는 상태 파일에
    저장한다. 전부 실패하면 호출자가 넘긴 sid를 돌려줘 실행 단계가
    스스로 실패하게 한다(예약 자동 비활성 = 현행 안전거동 유지).
    app/core/decodo.py 는 실행 시점에 없을 수 있어 늦은 import 로 가져온다.
    """
    stored_sid = read_proxy_session_state(output_dir)
    if _sid_namespace(stored_sid) != _sid_namespace(cli_session_id):
        stored_sid = ""
    start_sid = stored_sid or cli_session_id
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
