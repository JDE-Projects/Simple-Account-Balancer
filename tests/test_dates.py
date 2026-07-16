"""Tests for parse_iso_date and advance_one_month, including the classic
month-end/anchor-day traps advance_one_month exists to handle."""
import pytest

from simple_account_balancer import parse_iso_date, advance_one_month


# --- parse_iso_date ----------------------------------------------------------

def test_parse_iso_date_valid():
    s, err = parse_iso_date("2024-01-15")
    assert err is None
    assert s == "2024-01-15"


@pytest.mark.parametrize(
    "raw",
    [
        "01/15/2024",   # wrong format
        "2024-02-30",   # impossible day: Feb 30 never exists
        "2024-13-01",   # impossible month
        "",             # empty input
        None,
    ],
)
def test_parse_iso_date_invalid(raw):
    s, err = parse_iso_date(raw)
    assert s is None
    assert err == "Enter a valid date."


# --- advance_one_month --------------------------------------------------------

def test_advance_one_month_jan31_into_leap_february():
    # 2024 is a leap year: Feb has 29 days.
    assert advance_one_month("2024-01-31", 31) == "2024-02-29"


def test_advance_one_month_jan31_into_non_leap_february():
    assert advance_one_month("2023-01-31", 31) == "2023-02-28"


def test_advance_one_month_restores_anchor_after_short_month():
    # From the clamped Feb 29, the next step must jump back to the 31st,
    # not drift and stay anchored to 29 or 28.
    assert advance_one_month("2024-02-29", 31) == "2024-03-31"


def test_advance_one_month_30_day_month_clamped_to_30():
    # April has 30 days; a 31-anchor advancing into it clamps down to 30.
    assert advance_one_month("2024-03-15", 31) == "2024-04-30"


def test_advance_one_month_december_to_january_rollover():
    assert advance_one_month("2023-12-31", 31) == "2024-01-31"
