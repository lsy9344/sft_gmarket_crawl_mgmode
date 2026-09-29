"""AliExpress 사업자 전화번호 추출 회귀 테스트."""

from app.core.aliexpress_category_crawler import (
    _extract_phone_from_product_props,
    _merge_cached_phone,
)


def test_phone_label_accepts_normalized_korean_and_english_aliases_from_both_shapes():
    props = {
        "showedProps": [
            {"attrName": "소비자 상담 전화 번호", "attrValue": ""},
        ],
        "showedPropsMap": {
            "phone": {
                "attrName": "Customer-Service Phone Number",
                "attrValue": "+82 2-1234-5678",
            }
        },
    }

    assert _extract_phone_from_product_props(props) == "+82 2-1234-5678"


def test_detail_placeholders_are_never_returned_as_phone():
    props = {
        "showedProps": [
            {"attrName": "전화번호", "attrValue": "상세페이지 참고"},
            {"attrName": "고객센터", "attrValue": "Refer to Product Details"},
        ],
        "showedPropsMap": {
            "phone": {"attrName": "Phone Number", "attrValue": "참조"},
        },
    }

    assert _extract_phone_from_product_props(props) == ""


def test_additional_detail_placeholder_phrasings_are_never_returned_as_phone():
    for value in ("See the details", "상세페이지 별도 표기", "상세설명 참고"):
        assert _extract_phone_from_product_props(
            {"showedProps": [{"attrName": "전화번호", "attrValue": value}]}
        ) == ""


def test_business_number_is_not_treated_as_phone_without_phone_label():
    props = {
        "showedProps": [
            {"attrName": "사업자등록번호", "attrValue": "123-45-67890"},
            {"attrName": "대표자", "attrValue": "홍길동"},
        ],
        "showedPropsMap": {},
    }

    assert _extract_phone_from_product_props(props) == ""


def test_phone_number_is_extracted_when_consultation_hours_follow_it():
    props = {
        "showedProps": [
            {
                "attrName": "고객센터 전화번호",
                "attrValue": "070-1234-5678 (상담 가능시간 09:00~18:00)",
            },
        ],
    }

    assert _extract_phone_from_product_props(props) == "070-1234-5678"


def test_country_code_with_contiguous_national_number_is_preserved():
    props = {
        "showedProps": [
            {"attrName": "전화번호", "attrValue": "82-1012345678"},
        ],
    }

    assert _extract_phone_from_product_props(props) == "82-1012345678"


def test_eight_digit_representative_number_is_valid_but_four_digits_is_not():
    assert _extract_phone_from_product_props(
        {"showedProps": [{"attrName": "전화번호", "attrValue": "15881234"}]}
    ) == "15881234"
    assert _extract_phone_from_product_props(
        {"showedProps": [{"attrName": "전화번호", "attrValue": "1588"}]}
    ) == ""


def test_cached_phone_fills_missing_current_value_and_current_phone_replaces_placeholder():
    cached = {
        "company_name": "상호",
        "phone": "상세 참고",
        "email": "seller@example.com",
    }

    phone, merged = _merge_cached_phone(cached, "070 - 1234 - 5678")

    assert phone == "070 - 1234 - 5678"
    assert merged == {
        "company_name": "상호",
        "phone": "070 - 1234 - 5678",
        "email": "seller@example.com",
    }

    phone, merged = _merge_cached_phone(merged, "상세페이지 참고")
    assert phone == "070 - 1234 - 5678"
    assert merged is None
