"""Safety-net contract tests for the Python-to-HTML bridge."""

import inspect
import re
from pathlib import Path

from app.api import Api


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = REPOSITORY_ROOT / "simple_account_balancer-UI.html"
PUBLIC_API_SIGNATURES = [
    ("add_autopay", "(self, account_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable=0)"),
    ("add_category", "(self, name)"),
    ("add_transaction", "(self, account_id, date, payee, category, notes, amount, direction)"),
    ("check_update", "(self)"),
    ("choose_backup_folder", "(self)"),
    ("close_conn", "(self)"),
    ("confirm_estimated_amount", "(self, transaction_id, amount)"),
    ("create_account", "(self, name, starting_balance, starting_date)"),
    ("delete_account", "(self, account_id)"),
    ("delete_autopay", "(self, autopay_id)"),
    ("delete_category", "(self, category_id, reassign_to=None)"),
    ("delete_transaction", "(self, transaction_id)"),
    ("export_csv", "(self, account_id, from_date, to_date, range_label='')"),
    ("find_compare_matches", "(self, account_id=None, amount=None, from_date=None, to_date=None)"),
    ("get_autopays", "(self, account_id)"),
    ("get_categories", "(self)"),
    ("get_compare_data", "(self, account_id=None, from_date=None, to_date=None)"),
    ("get_config", "(self)"),
    ("get_payees", "(self, account_id=None)"),
    ("get_transactions", "(self, account_id=None, from_date=None, to_date=None, search='')"),
    ("list_backups", "(self)"),
    ("log", "(self, msg: str)"),
    ("open_url", "(self, url: str)"),
    ("post_due_autopays", "(self)"),
    ("preview_restore", "(self, filename)"),
    ("rename_category", "(self, category_id, new_name)"),
    ("reorder_transactions", "(self, account_id, date, ordered_ids)"),
    ("reset_backup_folder", "(self)"),
    ("restore_backup", "(self, filename, fingerprint=None)"),
    ("save_theme", "(self, theme: str)"),
    ("set_active_account", "(self, account_id)"),
    ("set_backup_keep", "(self, n)"),
    ("set_conn", "(self, conn: sqlite3.Connection)"),
    ("set_db_path", "(self, path: str)"),
    ("set_debug", "(self, on: bool)"),
    ("set_window", "(self, w)"),
    ("update_account", "(self, account_id, name, starting_balance, starting_date)"),
    ("update_autopay", "(self, autopay_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable=0)"),
    ("update_transaction", "(self, transaction_id, date, payee, category, notes, amount, direction)"),
]
PUBLIC_API_ATTRIBUTES = ["autopay_notice", "autopay_notice_is_error", "backup_notice"]
BACKEND_CALL_RE = re.compile(
    r"(?:api\(\)|window\s*\.\s*pywebview\s*\.\s*api)\s*\.\s*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\("
)


def _public_api_methods():
    return {
        name: member
        for name, member in inspect.getmembers(Api)
        if not name.startswith("_") and callable(member)
    }


def _call_end(source: str, opening_parenthesis: int) -> int | None:
    """Find a call's closing parenthesis while respecting quoted strings."""
    depth = 0
    quote = None
    escaped = False
    for index in range(opening_parenthesis, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _argument_count(arguments: str) -> int | None:
    """Count simple JavaScript positional arguments, or return None if unsure."""
    if "\n" in arguments or "..." in arguments:
        return None
    if not arguments.strip():
        return 0

    depth = 0
    quote = None
    escaped = False
    count = 1
    for char in arguments:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count if depth == 0 and quote is None else None


def _html_backend_calls():
    source = HTML_PATH.read_text(encoding="utf-8")
    for match in BACKEND_CALL_RE.finditer(source):
        closing_parenthesis = _call_end(source, match.end() - 1)
        line = source.count("\n", 0, match.start()) + 1
        if closing_parenthesis is None:
            yield match.group("name"), None, line
            continue
        arguments = source[match.end() : closing_parenthesis]
        yield match.group("name"), _argument_count(arguments), line


def test_public_api_method_signatures_are_unchanged():
    actual = [(name, str(inspect.signature(method))) for name, method in _public_api_methods().items()]
    assert actual == PUBLIC_API_SIGNATURES


def test_fresh_api_public_attributes_are_unchanged():
    actual = sorted(
        name for name, value in vars(Api("test")).items() if not name.startswith("_") and not callable(value)
    )
    assert actual == PUBLIC_API_ATTRIBUTES


def test_html_backend_calls_match_public_api_signatures():
    methods = _public_api_methods()
    calls = list(_html_backend_calls())
    assert calls, "found no backend calls in the HTML; the call pattern no longer matches"
    for name, argument_count, line in calls:
        assert name in methods, f"HTML line {line} calls unknown Api method {name!r}"
        assert argument_count is not None, (
            f"HTML line {line}: arguments to Api.{name} could not be counted; "
            "extend _argument_count before relying on this check"
        )
        arguments = [object()] * argument_count
        try:
            inspect.signature(methods[name]).bind(object(), *arguments)
        except TypeError as exc:
            raise AssertionError(
                f"HTML line {line} calls Api.{name} with {argument_count} argument(s): {exc}"
            ) from exc


def test_html_reaches_backend_only_through_checked_forms():
    """Every bridge reference is a call the scan above checks, or a presence check.

    Allowed: the helper's definition, `!api()`, `if (api())`, and
    `api().name(...)`. Anything else (an alias, bracket access) would hide
    calls from the scan, so it fails here.
    """
    source = HTML_PATH.read_text(encoding="utf-8")
    assert source.count("pywebview.api") == 1, "only the api() helper may reference pywebview.api"
    for match in re.finditer(r"\bapi\(\)", source):
        before = source[: match.start()]
        rest = source[match.end():]
        line = source.count("\n", 0, match.start()) + 1
        is_definition = before.endswith("function ")
        is_negated_check = before.endswith("!")
        is_if_check = re.search(r"\bif\s*\(\s*$", before) is not None and rest.startswith(")")
        is_call = re.match(r"\s*\.\s*[A-Za-z_$][\w$]*\s*\(", rest) is not None
        assert is_definition or is_negated_check or is_if_check or is_call, (
            f"HTML line {line} uses api() in a form the call scan does not check"
        )
