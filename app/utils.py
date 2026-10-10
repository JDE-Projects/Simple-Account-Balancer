"""Shared pure helper functions."""

import calendar
import datetime
import os
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext


# ---------------------------------------------------------------------------
# Money helpers. All amounts are integer cents in Python and SQLite. Never
# floats. Display formatting ("$1,234.56") happens in the UI, not here.
# ---------------------------------------------------------------------------
# The one accepted amount format, checked against the whole input after
# surrounding spaces are trimmed: an optional + or - sign, an optional $, then
# digits that are either plain (1234) or grouped by commas in threes (1,234),
# then an optional decimal point and fraction digits. At least one digit is
# required. Exponents (1e3), repeated or misplaced signs and dollar signs,
# badly grouped commas, inner spaces, NaN, and infinity all fail the match.
# [0-9] rather than \d, so non-ASCII digits are refused too.
_AMOUNT_RE = re.compile(
    r"(?P<sign>[+-])?\$?(?P<whole>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)?(?:\.(?P<frac>[0-9]*))?"
)

# Largest accepted amount, in cents: $999,999,999,999.99. Far below SQLite's
# integer limit, and low enough that about 90 maximum-size amounts can add up
# in a running balance before passing JavaScript's exact-integer limit
# (Number.MAX_SAFE_INTEGER), which the UI needs to display balances exactly.
MAX_AMOUNT_CENTS = 99_999_999_999_999


def parse_amount_to_cents(raw, *, allow_negative=False, allow_zero=False):
    """Parse a user-entered amount ('1,234.56', '$50', '12', '-40') to cents.

    The input must match _AMOUNT_RE in full. Fractions past the cent round
    half up ('10.999' -> 1100). Returns (cents, None) on success or
    (None, error_message) on failure.
    """
    if allow_negative:
        invalid = "Enter a valid amount, such as 12, 12.34, $1,234.56, or -40."
    else:
        invalid = "Enter a valid amount, such as 12, 12.34, or $1,234.56."
    if raw is None:
        return None, "Amount is required."
    s = str(raw).strip()
    if not s:
        return None, "Amount is required."
    m = _AMOUNT_RE.fullmatch(s)
    if m is None:
        return None, invalid
    whole = (m.group("whole") or "").replace(",", "")
    frac = m.group("frac") or ""
    if not whole and not frac:
        return None, invalid
    neg = m.group("sign") == "-"
    if neg and not allow_negative:
        return None, invalid
    # Enough precision for every digit typed, so a long fraction can never
    # be rounded once by the decimal context and again to the cent.
    with localcontext() as ctx:
        ctx.prec = len(whole) + len(frac) + 4
        try:
            value = Decimal(f"{whole or '0'}.{frac or '0'}")
        except InvalidOperation:
            return None, invalid
        cents = int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if cents > MAX_AMOUNT_CENTS:
        return None, "Amount is too large. The most it can be is $999,999,999,999.99."
    if neg:
        cents = -cents
    if not allow_zero and cents == 0:
        return None, "Amount must be greater than zero."
    return cents, None


def parse_iso_date(raw):
    """Validate a yyyy-mm-dd date string. Returns (date_str, None) or (None, error)."""
    s = (raw or "").strip()
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return None, "Enter a valid date."
    return s, None


def advance_one_month(iso_date: str, anchor_day: int) -> str:
    """Return iso_date advanced by one calendar month, re-anchored to
    anchor_day and clamped to that month's length so a day-31 anchor still
    lands somewhere sensible in short months, e.g. Jan 31 -> Feb 28 -> Mar 31,
    with no drift back toward the 28th. Handles the December -> January
    year rollover."""
    d = datetime.date.fromisoformat(iso_date)
    year = d.year
    month = d.month + 1
    if month > 12:
        month = 1
        year += 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(anchor_day, last_day)
    return datetime.date(year, month, day).isoformat()


def cents_to_decimal_str(cents: int) -> str:
    """Format integer cents as a plain unrounded decimal string for CSV export,
    e.g. -140 -> '-1.40', 500 -> '5.00'. Never uses float, so it never drifts."""
    neg = cents < 0
    cents = abs(cents)
    dollars, rem = divmod(cents, 100)
    s = f"{dollars}.{rem:02d}"
    return f"-{s}" if neg else s


_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def sanitize_filename(name: str) -> str:
    """Strip characters that Windows doesn't allow in file names."""
    cleaned = "".join(c for c in name if c not in _INVALID_FILENAME_CHARS)
    return cleaned.strip()


# Quoted text in a log line, as Python's error messages print file names and
# rejected values. The quote must not follow a letter or digit, so the
# apostrophe in words like "couldn't" never opens a match.
_LOG_QUOTED_RE = re.compile(r"""(?<![\w])(['"])(.*?)\1""")
# An unquoted file path runs to the end of the line: a drive path (C:\ or C:/)
# or a network path (\\server or //server, but not the // in https://).
_LOG_BARE_PATH_RE = re.compile(r"""(?:\b[A-Za-z]:[\\/]|(?<![\w:])[\\/]{2}[^\\/\s]).*""")


def redact_log_text(text: str) -> str:
    """Strip private details from a debug log line. File paths (which carry
    the Windows username, share names, and export file names that contain
    the account name) become <path>; other quoted text inside error messages,
    which can be a value the user typed, becomes <text>. Ids, dates, counts,
    and plain messages are kept."""
    def _quoted(m):
        quote, inner = m.group(1), m.group(2)
        kind = "<path>" if ("\\" in inner or "/" in inner) else "<text>"
        return f"{quote}{kind}{quote}"

    text = _LOG_QUOTED_RE.sub(_quoted, str(text))
    return _LOG_BARE_PATH_RE.sub("<path>", text)


def _prune_failed_message(count: int) -> str:
    """Short enough for the bottom bar; the folder itself is shown in
    Account settings, and the debug log line carries it."""
    noun = "backup" if count == 1 else "backups"
    return f"Couldn't delete {count} old {noun}. The backup folder may be read-only."


def _writable_check(folder: str) -> bool:
    """Try creating and deleting a temp file next to the exe."""
    try:
        test_path = os.path.join(folder, f".wtest_{os.getpid()}.tmp")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(test_path)
        return True
    except Exception:
        return False
