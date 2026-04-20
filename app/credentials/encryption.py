"""
app/credentials/encryption.py
==============================
AES-256-CBC encryption / decryption service.

This is the exact algorithm used in the POC (main.py), promoted to a
production-grade service:

  ENCRYPT
  -------
  1. Derive a 32-byte AES key: SHA-256(raw_key_string).
  2. Generate a random 16-byte IV via secrets.token_bytes().
  3. PKCS7-pad the plaintext, run AES-256-CBC, base64-encode iv + ciphertext.
  4. Return an ``EncryptedPayload`` (iv, data, algorithm, key_version).
  5. Call ``payload.to_db_string()`` to get the JSON string stored in the DB.

  DECRYPT
  -------
  1. Parse the DB JSON string back into an ``EncryptedPayload``.
  2. Derive the same AES key from the raw Infisical secret value.
  3. Reverse: base64-decode → AES-256-CBC decrypt → PKCS7-unpad → UTF-8 decode.

Usage
-----
    service = EncryptionService(raw_key="..from infisical..", key_version="v1")

    # Encrypt a string (e.g. an API token)
    payload = service.encrypt("secret-api-token-abc123")
    db_value = payload.to_db_string()          # store this TEXT in postgres

    # Later: decrypt
    payload  = EncryptedPayload.from_db_string(db_value)
    token    = service.decrypt(payload)        # "secret-api-token-abc123"

    # Encrypt a dict (e.g. webhook_secrets JSON map)
    payload  = service.encrypt_dict({"Case.create": "s1", "Case.update": "s2"})
    db_value = payload.to_db_string()

    # Decrypt back to dict
    data     = service.decrypt_dict(EncryptedPayload.from_db_string(db_value))
"""

from __future__ import annotations

import base64
import json
import secrets as secrets_module
from typing import Any, Dict

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# EncryptedPayload — the JSON blob stored in every _enc column
# ---------------------------------------------------------------------------

class EncryptedPayload(BaseModel):
    """
    Serialisable envelope stored as TEXT in each ``_enc`` DB column.

    Fields
    ------
    iv:
        Base64-encoded 16-byte initialisation vector (fresh per encrypt call).
    data:
        Base64-encoded AES-256-CBC ciphertext.
    algorithm:
        Always ``"AES-256-CBC"``.  Present for future-proofing / auditing.
    key_version:
        Matches ``crm_integrations.key_version``.  Used during key rotation
        to select the correct Infisical secret (``ENCRYPTION_KEY_<version>``).
    """

    iv: str = Field(..., description="Base64-encoded 16-byte IV.")
    data: str = Field(..., description="Base64-encoded ciphertext.")
    algorithm: str = Field(default="AES-256-CBC")
    key_version: str = Field(..., description="e.g. 'v1', 'v2'.")

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_db_string(self) -> str:
        """
        Serialise to a compact JSON string suitable for storing in TEXT columns.
        This is what you write to ``credential_enc`` / ``webhook_secret_enc`` etc.
        """
        return self.model_dump_json()

    @classmethod
    def from_db_string(cls, raw: str) -> "EncryptedPayload":
        """
        Deserialise a JSON string retrieved from a TEXT DB column.

        Raises
        ------
        ValueError
            If the string is not valid JSON or fails Pydantic validation.
        """
        try:
            return cls.model_validate_json(raw)
        except Exception as exc:
            raise ValueError(
                f"Cannot parse EncryptedPayload from DB string: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# EncryptionService
# ---------------------------------------------------------------------------

class EncryptionService:
    """
    Stateless AES-256-CBC encryption / decryption bound to one key version.

    Parameters
    ----------
    raw_key:
        The plaintext secret value fetched from Infisical
        (``ENCRYPTION_KEY_V1``, ``ENCRYPTION_KEY_V2``, …).
        SHA-256 is applied internally to produce a 32-byte AES key.
    key_version:
        Version tag stored alongside every ciphertext so the correct key can
        be selected during rotation (e.g. ``"v1"``).

    Thread-safety
    -------------
    All methods are pure-functional (no mutable state after __init__).
    Safe to share a single instance across threads / async tasks.
    """

    ALGORITHM = "AES-256-CBC"
    _BLOCK_BITS = 128  # AES block size in bits (always 128)

    def __init__(self, raw_key: str, key_version: str) -> None:
        if not raw_key:
            raise ValueError("raw_key must be a non-empty string.")
        if not key_version:
            raise ValueError("key_version must be a non-empty string.")

        self._key_version = key_version
        self._aes_key: bytes = self._derive_key(raw_key)

    # ------------------------------------------------------------------
    # Key derivation — identical to POC
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_key(raw_key: str) -> bytes:
        """SHA-256(raw_key_string) → 32-byte AES key."""
        digest = hashes.Hash(hashes.SHA256(), backend=default_backend())
        digest.update(raw_key.encode("utf-8"))
        return digest.finalize()

    # ------------------------------------------------------------------
    # Public API: strings
    # ------------------------------------------------------------------

    def encrypt(self, plaintext: str) -> EncryptedPayload:
        """
        Encrypt a UTF-8 string and return an ``EncryptedPayload``.

        Parameters
        ----------
        plaintext:
            Any non-empty string (API token, bearer token, password, …).

        Returns
        -------
        EncryptedPayload
            Call ``.to_db_string()`` to get the TEXT value for the DB column.

        Raises
        ------
        ValueError
            If *plaintext* is empty.
        """
        if not plaintext:
            raise ValueError("plaintext must be a non-empty string.")

        iv = secrets_module.token_bytes(16)

        # PKCS7 pad to AES block boundary
        padder = padding.PKCS7(self._BLOCK_BITS).padder()
        padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

        # AES-256-CBC encrypt
        cipher = Cipher(
            algorithms.AES(self._aes_key),
            modes.CBC(iv),
            backend=default_backend(),
        )
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()

        return EncryptedPayload(
            iv=base64.b64encode(iv).decode("utf-8"),
            data=base64.b64encode(ciphertext).decode("utf-8"),
            algorithm=self.ALGORITHM,
            key_version=self._key_version,
        )

    def decrypt(self, payload: EncryptedPayload) -> str:
        """
        Decrypt an ``EncryptedPayload`` back to a UTF-8 string.

        Parameters
        ----------
        payload:
            Produced by ``encrypt()`` or ``EncryptedPayload.from_db_string()``.

        Returns
        -------
        str
            The original plaintext.

        Raises
        ------
        ValueError
            On base64 decode error, padding error, or UTF-8 decode error.
        """
        try:
            iv_bytes = base64.b64decode(payload.iv)
            ciphertext = base64.b64decode(payload.data)
        except Exception as exc:
            raise ValueError(f"Base64 decode failed: {exc}") from exc

        cipher = Cipher(
            algorithms.AES(self._aes_key),
            modes.CBC(iv_bytes),
            backend=default_backend(),
        )
        decryptor = cipher.decryptor()
        padded_plain = decryptor.update(ciphertext) + decryptor.finalize()

        try:
            unpadder = padding.PKCS7(self._BLOCK_BITS).unpadder()
            plain_bytes = unpadder.update(padded_plain) + unpadder.finalize()
        except Exception as exc:
            raise ValueError(
                f"PKCS7 unpad failed — wrong key or corrupted data: {exc}"
            ) from exc

        return plain_bytes.decode("utf-8")

    # ------------------------------------------------------------------
    # Public API: dicts  (webhook_secrets JSON map)
    # ------------------------------------------------------------------

    def encrypt_dict(self, data: Dict[str, Any]) -> EncryptedPayload:
        """
        Serialise *data* to a compact JSON string, then encrypt it.

        Use this for ``webhook_secrets_enc`` which stores a mapping of
        CRM event names to per-event HMAC secrets.

        Example input: ``{"Case.create": "s1", "Case.update": "s2"}``
        """
        return self.encrypt(json.dumps(data, separators=(",", ":")))

    def decrypt_dict(self, payload: EncryptedPayload) -> Dict[str, Any]:
        """
        Decrypt an ``EncryptedPayload`` and deserialise the JSON back to a dict.

        Raises
        ------
        ValueError
            If decryption succeeds but the result is not valid JSON.
        """
        raw = self.decrypt(payload)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Decrypted value is not valid JSON: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Convenience: decrypt straight from a DB TEXT column value
    # ------------------------------------------------------------------

    def decrypt_from_db(self, db_value: str) -> str:
        """
        Parse a TEXT column value and decrypt in one call.

        Equivalent to:
            ``service.decrypt(EncryptedPayload.from_db_string(db_value))``
        """
        return self.decrypt(EncryptedPayload.from_db_string(db_value))

    def decrypt_dict_from_db(self, db_value: str) -> Dict[str, Any]:
        """
        Parse a TEXT column value and decrypt to dict in one call.

        Equivalent to:
            ``service.decrypt_dict(EncryptedPayload.from_db_string(db_value))``
        """
        return self.decrypt_dict(EncryptedPayload.from_db_string(db_value))

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # never expose key bytes
        return f"<EncryptionService key_version={self._key_version!r}>"