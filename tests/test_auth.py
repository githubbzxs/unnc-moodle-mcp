import base64
import hashlib

import pytest

from conftest import BASE, TOKEN


def _callback(passport: str, token: str = TOKEN, site: str = BASE, private: str | None = "p" * 32) -> str:
    raw = f"{hashlib.md5(f'{site}{passport}'.encode()).hexdigest()}:::{token}"
    if private:
        raw += f":::{private}"
    return "moodlemobile://token=" + base64.b64encode(raw.encode()).decode()


def test_decode_app_token_accepts_valid_callback():
    from unnc_moodle_mcp.auth import decode_app_token

    assert decode_app_token(_callback("abc123"), "abc123") == TOKEN
    # 没有 privatetoken 的回调（非首次登录）同样可用
    assert decode_app_token(_callback("abc123", private=None), "abc123") == TOKEN


def test_decode_app_token_rejects_wrong_passport_or_site():
    from unnc_moodle_mcp.auth import LoginError, decode_app_token

    with pytest.raises(LoginError):
        decode_app_token(_callback("other"), "abc123")
    with pytest.raises(LoginError):
        decode_app_token(_callback("abc123", site="https://evil.example"), "abc123")
    with pytest.raises(LoginError):
        decode_app_token("https://moodle.nottingham.ac.uk/", "abc123")


def test_session_roundtrip_is_private(tmp_path, monkeypatch):
    from unnc_moodle_mcp import auth

    monkeypatch.setattr(auth, "STATE_DIR", tmp_path / "s")
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "s" / "token.json")
    s = auth.Session(token=TOKEN, site=BASE, userid=1, username="u", fullname="中文 名", created_at="x")
    auth.save_session(s)
    assert auth.load_session() == s
    assert (tmp_path / "s" / "token.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "s").stat().st_mode & 0o777 == 0o700
    assert auth.clear_session() is True
    assert auth.load_session() is None


def test_session_for_other_site_is_ignored(tmp_path, monkeypatch):
    from unnc_moodle_mcp import auth

    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "token.json")
    auth.save_session(auth.Session(TOKEN, "https://other.example", 1, "u", "n", "x"))
    assert auth.load_session() is None
