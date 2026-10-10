"""Tests for parse_amount_to_cents and cents_to_decimal_str: the money math
that everything else in the app is built on. All amounts are integer cents;
these tests exist to prove no float drift and correct rounding at the edges.
"""
import pytest

from app.utils import cents_to_decimal_str, parse_amount_to_cents

INVALID = "Enter a valid amount, such as 12, 12.34, or $1,234.56."
INVALID_NEG_OK = "Enter a valid amount, such as 12, 12.34, $1,234.56, or -40."
TOO_LARGE = "Amount is too large. The most it can be is $999,999,999,999.99."


# --- parse_amount_to_cents: happy path formats -----------------------------

@pytest.mark.parametrize(
    "raw, expected_cents",
    [
        ("12", 1200),
        ("12.34", 1234),
        ("$50", 5000),
        ("1,234.56", 123456),
        ("$1,234.56", 123456),
    ],
)
def test_parse_amount_happy_path(raw, expected_cents):
    cents, err = parse_amount_to_cents(raw)
    assert err is None
    assert cents == expected_cents


# --- rounding edges ---------------------------------------------------------

def test_parse_amount_rounds_half_cent_up():
    # 0.005 dollars = 0.5 cents; ROUND_HALF_UP takes it to 1 cent, not 0.
    cents, err = parse_amount_to_cents("0.005", allow_zero=True)
    assert err is None
    assert cents == 1


def test_parse_amount_rounds_sub_cent_down_to_zero():
    # 0.004 dollars = 0.4 cents; rounds down to 0.
    cents, err = parse_amount_to_cents("0.004", allow_zero=True)
    assert err is None
    assert cents == 0


def test_parse_amount_rounds_999_up():
    cents, err = parse_amount_to_cents("10.999")
    assert err is None
    assert cents == 1100


def test_parse_amount_no_float_drift():
    # The classic 0.1 + 0.2 float trap: parsed via Decimal, sums must match
    # exactly instead of landing on 30.000000000000004-style drift.
    a, err_a = parse_amount_to_cents("0.1")
    b, err_b = parse_amount_to_cents("0.2", allow_zero=True)
    c, err_c = parse_amount_to_cents("0.3")
    assert err_a is None and err_b is None and err_c is None
    assert a + b == c


def test_parse_amount_very_large_value():
    cents, err = parse_amount_to_cents("1000000000.00")
    assert err is None
    assert cents == 100000000000


# --- negative handling -------------------------------------------------------

def test_parse_amount_negative_rejected_by_default():
    cents, err = parse_amount_to_cents("-40")
    assert cents is None
    assert err == INVALID


def test_parse_amount_negative_allowed():
    cents, err = parse_amount_to_cents("-40", allow_negative=True)
    assert err is None
    assert cents == -4000


def test_parse_amount_negative_comma_and_dollar():
    cents, err = parse_amount_to_cents("-$1,234.56", allow_negative=True)
    assert err is None
    assert cents == -123456


# --- zero handling ------------------------------------------------------------

def test_parse_amount_zero_rejected_by_default():
    cents, err = parse_amount_to_cents("0")
    assert cents is None
    assert err == "Amount must be greater than zero."


def test_parse_amount_zero_allowed():
    cents, err = parse_amount_to_cents("0", allow_zero=True)
    assert err is None
    assert cents == 0


# --- garbage / empty input -----------------------------------------------------

def test_parse_amount_none_input():
    cents, err = parse_amount_to_cents(None)
    assert cents is None
    assert err == "Amount is required."


def test_parse_amount_empty_string():
    cents, err = parse_amount_to_cents("")
    assert cents is None
    assert err == "Amount is required."


def test_parse_amount_dollar_sign_only():
    # A lone "$" has no digits, so it is malformed rather than blank.
    cents, err = parse_amount_to_cents("$")
    assert cents is None
    assert err == INVALID


def test_parse_amount_garbage_text():
    cents, err = parse_amount_to_cents("abc")
    assert cents is None
    assert err == INVALID


def test_parse_amount_garbage_second_decimal_point():
    # A second decimal point does not match the amount pattern.
    cents, err = parse_amount_to_cents("12.34.56")
    assert cents is None
    assert err == INVALID


# --- malformed input is rejected ------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "--5", "-+5", "+-5", "1$2", "$$5", "$-5", "5$",
        "1,2,3", "12,34", "1,234,56", ",123", "123,", "1,,234",
        "1e3", "1E3", "NaN", "nan", "Infinity", "-Infinity", "inf",
        "- 5", "$ 5", "1 234", "-", "+", "$", ".", "-$",
        "12.34.56", "0x10", "１２", "1_000",
    ],
)
def test_parse_amount_rejects_malformed(raw):
    cents, err = parse_amount_to_cents(raw)
    assert cents is None
    assert err == INVALID


@pytest.mark.parametrize("raw", ["1e3", "--5", "1,2,3", "abc"])
def test_parse_amount_rejects_malformed_with_negative_allowed(raw):
    cents, err = parse_amount_to_cents(raw, allow_negative=True)
    assert cents is None
    assert err == INVALID_NEG_OK


def test_parse_amount_message_differs_by_negative_allowed():
    _, plain = parse_amount_to_cents("abc")
    _, neg_ok = parse_amount_to_cents("abc", allow_negative=True)
    assert plain == INVALID
    assert neg_ok == INVALID_NEG_OK
    assert plain != neg_ok


def test_parse_amount_whitespace_only_is_required():
    cents, err = parse_amount_to_cents("   ")
    assert cents is None
    assert err == "Amount is required."


# --- extra accepted formats -----------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected_cents",
    [
        ("+5", 500),
        (".5", 50),
        ("5.", 500),
        (" 12 ", 1200),
        ("1,234,567.89", 123456789),
        ("999999999999.99", 99999999999999),
    ],
)
def test_parse_amount_accepts_extra_formats(raw, expected_cents):
    cents, err = parse_amount_to_cents(raw)
    assert err is None
    assert cents == expected_cents


def test_parse_amount_negative_with_dollar_after_sign():
    cents, err = parse_amount_to_cents("-$1,234.56", allow_negative=True)
    assert err is None
    assert cents == -123456


# --- upper limit -----------------------------------------------------------------

def test_parse_amount_maximum_accepted():
    cents, err = parse_amount_to_cents("999999999999.99")
    assert err is None
    assert cents == 99_999_999_999_999


def test_parse_amount_rounding_over_maximum_rejected():
    cents, err = parse_amount_to_cents("999999999999.995")
    assert cents is None
    assert err == TOO_LARGE


def test_parse_amount_over_maximum_rejected():
    cents, err = parse_amount_to_cents("1000000000000")
    assert cents is None
    assert err == TOO_LARGE


def test_parse_amount_negative_maximum_accepted():
    cents, err = parse_amount_to_cents("-999999999999.99", allow_negative=True)
    assert err is None
    assert cents == -99_999_999_999_999


def test_parse_amount_negative_over_maximum_rejected():
    cents, err = parse_amount_to_cents("-1000000000000", allow_negative=True)
    assert cents is None
    assert err == TOO_LARGE


def test_parse_amount_long_fraction_under_half_cent_has_no_double_rounding():
    cents, err = parse_amount_to_cents("0.00499999999999999999999999999999", allow_zero=True)
    assert err is None
    assert cents == 0


# --- cents_to_decimal_str ---------------------------------------------------

@pytest.mark.parametrize(
    "cents, expected",
    [
        (0, "0.00"),
        (500, "5.00"),
        (-140, "-1.40"),
        (5, "0.05"),
        (-5, "-0.05"),
    ],
)
def test_cents_to_decimal_str(cents, expected):
    assert cents_to_decimal_str(cents) == expected


def test_cents_to_decimal_str_round_trips_with_parse():
    cents, err = parse_amount_to_cents("$1,234.56")
    assert err is None
    assert cents_to_decimal_str(cents) == "1234.56"


def test_cents_to_decimal_str_round_trips_negative():
    cents, err = parse_amount_to_cents("-40.05", allow_negative=True)
    assert err is None
    assert cents_to_decimal_str(cents) == "-40.05"
