#!/usr/bin/env python3
"""
test_db_decryption.py
=====================
Verifies that credentials stored in crm_integrations.credential_enc
can be successfully decrypted using the AES key from Infisical.

Flow
----
  1. Load .env
  2. Connect to Infisical → fetch active AES key
  3. Connect to PostgreSQL → fetch all rows from crm_integrations
  4. For each row: decrypt credential_enc → print plaintext
  5. Report pass / fail per row

Run from project root:
    python test_db_decryption.py

Expected output (per row):
    ✅ integration_id=<uuid>  crm_type=zammad  auth_type=api_token
       decrypted → {"token": "zammad-real-api-token-abc123xyz"}
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Load .env before any app imports ─────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
    print("✅ .env loaded\n")
except ImportError:
    print("⚠️  python-dotenv not installed — relying on shell exports\n")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.settings import get_settings
from app.credentials.models import InfisicalSettings
from app.credentials.manager import InfisicalCredentialManager
from app.credentials.encryption import EncryptionService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def section(title: str) -> None:
    print(f"\n{'─' * 62}")
    print(f"  {title}")
    print('─' * 62)

def ok(msg: str)   -> None: print(f"  ✅  {msg}")
def fail(msg: str) -> None: print(f"  ❌  {msg}")
def info(msg: str) -> None: print(f"  ℹ️   {msg}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> None:
    settings = get_settings()

    # ── STEP 1: Connect to Infisical and fetch AES key ────────────────────
    section("STEP 1 — Connect to Infisical, fetch active AES key")
    try:
        infisical_settings = InfisicalSettings.from_env()
        key_manager = InfisicalCredentialManager(infisical_settings)
        version, raw_key = key_manager.get_active_key_and_version()
        ok(f"Connected to Infisical")
        info(f"key_version : {version}")
        info(f"key_preview : {raw_key[:6]}{'*' * (len(raw_key) - 6)}")
        enc_service = EncryptionService(raw_key=raw_key, key_version=version)
    except Exception as exc:
        fail(f"Infisical connection failed: {exc}")
        sys.exit(1)

    # ── STEP 2: Connect to PostgreSQL ─────────────────────────────────────
    section("STEP 2 — Connect to PostgreSQL")
    try:
        engine = create_async_engine(settings.DATABASE_URL, echo=False)
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        ok("Connected to PostgreSQL")
        info(f"DATABASE_URL : {settings.DATABASE_URL.split('@')[-1]}")  # hide credentials
    except Exception as exc:
        fail(f"DB connection failed: {exc}")
        sys.exit(1)

    # ── STEP 3: Fetch all rows from crm_integrations ──────────────────────
    section("STEP 3 — Fetch rows from crm_integrations")
    try:
        async with async_session() as db:
            result = await db.execute(text("""
                SELECT
                    id,
                    tenant_id,
                    source_system_id,
                    webhook_uuid,
                    auth_type,
                    base_url,
                    credential_enc,
                    webhook_secrets_enc,
                    token_expires_at,
                    is_active,
                    created_at
                FROM crm_integrations
                ORDER BY created_at DESC
            """))
            rows = result.fetchall()
    except Exception as exc:
        fail(f"Query failed: {exc}")
        sys.exit(1)

    if not rows:
        print("\n  ⚠️  No rows found in crm_integrations — nothing to decrypt.\n")
        return

    ok(f"Found {len(rows)} row(s)")

    # ── STEP 4: Decrypt each row ──────────────────────────────────────────
    section(f"STEP 4 — Decrypt credential_enc for each row")

    passed = 0
    failed = 0

    for row in rows:
        integration_id = row.id
        tenant_id      = row.tenant_id
        credential_enc = row.credential_enc
        webhook_secrets_enc = row.webhook_secrets_enc
        is_active      = row.is_active
        auth_type      = row.auth_type or "unknown"
        base_url       = row.base_url or "unknown"
        row_kv         = row.token_expires_at or version  # fallback to active version
        crm_type       = "unknown"  # not stored in DB — set to 'unknown' for display only
        

        # crm_type  = config.get("crm_type", "unknown")
        # auth_type = config.get("auth_type", "unknown")
        # base_url  = config.get("base_url", "unknown")
        # row_kv    = config.get("key_version", version)  # use row's key version if present

        print(f"\n  {'─' * 56}")
        info(f"integration_id : {integration_id}")
        info(f"tenant_id      : {tenant_id}")
        #info(f"crm_type       : {crm_type}")
        info(f"auth_type      : {auth_type}")
        info(f"base_url       : {base_url}")
        info(f"key_version    : {row_kv}")
        info(f"is_active      : {is_active}")

        if not credential_enc:
            fail("credential_enc is NULL — skipping")
            failed += 1
            continue

        # If this row used a different key version, fetch that key
        try:
            if row_kv != version:
                info(f"Row uses different key version ({row_kv}) — fetching from Infisical")
                alt_raw_key = key_manager.get_encryption_key(row_kv)
                row_enc_service = EncryptionService(raw_key=alt_raw_key, key_version=row_kv)
            else:
                row_enc_service = enc_service

            decrypted_json = row_enc_service.decrypt_from_db(credential_enc)
            secret_dict    = json.loads(decrypted_json)

            ok(f"Decrypted successfully")
            print(f"\n  {'─' * 20} PLAINTEXT {'─' * 20}")
            print(f"  {json.dumps(secret_dict, indent=4)}")
            print(f"  {'─' * 50}")
            passed += 1

        except Exception as exc:
            fail(f"Decryption FAILED: {exc}")
            failed += 1

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'═' * 62}")
    print(f"  RESULTS:  {passed} passed  |  {failed} failed  |  {len(rows)} total")
    print(f"{'═' * 62}\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())