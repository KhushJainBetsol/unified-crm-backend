# """
# app/services/crm_integration_service.py
# =========================================
# Full encrypt → store → fetch → decrypt flow for CRM integrations.

# STORAGE MODEL (v2)
# ------------------
# credential_enc   TEXT    — single AES-256-CBC encrypted JSON blob (ALL secrets)
# metadata         JSONB   — non-sensitive config (auth_type, key_version, base_url, …)

# Secret dict shapes per auth_type (what lives INSIDE credential_enc):
#   api_token / bearer_token / access_token / api_key
#       {"token": "abc123"}

#   basic_auth
#       {"username": "u", "password": "p"}

#   oauth2
#       {"access_token": "...", "refresh_token": "...", "token_type": "Bearer",
#        "expires_at": 1700000000, "client_id": "...", "client_secret": "..."}

#   hmac
#       {"api_token": "...", "webhook_secret": "...",
#        "per_event_secrets": {"Case.create": "s1", "Case.update": "s2"}}

# SAVE FLOW
# ---------
# 1. Accept plaintext credentials from the caller.
# 2. Fetch ACTIVE_KEY_VERSION + ENCRYPTION_KEY_<version> from Infisical.
# 3. Serialize secrets → JSON string.
# 4. AES-256-CBC encrypt → store in credential_enc.
# 5. Write non-sensitive fields into metadata JSONB.
# 6. Commit.

# READ FLOW
# ---------
# 1. Load CrmIntegration row from DB.
# 2. Read key_version from row.config["key_version"].
# 3. Fetch ENCRYPTION_KEY_<key_version> from Infisical.
# 4. AES decrypt credential_enc → parse JSON → get secret dict.
# 5. Use in-memory — NEVER persist decrypted values.
# """

# from __future__ import annotations

# import json
# import logging
# import uuid
# from typing import Any, Dict, Optional

# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession

# from app.credentials.async_manager import AsyncInfisicalCredentialManager
# from app.credentials.encryption import EncryptionService
# from app.credentials.exceptions import CredentialDecodeError, CredentialNotFoundError
# from app.credentials.models import CrmCredentialEnvelope
# from app.models.crm_integration import CrmIntegration

# logger = logging.getLogger(__name__)


# # ---------------------------------------------------------------------------
# # Supported auth_type values — single source of truth
# # ---------------------------------------------------------------------------

# AUTH_TYPES = frozenset({
#     "api_token",
#     "bearer_token",
#     "access_token",
#     "api_key",
#     "basic_auth",
#     "oauth2",
#     "hmac",
# })


# class CrmIntegrationService:
#     """
#     Service layer for creating, reading, and updating CRM integrations
#     with encrypted credential storage.

#     Parameters
#     ----------
#     db:
#         Async SQLAlchemy session (injected via FastAPI Depends).
#     key_manager:
#         Async Infisical key manager (singleton on app.state).
#     """

#     def __init__(
#         self,
#         db: AsyncSession,
#         key_manager: AsyncInfisicalCredentialManager,
#     ) -> None:
#         self._db = db
#         self._km = key_manager

#     # =========================================================================
#     # CREATE — encrypt and store a new integration
#     # =========================================================================

#     async def create_integration(
#         self,
#         *,
#         tenant_id: uuid.UUID,
#         source_system_id: int,
#         auth_type: str,
#         base_url: Optional[str],
#         # ── API token / bearer / access token ──
#         token: Optional[str] = None,
#         # ── Basic auth ──
#         username: Optional[str] = None,
#         password: Optional[str] = None,
#         # ── OAuth2 ──
#         access_token: Optional[str] = None,
#         refresh_token: Optional[str] = None,
#         token_type: str = "Bearer",
#         expires_at: Optional[int] = None,
#         client_id: Optional[str] = None,
#         client_secret: Optional[str] = None,
#         # ── HMAC / webhook ──
#         api_token: Optional[str] = None,
#         webhook_secret: Optional[str] = None,
#         per_event_secrets: Optional[Dict[str, str]] = None,
#         # ── Token expiry (plaintext) ──
#         token_expires_at: Any = None,
#         # ── Extra non-sensitive metadata ──
#         extra_metadata: Optional[Dict[str, Any]] = None,
#     ) -> CrmIntegration:
#         """
#         Encrypt all secrets into a single blob and persist a new CrmIntegration row.

#         Only supply the fields relevant to your auth_type — unused fields are
#         silently ignored when building the secret dict.

#         Returns
#         -------
#         CrmIntegration
#             The persisted row with id and metadata populated.
#         """
#         if auth_type not in AUTH_TYPES:
#             raise ValueError(
#                 f"Unknown auth_type '{auth_type}'. Valid values: {sorted(AUTH_TYPES)}"
#             )

#         # 1. Fetch active key from Infisical
#         version, raw_key = await self._km.get_active_key_and_version()
#         svc = EncryptionService(raw_key=raw_key, key_version=version)

#         # 2. Build the secret dict for this auth_type and encrypt
#         secret_dict = _build_secret_dict(
#             auth_type=auth_type,
#             token=token,
#             username=username,
#             password=password,
#             access_token=access_token,
#             refresh_token=refresh_token,
#             token_type=token_type,
#             expires_at=expires_at,
#             client_id=client_id,
#             client_secret=client_secret,
#             api_token=api_token,
#             webhook_secret=webhook_secret,
#             per_event_secrets=per_event_secrets,
#         )
#         encrypted_blob = svc.encrypt(json.dumps(secret_dict)).to_db_string()

#         # 3. Build metadata JSONB — everything non-sensitive
#         metadata: Dict[str, Any] = {
#             "auth_type":   auth_type,
#             "key_version": version,
#             "base_url":    (base_url or "").rstrip("/"),
#             **(extra_metadata or {}),
#         }

#         # 4. Persist row
#         integration = CrmIntegration(
#             tenant_id=tenant_id,
#             source_system_id=source_system_id,
#             credential_enc=encrypted_blob,
#             metadata=metadata,
#             token_expires_at=token_expires_at,
#             is_active=True,
#         )
#         self._db.add(integration)
#         await self._db.flush()   # populate .id before caller commits

#         logger.info(
#             "Created CrmIntegration id=%s tenant=%s auth_type=%s key_version=%s",
#             integration.id, tenant_id, auth_type, version,
#         )
#         return integration

#     # =========================================================================
#     # READ — decrypt credentials for use at request time
#     # =========================================================================

#     async def get_decrypted_credentials(
#         self,
#         integration: CrmIntegration,
#     ) -> Dict[str, Any]:
#         """
#         Fetch the AES key for this row's key_version, decrypt credential_enc,
#         and return the full secret dict.

#         Returns
#         -------
#         dict — contents vary by auth_type:

#           api_token / bearer_token / access_token / api_key:
#               {"auth_type": "api_token", "token": "...", "base_url": "..."}

#           basic_auth:
#               {"auth_type": "basic_auth", "username": "...", "password": "...", "base_url": "..."}

#           oauth2:
#               {"auth_type": "oauth2", "access_token": "...", "refresh_token": "...",
#                "token_type": "Bearer", "expires_at": 0, "base_url": "..."}

#           hmac:
#               {"auth_type": "hmac", "api_token": "...", "webhook_secret": "...",
#                "per_event_secrets": {...}, "base_url": "..."}

#         The decrypted values are NEVER written back to the DB or logged.

#         Raises
#         ------
#         CredentialNotFoundError
#             If credential_enc is empty (integration never provisioned).
#         CredentialDecodeError
#             If decryption or JSON parsing fails.
#         """
#         if not integration.credential_enc:
#             raise CredentialNotFoundError(str(integration.id))

#         key_version = integration.key_version   # reads from metadata JSONB
#         raw_key = await self._km.get_encryption_key(key_version)
#         svc = EncryptionService(raw_key=raw_key, key_version=key_version)

#         try:
#             decrypted_json = svc.decrypt_from_db(integration.credential_enc)
#             secret_dict: Dict[str, Any] = json.loads(decrypted_json)
#         except Exception as exc:
#             raise CredentialDecodeError(str(integration.id), str(exc)) from exc

#         # Attach plaintext metadata so callers have a one-stop result dict
#         return {
#             "auth_type": integration.auth_type,
#             "base_url":  integration.base_url or "",
#             **secret_dict,
#         }

#     async def get_credential_envelope(
#         self,
#         integration: CrmIntegration,
#     ) -> CrmCredentialEnvelope:
#         """
#         Higher-level variant: decrypt and return a CrmCredentialEnvelope
#         ready for the adapter factory.

#         Use get_decrypted_credentials() when you need raw fields;
#         use this when you need to pass to CrmAdapterFactory.
#         """
#         secret = await self.get_decrypted_credentials(integration)
#         auth_type = integration.auth_type
#         crm_type = integration.config.get("crm_type", "unknown")

#         credentials_dict = _secret_to_envelope_creds(auth_type, secret)

#         return CrmCredentialEnvelope(
#             crm_type=crm_type,
#             base_url=integration.base_url or "",
#             credentials=credentials_dict,
#             metadata={
#                 "key_version": integration.key_version,
#                 "auth_type":   auth_type,
#             },
#         )

#     # =========================================================================
#     # UPDATE — replace one or more secret fields (re-encrypts entire blob)
#     # =========================================================================

#     async def update_credentials(
#         self,
#         integration: CrmIntegration,
#         *,
#         # Pass only the fields you want to change; omit the rest
#         token: Optional[str] = None,
#         username: Optional[str] = None,
#         password: Optional[str] = None,
#         access_token: Optional[str] = None,
#         refresh_token: Optional[str] = None,
#         token_type: Optional[str] = None,
#         expires_at: Optional[int] = None,
#         client_id: Optional[str] = None,
#         client_secret: Optional[str] = None,
#         api_token: Optional[str] = None,
#         webhook_secret: Optional[str] = None,
#         per_event_secrets: Optional[Dict[str, str]] = None,
#         # Metadata updates (non-sensitive)
#         base_url: Optional[str] = None,
#         extra_metadata: Optional[Dict[str, Any]] = None,
#     ) -> CrmIntegration:
#         """
#         Merge new values into the existing secret dict and re-encrypt with
#         the *current* active key.

#         Always upgrades key_version to the latest — so this doubles as a
#         key-rotation operation for the row.

#         Only fields explicitly passed (not None) overwrite the stored value.
#         """
#         auth_type = integration.auth_type

#         # 1. Decrypt current secrets (we'll merge into them)
#         existing_secrets: Dict[str, Any] = {}
#         if integration.credential_enc:
#             old_raw_key = await self._km.get_encryption_key(integration.key_version)
#             old_svc = EncryptionService(
#                 raw_key=old_raw_key, key_version=integration.key_version
#             )
#             try:
#                 decrypted = old_svc.decrypt_from_db(integration.credential_enc)
#                 existing_secrets = json.loads(decrypted)
#             except Exception as exc:
#                 raise CredentialDecodeError(str(integration.id), str(exc)) from exc

#         # 2. Build a partial update dict from supplied args (None = keep existing)
#         updates = _build_secret_dict(
#             auth_type=auth_type,
#             token=token,
#             username=username,
#             password=password,
#             access_token=access_token,
#             refresh_token=refresh_token,
#             token_type=token_type,
#             expires_at=expires_at,
#             client_id=client_id,
#             client_secret=client_secret,
#             api_token=api_token,
#             webhook_secret=webhook_secret,
#             per_event_secrets=per_event_secrets,
#         )

#         # 3. Merge: existing values kept, updates overwrite
#         merged = {**existing_secrets, **{k: v for k, v in updates.items() if v is not None}}

#         # 4. Re-encrypt merged dict with current active key
#         new_version, new_raw_key = await self._km.get_active_key_and_version()
#         new_svc = EncryptionService(raw_key=new_raw_key, key_version=new_version)
#         integration.credential_enc = new_svc.encrypt(json.dumps(merged)).to_db_string()

#         # 5. Update metadata
#         updated_meta = dict(integration.config)
#         updated_meta["key_version"] = new_version
#         if base_url is not None:
#             updated_meta["base_url"] = base_url.rstrip("/")
#         if extra_metadata:
#             updated_meta.update(extra_metadata)
#         integration.config = updated_meta

#         await self._db.flush()
#         logger.info(
#             "Updated credentials for id=%s (key_version %s → %s)",
#             integration.id, integration.config.get("key_version"), new_version,
#         )
#         return integration

#     # =========================================================================
#     # ROTATE — re-encrypt every active integration with the current key
#     # =========================================================================

#     async def rotate_integration(self, integration: CrmIntegration) -> CrmIntegration:
#         """
#         Re-encrypt credential_enc with the current active Infisical key.

#         Functionally equivalent to update_credentials() with no field changes
#         but is more explicit in intent — use this in batch rotation scripts.
#         """
#         return await self.update_credentials(integration)

#     async def rotate_all_for_tenant(self, tenant_id: uuid.UUID) -> Dict[str, int]:
#         """
#         Rotate all active integrations for a tenant.
#         Returns {"rotated": N, "failed": M}.
#         """
#         result = await self._db.execute(
#             select(CrmIntegration).where(
#                 CrmIntegration.tenant_id == tenant_id,
#                 CrmIntegration.is_active == True,
#                 CrmIntegration.credential_enc.isnot(None),
#             )
#         )
#         rows = result.scalars().all()

#         rotated = 0
#         failed = 0
#         for row in rows:
#             try:
#                 await self.rotate_integration(row)
#                 rotated += 1
#             except Exception as exc:
#                 logger.error("Failed to rotate integration id=%s: %s", row.id, exc)
#                 failed += 1

#         await self._db.flush()
#         logger.info(
#             "Rotation complete for tenant=%s: rotated=%d failed=%d",
#             tenant_id, rotated, failed,
#         )
#         return {"rotated": rotated, "failed": failed}

#     # =========================================================================
#     # REVOKE
#     # =========================================================================

#     async def revoke_integration(
#         self,
#         integration: CrmIntegration,
#         wipe: bool = False,
#     ) -> None:
#         """
#         Soft-disable an integration (is_active = False).

#         Parameters
#         ----------
#         wipe:
#             If True, also nulls out credential_enc — a hard wipe of the
#             encrypted secret. Irreversible without re-provisioning.
#         """
#         integration.is_active = False
#         if wipe:
#             integration.credential_enc = None
#             logger.warning("Wiped credential_enc for integration id=%s", integration.id)
#         await self._db.flush()
#         logger.info("Revoked integration id=%s (wipe=%s)", integration.id, wipe)

#     # =========================================================================
#     # HELPERS — load row by ID (avoid repeating select logic in routes)
#     # =========================================================================

#     async def get_by_id(
#         self,
#         integration_id: uuid.UUID,
#         tenant_id: Optional[uuid.UUID] = None,
#         active_only: bool = True,
#     ) -> CrmIntegration:
#         """
#         Load a CrmIntegration row, optionally scoped to a tenant.

#         Raises
#         ------
#         CredentialNotFoundError
#             If the row does not exist or doesn't match the tenant.
#         """
#         query = select(CrmIntegration).where(CrmIntegration.id == integration_id)
#         if tenant_id is not None:
#             query = query.where(CrmIntegration.tenant_id == tenant_id)
#         if active_only:
#             query = query.where(CrmIntegration.is_active == True)

#         result = await self._db.execute(query)
#         row = result.scalar_one_or_none()
#         if row is None:
#             raise CredentialNotFoundError(str(integration_id))
#         return row


# # =============================================================================
# # Module-level helpers — pure functions, no I/O
# # =============================================================================

# def _build_secret_dict(
#     auth_type: str,
#     *,
#     token: Optional[str] = None,
#     username: Optional[str] = None,
#     password: Optional[str] = None,
#     access_token: Optional[str] = None,
#     refresh_token: Optional[str] = None,
#     token_type: Optional[str] = None,
#     expires_at: Optional[int] = None,
#     client_id: Optional[str] = None,
#     client_secret: Optional[str] = None,
#     api_token: Optional[str] = None,
#     webhook_secret: Optional[str] = None,
#     per_event_secrets: Optional[Dict[str, str]] = None,
# ) -> Dict[str, Any]:
#     """
#     Build the auth-type-specific secret dict that gets encrypted.
#     Only include keys whose values are not None.
#     """
#     if auth_type in ("api_token", "bearer_token", "access_token", "api_key"):
#         d: Dict[str, Any] = {}
#         if token is not None:
#             d["token"] = token
#         return d

#     if auth_type == "basic_auth":
#         d = {}
#         if username is not None:
#             d["username"] = username
#         if password is not None:
#             d["password"] = password
#         return d

#     if auth_type == "oauth2":
#         d = {}
#         if access_token is not None:
#             d["access_token"] = access_token
#         if refresh_token is not None:
#             d["refresh_token"] = refresh_token
#         if token_type is not None:
#             d["token_type"] = token_type
#         if expires_at is not None:
#             d["expires_at"] = expires_at
#         if client_id is not None:
#             d["client_id"] = client_id
#         if client_secret is not None:
#             d["client_secret"] = client_secret
#         return d

#     if auth_type == "hmac":
#         d = {}
#         if api_token is not None:
#             d["api_token"] = api_token
#         if webhook_secret is not None:
#             d["webhook_secret"] = webhook_secret
#         if per_event_secrets is not None:
#             d["per_event_secrets"] = per_event_secrets
#         return d

#     # Fallback — treat as generic token
#     d = {}
#     if token is not None:
#         d["token"] = token
#     return d


# def _secret_to_envelope_creds(
#     auth_type: str,
#     secret: Dict[str, Any],
# ) -> Dict[str, Any]:
#     """
#     Convert a decrypted secret dict into the CrmCredentialEnvelope
#     credentials format (always includes 'strategy').
#     """
#     if auth_type in ("api_token", "bearer_token", "access_token", "api_key"):
#         return {"strategy": "api_token", "token": secret.get("token", "")}

#     if auth_type == "basic_auth":
#         return {
#             "strategy": "basic",
#             "username": secret.get("username", ""),
#             "password": secret.get("password", ""),
#         }

#     if auth_type == "oauth2":
#         return {
#             "strategy": "oauth2",
#             "access_token":  secret.get("access_token", ""),
#             "refresh_token": secret.get("refresh_token"),
#             "token_type":    secret.get("token_type", "Bearer"),
#             "expires_at":    secret.get("expires_at"),
#         }

#     if auth_type == "hmac":
#         # Primary outbound credential is the api_token (if present)
#         return {
#             "strategy": "api_token",
#             "token":    secret.get("api_token", ""),
#         }

#     return {"strategy": "api_token", "token": secret.get("token", "")}