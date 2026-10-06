"""Patchright 카테고리 상위 데이터셋 — 3P 상품 1,000개 리뷰 내림차순 수집.

쿠팡 카테고리 목록 페이지는 리뷰 수 정렬을 제공하지 않으므로, 카드 DOM에서
리뷰 수와 배송 표시를 함께 읽어 파이썬에서 분류한다. 로켓배송(로켓프레시,
로켓직구, 판매자로켓 포함)과 제트배송 표시가 있는 카드는 제외하고 판매자
직배송(3P) 상품만 카테고리별 1,000개까지 저장한 뒤 리뷰 수 내림차순으로
최종 파일을 만든다.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.coupang.patchright_canary import (
    CATEGORY_URL,
    HOME_URL,
    claim_live_attempt,
    patchright_browser,
    profile_dir,
    record_block,
    settle_live_attempt,
)
from app.core.coupang.patchright_sample import _checkpoint, _navigate, _wait
from app.core.coupang.search_parser import parse_href, parse_price
from app.core.coupang.store_files import (
    category_item_key,
    merge_by_key,
    read_csv,
    write_csv,
    write_json,
)

# 상위 두 카테고리를 먼저 확인한 뒤 각 하위 카테고리를 트리 순서대로
# 이어서 확인한다. 기존 진행 상태의 카테고리 위치를 그대로 보존한다.
TOP_CATEGORIES = (
    ("194373", "식품/견과/건과"),
    ("194688", "식품/축산/계란/식용곤충"),
    ("194376", "식품/견과/건과/땅콩/호두"),
    ("194381", "식품/견과/건과/밤/잣/은행"),
    ("194387", "식품/견과/건과/아몬드/피스타치오"),
    ("194392", "식품/견과/건과/기타견과류"),
    ("194401", "식품/견과/건과/호박씨/해바라기씨"),
    ("194405", "식품/견과/건과/기타씨앗"),
    ("194407", "식품/견과/건과/혼합견과/세트"),
    ("194411", "식품/견과/건과/건과일/건채소"),
    ("394584", "식품/견과/건과/과일가루"),
    ("194690", "식품/축산/계란/식용곤충/소고기"),
    ("194726", "식품/축산/계란/식용곤충/돼지고기"),
    ("194756", "식품/축산/계란/식용곤충/닭/오리고기"),
    ("194792", "식품/축산/계란/식용곤충/양/말고기"),
    ("194805", "식품/축산/계란/식용곤충/기타 육고기"),
    ("194810", "식품/축산/계란/식용곤충/계란/알류/가공란"),
    ("194817", "식품/축산/계란/식용곤충/축산선물세트"),
    ("569073", "식품/축산/계란/식용곤충/식용곤충"),
)
TARGET_CATEGORY_ITEMS = 1_000
MAX_LISTING_ITEMS = 60
MAX_CATEGORY_PAGES = 50
EMPTY_PAGE_TOLERANCE = 2
PAGE_DELAY_MS = 15_000
MAX_SESSION_PAGES = 10
MAX_SESSION_PRODUCTS = MAX_SESSION_PAGES * MAX_LISTING_ITEMS

STATE_FILENAME = "top_state.json"
PRODUCTS_FILENAME = "top_products.csv"
SUMMARY_FILENAME = "top_summary.json"

PRODUCT_FIELDS = (
    "category_id",
    "category_name",
    "page_number",
    "product_id",
    "item_id",
    "vendor_item_id",
    "title",
    "price",
    "review_count",
    "delivery_markers",
    "url",
    "collected_at",
)
FINAL_FIELDS = (*PRODUCT_FIELDS, "matched_categories")

# 완성형 데이터셋은 두 상위 카테고리 각각 하나씩 만든다. 각 가족은 그
# 상위 카테고리와 모든 하위 카테고리를 뜻한다.
FINAL_FAMILY_CATEGORIES = {
    "194373": frozenset(
        {
            "194373",
            "194376",
            "194381",
            "194387",
            "194392",
            "194401",
            "194405",
            "194407",
            "194411",
            "394584",
        }
    ),
    "194688": frozenset(
        {
            "194688",
            "194690",
            "194726",
            "194756",
            "194792",
            "194805",
            "194810",
            "194817",
            "569073",
        }
    ),
}

# 병렬 확장 설계 §3.1: 작업 분할 단위는 카테고리 가족이다. 인스턴스 A는
# 위 기본 가족(TOP_CATEGORIES)을 그대로 쓰고, 다른 인스턴스(B/C)는 아래
# 로더로 외부 파일에서 자기 가족 목록을 받는다.
def load_categories_file(path: Path) -> list[tuple[str, str]]:
    """카테고리 가족 정의 JSON 파일에서 (id, 이름) 목록을 읽는다.

    파일 형식은 {"instance": ..., "description": ..., "categories":
    [[id, 이름], ...]}이다. instance/description은 사람용 메모라 이 로더는
    보지 않는다. 형식이 어긋나면 한국어 ValueError를 던진다.
    """
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(
            f"카테고리 목록 파일을 읽지 못했습니다({path}): {error}"
        ) from error
    if not isinstance(value, dict) or "categories" not in value:
        raise ValueError("카테고리 목록 파일에 categories 키가 없습니다.")
    items = value["categories"]
    if not isinstance(items, list) or not items:
        raise ValueError(
            "카테고리 목록 categories는 비어 있지 않은 목록이어야 합니다."
        )
    categories: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("카테고리 항목은 [id, 이름] 두 값이어야 합니다.")
        category_id, name = item
        if not isinstance(category_id, str) or not re.fullmatch(
            r"[0-9]+", category_id
        ):
            raise ValueError("카테고리 id는 숫자 문자열이어야 합니다.")
        if not isinstance(name, str):
            raise ValueError("카테고리 이름은 문자열이어야 합니다.")
        if category_id in seen:
            raise ValueError(f"카테고리 id가 중복됩니다: {category_id}")
        seen.add(category_id)
        categories.append((category_id, name))
    return categories


def _resolve_categories(
    categories: list[tuple[str, str]] | None,
) -> tuple[tuple[str, str], ...]:
    """주입 목록이 없으면 기본 A 가족(TOP_CATEGORIES)을 그대로 쓴다."""
    if categories is None:
        return TOP_CATEGORIES
    return tuple((category_id, name) for category_id, name in categories)


def _final_roots(
    categories: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """활성 목록과 겹치는 가족의 완성형 루트만 골라낸다.

    주입 목록(다른 인스턴스 가족)과 한 카테고리도 겹치지 않는
    FINAL_FAMILY_CATEGORIES 루트의 완성형은 비는 결과라 만들지 않는다.
    """
    active_ids = {category_id for category_id, _ in categories}
    return tuple(
        root
        for root, family in FINAL_FAMILY_CATEGORIES.items()
        if family & active_ids
    )


# 카드 DOM에서 상품 앵커 하나가 담고 있는 원시 표시를 모은다. 분류와 리뷰 수
# 파싱은 파이썬에서 하므로 실측 결과에 맞춰 고치기 쉽다.
EXTRACT_CARDS_JS = r"""
(limit) => {
  const products = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/vp/products/"]')) {
    const href = a.getAttribute('href') || '';
    let key = href;
    try {
      key = new URL(href, location.origin).searchParams.get('vendorItemId') || href;
    } catch (_error) {
      // 잘못된 링크는 href 자체로 중복을 판단한다.
    }
    if (!href || seen.has(key)) continue;
    seen.add(key);
    const card = a.closest('li') || a;
    const title = (a.innerText || a.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 200);
    const fullText = (card.innerText || '')
      .replace(/\s+/g, ' ').trim().slice(0, 400);
    const imageSrcs = Array.from(card.querySelectorAll('img'))
      .map(img => img.getAttribute('src') || '')
      .filter(Boolean).join(' ').slice(0, 600);
    products.push({ href, title, fullText, imageSrcs });
    if (products.length >= limit) break;
  }
  return products;
}
"""

# 실측 근거(search_parser DOM_EXTRACTION_JS) + 텍스트 표식. 로켓 계열
# 표지 이미지와 '로켓' 문구, 도착 보증 문구(불가 제외)를 모두 로켓으로 본다.
_ROCKET_IMAGE_MARKERS = ("rds/logo", "rds/delivery_badge", "badges/falcon")
_ROCKET_TEXT_MARKERS = ("로켓",)
_JET_IMAGE_MARKERS = ("rds/jet", "jet_delivery", "jetdelivery")
_JET_TEXT_MARKERS = ("제트",)
_REVIEW_TEXT_RES = (
    re.compile(r"리뷰\s*([\d,]+)\s*\+?"),
    re.compile(r"\(([\d,]{2,})\)"),
)


def _first_marker(text: str, image_srcs: str, markers: tuple[str, ...]) -> str:
    hay = f"{text} {image_srcs}".lower()
    for marker in markers:
        if marker.lower() in hay:
            return marker
    return ""


def classify_card(row: dict) -> dict:
    """추출 카드 하나에서 배송 표식과 리뷰 수를 판정한다."""
    text = str(row.get("fullText") or row.get("title") or "")
    image_srcs = str(row.get("imageSrcs") or "")
    markers: list[str] = []
    rocket_image = _first_marker("", image_srcs, _ROCKET_IMAGE_MARKERS)
    is_rocket = bool(rocket_image) or bool(
        _first_marker(text, "", _ROCKET_TEXT_MARKERS)
    )
    if is_rocket:
        markers.append(rocket_image or "로켓")
    if (
        "도착보증" in text.replace(" ", "")
        and "도착보증불가" not in text.replace(" ", "")
    ):
        is_rocket = True
        markers.append("도착 보증")
    jet_image = _first_marker("", image_srcs, _JET_IMAGE_MARKERS)
    is_jet = bool(jet_image) or bool(_first_marker(text, "", _JET_TEXT_MARKERS))
    if is_jet:
        markers.append(jet_image or "제트")
    review_count = 0
    for pattern in _REVIEW_TEXT_RES:
        match = pattern.search(text)
        if match:
            review_count = int(match.group(1).replace(",", ""))
            break
    return {
        "rocket": is_rocket,
        "jet": is_jet,
        "review_count": review_count,
        "delivery_markers": "|".join(markers),
    }


def _category_id_list(family: tuple[tuple[str, str], ...]) -> list[str]:
    return [category_id for category_id, _ in family]


def _pending_record(name: str) -> dict:
    return {
        "category_name": name,
        "status": "pending",
        "raw_seen": 0,
        "rocket_seen": 0,
        "jet_seen": 0,
        "tp_collected": 0,
        "pages_scanned": 0,
    }


def _new_state(
    family: tuple[tuple[str, str], ...] = TOP_CATEGORIES,
) -> dict:
    return {
        "version": 1,
        "status": "running",
        "category_index": 0,
        "page_number": 1,
        "consecutive_empty_pages": 0,
        "categories": {
            category_id: _pending_record(name)
            for category_id, name in family
        },
    }


_CATEGORY_STATUSES = (
    "pending",
    "running",
    "target_reached",
    "exhausted",
    "incomplete_limit_reached",
    "skipped",
)


def _validate_state(
    value: object,
    family: tuple[tuple[str, str], ...] = TOP_CATEGORIES,
) -> dict:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("상위 수집 상태 형식이 잘못됐습니다.")
    if value.get("status") not in ("running", "completed"):
        raise ValueError("상위 수집 상태가 잘못됐습니다.")
    for field in ("category_index", "page_number", "consecutive_empty_pages"):
        if not isinstance(value.get(field), int):
            raise ValueError(  # noqa: TRY004 - 저장 파일 형식 오류 계약
                "상위 수집 상태 숫자 형식이 잘못됐습니다."
            )
    if not 0 <= value["category_index"] <= len(family):
        raise ValueError("상위 수집 카테고리 위치가 잘못됐습니다.")
    if not 1 <= value["page_number"] <= MAX_CATEGORY_PAGES + 1:
        raise ValueError("상위 수집 페이지 위치가 잘못됐습니다.")
    stop_when = value.get("stop_when_final")
    if stop_when is not None:
        if (
            not isinstance(stop_when, dict)
            or set(stop_when) != {"category_id", "target"}
            or stop_when.get("category_id") not in FINAL_FAMILY_CATEGORIES
            or not isinstance(stop_when.get("target"), int)
            or isinstance(stop_when.get("target"), bool)
            or stop_when["target"] <= 0
        ):
            raise ValueError("완성형 중단 기준이 잘못됐습니다.")
    categories = value.get("categories")
    known_ids = set(_category_id_list(family))
    if (
        not isinstance(categories, dict)
        or not categories
        or not set(categories) <= known_ids
    ):
        raise ValueError("상위 수집 카테고리 기록이 잘못됐습니다.")
    for record in categories.values():
        if not isinstance(record, dict):
            raise ValueError("상위 수집 카테고리 기록이 잘못됐습니다.")
        if record.get("status") not in _CATEGORY_STATUSES:
            raise ValueError("상위 수집 카테고리 상태가 잘못됐습니다.")
        for field in (
            "raw_seen",
            "rocket_seen",
            "jet_seen",
            "tp_collected",
            "pages_scanned",
        ):
            if not isinstance(record.get(field), int) or record[field] < 0:
                raise ValueError("상위 수집 카테고리 수치가 잘못됐습니다.")
    return value


class TopThousandStore:
    """상위 데이터셋 결과 파일을 검증하고 덮어쓰기 없이 합친다."""

    def __init__(
        self,
        output_dir: Path,
        categories: list[tuple[str, str]] | None = None,
    ) -> None:
        self.output_dir = output_dir
        # 이 인스턴스가 수집할 카테고리 가족(병렬 확장 설계 §3.1). 주입이
        # 없으면 기본 A 가족을 그대로 쓴다.
        self.family = _resolve_categories(categories)
        self.products_path = output_dir / PRODUCTS_FILENAME
        self.state_path = output_dir / STATE_FILENAME
        self.summary_path = output_dir / SUMMARY_FILENAME

    def ensure_files(self) -> None:
        if not self.products_path.exists():
            write_csv(self.products_path, PRODUCT_FIELDS, [])
        if not self.state_path.exists():
            write_json(self.state_path, _new_state(self.family))
        if not self.summary_path.exists():
            write_json(
                self.summary_path,
                {
                    "status": "running",
                    "categories": {
                        category_id: {"category_name": name, "status": "pending"}
                        for category_id, name in self.family
                    },
                    "updated_at": "",
                },
            )

    def validate(self) -> None:
        read_csv(self.products_path, PRODUCT_FIELDS)
        _validate_state(
            json.loads(self.state_path.read_text(encoding="utf-8")), self.family
        )
        try:
            summary = json.loads(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(  # noqa: TRY004 - 저장 파일 형식 오류 계약
                f"{self.summary_path.name}을 읽지 못했습니다: {error}"
            ) from error
        if not isinstance(summary, dict):
            raise ValueError(f"{self.summary_path.name} 형식이 잘못됐습니다.")

    def merge_products(self, rows: list[dict]) -> tuple[int, int]:
        """카테고리·상품 쌍을 기준으로 합친다.

        같은 상품이 부모와 하위 카테고리 목록에 모두 나오면 각 카테고리
        데이터셋에 모두 남는다. 카테고리가 다른 행은 서로 덮어쓰지 않는다.
        새 행은 카테고리·상품 ID 가 모두 있어야 들어가지만, 저장된 행은
        상품 ID 만으로 인덱싱해 카테고리가 비은 행도 보존한다.
        """
        return merge_by_key(
            self.products_path,
            PRODUCT_FIELDS,
            rows,
            key_of=category_item_key,
            existing_key_of=_existing_product_key,
        )

    def products_for_category(self, category_id: str) -> list[dict]:
        return [
            row
            for row in read_csv(self.products_path, PRODUCT_FIELDS)
            if row["category_id"] == category_id
        ]

    def write_top_csv(self, category_id: str) -> Path:
        """카테고리 결과를 리뷰 수 내림차순으로 정렬해 최종 파일을 쓴다."""
        rows = self.products_for_category(category_id)
        rows.sort(key=lambda row: -int(row["review_count"] or 0))
        path = self.output_dir / f"top_1000_{category_id}.csv"
        write_csv(path, PRODUCT_FIELDS, rows[:TARGET_CATEGORY_ITEMS])
        return path

    def update_summary(self, state: dict, *, extra: dict | None = None) -> None:
        value = {
            "status": state["status"],
            "categories": state["categories"],
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if extra:
            value.update(extra)
        write_json(self.summary_path, value)


def family_unique_products(store: TopThousandStore, root_category_id: str) -> int:
    """가족(상위+하위 카테고리 전체)에서 중복 제거한 고유 상품 수."""
    family = FINAL_FAMILY_CATEGORIES[root_category_id]
    rows = read_csv(store.products_path, PRODUCT_FIELDS)
    return len(
        {
            row["vendor_item_id"]
            for row in rows
            if row["category_id"] in family and row["vendor_item_id"]
        }
    )


def build_final_dataset(
    store: TopThousandStore, root_category_id: str
) -> tuple[Path, int]:
    """가족 전체를 합친 완성형 데이터셋을 만든다.

    같은 상품이 여러 카테고리에 있으면 리뷰 수가 가장 큰 행 하나만 남기고,
    그 상품이 나왔던 카테고리는 matched_categories 열에 모두 기록한다.
    리뷰 수 내림차순으로 정렬해 상위 1,000행만 파일로 쓴다. 반환하는 수는
    컷 이전의 고유 상품 수다.
    """
    family = FINAL_FAMILY_CATEGORIES[root_category_id]
    rows = read_csv(store.products_path, PRODUCT_FIELDS)
    by_vendor: dict[str, dict] = {}
    matched: dict[str, list[str]] = {}
    for row in rows:
        if row["category_id"] not in family or not row["vendor_item_id"]:
            continue
        viid = row["vendor_item_id"]
        names = matched.setdefault(viid, [])
        if row["category_name"] not in names:
            names.append(row["category_name"])
        current = by_vendor.get(viid)
        if current is None or int(row["review_count"] or 0) > int(
            current["review_count"] or 0
        ):
            by_vendor[viid] = row
    unique_rows = [
        {
            **row,
            "matched_categories": " > ".join(matched[row["vendor_item_id"]]),
        }
        for row in by_vendor.values()
    ]
    unique_rows.sort(key=lambda row: -int(row["review_count"] or 0))
    unique_count = len(unique_rows)
    path = store.output_dir / f"final_dataset_{root_category_id}.csv"
    write_csv(path, FINAL_FIELDS, unique_rows[:TARGET_CATEGORY_ITEMS])
    return path, unique_count


def build_combined_final_dataset(store: TopThousandStore) -> tuple[Path, int]:
    """두 가족을 모두 합친 단일 완성형 데이터셋을 만든다(상한 없음).

    사용자가 최종 산출물로 요청한 형태다. 카테고리 중복은 vendorItemId 기준으로
    제거하고, 그 상품이 나왔던 카테고리는 matched_categories 열에 모두 남긴다.
    """
    rows = read_csv(store.products_path, PRODUCT_FIELDS)
    by_vendor: dict[str, dict] = {}
    matched: dict[str, list[str]] = {}
    for row in rows:
        viid = row["vendor_item_id"]
        if not viid:
            continue
        names = matched.setdefault(viid, [])
        if row["category_name"] not in names:
            names.append(row["category_name"])
        current = by_vendor.get(viid)
        if current is None or int(row["review_count"] or 0) > int(
            current["review_count"] or 0
        ):
            by_vendor[viid] = row
    unique_rows = [
        {
            **row,
            "matched_categories": " > ".join(matched[row["vendor_item_id"]]),
        }
        for row in by_vendor.values()
    ]
    unique_rows.sort(key=lambda row: -int(row["review_count"] or 0))
    path = store.output_dir / "final_dataset_all.csv"
    write_csv(path, FINAL_FIELDS, unique_rows)
    return path, len(unique_rows)


def _migrate_state(
    value: dict,
    family: tuple[tuple[str, str], ...] = TOP_CATEGORIES,
) -> dict:
    """카테고리 목록이 늘어나면 기존 진행 상태에 이어서 새 카테고리를 채운다."""
    for category_id, name in family:
        value["categories"].setdefault(category_id, _pending_record(name))
    if value["status"] == "completed" and any(
        record["status"] == "pending"
        for record in value["categories"].values()
    ):
        value["status"] = "running"
    if value["status"] == "running":
        while value["category_index"] < len(family) and value["categories"][
            family[value["category_index"]][0]
        ]["status"] not in (
            "pending",
            "running",
        ):
            value["category_index"] += 1
        if value["category_index"] == len(family):
            value["status"] = "completed"
        else:
            current = value["categories"][family[value["category_index"]][0]]
            if current["status"] == "pending":
                current["status"] = "running"
    return value


def read_state(
    output_dir: Path,
    categories: list[tuple[str, str]] | None = None,
) -> dict:
    family = _resolve_categories(categories)
    path = output_dir / STATE_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _new_state(family)
    except (OSError, ValueError) as error:
        raise ValueError(f"상위 수집 상태를 읽지 못했습니다: {error}") from error
    return _migrate_state(_validate_state(value, family), family)


def save_state(output_dir: Path, state: dict) -> str:
    value = dict(state)
    value["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_json(output_dir / STATE_FILENAME, value)
    return str(output_dir / STATE_FILENAME)


def _finish_category(
    state: dict,
    category_id: str,
    status: str,
    family: tuple[tuple[str, str], ...],
) -> None:
    state["categories"][category_id]["status"] = status
    state["category_index"] += 1
    state["page_number"] = 1
    state["consecutive_empty_pages"] = 0
    while state["category_index"] < len(family) and state["categories"][
        family[state["category_index"]][0]
    ]["status"] not in ("pending", "running"):
        state["category_index"] += 1
    if state["category_index"] == len(family):
        state["status"] = "completed"
    else:
        next_id = family[state["category_index"]][0]
        state["categories"][next_id]["status"] = "running"


def _apply_total_stop(
    state: dict,
    store: "TopThousandStore",
    category_id: str,
    family: tuple[tuple[str, str], ...],
) -> None:
    """완성형 중단 기준에 닿으면 모든 카테고리를 마감 처리한다.

    진행 중이던 카테고리는 지금까지 모은 행으로 최종 파일을 만들고 사용자
    중단(skipped) 상태로 남긴다. 아직 시작하지 않은 카테고리도 건너뛴 것으로
    기록해 이후 예약 실행이 남은 작업 없이 완료로 종료되게 한다.
    """
    record = state["categories"][category_id]
    if record["status"] in ("running", "pending"):
        record["status"] = "skipped"
        store.write_top_csv(category_id)
    for other in state["categories"].values():
        if other["status"] in ("pending", "running"):
            other["status"] = "skipped"
    state["status"] = "completed"
    state["category_index"] = len(family)
    state["page_number"] = 1
    state["consecutive_empty_pages"] = 0


def _advance_state(
    state: dict,
    *,
    raw_count: int,
    rocket_count: int,
    jet_count: int,
    saved_count: int,
    family: tuple[tuple[str, str], ...],
) -> None:
    category_id, _ = family[state["category_index"]]
    record = state["categories"][category_id]
    if record["status"] == "pending":
        record["status"] = "running"
    record["raw_seen"] += raw_count
    record["rocket_seen"] += rocket_count
    record["jet_seen"] += jet_count
    record["tp_collected"] += saved_count
    record["pages_scanned"] += 1
    state["page_number"] += 1
    if raw_count:
        state["consecutive_empty_pages"] = 0
    else:
        state["consecutive_empty_pages"] += 1
        if state["consecutive_empty_pages"] >= EMPTY_PAGE_TOLERANCE:
            _finish_category(state, category_id, "exhausted", family)
            return
    if record["tp_collected"] >= TARGET_CATEGORY_ITEMS:
        _finish_category(state, category_id, "target_reached", family)
        return
    if state["page_number"] > MAX_CATEGORY_PAGES:
        _finish_category(state, category_id, "incomplete_limit_reached", family)


def _product_row(
    card: dict,
    classification: dict,
    *,
    category_id: str,
    category_name: str,
    page_number: int,
    collected_at: str,
) -> dict | None:
    legacy, item_id, vendor_item_id = parse_href(str(card.get("href") or ""))
    if not vendor_item_id:
        return None
    return {
        "category_id": category_id,
        "category_name": category_name,
        "page_number": str(page_number),
        "product_id": legacy,
        "item_id": item_id,
        "vendor_item_id": vendor_item_id,
        "title": str(card.get("title") or "")[:200],
        "price": str(parse_price(str(card.get("fullText") or "")) or 0),
        "review_count": str(classification["review_count"]),
        "delivery_markers": classification["delivery_markers"],
        "url": (
            f"https://www.coupang.com/vp/products/{legacy}"
            f"?itemId={item_id}&vendorItemId={vendor_item_id}"
        ),
        "collected_at": collected_at,
    }


def _existing_product_key(row: dict):
    """저장된 상품 행의 인덱스 키 — 카테고리가 비어 있어도 상품은 보존한다."""
    if not row.get("vendor_item_id"):
        return None
    return (str(row.get("category_id") or ""), str(row.get("vendor_item_id")))


def _cards_from_page(page, limit: int) -> list[dict]:
    rows = page.evaluate(EXTRACT_CARDS_JS, limit)
    if not isinstance(rows, list):
        raise ValueError("카테고리 카드 추출 결과가 목록이 아닙니다.")
    return rows


def run_top_pages(
    *,
    output_dir: Path,
    page_count: int = 8,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
    state_root: Path | None = None,
    browser_scope_factory: Callable | None = None,
    proxy: dict | None = None,
    categories: list[tuple[str, str]] | None = None,
) -> dict:
    """현재 카테고리 위치에서 3P 상품만 page_count쪽까지 확인하고 저장한다.

    categories에 다른 인스턴스의 카테고리 가족 목록을 주면 그 목록만
    수집한다(병렬 확장 설계 §3.1). None이면 기본 A 가족을 쓴다.
    """
    if not 1 <= page_count <= MAX_SESSION_PAGES:
        raise ValueError(f"page_count는 1~{MAX_SESSION_PAGES}이어야 합니다.")
    output_dir.mkdir(parents=True, exist_ok=True)
    family = _resolve_categories(categories)
    store = TopThousandStore(output_dir, categories)
    store.ensure_files()
    state = read_state(output_dir, categories)
    store.validate()
    if state["status"] != "running":
        return {"event": "top_collection_complete", "status": state["status"]}

    starting_category_index = state["category_index"]
    category_id, category_name = family[state["category_index"]]
    result = {
        "mode": "top_thousand_products",
        "browser": "Patchright + installed Google Chrome",
        "category_id": category_id,
        "category_name": category_name,
        "page_number": state["page_number"],
        "requested_page_count": page_count,
        "document_navigations": 0,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    planned_items = min(page_count * MAX_LISTING_ITEMS, MAX_SESSION_PRODUCTS)
    allowed, reason = claim_live_attempt(
        state_root, planned_items=planned_items, planned_pages=page_count
    )
    if not allowed:
        result.update(event="guard_refused", reason=reason)
        return result

    factory = browser_scope_factory or patchright_browser
    try:
        with ExitStack() as stack:
            user_data_dir = profile_dir(state_root)
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = stack.enter_context(
                factory(user_data_dir, headless=False, proxy=proxy)
            )
            page = context.pages[0] if context.pages else context.new_page()
            if on_event:
                on_event({**result, "event": "browser_started"})
            blocked, status, reference = _navigate(
                page, HOME_URL, 1_500, control, result
            )
            if blocked:
                record_block(state_root, reference=reference)
                result.update(
                    event="blocked",
                    blocked_at="home",
                    reference=reference,
                    home_status=status,
                )
                return result
            result["home_status"] = status

            page_results: list[dict] = []
            total_raw = 0
            total_saved = 0
            for attempt in range(page_count):
                if state["status"] != "running":
                    break
                if state["category_index"] != starting_category_index:
                    break
                category_id, category_name = family[state["category_index"]]
                page_number = state["page_number"]
                category_url = CATEGORY_URL.format(category_id=category_id).replace(
                    "?page=1", f"?page={page_number}"
                )
                _checkpoint(control)
                blocked, status, reference = _navigate(
                    page, category_url, 2_000, control, result
                )
                if blocked:
                    record_block(state_root, reference=reference)
                    result.update(
                        event="blocked",
                        blocked_at="category",
                        blocked_category_id=category_id,
                        blocked_page_number=page_number,
                        reference=reference,
                        category_status=status,
                        page_results=page_results,
                    )
                    return result
                result["category_status"] = status

                cards = _cards_from_page(page, MAX_LISTING_ITEMS)
                _checkpoint(control)
                collected_at = time.strftime("%Y-%m-%d %H:%M:%S")
                remaining = TARGET_CATEGORY_ITEMS - state["categories"][
                    category_id
                ]["tp_collected"]
                product_rows: list[dict] = []
                rocket_count = 0
                jet_count = 0
                seen_vendor_ids = {
                    row["vendor_item_id"]
                    for row in store.products_for_category(category_id)
                }
                for card in cards:
                    classification = classify_card(card)
                    if classification["rocket"]:
                        rocket_count += 1
                        continue
                    if classification["jet"]:
                        jet_count += 1
                        continue
                    if len(product_rows) >= remaining:
                        break
                    row = _product_row(
                        card,
                        classification,
                        category_id=category_id,
                        category_name=category_name,
                        page_number=page_number,
                        collected_at=collected_at,
                    )
                    if row is None or row["vendor_item_id"] in seen_vendor_ids:
                        continue
                    seen_vendor_ids.add(row["vendor_item_id"])
                    product_rows.append(row)
                unique_total, added_count = store.merge_products(product_rows)
                _advance_state(
                    state,
                    raw_count=len(cards),
                    rocket_count=rocket_count,
                    jet_count=jet_count,
                    saved_count=added_count,
                    family=family,
                )
                state_path = save_state(output_dir, state)
                category_status = state["categories"][category_id]["status"]
                if category_status != "running":
                    top_path = store.write_top_csv(category_id)
                else:
                    top_path = None
                store.update_summary(state)
                page_result = {
                    "category_id": category_id,
                    "category_name": category_name,
                    "page_number": page_number,
                    "raw_count": len(cards),
                    "rocket_count": rocket_count,
                    "jet_count": jet_count,
                    "products_added": added_count,
                    "tp_collected": state["categories"][category_id][
                        "tp_collected"
                    ],
                    "category_status": category_status,
                    "status": status,
                }
                page_results.append(page_result)
                total_raw += len(cards)
                total_saved += added_count
                result.update(
                    category_id=category_id,
                    category_name=category_name,
                    page_number=page_number,
                    page_results=page_results,
                    tp_collected=state["categories"][category_id]["tp_collected"],
                    state_path=state_path,
                    products_path=str(store.products_path),
                )
                if top_path is not None:
                    result["top_csv_path"] = str(top_path)
                if on_event:
                    on_event({**result, "event": "top_page_saved"})

                stop_when = state.get("stop_when_final")
                if stop_when and family_unique_products(
                    store, stop_when["category_id"]
                ) >= stop_when["target"]:
                    _apply_total_stop(state, store, category_id, family)
                    result["state_path"] = save_state(output_dir, state)
                    result["final_datasets"] = {
                        root: str(build_final_dataset(store, root)[0])
                        for root in _final_roots(family)
                    }
                    result["final_datasets"]["all"] = str(
                        build_combined_final_dataset(store)[0]
                    )
                    store.update_summary(state)
                    break

                category_finished = state["category_index"] != (
                    starting_category_index
                )
                should_continue = (
                    attempt + 1 < page_count
                    and state["status"] == "running"
                    and not category_finished
                )
                if should_continue:
                    _wait(page, PAGE_DELAY_MS, control)
                    _checkpoint(control)

            final_event = "top_pages_completed"
            if state["status"] == "completed":
                final_event = "top_collection_complete"
            elif state["category_index"] != starting_category_index:
                final_event = "top_category_completed"
            if state["status"] == "completed":
                result["final_datasets"] = {
                    root: str(build_final_dataset(store, root)[0])
                    for root in _final_roots(family)
                }
                result["final_datasets"]["all"] = str(
                    build_combined_final_dataset(store)[0]
                )
            result.update(
                event=final_event,
                completed_page_attempts=len(page_results),
                total_raw_count=total_raw,
                total_products_added=total_saved,
            )
            settled, settle_reason = settle_live_attempt(
                planned_items,
                total_raw,
                state_root,
                planned_pages=page_count,
                actual_pages=len(page_results),
            )
            if not settled:
                result.update(event="failed", reason=settle_reason)
            return result
    except CancelledError:
        result["event"] = "cancelled"
        return result
    except Exception as error:  # noqa: BLE001 - 브라우저 경계 실패를 결과로 반환
        result.update(event="failed", error=f"{type(error).__name__}: {error}")
        return result
