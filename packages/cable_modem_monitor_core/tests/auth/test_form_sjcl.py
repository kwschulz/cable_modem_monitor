"""Tests for FormSjclAuthManager."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from solentlabs.cable_modem_monitor_core.auth.form_sjcl import (
    FormSjclAuthManager,
    _derive_key,
    _fetch_page_vars,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import (
    FormSjclAuth,
)

# Pre-computed test values.
# password="password", salt="1122334455667788", iterations=1000,
# key_length=128, iv="aabbccddeeff0011", tag_length=16.
_TEST_SALT = "1122334455667788"
_TEST_IV = "aabbccddeeff0011"
_TEST_SESSION_ID = "test_session_id"
_TEST_CSRF_NONCE = "test_csrf_nonce_12345"
_TEST_ENCRYPTED_NONCE = "1fae830db97a54e264865836515ada26cce31be46ee7f7588205f728d0f9a0163d737ac4aa"


def _make_config(**overrides: Any) -> FormSjclAuth:
    """Build a FormSjclAuth config with defaults."""
    defaults: dict[str, Any] = {
        "strategy": "form_sjcl",
        "login_endpoint": "/php/ajaxSet_Password.php",
        "login_page": "/",
        "session_validation_endpoint": "/php/ajaxSet_Session.php",
        "pbkdf2_iterations": 1000,
        "pbkdf2_key_length": 128,
        "ccm_tag_length": 16,
        "encrypt_aad": "loginPassword",
        "decrypt_aad": "nonce",
        "csrf_header": "csrfNonce",
    }
    defaults.update(overrides)
    return FormSjclAuth.model_validate(defaults)


def _login_page_html(
    iv: str = _TEST_IV,
    salt: str = _TEST_SALT,
    session_id: str = _TEST_SESSION_ID,
) -> str:
    """Build a minimal login page with JS variables."""
    return (
        f"<html><script>"
        f"var myIv = '{iv}';\n"
        f"var mySalt = '{salt}';\n"
        f"currentSessionId = '{session_id}';\n"
        f"</script></html>"
    )


class TestDeriveKey:
    """PBKDF2 key derivation utility."""

    def test_basic_derivation(self) -> None:
        """Derives raw bytes from password and hex-encoded salt."""
        result = _derive_key("password", _TEST_SALT, 1000, 128)
        assert len(result) == 16  # 128 bits = 16 bytes
        assert isinstance(result, bytes)

    def test_deterministic(self) -> None:
        """Same inputs produce same output."""
        a = _derive_key("password", _TEST_SALT, 1000, 128)
        b = _derive_key("password", _TEST_SALT, 1000, 128)
        assert a == b

    def test_matches_hashlib(self) -> None:
        """Matches hashlib.pbkdf2_hmac with hex-decoded salt."""
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            b"pass",
            bytes.fromhex(_TEST_SALT),
            1000,
            dklen=16,
        )
        result = _derive_key("pass", _TEST_SALT, 1000, 128)
        assert result == expected


class TestAesCcm:
    """AES-CCM encrypt/decrypt round-trip using cryptography directly."""

    def test_round_trip(self) -> None:
        """Encrypt then decrypt returns original plaintext."""
        key = _derive_key("password", _TEST_SALT, 1000, 128)
        iv = bytes.fromhex(_TEST_IV)
        plaintext = b"hello world"
        aad = b"test"

        cipher = AESCCM(key, tag_length=16)
        encrypted = cipher.encrypt(iv, plaintext, aad)
        decrypted = cipher.decrypt(iv, encrypted, aad)
        assert decrypted == plaintext

    def test_wrong_aad_fails(self) -> None:
        """Decrypt with wrong AAD raises InvalidTag."""
        key = _derive_key("password", _TEST_SALT, 1000, 128)
        iv = bytes.fromhex(_TEST_IV)

        cipher = AESCCM(key, tag_length=16)
        encrypted = cipher.encrypt(iv, b"data", b"correct_aad")

        with pytest.raises(InvalidTag):
            cipher.decrypt(iv, encrypted, b"wrong_aad")

    def test_wrong_key_fails(self) -> None:
        """Decrypt with wrong key raises InvalidTag."""
        key1 = _derive_key("password1", _TEST_SALT, 1000, 128)
        key2 = _derive_key("password2", _TEST_SALT, 1000, 128)
        iv = bytes.fromhex(_TEST_IV)

        cipher1 = AESCCM(key1, tag_length=16)
        encrypted = cipher1.encrypt(iv, b"secret", b"aad")

        cipher2 = AESCCM(key2, tag_length=16)
        with pytest.raises(InvalidTag):
            cipher2.decrypt(iv, encrypted, b"aad")


def _page_response(html: str) -> MagicMock:
    """Login-page GET mock carrying a real status, which _fetch_page_vars reads."""
    # A bare MagicMock's status_code is a MagicMock, and how that fails
    # depends on the comparison. Ordering raises, so `>= 400` is a
    # TypeError. Equality and truthiness answer silently and wrongly
    # instead; `!= 200` is True, `== 200` is False, `bool()` is True, and
    # `%d` renders it as 1. A guard written as an ordering comparison is
    # therefore the one a mock cannot quietly satisfy, and every response
    # mock here carries the shape the code under test branches on.
    resp = MagicMock()
    resp.status_code = 200
    resp.text = html
    return resp


class TestFetchPageVars:
    """JS variable extraction from login page."""

    def test_extracts_variables(self) -> None:
        """Extracts myIv, mySalt, currentSessionId from page."""
        session = requests.Session()
        resp = _page_response(_login_page_html())
        with patch.object(session, "get", return_value=resp):
            result = _fetch_page_vars(session, "http://modem/", 10)

        assert isinstance(result, dict)
        assert result["myIv"] == _TEST_IV
        assert result["mySalt"] == _TEST_SALT
        assert result["currentSessionId"] == _TEST_SESSION_ID

    def test_network_error_propagates(self) -> None:
        """ConnectionError propagates for collector to classify as CONNECTIVITY."""
        session = requests.Session()
        with (
            patch.object(session, "get", side_effect=requests.ConnectionError("refused")),
            pytest.raises(requests.ConnectionError),
        ):
            _fetch_page_vars(session, "http://modem/", 10)


class TestFormSjclAuthManager:
    """FormSjclAuthManager AES-CCM encrypted form auth."""

    def test_successful_login(self, session: requests.Session) -> None:
        """Full SJCL login flow succeeds with CSRF nonce extraction."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "AdminMatch",
            "encryptData": _TEST_ENCRYPTED_NONCE,
        }

        session_resp = MagicMock()
        session_resp.status_code = 200

        with patch.object(session, "get", return_value=page_resp), patch.object(session, "post") as mock_post:
            mock_post.side_effect = [login_resp, session_resp]

            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is True
        assert session.headers.get("csrfNonce") == _TEST_CSRF_NONCE
        assert mock_post.call_count == 2
        # Session validation POST sends empty body (no json= kwarg).
        session_call = mock_post.call_args_list[1]
        assert "json" not in session_call.kwargs

    def test_login_rejected(self, session: requests.Session) -> None:
        """Reports error when p_status is not AdminMatch."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "Lockout",
            "encryptData": "",
        }

        with (
            patch.object(session, "get", return_value=page_resp),
            patch.object(session, "post", return_value=login_resp),
        ):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "wrong")

        assert result.success is False
        assert "Lockout" in result.error

    def test_missing_iv_variable(self, session: requests.Session) -> None:
        """Reports error when login page is missing myIv."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response("<html><script>var mySalt = 'abc';</script></html>")

        with patch.object(session, "get", return_value=page_resp):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert "myIv" in result.error

    # ┌──────────────────┬───────────────────────────────────────┐
    # │ iv               │ expected error substring               │
    # ├──────────────────┼───────────────────────────────────────┤
    # │ "zzzz"           │ "not valid hex"                       │
    # │ "aabb"           │ "2 bytes" (below 7-byte minimum)      │
    # └──────────────────┴───────────────────────────────────────┘
    #
    # fmt: off
    IV_ERROR_CASES = [
        # (iv_value,  expected_substr, description)
        ("zzzz",      "not valid hex",  "non-hex IV"),
        ("aabb",      "2 bytes",        "IV too short"),
    ]
    # fmt: on

    @pytest.mark.parametrize(
        "iv_value,expected_substr,desc",
        IV_ERROR_CASES,
        ids=[c[2] for c in IV_ERROR_CASES],
    )
    def test_iv_validation_error(
        self,
        session: requests.Session,
        iv_value: str,
        expected_substr: str,
        desc: str,
    ) -> None:
        """Reports error when login page IV fails validation."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html(iv=iv_value))

        with patch.object(session, "get", return_value=page_resp):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert expected_substr in result.error

    def test_cryptography_import_missing(self, session: requests.Session) -> None:
        """Reports error when cryptography package is not installed."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        with patch.dict(
            "sys.modules",
            {"cryptography.hazmat.primitives.ciphers.aead": None},
        ):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert "cryptography" in result.error

    def test_page_fetch_request_error(self, session: requests.Session) -> None:
        """Non-connectivity RequestException on page fetch returns AuthResult."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        with patch.object(session, "get", side_effect=requests.TooManyRedirects("loop")):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert "Login page fetch failed" in result.error

    def test_encrypt_data_not_hex(self, session: requests.Session) -> None:
        """Reports error when encryptData in login response is not valid hex."""
        config = _make_config(session_validation_endpoint="")
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "AdminMatch",
            "encryptData": "not-valid-hex!",
        }

        with (
            patch.object(session, "get", return_value=page_resp),
            patch.object(session, "post", return_value=login_resp),
        ):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert "not valid hex" in result.error

    def test_page_fetch_connection_error_propagates(self, session: requests.Session) -> None:
        """ConnectionError on login page fetch propagates for collector."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        with (
            patch.object(session, "get", side_effect=requests.ConnectionError("refused")),
            pytest.raises(requests.ConnectionError),
        ):
            manager.authenticate(session, "http://192.168.0.1", "admin", "password")

    def test_login_post_connection_error_propagates(self, session: requests.Session) -> None:
        """ConnectionError on login POST propagates for collector."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        with (
            patch.object(
                session,
                "request",
                side_effect=[page_resp, requests.ConnectionError("lost")],
            ),
            pytest.raises(requests.ConnectionError),
        ):
            manager.authenticate(session, "http://192.168.0.1", "admin", "password")

    def test_login_response_not_json(self, session: requests.Session) -> None:
        """Reports error when login response is not JSON."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.side_effect = ValueError("not json")

        with (
            patch.object(session, "get", return_value=page_resp),
            patch.object(session, "post", return_value=login_resp),
        ):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert "json" in result.error.lower()

    def test_double_encoded_json_response(self, session: requests.Session) -> None:
        """Handles double-encoded JSON where resp.json() returns a string."""
        config = _make_config(session_validation_endpoint="")
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        # Modem returns a JSON string containing a serialised JSON object.
        inner = json.dumps({"p_status": "AdminMatch", "encryptData": _TEST_ENCRYPTED_NONCE})
        login_resp.json.return_value = inner

        with (
            patch.object(session, "get", return_value=page_resp),
            patch.object(session, "post", return_value=login_resp),
        ):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is True
        assert session.headers.get("csrfNonce") == _TEST_CSRF_NONCE

    # ┌────────────────────┬─────────────────┬─────────────────────────────────┐
    # │ json_value         │ expected_preview│ description                     │
    # ├────────────────────┼─────────────────┼─────────────────────────────────┤
    # │ "not a dict"       │ "not a dict"    │ string response                 │
    # │ [1, 2, 3]          │ "[1, 2, 3]"     │ list response                   │
    # │ 42                 │ "42"            │ integer response                │
    # └────────────────────┴─────────────────┴─────────────────────────────────┘
    #
    # fmt: off
    JSON_NOT_DICT_CASES = [
        # (json_value,    expected_preview, description)
        ("not a dict",    "not a dict",     "string response"),
        ([1, 2, 3],       "[1, 2, 3]",      "list response"),
        (42,              "42",             "integer response"),
    ]
    # fmt: on

    @pytest.mark.parametrize(
        "json_value,expected_preview,desc",
        JSON_NOT_DICT_CASES,
        ids=[c[2] for c in JSON_NOT_DICT_CASES],
    )
    def test_json_response_not_dict(
        self,
        session: requests.Session,
        json_value: object,
        expected_preview: str,
        desc: str,
    ) -> None:
        """Reports error when JSON response is a non-dict type."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = json_value

        with (
            patch.object(session, "get", return_value=page_resp),
            patch.object(session, "post", return_value=login_resp),
        ):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert "expected json object" in result.error.lower()
        assert expected_preview in result.error

    def test_nonce_decryption_failure(self, session: requests.Session) -> None:
        """Reports error when response decryption fails."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "AdminMatch",
            "encryptData": "deadbeef" * 4,  # garbage ciphertext
        }

        with (
            patch.object(session, "get", return_value=page_resp),
            patch.object(session, "post", return_value=login_resp),
        ):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert "decryption failed" in result.error.lower()

    def test_no_session_validation(self, session: requests.Session) -> None:
        """Succeeds without session validation when endpoint is empty."""
        config = _make_config(session_validation_endpoint="")
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "AdminMatch",
            "encryptData": _TEST_ENCRYPTED_NONCE,
        }

        with (
            patch.object(session, "get", return_value=page_resp),
            patch.object(session, "post", return_value=login_resp),
        ):
            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is True
        assert session.headers.get("csrfNonce") == _TEST_CSRF_NONCE

    def test_session_validation_empty_body(self, session: requests.Session) -> None:
        """Succeeds when session validation returns 200 with empty body."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "AdminMatch",
            "encryptData": _TEST_ENCRYPTED_NONCE,
        }

        session_resp = MagicMock()
        session_resp.status_code = 200

        with patch.object(session, "get", return_value=page_resp), patch.object(session, "post") as mock_post:
            mock_post.side_effect = [login_resp, session_resp]

            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is True

    def test_session_validation_non_200(self, session: requests.Session) -> None:
        """Reports error when session validation returns non-200 status."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "AdminMatch",
            "encryptData": _TEST_ENCRYPTED_NONCE,
        }

        session_resp = MagicMock()
        session_resp.status_code = 403

        with patch.object(session, "get", return_value=page_resp), patch.object(session, "post") as mock_post:
            mock_post.side_effect = [login_resp, session_resp]

            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert "403" in result.error
        assert result.response is session_resp

    def test_session_validation_connection_error(self, session: requests.Session) -> None:
        """ConnectionError on session validation propagates for collector."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "AdminMatch",
            "encryptData": _TEST_ENCRYPTED_NONCE,
        }

        with patch.object(session, "get", return_value=page_resp), patch.object(session, "post") as mock_post:
            mock_post.side_effect = [login_resp, requests.ConnectionError("lost")]

            with pytest.raises(requests.ConnectionError):
                manager.authenticate(session, "http://192.168.0.1", "admin", "password")

    def test_session_validation_request_error(self, session: requests.Session) -> None:
        """Non-connectivity RequestException on session validation returns AuthResult."""
        config = _make_config()
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "AdminMatch",
            "encryptData": _TEST_ENCRYPTED_NONCE,
        }

        with patch.object(session, "get", return_value=page_resp), patch.object(session, "post") as mock_post:
            mock_post.side_effect = [login_resp, requests.TooManyRedirects("too many")]

            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is False
        assert "Session validation POST failed" in result.error

    def test_no_csrf_header(self, session: requests.Session) -> None:
        """Succeeds without decrypting nonce when csrf_header is empty."""
        config = _make_config(csrf_header="")
        manager = FormSjclAuthManager(config)

        page_resp = _page_response(_login_page_html())

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "p_status": "AdminMatch",
            "encryptData": "anything",
        }

        session_resp = MagicMock()
        session_resp.status_code = 200

        with patch.object(session, "get", return_value=page_resp), patch.object(session, "post") as mock_post:
            mock_post.side_effect = [login_resp, session_resp]

            result = manager.authenticate(session, "http://192.168.0.1", "admin", "password")

        assert result.success is True
