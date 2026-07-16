"""Tests for sanitize_filename.

Note: sanitize_filename only strips the Windows-illegal characters; it does
not special-case reserved device names like CON or PRN, so that behavior is
not asserted here (there's nothing in the source to verify against).
"""
from simple_account_balancer import sanitize_filename


def test_sanitize_filename_no_illegal_chars_unchanged():
    assert sanitize_filename("My File.txt") == "My File.txt"


def test_sanitize_filename_strips_all_illegal_chars():
    assert sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"


def test_sanitize_filename_strips_surrounding_whitespace_after_cleaning():
    assert sanitize_filename("  test  ") == "test"


def test_sanitize_filename_all_illegal_chars_yields_empty_string():
    assert sanitize_filename('<>:"/\\|?*') == ""
