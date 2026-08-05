"""工号登录识别逻辑单测。"""
import pytest
from app.services.auth_service import _detect_account_type


def test_detect_employee_id_8_digits():
    assert _detect_account_type("12345678") == "employee_id"


def test_detect_employee_id_leading_zeros():
    assert _detect_account_type("00000001") == "employee_id"


def test_detect_email():
    assert _detect_account_type("user@example.com") == "email"


def test_detect_phone_intl():
    assert _detect_account_type("+8613812345678") == "phone"


def test_detect_phone_digits_9():
    # 9 位数字：不是工号（8 位），满足 7-15 位数字 → phone
    assert _detect_account_type("123456789") == "phone"


def test_detect_username():
    assert _detect_account_type("alice_bob") == "username"


def test_detect_7_digits_is_phone():
    # 7 位纯数字：满足 phone 正则（7-15 位），不是工号（需精确 8 位）
    assert _detect_account_type("1234567") == "phone"
