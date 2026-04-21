#!/usr/bin/env python3
"""
demo_credential_flow.py  (v2 — corrected architecture)
========================================================
Tests the REAL credential flow:

  Infisical          → fetch AES key (ENCRYPTION_KEY_V1)
  EncryptionService  → encrypt a CRM token  →  db_string  (stored in DB)
  EncryptionService  → decrypt db_string    →  plaintext
  CrmCredentialEnvelope → built in memory, never persisted

What this proves before you run the full migration
---------------------------------------------------
  ✅  Infisical Docker container is reachable
  ✅  Machine Identity auth works
  ✅  ACTIVE_KEY_VERSION secret is readable
  ✅  ENCRYPTION_KEY_<V> secret is readable
  ✅  AES encrypt → DB string → decrypt round-trip is lossless
  ✅  CrmCredentialEnvelope builds correctly from decrypted data
  ✅  to_credential_dict() strips 'strategy' correctly (factory-safe)
  ✅  Same flow works for both Zammad and EspoCRM

Run from project root:
    python demo_credential_flow.py

.env variables needed:
    INFISICAL_CLIENT_ID=...
    INFISICAL_CLIENT_SECRET=...
    INFISICAL_PROJECT_ID=...
    INFISICAL_HOST=http://192.168.80.229:6002
    INFISICAL_SECRET_PATH=/app        ← wherever your keys live
    INFISICAL_ENVIRONMENT=development

Infisical secrets required (existing ones you already have):
    ACTIVE_KEY_VERSION   → e.g. v1
    ENCRYPTION_KEY_V1    → your AES key string
"""

from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ .env loaded")
except ImportError:
    print("⚠️  python-dotenv not installed — relying on shell exports")

from app.credentials.models import CrmCredentialEnvelope, InfisicalSettings
from app.credentials.manager import InfisicalCredentialManager
from app.credentials.encryption import EncryptionService, EncryptedPayload
from app.credentials.exceptions import InfisicalConfigError, CredentialNotFoundError


# ---------------------------------------------------------------------------
# Test tokens — these simulate what is already in your DB (unencrypted form)
# ---------------------------------------------------------------------------

ZAMMAD_TOKEN  = "zammad-real-api-token-abc123xyz"
ESPO_TOKEN    = "f177888efa9b2814b150291a24aa7703"
ZAMMAD_URL    = "http://192.168.80.229:3000"
ESPO_URL      = "http://192.168.80.229:9091"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def section(title: str) -> None:
    print(f"\n{'─' * 62}")
    print(f"  {title}")
    print('─' * 62)

def ok(msg: str)   -> None: print(f"  ✅  {msg}")
def info(msg: str) -> None: print(f"  ℹ️   {msg}")
def fail(msg: str) -> None:
    print(f"  ❌  {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def run_demo() -> None:
    print("\n🚀  Infisical + AES Credential Flow Demo")
    print("    (Credentials stay in DB — Infisical holds keys only)\n")

    # ── STEP 1: Settings ──────────────────────────────────────────────────
    section("STEP 1 — Load Infisical settings")
    try:
        settings = InfisicalSettings.from_env()
        ok("Settings loaded")
        info(f"Host        : {settings.host}")
        info(f"Project ID  : {settings.project_id}")
        info(f"Environment : {settings.environment}")
        info(f"Secret path : {settings.secret_path}")
    except InfisicalConfigError as exc:
        fail(str(exc))

    # ── STEP 2: Connect + fetch active key version ────────────────────────
    section("STEP 2 — Connect to Infisical, fetch ACTIVE_KEY_VERSION")
    try:
        key_manager = InfisicalCredentialManager(settings)
        ok("Connected to Infisical")
    except InfisicalConfigError as exc:
        fail(
            f"Cannot connect: {exc}\n\n"
            "    Check:\n"
            "      • Docker container is running (docker ps)\n"
            "      • INFISICAL_HOST is correct\n"
            "      • Machine Identity client_id / client_secret are correct\n"
        )

    try:
        active_version = key_manager.get_active_key_version()
        ok(f"ACTIVE_KEY_VERSION = '{active_version}'")
    except CredentialNotFoundError:
        fail(
            "ACTIVE_KEY_VERSION secret not found in Infisical.\n"
            "    Create it: Infisical → Secrets → /app → Add Secret\n"
            "    Name: ACTIVE_KEY_VERSION   Value: v1"
        )

    # ── STEP 3: Fetch the AES key ─────────────────────────────────────────
    section(f"STEP 3 — Fetch AES key for version='{active_version}'")
    try:
        raw_key = key_manager.get_encryption_key(active_version)
        ok(f"ENCRYPTION_KEY_{active_version.upper()} fetched")
        info(f"Key length  : {len(raw_key)} chars")
        info(f"Key preview : {raw_key[:6]}{'*' * (len(raw_key) - 6)}")
    except CredentialNotFoundError:
        fail(
            f"ENCRYPTION_KEY_{active_version.upper()} not found.\n"
            f"    Create it: Name=ENCRYPTION_KEY_{active_version.upper()}  Value=<your-aes-key>"
        )

    # ── STEP 4: Convenience method (version + key in one call) ───────────
    section("STEP 4 — get_active_key_and_version() convenience check")
    version2, key2 = key_manager.get_active_key_and_version()
    assert version2 == active_version, "Version mismatch between calls"
    assert key2 == raw_key, "Key mismatch between calls"
    ok(f"get_active_key_and_version() → ('{version2}', '<key>')")

    # ── STEP 5: Build EncryptionService ───────────────────────────────────
    section("STEP 5 — Build EncryptionService with the fetched key")
    enc_service = EncryptionService(raw_key=raw_key, key_version=active_version)
    ok(f"EncryptionService ready: {enc_service}")

    # ── STEP 6: Encrypt Zammad token ─────────────────────────────────────
    section("STEP 6 — Encrypt a Zammad API token (simulates DB write)")
    info(f"Plaintext token   : {ZAMMAD_TOKEN}")
    zammad_payload = enc_service.encrypt(ZAMMAD_TOKEN)
    zammad_db_str   = zammad_payload.to_db_string()

    ok("Token encrypted successfully")
    info(f"DB string (stored): {zammad_db_str[:80]}...")

    # Parse and show the JSON structure
    parsed = json.loads(zammad_db_str)
    info(f"  iv           : {parsed['iv'][:20]}...")
    info(f"  data (cipher): {parsed['data'][:20]}...")
    info(f"  algorithm    : {parsed['algorithm']}")
    info(f"  key_version  : {parsed['key_version']}")

    # ── STEP 7: Decrypt back ──────────────────────────────────────────────
    section("STEP 7 — Decrypt the DB string back to plaintext")
    decrypted_zammad = enc_service.decrypt_from_db(zammad_db_str)
    info(f"Decrypted token   : {decrypted_zammad}")
    assert decrypted_zammad == ZAMMAD_TOKEN, \
        f"Round-trip failed: got {decrypted_zammad!r}"
    ok("Round-trip lossless — plaintext matches original exactly")

    # ── STEP 8: Build CrmCredentialEnvelope ──────────────────────────────
    section("STEP 8 — Build CrmCredentialEnvelope for Zammad (in-memory only)")
    zammad_envelope = CrmCredentialEnvelope(
        crm_type="zammad",
        base_url=ZAMMAD_URL,
        credentials={"strategy": "api_token", "token": decrypted_zammad},
        metadata={"key_version": active_version},
    )
    ok("CrmCredentialEnvelope built")
    info(f"crm_type      : {zammad_envelope.crm_type}")
    info(f"base_url      : {zammad_envelope.base_url}")
    info(f"strategy      : {zammad_envelope.credentials['strategy']}")
    info(f"token         : {zammad_envelope.credentials['token']}")

    # ── STEP 9: to_credential_dict() — strategy stripped ─────────────────
    section("STEP 9 — to_credential_dict() — factory-safe injection check")
    clean = zammad_envelope.to_credential_dict()
    info(f"Full dict (envelope): {zammad_envelope.credentials}")
    info(f"Clean dict (client) : {clean}")
    assert "strategy" not in clean, "'strategy' still present in clean dict"
    assert "token" in clean, "'token' missing from clean dict"
    ok("'strategy' stripped — safe to inject into BaseCrmClient")

    # ── STEP 10: Repeat for EspoCRM ───────────────────────────────────────
    section("STEP 10 — Same flow for EspoCRM token")
    info(f"Plaintext token   : {ESPO_TOKEN}")
    espo_payload     = enc_service.encrypt(ESPO_TOKEN)
    espo_db_str      = espo_payload.to_db_string()
    decrypted_espo   = enc_service.decrypt_from_db(espo_db_str)

    assert decrypted_espo == ESPO_TOKEN
    ok("EspoCRM token encrypt → decrypt round-trip OK")

    espo_envelope = CrmCredentialEnvelope(
        crm_type="espocrm",
        base_url=ESPO_URL,
        credentials={"strategy": "api_token", "token": decrypted_espo},
    )
    ok(f"EspoCRM envelope: crm_type={espo_envelope.crm_type}, token={espo_envelope.credentials['token'][:8]}...")

    # ── STEP 11: Different key version produces different ciphertext ──────
    section("STEP 11 — Verify same plaintext produces different ciphertext (random IV)")
    payload_a = enc_service.encrypt(ZAMMAD_TOKEN)
    payload_b = enc_service.encrypt(ZAMMAD_TOKEN)
    assert payload_a.data != payload_b.data, \
        "Two encryptions of the same plaintext produced identical ciphertext — IV not random!"
    ok("Random IV confirmed — each encryption produces unique ciphertext")
    assert enc_service.decrypt(payload_a) == ZAMMAD_TOKEN
    assert enc_service.decrypt(payload_b) == ZAMMAD_TOKEN
    ok("Both ciphertexts decrypt correctly to same plaintext")

    # ── STEP 12: Wrong key fails gracefully ───────────────────────────────
    section("STEP 12 — Wrong key produces clean ValueError (not silent corruption)")
    wrong_service = EncryptionService(raw_key="completely-wrong-key", key_version="v0")
    try:
        wrong_service.decrypt_from_db(zammad_db_str)
        fail("Expected ValueError from wrong key — got nothing")
    except ValueError as exc:
        ok(f"Wrong key raises ValueError: {str(exc)[:60]}")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'═' * 62}")
    print("  🎉  ALL STEPS PASSED")
    print(f"{'═' * 62}")
    print()
    print("  Architecture confirmed:")
    print(f"    Infisical  → ENCRYPTION_KEY_{active_version.upper()} (AES key, ~{len(raw_key)} chars)")
    print(f"    PostgreSQL → credential_enc column stores: iv + ciphertext + version")
    print(f"    In memory  → CrmCredentialEnvelope (never persisted)")
    print()
    print("  DbBackedCredentialService will do this automatically:")
    print("    1. Load CrmIntegration row from DB")
    print("    2. Call key_manager.get_encryption_key(row.key_version)")
    print("    3. Call enc_service.decrypt_from_db(row.credential_enc)")
    print("    4. Return CrmCredentialEnvelope to the factory")
    print()
    print("  Next steps:")
    print("    1. Replace _bootstrap_adapter_factory in main.py")
    print("       (use the version in app/credentials/bootstrap.py)")
    print("    2. Update deps.py to expose credential_service from app.state")
    print("    3. Run: python scripts/migrate_credentials_to_infisical.py --dry-run")
    print()


if __name__ == "__main__":
    run_demo()