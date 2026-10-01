"""Tests for the open_url bridge: only the JDE-Projects website may be opened
in the system browser. webbrowser.open is replaced, so nothing is launched."""
import webbrowser

import pytest

import simple_account_balancer as sab


ALLOWED = [
    "https://jde-projects.com",
    "https://jde-projects.com/",
    "https://jde-projects.com/support/#commercial-licensing",
]

REFUSED = [
    "http://jde-projects.com",
    "https://evil.example",
    "https://jde-projects.com.evil.example",
    "https://evil.example/jde-projects.com",
    "https://jde-projects.com@evil.example",
    "https://user@jde-projects.com",
    "https://jde-projects.com:8443/",
    "https://sub.jde-projects.com",
    "file:///C:/Windows/System32/calc.exe",
    "javascript:alert(1)",
    "jde-projects.com",
    "https://jde-projects.com/\nhttps://evil.example",
    " https://jde-projects.com",
    "",
    None,
    123,
]


@pytest.mark.parametrize("url", ALLOWED)
def test_is_allowed_url_accepts_site(url):
    assert sab._is_allowed_url(url) is True


@pytest.mark.parametrize("url", REFUSED)
def test_is_allowed_url_refuses_everything_else(url):
    assert sab._is_allowed_url(url) is False


def test_open_url_opens_allowed_site(monkeypatch):
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert sab.Api().open_url("https://jde-projects.com")["ok"] is True
    assert opened == ["https://jde-projects.com"]


def test_open_url_refuses_other_site_without_opening(monkeypatch):
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    result = sab.Api().open_url("https://evil.example")
    assert result["ok"] is False
    assert "error" in result
    assert opened == []
