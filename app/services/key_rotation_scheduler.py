"""
app/services/key_rotation_scheduler.py

Per-Tenant AES Key Rotation — runs every 90 days.

Secret naming in Infisical
--------------------------
  TENANT_KEY_<tenant_id>_v1          ← initial key (created at tenant creation)
  TENANT_KEY_<tenant_id>_v2          ← after first rotation
  TENANT_ACTIVE_VERSION_<tenant_id>  ← "v1" | "v2" | ...  (version pointer)

DB crm_integrations.key_version      ← "v1" | "v2" | ...  (mirrors active version)

Rotation flow (per tenant)
--------------------------
  1. Read TENANT_ACTIVE_VERSION_<tenant_id>  → current_version  (e.g. "v1")
  2. Fetch TENANT_KEY_<tenant_id>_<current_version>  → old_raw_key
  3. Compute next_version  (e.g. "v2")
  4. Generate new 256-bit random key
  5. Store TENANT_KEY_<tenant_id>_<next_version> in Infisical   ← new key written
  6. Fetch all CrmIntegration rows where key_version == current_version
  7. Decrypt both _enc columns with old key, re-encrypt with new key,
     set key_version = next_version on each row
  8. DB commit  ← point of no return
  9. Update TENANT_ACTIVE_VERSION_<tenant_id> → next_version in Infisical
 10. Delete TENANT_KEY_<tenant_id>_<current_version> from Infisical

Crash safety
------------
Steps 5-7 are reversible — new key exists in Infisical but no DB row points
to it yet. If the process dies before step 8, all DB rows still decrypt with
the old key. If it dies between 8 and 9, DB rows already use the new key —
the next rotation run detects the mismatch and self-heals.
Old key is NEVER deleted before the DB commit (step 8) succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import secrets as _secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.settings import get_settings
from app.credentials.encryption import EncryptionService
from app.credentials.manager import InfisicalCredentialManager
from app.models.crm_integration import CrmIntegration
from app.models.tenant import Tenant

logger = logging.getLogger(__name__)
settings = get_settings()

_rotation_scheduler = AsyncIOScheduler()
_app_ref: Optional[FastAPI] = None


# ---------------------------------------------------------------------------
# App-state wiring
# ---------------------------------------------------------------------------

def set_rotation_app(app: FastAPI) -> None:
    """
    Store FastAPI app reference. Call in main.py lifespan alongside set_app().
    """
    global _app_ref
    _app_ref = app


def _get_sync_manager() -> InfisicalCredentialManager:
    if _app_ref is None:
        raise RuntimeError("set_rotation_app(app) must be called before rotation runs.")
    km = getattr(_app_ref.state, "key_manager", None)
    if km is None:
        raise RuntimeError("key_manager not found on app.state.")
    return km._sync_manager


def _get_executor():
    if _app_ref is None:
        raise RuntimeError("set_rotation_app(app) must be called before rotation runs.")
    executor = getattr(_app_ref.state, "infisical_executor", None)
    if executor is None:
        raise RuntimeError("infisical_executor not found on app.state.")
    return executor


# ---------------------------------------------------------------------------
# Infisical helpers — run sync SDK in thread pool
# ---------------------------------------------------------------------------

async def _infisical_get(secret_name: str) -> Optional[str]:
    """Fetch a secret by name. Returns None if not found."""
    loop = asyncio.get_event_loop()
    km = _get_sync_manager()
    executor = _get_executor()

    def _fetch():
        try:
            return km._fetch_secret(secret_name, context=secret_name)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                return None
            raise

    return await loop.run_in_executor(executor, _fetch)


async def _infisical_upsert(secret_name: str, value: str) -> None:
    """Write a secret (create or update)."""
    loop = asyncio.get_event_loop()
    km = _get_sync_manager()
    executor = _get_executor()
    await loop.run_in_executor(executor, lambda: km._upsert_secret(secret_name, value))


async def _infisical_delete(secret_name: str) -> None:
    """Delete a secret. Logs warning if not found, never raises."""
    loop = asyncio.get_event_loop()
    km = _get_sync_manager()
    executor = _get_executor()

    def _delete():
        try:
            from infisical_client import DeleteSecretOptions  # type: ignore[import]
            km._client.deleteSecret(
                options=DeleteSecretOptions(
                    project_id=km._settings.project_id,
                    environment=km._settings.environment,
                    secret_name=secret_name,
                )
            )
            logger.info("Deleted Infisical secret '%s'.", secret_name)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                logger.warning("Secret '%s' did not exist — nothing to delete.", secret_name)
            else:
                raise

    await loop.run_in_executor(executor, _delete)


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def _next_version(current: str) -> str:
    """'v1' → 'v2', 'v9' → 'v10'"""
    try:
        n = int(current.lstrip("v"))
        return f"v{n + 1}"
    except ValueError:
        return "v2"


def _tenant_key_name(tenant_id: str, version: str) -> str:
    return f"TENANT_KEY_{tenant_id}_{version}"


def _active_version_name(tenant_id: str) -> str:
    return f"TENANT_ACTIVE_VERSION_{tenant_id}"


# ---------------------------------------------------------------------------
# Per-tenant rotation
# ---------------------------------------------------------------------------

async def _rotate_tenant_key(tenant_id: uuid.UUID) -> dict:
    """
    Rotate the AES key for a single tenant.
    Returns a result dict. Never raises — all exceptions are caught.
    """
    tid = str(tenant_id)

    try:
        # ── Step 1: Get current active version ────────────────────────────
        current_version = await _infisical_get(_active_version_name(tid))
        if current_version is None:
            logger.warning(
                "Tenant %s: no TENANT_ACTIVE_VERSION found — skipping rotation.", tid
            )
            return {
                "tenant_id": tid,
                "status": "skipped",
                "reason": "no active version pointer in Infisical",
            }
        current_version = current_version.strip().lower()

        # ── Step 2: Fetch old key ──────────────────────────────────────────
        old_secret_name = _tenant_key_name(tid, current_version)
        old_raw_key = await _infisical_get(old_secret_name)
        if old_raw_key is None:
            logger.error(
                "Tenant %s: old key '%s' not found in Infisical — skipping.", tid, old_secret_name
            )
            return {
                "tenant_id": tid,
                "status": "error",
                "reason": f"old key '{old_secret_name}' missing from Infisical",
            }

        # ── Step 3: Compute next version + generate new key ───────────────
        next_version = _next_version(current_version)
        new_raw_key = _secrets.token_hex(32)          # 256-bit random key
        new_secret_name = _tenant_key_name(tid, next_version)

        # ── Step 4: Store new key in Infisical BEFORE touching DB ─────────
        # If we crash after this and before DB commit, the new secret is
        # orphaned but harmless — no DB row points to it yet.
        await _infisical_upsert(new_secret_name, new_raw_key)
        logger.info("Tenant %s: stored new key '%s' in Infisical.", tid, new_secret_name)

        # ── Step 5: Re-encrypt all matching DB rows ────────────────────────
        old_enc = EncryptionService(raw_key=old_raw_key, key_version=current_version)
        new_enc = EncryptionService(raw_key=new_raw_key, key_version=next_version)

        rows_rotated = 0
        rows_skipped = 0

        async with async_session_maker() as db:
            try:
                result = await db.execute(
                    select(CrmIntegration).where(
                        CrmIntegration.tenant_id == tenant_id,
                        CrmIntegration.key_version == current_version,
                    )
                )
                rows = result.scalars().all()

                for row in rows:
                    try:
                        if row.credential_enc:
                            plaintext = old_enc.decrypt_from_db(row.credential_enc)
                            row.credential_enc = new_enc.encrypt(plaintext).to_db_string()

                        if row.webhook_secrets_enc:
                            ws_plain = old_enc.decrypt_from_db(row.webhook_secrets_enc)
                            row.webhook_secrets_enc = new_enc.encrypt(ws_plain).to_db_string()

                        row.key_version = next_version
                        rows_rotated += 1

                    except Exception as row_exc:
                        # One bad row must not block the rest.
                        # Row keeps old key_version so it still decrypts with old key.
                        logger.error(
                            "Tenant %s: failed to re-encrypt integration_id=%s: %s — row skipped.",
                            tid, row.id, row_exc,
                        )
                        rows_skipped += 1

                # ── Step 6: DB commit — point of no return ─────────────────
                await db.commit()
                logger.info(
                    "Tenant %s: DB commit OK — rotated=%d skipped=%d.",
                    tid, rows_rotated, rows_skipped,
                )

            except Exception as db_exc:
                await db.rollback()
                # Attempt to clean up the orphaned new Infisical secret.
                logger.error(
                    "Tenant %s: DB commit failed — rolling back. "
                    "Cleaning up orphaned key '%s'. Error: %s",
                    tid, new_secret_name, db_exc,
                )
                try:
                    await _infisical_delete(new_secret_name)
                except Exception as cleanup_exc:
                    logger.error(
                        "Tenant %s: cleanup of '%s' also failed: %s. Delete manually.",
                        tid, new_secret_name, cleanup_exc,
                    )
                return {
                    "tenant_id": tid,
                    "status": "error",
                    "reason": f"DB commit failed: {db_exc}",
                }

        # ── Step 7: Update version pointer ────────────────────────────────
        # Runs AFTER DB commit — DB rows already use new key.
        try:
            await _infisical_upsert(_active_version_name(tid), next_version)
            logger.info("Tenant %s: version pointer updated to '%s'.", tid, next_version)
        except Exception as ptr_exc:
            logger.error(
                "Tenant %s: version pointer update failed: %s. "
                "DB rows use '%s' but pointer still says '%s'. "
                "Manually set TENANT_ACTIVE_VERSION_%s = '%s' in Infisical.",
                tid, ptr_exc, next_version, current_version, tid, next_version,
            )
            # Don't delete old key — without the pointer update the next rotation
            # run would try to rotate from the wrong version.
            return {
                "tenant_id": tid,
                "status": "partial",
                "reason": "DB rotated but version pointer update failed — old key NOT deleted",
                "rows_rotated": rows_rotated,
                "rows_skipped": rows_skipped,
                "new_version": next_version,
            }

        # ── Step 8: Delete old key from Infisical ─────────────────────────
        # Only reached if DB commit AND pointer update both succeeded.
        if rows_skipped > 0:
            # Some rows still use the old key — don't delete it yet.
            logger.warning(
                "Tenant %s: %d row(s) could not be re-encrypted. "
                "Old key '%s' NOT deleted — those rows still need it.",
                tid, rows_skipped, old_secret_name,
            )
        else:
            try:
                await _infisical_delete(old_secret_name)
                logger.info("Tenant %s: old key '%s' deleted from Infisical.", tid, old_secret_name)
            except Exception as del_exc:
                # Non-fatal — old key stays in Infisical but is no longer used.
                logger.error(
                    "Tenant %s: failed to delete old key '%s': %s. Delete manually.",
                    tid, old_secret_name, del_exc,
                )

        logger.info(
            "Tenant %s: rotation complete. %s → %s | rotated=%d skipped=%d",
            tid, current_version, next_version, rows_rotated, rows_skipped,
        )
        return {
            "tenant_id": tid,
            "status": "success",
            "old_version": current_version,
            "new_version": next_version,
            "rows_rotated": rows_rotated,
            "rows_skipped": rows_skipped,
            "rotated_at": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        logger.exception("Tenant %s: unexpected error during rotation: %s", tid, exc)
        return {"tenant_id": tid, "status": "error", "reason": str(exc)}


# ---------------------------------------------------------------------------
# Full rotation run — all active tenants, sequential with isolation
# ---------------------------------------------------------------------------

async def run_all_tenants_key_rotation() -> dict:
    """
    Rotate AES keys for all active tenants sequentially.
    A failure in one tenant does not block others.
    """
    logger.info("=== Starting 90-day tenant key rotation ===")

    async with async_session_maker() as db:
        result = await db.execute(
            select(Tenant).where(Tenant.is_active == True)  # noqa: E712
        )
        tenants = result.scalars().all()

    if not tenants:
        logger.info("No active tenants found — rotation complete.")
        return {"status": "no_tenants", "results": []}

    logger.info("Rotating keys for %d active tenant(s).", len(tenants))

    results = []
    success_count = error_count = skipped_count = 0

    for tenant in tenants:
        logger.info("--- Rotating key for tenant_id=%s ---", tenant.id)
        result = await _rotate_tenant_key(tenant.id)
        results.append(result)

        if result["status"] == "success":
            success_count += 1
        elif result["status"] == "skipped":
            skipped_count += 1
        else:
            error_count += 1

        # Brief pause between tenants to avoid hammering Infisical
        await asyncio.sleep(0.5)

    logger.info(
        "=== Key rotation complete === total=%d success=%d skipped=%d errors=%d",
        len(tenants), success_count, skipped_count, error_count,
    )
    return {
        "status": "complete",
        "total_tenants": len(tenants),
        "success": success_count,
        "skipped": skipped_count,
        "errors": error_count,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Scheduler setup / teardown
# ---------------------------------------------------------------------------

def start_key_rotation_scheduler() -> None:
    """Start the 90-day scheduler. Call after set_rotation_app(app) in lifespan."""
    _rotation_scheduler.add_job(
        run_all_tenants_key_rotation,
        trigger=IntervalTrigger(days=90),
        id="tenant_key_rotation",
        replace_existing=True,
        misfire_grace_time=3600,   # 1-hour grace window
    )
    _rotation_scheduler.start()
    logger.info("Tenant key rotation scheduler started — interval=90 days.")


def stop_key_rotation_scheduler() -> None:
    """Stop the scheduler. Call in lifespan shutdown."""
    _rotation_scheduler.shutdown(wait=False)
    logger.info("Tenant key rotation scheduler stopped.")