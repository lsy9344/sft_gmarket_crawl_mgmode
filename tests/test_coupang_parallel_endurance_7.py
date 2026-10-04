"""7-병렬 내구성 시뮬레이션 — 장시간 다중 사이클·회전·승계·도중 복원(오프라인).

7개 인스턴스(직접 1 + Decodo 6)가 12개 가족을 80분 봉투로 순환하며:
세션 수백 회 규모의 사이클, 주입된 세션 사망(자동 교체), 가족 완주 승계,
실행 중간의 매니저 재구성(앱 재시작 = 상태 복원)이 모두 일어나도
데이터 무결성·공정성·종료 조건이 유지되는지 검증한다.
실시간 대기 없음(시뮬레이션 시계 + 가드 되감기), 네트워크·브라우저 없음.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_coupang_parallel_manager import (  # noqa: E402
    INTERVAL_SECONDS,
    T0,
    _RoutingFactory,
    _age_guard,
    _decodo,
    _events_of,
    _fake_decodo,
    _make_manager,
)

STEP_SECONDS = 600  # 시뮬레이션 시계 보폭 (10분)
FAMILY_COUNT = 12
INSTANCE_COUNT = 7
MAX_STEPS = 600  # 100시간분 — 넉넉한 상한


class _MixedPage:
    """목록(카드)·판매자(매핑·사업자정보) evaluate 를 모두 소화하는 가짜 페이지."""

    def __init__(self, family_vendors: dict[str, str], seller_payloads: dict):
        self.url = "about:blank"
        self.family_vendors = family_vendors  # viid → vendor_id
        self.seller_payloads = seller_payloads

    # ── 브라우저 계약 ──────────────────────────────────────────────
    def goto(self, url, **_kwargs):
        self.url = str(url)
        return SimpleNamespace(status=200)

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        return "<html><body>normal page content</body></html>"

    # ── evaluate 라우팅 ────────────────────────────────────────────
    def evaluate(self, script, argument=None):
        from app.core.coupang.patchright_top_thousand import EXTRACT_CARDS_JS
        from app.core.coupang.patchright_sample import (
            FETCH_STORE_REVIEW_JS,
            FETCH_VENDORS_JS,
        )

        if script == EXTRACT_CARDS_JS:
            return self._cards()
        if script == FETCH_VENDORS_JS:
            products = []
            for viid in argument:
                vendor_id = self.family_vendors.get(viid)
                if vendor_id is None:
                    continue
                products.append(
                    {
                        "productId": 10,
                        "itemId": 20,
                        "vendorItemId": viid,
                        "storeInfoArea": {
                            "vendorId": vendor_id,
                            "storeId": 99,
                            "displayName": "내구테스트스토어",
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
            payload = self.seller_payloads.get(argument)
            return {
                "status": 200,
                "body": json.dumps(payload if payload is not None else {}),
            }
        raise AssertionError(f"unexpected script: {script[:40]}")

    def _cards(self):
        from app.core.coupang.patchright_top_thousand import EXTRACT_CARDS_JS  # noqa: F401

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
        if not category_id or page_number != 1:
            return []  # 1페이지만 상품 — 이후 연속 빈 페이지로 카테고리 종료
        viid = f"endur-{category_id}"
        return [
            {
                "href": f"/vp/products/10?itemId=20&vendorItemId={viid}",
                "deliveryMarkers": "",
                "reviewCount": "123",
                "priceText": "1,000원 상품",
            },
            {
                "href": f"/vp/products/11?itemId=21&vendorItemId={viid}-b",
                "deliveryMarkers": "",
                "reviewCount": "45",
                "priceText": "2,000원 상품",
            },
        ]


def _seller_payload(vendor_id: str) -> dict:
    return {
        "name": f"주식회사{vendor_id}",
        "repPersonName": "홍길동",
        "businessNumber": "123-45-67890",
        "tel": {"tel1": "02", "tel2": "1234", "tel3": "5678"},
        "email": f"{vendor_id.lower()}@example.com",
    }


class ParallelEndurance7Test(unittest.TestCase):
    """7-병렬 장시간 순환 — 공정성·무결성·회전·복원."""

    def test_seven_instances_complete_all_families_with_rotation_and_resume(
        self,
    ) -> None:
        calls: list = []
        dead: dict = {}
        fake = _fake_decodo(calls, ready=True, dead_sids=dead)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_root = root / "out"
            events: list = []

            # 12개 가족(카테고리 1개씩) — 풀과 무관하게 자체 생성
            families = [
                [(f"90{index:04d}", f"내구가족/{index}")] for index in range(FAMILY_COUNT)
            ]
            family_vendors: dict[str, str] = {}
            seller_payloads: dict = {}
            for index, family in enumerate(families):
                cid = family[0][0]
                vendor_id = f"A{index:05d}"
                family_vendors[f"endur-{cid}"] = vendor_id
                family_vendors[f"endur-{cid}-b"] = vendor_id  # 같은 판매자
                seller_payloads[vendor_id] = _seller_payload(vendor_id)

            page = _MixedPage(family_vendors, seller_payloads)
            factory = _RoutingFactory(direct_page=page, decodo_page=page)

            with _decodo(fake):
                manager = _make_manager(
                    root,
                    families,
                    events,
                    factory=factory,
                    instance_count=INSTANCE_COUNT,
                    interval_minutes=int(INTERVAL_SECONDS // 60),
                    listing_pages=1,
                )
                self.assertEqual(len(manager.instances), INSTANCE_COUNT)

                session_before_resume = 0
                rotated_total = 0
                t = T0
                resumed = False
                for step in range(MAX_STEPS):
                    # 세션 사망 주입 — 특정 시점에 인스턴스 2의 sid 를 죽인다.
                    if step == 8:
                        dead["i2"] = fake.DecodoError(
                            "회선 확인 응답 오류(HTTP 502).", kind="response"
                        )
                    if step == 40:
                        dead["i2"] = fake.DecodoError(
                            "다시 사망", kind="response"
                        )

                    manager.run_due(t)
                    t += STEP_SECONDS
                    for sid in range(1, INSTANCE_COUNT + 1):
                        ledger = root / f"state_{sid}" / "canary_guard.json"
                        if ledger.exists():
                            _age_guard(ledger.parent)

                    rotated_total = sum(
                        1
                        for event in _events_of(events, "exit_ip")
                        if event.get("rotated")
                    )

                    # 중간 복원 지점 — 절반가량 진행됐을 때 매니저 재구성.
                    if not resumed and step == 60:
                        totals_before = {
                            key: state.total_products
                            for key, state in manager.instances.items()
                        }
                        session_before_resume = len(factory.captured)
                        manager = _make_manager(
                            root,
                            families,
                            events,
                            factory=factory,
                            instance_count=INSTANCE_COUNT,
                            interval_minutes=int(INTERVAL_SECONDS // 60),
                            listing_pages=1,
                        )
                        resumed = True
                        # 복원 검증 1 — 가족 배분·누적이 보존된다.
                        for key, before in totals_before.items():
                            self.assertGreaterEqual(
                                manager.instances[key].total_products,
                                before,
                                f"복원 후 인스턴스 {key} 누적 감소",
                            )

                    if manager.all_done():
                        break
                else:
                    self.fail(
                        f"{MAX_STEPS}보 내에 완주하지 못함 — "
                        f"상태: {[(k, v.status) for k, v in manager.instances.items()]}"
                    )

                # ── 종합 단정 ────────────────────────────────────────
                self.assertTrue(manager.all_done())
                # 1) 모든 인스턴스가 done — error/blocked 없이 버텼다.
                for key, state in manager.instances.items():
                    self.assertEqual(
                        state.status, "done", f"인스턴스 {key}: {state.status}"
                    )
                # 2) 12개 가족 전부 상품+판매자 확보 (데이터 무결성)
                for index in range(FAMILY_COUNT):
                    family_dir = out_root / f"family_{index + 1:02d}"
                    products = family_dir / "top_products.csv"
                    sellers = family_dir / "top_sellers.csv"
                    self.assertTrue(products.exists(), f"{family_dir} 상품 없음")
                    rows = list(
                        csv.DictReader(
                            products.open(encoding="utf-8-sig"), delimiter=","
                        )
                    )
                    self.assertGreaterEqual(len(rows), 2)
                    ids = [row["vendor_item_id"] for row in rows]
                    self.assertEqual(len(ids), len(set(ids)))
                    saved = [
                        row
                        for row in csv.DictReader(
                            sellers.open(encoding="utf-8-sig"), delimiter=","
                        )
                        if row.get("status") == "saved"
                    ]
                    self.assertEqual(len(saved), 1, f"{family_dir} 판매자 미확보")
                # 3) 공정성 — 인스턴스별 세션 수 편차가 작다(기아 없음).
                per_instance: dict[str, int] = {}
                for user_data_dir, _proxy in factory.captured:
                    instance_id = user_data_dir.parent.name.split("_")[-1]
                    per_instance[instance_id] = (
                        per_instance.get(instance_id, 0) + 1
                    )
                self.assertEqual(len(per_instance), INSTANCE_COUNT)
                counts = sorted(per_instance.values())
                self.assertGreaterEqual(counts[0], 3)
                self.assertLessEqual(counts[-1] - counts[0], 4, per_instance)
                # 4) 세션 사망 주입에 자동 교체가 실제로 일어났다.
                self.assertGreaterEqual(rotated_total, 1)
                # 5) 복원 이후에도 세션이 계속돌았다(재시작이 정지가 아님).
                self.assertGreater(len(factory.captured), session_before_resume)


if __name__ == "__main__":
    unittest.main()
