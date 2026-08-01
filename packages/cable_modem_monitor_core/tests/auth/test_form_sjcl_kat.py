"""Known-answer tests for SJCL AES-CCM crypto chain.

Validates our Python implementation against pre-computed reference
values derived from the modem's ``sjclCrypto.js`` behavior.  The
fixture file (``sjcl_known_answers.json``) contains vectors that are
independent of our code — they represent what SJCL produces when the
salt is hex-decoded via ``sjcl.codec.hex.toBits()`` before PBKDF2.

If someone changes ``bytes.fromhex(salt)`` back to
``salt.encode("utf-8")``, these tests fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from solentlabs.cable_modem_monitor_core.auth.form_sjcl import _derive_key

from tests._helpers import load_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_KAT = load_fixture(FIXTURES_DIR / "sjcl_known_answers.json")
_VECTORS: list[dict[str, Any]] = _KAT["vectors"]
_REGRESSION_GUARD: dict[str, str] = _KAT["regression_guard"]


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


class TestSjclKeyDerivation:
    """PBKDF2 key derivation must hex-decode the salt, matching SJCL."""

    @pytest.mark.parametrize(
        "vec",
        _VECTORS,
        ids=[v["description"] for v in _VECTORS],
    )
    def test_key_matches_sjcl_reference(self, vec: dict[str, Any]) -> None:
        """Derived key matches the SJCL reference value."""
        key = _derive_key(
            vec["password"],
            vec["salt_hex"],
            vec["iterations"],
            vec["key_length_bits"],
        )
        assert key.hex() == vec["expected_key_hex"]

    @pytest.mark.parametrize(
        "vec",
        _VECTORS,
        ids=[v["description"] for v in _VECTORS],
    )
    def test_key_rejects_utf8_salt(self, vec: dict[str, Any]) -> None:
        """Derived key must NOT match the UTF-8-salt regression value.

        If this test passes with the wrong key, the salt is being
        UTF-8 encoded instead of hex-decoded.
        """
        key = _derive_key(
            vec["password"],
            vec["salt_hex"],
            vec["iterations"],
            vec["key_length_bits"],
        )
        assert key.hex() != _REGRESSION_GUARD["wrong_key_hex"]


# ---------------------------------------------------------------------------
# Encrypt / decrypt
# ---------------------------------------------------------------------------


class TestSjclEncryption:
    """AES-CCM encrypt/decrypt must match SJCL reference ciphertext."""

    @pytest.mark.parametrize(
        "vec",
        _VECTORS,
        ids=[v["description"] for v in _VECTORS],
    )
    def test_encrypt_credentials_matches_reference(self, vec: dict[str, Any]) -> None:
        """Encrypting the credential plaintext produces the expected ciphertext."""
        key = bytes.fromhex(vec["expected_key_hex"])
        iv = bytes.fromhex(vec["iv_hex"])
        cipher = AESCCM(key, tag_length=vec["ccm_tag_length"])

        ciphertext = cipher.encrypt(
            iv,
            vec["plaintext"].encode("utf-8"),
            vec["encrypt_aad"].encode("utf-8"),
        )
        assert ciphertext.hex() == vec["expected_ciphertext_hex"]

    @pytest.mark.parametrize(
        "vec",
        _VECTORS,
        ids=[v["description"] for v in _VECTORS],
    )
    def test_decrypt_nonce_matches_reference(self, vec: dict[str, Any]) -> None:
        """Decrypting the reference nonce ciphertext produces the expected plaintext."""
        key = bytes.fromhex(vec["expected_key_hex"])
        iv = bytes.fromhex(vec["iv_hex"])
        cipher = AESCCM(key, tag_length=vec["ccm_tag_length"])

        plaintext = cipher.decrypt(
            iv,
            bytes.fromhex(vec["expected_nonce_ciphertext_hex"]),
            vec["decrypt_aad"].encode("utf-8"),
        )
        assert plaintext.decode("utf-8") == vec["nonce_plaintext"]
