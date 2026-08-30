"""저장 계층: JSON/CSV 결과 저장, collected_ids/state 상태 파일 관리 (WORK_ORDER §8 / §9).

출력 디렉터리를 인자로 받아 사용자가 지정한 '저장 경로'를 지원한다.
collected_ids.json / fastcrawl_state.json 은 항상 지정 출력 디렉터리 하위에 둔다.

내구성 원칙: goodscode 를 collected_ids 에 커밋하기 전에, 그 결과 레코드가 먼저
디스크에 기록되어 있어야 한다. 순서가 뒤바뀌면 '수집됨으로 표시되었지만 결과 파일에는
없는' 유실이 발생할 수 있다. crawler.py 는 항상 save_partial_results()(또는
save_results())를 save_collected_ids() 보다 먼저 호출한다.
"""

from __future__ import annotations

import atexit
import csv
import hashlib
import json
import os
import sys
import threading
import warnings
from datetime import datetime
from enum import Enum
from pathlib import Path

from app.core import config
from app.models.records import RECORD_FIELDS
from app.utils.helpers import sanitize_filename

_LOCK_FILENAME = ".gmarket_fast.lock"
# 프로세스당 출력 디렉터리 1개에 대해 잠금 파일 핸들을 1개만 유지한다(경로
# 문자열 → 열린 파일 객체). 같은 프로세스 안에서 같은 출력 경로로 여러
# Storage 인스턴스를 만드는 기존 패턴(main_window._make_storage() 가 버튼
# 클릭마다 새 Storage 를 만듦)은 그대로 지원해야 하므로, 잠금은 Storage
# 인스턴스가 아니라 프로세스+경로 단위로 딱 한 번만 건다 — 이미 이 프로세스가
# 잠근 경로라면 조용히 통과한다. 실제로 막고 싶은 것은 "서로 다른 프로세스
# (예: 사용자가 실행 파일을 실수로 두 번 실행)가 같은 저장 경로를 동시에
# 쓰는 상황"이다(6차 리뷰 MEDIUM 회귀 방지).
_held_locks: dict[str, object] = {}
_held_locks_guard = threading.Lock()


def acquire_output_lock(output_dir: Path) -> None:
    """한 프로세스만 지정 출력 디렉터리를 사용하도록 잠근다.

    Gmarket 상태 파일뿐 아니라 Coupang JSON/CSV 쌍도 같은 출력 디렉터리에서
    서로 덮어쓰면 안 되므로 두 수집 경로가 이 잠금을 공유한다. 잠금을 만들거나
    적용할 수 없으면 안전을 증명할 수 없으므로 fail-closed 한다.
    """
    key = os.path.normcase(str(output_dir.resolve()))
    with _held_locks_guard:
        if key in _held_locks:
            return
        if sys.platform == "win32":
            _acquire_windows_output_mutex(output_dir, key)
            return
        lock_path = output_dir / _LOCK_FILENAME
        try:
            f = open(lock_path, "a+")  # noqa: SIM115 - 프로세스 수명 동안 의도적으로 열어둔다(잠금 유지)
        except OSError as e:
            raise OSError(
                f"저장 경로 '{output_dir}' 에 안전 잠금 파일을 만들 수 없습니다: {e}"
            ) from e
        try:
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            f.close()
            raise OSError(
                f"저장 경로 '{output_dir}' 가 다른 프로세스(다른 실행 중인 창)에서 "
                f"이미 사용 중인 것 같습니다. 같은 저장 경로를 동시에 사용하는 두 "
                f"개의 인스턴스는 결과/체크포인트를 서로 덮어써 손상시킬 수 "
                f"있어 잠급니다. 다른 창을 먼저 닫거나 다른 저장 경로를 사용하세요."
            ) from e
        except ImportError as e:
            f.close()
            raise OSError("현재 플랫폼에서 출력 경로 안전 잠금을 사용할 수 없습니다.") from e
        _held_locks[key] = f
        # 프로세스 종료 시 명시적으로 닫는다 — 그래야 (a) 잠금이 확실히 풀리고
        # (b) 인터프리터 종료 중 GC 가 "닫지 않은 파일"이라고 경고하지 않는다.
        atexit.register(f.close)


def _acquire_windows_output_mutex(output_dir: Path, key: str) -> None:
    """파일 삭제를 막지 않는 경로 기반 Windows named mutex를 획득한다."""
    import ctypes
    from ctypes import wintypes

    mutex_name = "Local\\SellerCollector-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    ctypes_api = vars(ctypes)
    win_dll = ctypes_api["WinDLL"]
    get_last_error = ctypes_api["get_last_error"]
    win_error = ctypes_api["WinError"]
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise OSError(
            f"저장 경로 '{output_dir}' 의 Windows 안전 잠금을 만들 수 없습니다: "
            f"{win_error(get_last_error())}"
        )
    if get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise OSError(
            f"저장 경로 '{output_dir}' 가 다른 프로세스(다른 실행 중인 창)에서 "
            "이미 사용 중입니다. 다른 창을 먼저 닫거나 다른 저장 경로를 사용하세요."
        )
    _held_locks[key] = handle
    atexit.register(kernel32.CloseHandle, handle)


# 기존 내부 테스트/호출 경로의 호환성을 유지한다.
_acquire_output_lock = acquire_output_lock


class LoadStatus(Enum):
    """load_partial_results_status() 가 반환하는 상태(5차 리뷰 HIGH 회귀 방지).

    예전에는 load_partial_results() 가 손상 여부와 무관하게 항상 list 만
    반환했다 — 호출자(reconcile_leftover_checkpoints)는 "격리 성공" 과
    "격리 자체가 실패해 원본이 그대로 위험하게 남아있는 상태"를 구분할 수
    없었고, path.exists() 로 대충 유추하다가 격리 실패 시 유일한 원본까지
    삭제하는 사고가 났다. 이제는 상태를 명시적으로 구분해 반환한다.
    """

    MISSING = "missing"        # 파일이 없음(정상 — 체크포인트가 아예 없거나 이미 정리됨)
    EMPTY = "empty"             # 문법/의미 모두 정상이지만 빈 리스트
    VALID = "valid"              # 정상적으로 파싱/검증된 비어있지 않은 리스트
    QUARANTINED = "quarantined"  # 손상 감지 + 백업(격리) 성공 — 원본은 안전하게 보존됨
    QUARANTINE_FAILED = "quarantine_failed"  # 손상 감지했지만 격리도 실패 — 원본이 위험하게 남아있음

# CSV 수식 주입(CSV/Formula Injection) 방지: 셀 값이 아래 문자로 시작하면
# Excel 등에서 수식으로 해석될 수 있어 앞에 작은따옴표를 붙여 강제로 텍스트 처리한다.
# OWASP 는 LF(\n) 도 위험 문자로 명시한다: https://owasp.org/www-community/attacks/CSV_Injection
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")

# 체크포인트(.partial_*) 파일명 접두사 — find_leftover_partials 의 glob 패턴과
# _partial_path 가 반드시 같은 접두사를 사용해야 한다.
_PARTIAL_PREFIX = f".partial_{config.FILE_PREFIX}_"


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + text
    return text


class Storage:
    """출력 디렉터리 1개에 대한 저장/상태 관리자."""

    def __init__(self, output_dir: Path | str | None = None):
        self.output_dir = Path(output_dir) if output_dir else config.DEFAULT_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 다른 프로세스가 같은 저장 경로를 동시에 쓰지 못하도록 잠근다
        # (6차 리뷰 MEDIUM). 잠금 실패는 OSError 로 전파되며, 호출자
        # (main_window._make_storage) 는 이미 OSError 를 잡아 사용자에게
        # 안내하는 경로가 있으므로 새 예외 타입을 추가하지 않는다.
        acquire_output_lock(self.output_dir)

    # ── 경로 ────────────────────────────────────────────────────────
    @property
    def collected_ids_path(self) -> Path:
        return self.output_dir / config.COLLECTED_IDS_FILENAME

    @property
    def state_path(self) -> Path:
        return self.output_dir / config.STATE_FILENAME

    def _partial_path(self, label: str) -> Path:
        safe = sanitize_filename(label) if label else "unlabeled"
        return self.output_dir / f"{_PARTIAL_PREFIX}{safe}.json"

    # ── collected_ids.json (§8.1) ──────────────────────────────────
    def load_collected_ids_status(self) -> tuple[LoadStatus, set[str]]:
        """load_collected_ids() 의 구조화된 버전 — QUARANTINE_FAILED 를 구분한다
        (6차 리뷰 HIGH 회귀 방지). collected_ids.json 은 중복 방지의 단일
        진실 공급원이므로, 격리(백업)조차 실패해 원본이 위험한 상태로
        남아있는데도 호출자가 이를 모른 채 계속 진행하면 두 가지 위험이
        있다: ① 신규/기수집 판정이 (사실은 알 수 없는데) 텅 빈 것으로
        오판되어 대량 재수집이 발생하고, ② 그 뒤 save_collected_ids() 가
        원본을 백업 없이 덮어써 영구히 잃는다. 호출자(crawl()/on_start())
        는 이 상태를 보고 실행을 중단해야 한다.
        """
        path = self.collected_ids_path
        if not path.exists():
            return LoadStatus.MISSING, set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise TypeError(
                    f"최상위 타입이 list 가 아님(실제: {type(data).__name__})"
                )
            ids = {str(x) for x in data}
            return (LoadStatus.VALID if ids else LoadStatus.EMPTY), ids
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
            quarantined = self._quarantine_corrupt(path, e)
            return (LoadStatus.QUARANTINED if quarantined else LoadStatus.QUARANTINE_FAILED), set()

    def load_collected_ids(self) -> set[str]:
        """상태 구분이 필요없는 호출자를 위한 편의 래퍼."""
        _status, ids = self.load_collected_ids_status()
        return ids

    def save_collected_ids(self, ids: set[str]) -> None:
        payload = json.dumps(sorted(ids), ensure_ascii=False)
        self._atomic_write(self.collected_ids_path, payload)

    def reset_collected_ids(self) -> None:
        """수집 이력 초기화 (UI '초기화' 버튼)."""
        self.save_collected_ids(set())

    # ── fastcrawl_state.json (§8.2) ────────────────────────────────
    @staticmethod
    def _empty_state() -> dict:
        # 매번 새 dict/list 를 만든다 — 클래스 속성으로 공유된 가변 dict 를
        # 얕은 복사만 하면 내부 "completed" 리스트가 여러 호출 간에 같은
        # 객체로 공유되어, 한쪽에서 append() 하면 다른 호출의 "빈 상태"
        # 기본값까지 오염된다.
        return {"completed": [], "total_success": 0}

    def load_state_status(self) -> tuple[LoadStatus, dict]:
        """load_state() 의 구조화된 버전 — QUARANTINE_FAILED 를 구분한다
        (6차 리뷰 HIGH 회귀 방지)."""
        path = self.state_path
        if not path.exists():
            return LoadStatus.MISSING, self._empty_state()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise TypeError(
                    f"최상위 타입이 dict 가 아님(실제: {type(state).__name__})"
                )
            state.setdefault("completed", [])
            state.setdefault("total_success", 0)
            if not isinstance(state["completed"], list):
                raise TypeError("completed 필드가 list 가 아님")
            if not isinstance(state["total_success"], (int, float)):
                raise TypeError("total_success 필드가 숫자가 아님")
            is_empty = not state["completed"] and not state["total_success"]
            return (LoadStatus.EMPTY if is_empty else LoadStatus.VALID), state
        except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError) as e:
            quarantined = self._quarantine_corrupt(path, e)
            status = LoadStatus.QUARANTINED if quarantined else LoadStatus.QUARANTINE_FAILED
            return status, self._empty_state()

    def load_state(self) -> dict:
        """상태 구분이 필요없는 호출자를 위한 편의 래퍼."""
        _status, state = self.load_state_status()
        return state

    def save_state(self, state: dict) -> None:
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        self._atomic_write(self.state_path, payload)

    def reset_state(self) -> None:
        self.save_state(self._empty_state())

    def mark_completed(self, category_name: str, success_count: int) -> dict:
        """카테고리 완료 기록 + 누적 성공 반영 후 저장 (중단 재개용).

        state.json 격리(백업)조차 실패해 원본이 위험한 상태로 남아있으면
        (QUARANTINE_FAILED), 그 위에 새 상태를 저장하지 않는다 — 이 fastcrawl_state.json
        은 "참고용 이력"이라 커밋 자체를 막을 정도는 아니지만, 백업 없는
        손상 원본을 이 저장이 조용히 덮어써 영구히 잃게 만들면 안 된다
        (6차 리뷰 HIGH 회귀 방지). 이번 기록만 건너뛰고 원본은 그대로 둔다.
        """
        status, state = self.load_state_status()
        if status == LoadStatus.QUARANTINE_FAILED:
            return state
        if category_name not in state["completed"]:
            state["completed"].append(category_name)
        state["total_success"] = int(state.get("total_success", 0)) + int(success_count)
        self.save_state(state)
        return state

    # ── 부분 결과 체크포인트 (내구성 순서 보장용) ───────────────────
    def save_partial_results(self, records: list[dict], label: str) -> Path:
        """카테고리 진행 중 결과를 고정 파일명으로 원자적 저장(체크포인트).

        collected_ids 커밋 전에 반드시 먼저 호출하여, 중간에 프로세스가 죽어도
        '이미 수집됨으로 표시됐지만 어디에도 저장되지 않은' 레코드가 생기지 않게 한다.
        """
        path = self._partial_path(label)
        payload = json.dumps(records, ensure_ascii=False, indent=2)
        self._atomic_write(path, payload)
        return path

    def load_partial_results_status(self, label: str) -> tuple[LoadStatus, list[dict]]:
        """이전 실행이 비정상 종료되어 남은 체크포인트가 있으면 로드(복구용).

        최상위 타입뿐 아니라 각 원소가 dict 이고 유효한 문자열 goodscode 를
        갖는지도 검증한다(4차 리뷰 MEDIUM 회귀 방지) — 예전에는 `[1]` 처럼
        원소 타입이 잘못된 경우를 통과시켜, 승격 시 save_results() 가 JSON은
        쓰고 CSV 작성 중에야 AttributeError 로 실패했다.

        (LoadStatus, records) 를 반환한다 — 호출자가 QUARANTINE_FAILED 를
        MISSING/EMPTY 와 절대 혼동하지 않도록 한다(5차 리뷰 HIGH 회귀 방지).
        """
        path = self._partial_path(label)
        if not path.exists():
            return LoadStatus.MISSING, []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise TypeError(f"최상위 타입이 list 가 아님(실제: {type(data).__name__})")
            for i, item in enumerate(data):
                if not isinstance(item, dict):
                    raise TypeError(f"[{i}] 항목이 dict 가 아님(실제: {type(item).__name__})")
                code = item.get("goodscode")
                if not isinstance(code, str) or not code:
                    raise ValueError(f"[{i}] 항목의 goodscode 가 유효한 문자열이 아님")
            return (LoadStatus.VALID if data else LoadStatus.EMPTY), data
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
            quarantined = self._quarantine_corrupt(path, e)
            return (LoadStatus.QUARANTINED if quarantined else LoadStatus.QUARANTINE_FAILED), []

    def load_partial_results(self, label: str) -> list[dict]:
        """상태 구분이 필요없는 호출자를 위한 편의 래퍼 — 레코드만 반환한다."""
        _status, data = self.load_partial_results_status(label)
        return data

    def clear_partial(self, label: str) -> bool:
        """카테고리가 정상적으로 최종 저장을 마치면 체크포인트 파일을 정리.

        반환: 파일이 없었거나 삭제에 성공하면 True, 삭제(unlink)가 실패하면
        False. 예전에는 OSError 를 여기서 조용히 삼켜 호출자가 삭제 실패를
        절대 알 수 없었다 — 그러면 승격 로직이 '정리 성공'으로 오인해 다음
        실행에서 같은 체크포인트를 또 승격시켜 중복 결과 파일을 만들 수
        있었다(4차 리뷰 HIGH). 이제는 호출자가 실패를 받아 로그/구조화된
        결과에 반영하고, 재시도 시 중복 승격을 막는 판단에 쓸 수 있다.
        """
        path = self._partial_path(label)
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def find_leftover_partials(self) -> list[tuple[str, Path]]:
        """출력 디렉터리에 남아 있는 모든 체크포인트 파일을 (라벨, 경로) 목록으로 반환.

        Pre-scan/실행 계획과 무관하게 디스크를 직접 훑는다 — 그래야 어떤 카테고리가
        (예: 다음 조사에서 '완료됨'으로 판정되어) 더 이상 계획에 포함되지 않더라도,
        이전에 죽은 실행이 남긴 체크포인트가 영구히 숨겨진 채로 방치되지 않는다.

        손상 격리 백업(`.corrupt_*.bak`)은 `.json` 로 끝나지 않으므로 이 glob 에
        걸리지 않는다. 그래도 `.corrupt_` 를 이름에 포함한 파일은 한 번 더
        방어적으로 제외한다(5차 리뷰 HIGH) — 예전 백업 파일명(`.corrupt_{ts}.json`)
        이 이 glob 에 다시 걸려, 격리 백업을 체크포인트로 오인해 재귀적으로
        또 손상 처리하거나(`.corrupt_T.corrupt_T2.json`), '초기화' 시
        clear_all_partials() 가 유일한 백업까지 지워버리는 사고가 있었다.
        """
        results = []
        for path in sorted(self.output_dir.glob(f"{_PARTIAL_PREFIX}*.json")):
            if ".corrupt_" in path.name:
                continue
            label = path.stem[len(_PARTIAL_PREFIX):] or "unlabeled"
            results.append((label, path))
        return results

    def clear_all_partials(self) -> list[Path]:
        """모든 체크포인트 파일 제거 (UI '초기화' 버튼에서 사용).

        삭제에 실패한 경로 목록을 반환한다(빈 리스트면 전부 성공). 예전에는
        OSError 를 조용히 삼켰는데, 그러면 '초기화'가 실제로는 일부 실패했는데도
        호출자(main_window.on_reset)가 무조건 "초기화 완료"로 표시했다. 삭제되지
        않고 남은 체크포인트는 다음 실행의 승격 로직이 되살려 collected_ids 를
        초기화 의도와 다르게 다시 채울 수 있다(3차 리뷰 MEDIUM 회귀 방지) —
        호출자가 실패를 알아야 사용자에게 정확히 경고할 수 있다.
        """
        failed: list[Path] = []
        for _label, path in self.find_leftover_partials():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                failed.append(path)
        return failed

    # ── 승격 완료 manifest (5차/6차 리뷰 HIGH-2/3 회귀 방지) ─────────
    @property
    def promoted_manifest_path(self) -> Path:
        return self.output_dir / config.PROMOTED_MANIFEST_FILENAME

    def promoted_partial_path(self, label: str, content_hash: str) -> tuple[Path, Path]:
        """promote_partial() 이 실제로 쓸 (json, csv) 경로를 미리 계산한다
        (순수 함수, I/O 없음).

        이 경로가 결정적이라는 사실 자체가 manifest 없이도 "이 내용이 이미
        promote_partial() 로 승격됐는지"를 파일시스템에서 직접 확인할 수 있게
        한다 — manifest 는 참고용 캐시일 뿐 신뢰의 근거가 아니다. 실제 파일이
        디스크에 있는지가 근거다(6차 리뷰 HIGH-2 회귀 방지: manifest 는 결과
        파일 경로·존재 여부를 검증하지 않고 해시 문자열만 믿었다).
        """
        safe = sanitize_filename(label) if label else "unlabeled"
        base_name = f"{config.FILE_PREFIX}_{safe}_recovered_{content_hash}"
        return self.output_dir / f"{base_name}.json", self.output_dir / f"{base_name}.csv"

    def load_promotion_manifest_status(self) -> tuple[LoadStatus, dict[str, dict[str, str]]]:
        """콘텐츠 해시 → {"json": path_str, "csv": path_str} 매핑을 로드한다.

        이 매핑은 "카테고리 정상 최종 저장(save_results, 타임스탬프 파일명)
        경로로 이미 저장된 내용"을 가리키는 참고용 캐시일 뿐이다 — 호출자는
        반드시 참조된 파일이 실제로 존재하는지 검증한 뒤에만 신뢰해야 한다
        (6차 리뷰 HIGH-2). manifest 가 손상되거나 사라져도 최악의 경우
        promote_partial() 로 중복 파일 하나가 다시 생성될 뿐 데이터 유실은
        없다 — 그래서 QUARANTINE_FAILED 를 fail-closed 로 막지 않고 그냥 빈
        매핑으로 계속 진행해도 안전하다(6차 리뷰 HIGH-3: manifest 손상은
        더 이상 fail-open 이 위험하지 않도록 설계 자체를 바꿨다).
        """
        path = self.promoted_manifest_path
        if not path.exists():
            return LoadStatus.MISSING, {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError(f"최상위 타입이 dict 가 아님(실제: {type(data).__name__})")
            entries: dict[str, dict[str, str]] = {}
            for h, v in data.items():
                if not isinstance(v, dict):
                    continue
                json_p, csv_p = v.get("json"), v.get("csv")
                if isinstance(json_p, str) and isinstance(csv_p, str):
                    entries[str(h)] = {"json": json_p, "csv": csv_p}
            return (LoadStatus.VALID if entries else LoadStatus.EMPTY), entries
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
            quarantined = self._quarantine_corrupt(path, e)
            status = LoadStatus.QUARANTINED if quarantined else LoadStatus.QUARANTINE_FAILED
            return status, {}

    def load_promotion_manifest(self) -> dict[str, dict[str, str]]:
        """상태 구분이 필요없는 호출자를 위한 편의 래퍼."""
        _status, entries = self.load_promotion_manifest_status()
        return entries

    def mark_promoted(self, content_hash: str, json_path: Path, csv_path: Path) -> None:
        """이 콘텐츠 해시가 이미 (json_path, csv_path) 로 저장됐음을 기록한다.

        체크포인트 승격(promote_partial, 결정적 파일명)과 카테고리 정상 최종
        저장(save_results, 타임스탬프 파일명)은 서로 다른 파일명 체계를 쓴다.
        정상 저장 후 체크포인트 삭제가 실패해 같은 체크포인트가 다음 실행에도
        남아 있으면, reconcile 이 이를 "아직 승격 안 됨"으로 오인해
        promote_partial 로 별도의 _recovered_{hash} 파일을 또 만들어 중복이
        생길 수 있다(5차 리뷰 HIGH). 정상 저장 직후에도 이 manifest 에
        경로를 기록해두면, 나중에 reconcile 이 같은 해시를 발견했을 때(그리고
        이 경로가 실제로 아직 존재함을 확인한 뒤) 재승격 없이 체크포인트
        정리만 시도할 수 있다.
        """
        status, entries = self.load_promotion_manifest_status()
        if status == LoadStatus.QUARANTINE_FAILED:
            # 손상된 manifest 를 격리조차 못한 상태 — 그 위에 덮어쓰면 원본을
            # 영구히 잃는다. manifest 는 재구축 가능한 캐시이므로 이번 기록만
            # 건너뛰고 원본은 보존한다(6차 리뷰 HIGH-4 회귀 방지).
            return
        entries[content_hash] = {"json": str(json_path), "csv": str(csv_path)}
        self._atomic_write(
            self.promoted_manifest_path,
            json.dumps(entries, ensure_ascii=False),
        )

    # ── 결과 저장 (§9) ─────────────────────────────────────────────
    def save_results(self, records: list[dict], label: str = "") -> tuple[Path, Path]:
        """JSON(utf-8, indent=2) + CSV(utf-8-sig) 저장. (json_path, csv_path) 반환.

        같은 초에 같은 라벨로 두 번 이상 저장되어도(예: 부분 체크포인트 직후 최종
        저장) 기존 파일을 덮어쓰지 않도록 경로 충돌 시 `_2`, `_3` ... 을 붙인다.
        """
        # .astimezone() 는 로컬 시각 값 자체는 바꾸지 않고 tzinfo 만 채운다
        # (strftime 의 날짜/시간 포맷 코드는 tzinfo 유무와 무관하게 동일한
        # 로컬 필드를 출력한다) — DTZ005(naive datetime) 린트만 해소한다.
        ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        safe = sanitize_filename(label) if label else ""
        suffix = f"_{safe}" if safe else ""
        base_name = f"{config.FILE_PREFIX}{suffix}_{ts}"
        json_path, csv_path = self._unique_pair(base_name)
        self._write_records(json_path, csv_path, records)
        return json_path, csv_path

    def promote_partial(
        self, records: list[dict], label: str, content_hash: str
    ) -> tuple[Path, Path]:
        """체크포인트 승격 전용 저장 — 결정적(deterministic) 파일명을 쓴다.

        save_results() 는 매번 새 타임스탬프로 고유 파일을 만든다. 체크포인트
        삭제가 실패해 같은 체크포인트가 다음 승격 시도에도 그대로 남아있으면,
        save_results() 를 다시 호출할 때마다 같은 내용의 결과가 새 타임스탬프
        파일로 계속 중복 생성된다(4차 리뷰 HIGH). 여기서는 체크포인트 내용
        (필드 값 포함, 5차 리뷰 HIGH)의 해시를 파일명에 포함해, 같은 내용을
        다시 승격해도 항상 같은 경로에 덮어쓴다(멱등) — 반대로 내용이 다르면
        (별개의 승격 사례) 해시도 달라 서로 다른 파일에 남으므로, 예전에
        고정 파일명을 쓰다가 서로 다른 크래시 회차의 데이터가 서로를
        덮어써 유실되던 문제도 함께 피한다.
        """
        json_path, csv_path = self.promoted_partial_path(label, content_hash)
        self._write_records(json_path, csv_path, records)
        return json_path, csv_path

    def _write_records(self, json_path: Path, csv_path: Path, records: list[dict]) -> None:
        """JSON+CSV 를 임시 파일에 먼저 쓰고, 둘 다 성공한 뒤에만 최종 이름으로
        교체한다(5차 리뷰 MEDIUM 회귀 방지).

        예전에는 JSON 을 최종 경로에 직접 쓴 뒤 CSV 를 작성했다 — CSV 작성이
        레코드 형태 문제 등으로 중간에 실패하면, JSON 은 이미 "완료된 결과"처럼
        보이는 최종 파일로 남아 있는데 짝이 되는 CSV 는 없거나 헤더만 있는
        반쪽짜리 상태가 됐다. 두 파일 모두 임시 이름으로 완전히 쓴 뒤 원자적
        rename(Path.replace) 으로 교체하면, 실패 시 최종 이름의 파일이 아예
        생기지 않아 "완료된 것처럼 보이는 불완전한 결과"가 남지 않는다.

        JSON replace 는 성공했는데 CSV replace 만 실패하는 좁은 창(예: 두
        rename 사이 디스크 오류)에도 대비한다 — 그 경우 이미 옮겨진 JSON 을
        최선을 다해 되돌려(unlink) "JSON 만 있고 CSV 는 없는" 반쪽 결과가
        남지 않게 한다(6차 리뷰 MEDIUM 회귀 방지).
        """
        json_tmp = json_path.with_name(json_path.name + ".tmp")
        csv_tmp = csv_path.with_name(csv_path.name + ".tmp")
        try:
            json_tmp.write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # CSV 는 항상 스키마 순서 고정 헤더로 기록(레코드가 비어도 헤더 생성)
            # + 수식 주입 방지를 위해 각 셀 값을 이스케이프한다.
            with open(csv_tmp, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=RECORD_FIELDS, extrasaction="ignore")
                writer.writeheader()
                for rec in records:
                    writer.writerow({k: _csv_safe(v) for k, v in rec.items()})

            json_tmp.replace(json_path)
            try:
                csv_tmp.replace(csv_path)
            except Exception:
                # CSV 교체만 실패 — 이미 최종 이름으로 옮겨진 JSON 을 되돌려
                # "JSON 만 완료된 것처럼 보이는" 반쪽 쌍이 남지 않게 한다.
                try:
                    json_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        finally:
            # 성공 시 위 replace() 로 이미 사라졌으므로 아래는 실패 시 정리용
            # (최선의 노력 — 이 정리 자체의 실패는 무시한다). 이 unlink 가
            # 예외를 던지면 이미 성공적으로 끝난 replace() 를 실패로 오인시켜
            # 호출자가 "쓰기 자체가 실패했다"고 잘못 판단하게 만들 수 있다.
            for tmp in (json_tmp, csv_tmp):
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    # ── 내부 ────────────────────────────────────────────────────────
    def _unique_pair(self, base_name: str) -> tuple[Path, Path]:
        """base_name.json/.csv 가 이미 존재하면 충돌하지 않는 경로를 찾아 반환."""
        json_path = self.output_dir / f"{base_name}.json"
        csv_path = self.output_dir / f"{base_name}.csv"
        if not json_path.exists() and not csv_path.exists():
            return json_path, csv_path
        n = 2
        while True:
            jp = self.output_dir / f"{base_name}_{n}.json"
            cp = self.output_dir / f"{base_name}_{n}.csv"
            if not jp.exists() and not cp.exists():
                return jp, cp
            n += 1

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """임시 파일에 쓰고 교체하여 중간 저장 중 손상 방지."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _quarantine_corrupt(path: Path, err: Exception) -> bool:
        """손상된 JSON 파일을 조용히 버리지 않고 백업 후 경고.

        기존에는 파싱 실패 시 빈 상태로 되돌리고 다음 저장 때 원본을 덮어썼다.
        이는 원본 데이터를 영구히 잃고(대규모 재수집/중복 유발) 사용자도 알 수
        없게 만든다. 원본을 백업으로 보존하고 RuntimeWarning 을 발생시킨다.

        반환: 백업(격리)에 성공하면 True, 실패하면 False. 예전에는 rename
        (Path.replace) 이 실패해도 그냥 `backup = path` 로 눙쳐 "처리됨"처럼
        보이게 했다 — 호출자가 `path.exists()` 만으로 격리 여부를 판단했는데,
        rename 실패 시 원본이 원래 자리에 그대로 남아 있어 "빈 체크포인트"로
        오인되어 다음 코드가 그 유일한 원본을 그냥 삭제해버리는 사고가 났다
        (5차 리뷰 HIGH). 이제는 실패를 명시적으로 반환해 호출자가 fail-closed
        로 처리할 수 있게 한다.

        백업 파일명은 원본 전체 파일명 뒤에 `.corrupt_{ts}.bak` 을 붙인다.
        예전에는 `{stem}.corrupt_{ts}{suffix}` 형태(예: `.json` 로 끝남)를 써서
        체크포인트 glob(`.partial_*.json`) 에 백업 파일 자신이 다시 걸려,
        재귀적으로 또 손상 처리되거나(`.corrupt_T.corrupt_T2.json`) '초기화'가
        유일한 백업까지 지워버리는 문제가 있었다(5차 리뷰 HIGH). `.bak` 확장자는
        이 코드베이스의 어떤 glob 패턴과도 겹치지 않는다.

        타임스탬프는 마이크로초까지 포함하지만, 같은 파일이 같은 마이크로초에
        두 번 손상·격리되는 극단적 경쟁까지 대비해 이미 존재하는 백업 경로와
        충돌하면 `_2`, `_3` ... 을 붙여 절대 덮어쓰지 않는다(6차 리뷰 MEDIUM
        회귀 방지) — 초 단위 타임스탬프만 쓰면 같은 초에 같은 파일이 두 번
        격리될 때 먼저 만든 백업이 조용히 덮어써질 수 있었다.
        """
        # .astimezone() 는 로컬 시각 값 자체는 바꾸지 않고 tzinfo 만 채운다
        # (strftime 의 날짜/시간 포맷 코드는 tzinfo 유무와 무관하게 동일한
        # 로컬 필드를 출력한다) — DTZ005(naive datetime) 린트만 해소한다.
        ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        backup = path.with_name(f"{path.name}.corrupt_{ts}.bak")
        n = 2
        while backup.exists():
            backup = path.with_name(f"{path.name}.corrupt_{ts}_{n}.bak")
            n += 1
        try:
            path.replace(backup)
        except OSError:
            # rename 실패(예: 크로스 디바이스) 시 복사로 한 번 더 시도해
            # 원본 데이터를 최대한 보존한다.
            try:
                backup.write_bytes(path.read_bytes())
            except OSError as copy_err:
                warnings.warn(
                    f"{path.name} 파일이 손상되어 격리를 시도했지만 백업 자체가 "
                    f"실패했습니다({err}; 백업 시도 오류: {copy_err}). 원본 파일이 "
                    f"그대로 남아 있으니 수동으로 확인/백업하세요. 이 파일은 "
                    f"자동으로 삭제되지 않습니다.",
                    RuntimeWarning,
                    stacklevel=3,
                )
                return False
            # 백업 복사는 성공했다 — 원본 정리는 최선을 다하되 실패해도
            # 데이터는 이미 안전하므로 격리 성공으로 취급한다.
            try:
                path.unlink()
            except OSError:
                pass
        warnings.warn(
            f"{path.name} 파일이 손상되어 읽을 수 없습니다({err}). "
            f"원본은 {backup.name} 으로 백업했습니다. 빈 상태로 새로 시작합니다.",
            RuntimeWarning,
            stacklevel=3,
        )
        return True
