"""app.models.records 회귀 테스트 (성공 판정, 상태 라벨)."""

from __future__ import annotations

import unittest

from app.models.records import (
    STATUS_BLOCKED,
    STATUS_COLLECTABLE,
    STATUS_COMPLETED,
    STATUS_EMPTY,
    PrescanResult,
    SellerRecord,
)


class SellerRecordTest(unittest.TestCase):
    def test_valid_with_one_success_field(self) -> None:
        rec = SellerRecord(goodscode="1", url="u", company_name="에스엠")
        self.assertTrue(rec.is_valid())
        self.assertEqual(rec.filled_count(), 1)

    def test_invalid_when_all_empty(self) -> None:
        rec = SellerRecord(goodscode="1", url="u")
        self.assertFalse(rec.is_valid())
        self.assertEqual(rec.filled_count(), 0)

    def test_address_alone_does_not_count_as_success(self) -> None:
        """WORK_ORDER §5.5: address/ceo_name 은 성공 판정 5개 필드에서 제외."""
        rec = SellerRecord(goodscode="1", url="u", address="서울시")
        self.assertFalse(rec.is_valid())
        # 하지만 filled_count(표시용 n/6)에는 포함된다.
        self.assertEqual(rec.filled_count(), 1)

    def test_filled_count_caps_at_six(self) -> None:
        rec = SellerRecord(
            goodscode="1", url="u", store_name="s", company_name="c",
            email="e", phone="p", business_number="b", address="a",
        )
        self.assertEqual(rec.filled_count(), 6)


class PrescanResultTest(unittest.TestCase):
    def test_status_labels(self) -> None:
        cases = {
            STATUS_COLLECTABLE: "수집 가능",
            STATUS_COMPLETED: "완료됨",
            STATUS_EMPTY: "상품 없음",
            STATUS_BLOCKED: "차단/오류",
        }
        for status, label in cases.items():
            result = PrescanResult("cat", "best", 1, 1, 0, ["1"], status)
            self.assertEqual(result.status_label, label)


if __name__ == "__main__":
    unittest.main()
