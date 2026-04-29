"""
app/services/entity_sync_service.py

Config-driven, CRM-agnostic entity sync service.

Syncs agents, customers, and companies from CRM raw payloads into the DB.

Supports two input shapes for sync_agents / sync_customers / sync_companies:
  1. List[UnifiedAgent | UnifiedCustomer | UnifiedOrganization]
     Produced by the new adapter pattern.  Fields are read directly from
     the typed Pydantic model — no config path resolution needed.

  2. List[dict]
     Legacy / backward-compat path.  Field extraction is config-driven via
     the AdapterConfig field_mappings from the CRM's YAML.

All field extraction (how to get name, email, phone from a raw dict) is
driven by the AdapterConfig loaded from the CRM's YAML config file, rather
than hard-coded per-CRM logic.

The config's field_mappings section is extended to cover agent and
organization entities.  If your YAML doesn't define agent/organization
mappings the service falls back to sensible defaults so existing configs
keep working.

YAML field_mappings (in config/<crm>/config.yaml):
  agent:
    id: "id"
    first_name: "firstname"     # or "firstName" for EspoCRM
    last_name:  "lastname"      # or "lastName"
    email:      "?email"        # optional
  organization:
    id:    "id"
    name:  "name"
    email: "?email"             # optional
    phone: "?phone"             # optional

Must be run BEFORE ticket sync so ticket sync can resolve:
  crm_agent_id    → agents.id    (UUID)
  crm_customer_id → customers.id (UUID)
  crm_company_id  → companies.id (UUID)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.registry import AdapterNotFoundError, AdapterRegistry
from app.domain.models import UnifiedAgent, UnifiedCustomer
from app.models.agent import Agent
from app.models.company import Company
from app.models.customer import Customer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Field extraction helpers  (dict / legacy path)
# ---------------------------------------------------------------------------

def _get(raw: dict, path: str) -> Any:
    """Resolve a dot-notation path; leading '?' = optional (returns None)."""
    optional = path.startswith("?")
    clean = path.lstrip("?")
    try:
        result = raw
        for part in clean.split("."):
            result = result[part]
        return result
    except (KeyError, TypeError):
        if optional:
            return None
        raise


def _extract_agent_fields(raw: dict, mappings: dict[str, str]) -> tuple[str, str, str | None]:
    """
    Extract (crm_agent_id, display_name, email) from a raw agent dict
    using the field mappings from the YAML config.
    """
    id_path = mappings.get("id", "id")
    crm_agent_id = str(_get(raw, id_path))

    first_path = mappings.get("first_name") or mappings.get("firstName")
    last_path  = mappings.get("last_name")  or mappings.get("lastName")

    if first_path and last_path:
        first = str(_get(raw, first_path) or "")
        last  = str(_get(raw, last_path)  or "")
        name  = f"{first} {last}".strip() or f"Agent {crm_agent_id}"
    elif "name" in mappings:
        name = str(_get(raw, mappings["name"]) or f"Agent {crm_agent_id}")
    else:
        first = str(raw.get("firstname") or raw.get("firstName") or "")
        last  = str(raw.get("lastname")  or raw.get("lastName")  or "")
        name  = f"{first} {last}".strip() or str(raw.get("name") or f"Agent {crm_agent_id}")

    email_path = mappings.get("email", "?email")
    email = _get(raw, email_path) or None
    if email is not None:
        email = str(email)

    return crm_agent_id, name, email


def _extract_customer_fields(
    raw: dict, mappings: dict[str, str]
) -> tuple[str, str, str | None, str | None]:
    """
    Extract (crm_customer_id, display_name, email, phone) from a raw customer dict.
    """
    id_path = mappings.get("id", "id")
    crm_customer_id = str(_get(raw, id_path))

    first_path = mappings.get("first_name") or mappings.get("firstName")
    last_path  = mappings.get("last_name")  or mappings.get("lastName")

    if first_path and last_path:
        first = str(_get(raw, first_path) or "Customer")
        last  = str(_get(raw, last_path)  or "")
        name  = f"{first} {last}".strip()
    elif "name" in mappings:
        name = str(_get(raw, mappings["name"]) or f"Customer {crm_customer_id}")
    else:
        first = str(raw.get("firstname") or raw.get("firstName") or "Customer")
        last  = str(raw.get("lastname")  or raw.get("lastName")  or "")
        name  = f"{first} {last}".strip()

    email_path = mappings.get("email", "?email")
    email = _get(raw, email_path) or None
    if email is not None:
        email = str(email)

    phone_path = mappings.get("phone", "?phone")
    phone = _get(raw, phone_path) or None
    if phone is not None:
        phone = str(phone)
    else:
        phone = raw.get("phoneNumber") or raw.get("phone") or None
        if phone is not None:
            phone = str(phone)

    return crm_customer_id, name, email, phone


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class EntitySyncService:

    def __init__(
        self,
        db: AsyncSession,
        source_system_id: int,
        tenant_id: uuid.UUID,
    ) -> None:
        self.db               = db
        self.source_system_id = source_system_id
        self.tenant_id        = tenant_id

    # ------------------------------------------------------------------
    # Internal upsert helpers
    # ------------------------------------------------------------------

    async def _upsert_agent(
        self,
        crm_agent_id: str,
        name: str,
        email: str | None,
    ) -> bool:
        """Returns True if created, False if updated."""
        result = await self.db.execute(
            select(Agent).where(
                Agent.tenant_id        == self.tenant_id,
                Agent.crm_agent_id     == crm_agent_id,
                Agent.source_system_id == self.source_system_id,
            )
        )
        agent = result.scalars().first()

        if agent:
            agent.name  = name
            agent.email = email
            await self.db.flush()
            return False
        else:
            self.db.add(Agent(
                tenant_id        = self.tenant_id,
                crm_agent_id     = crm_agent_id,
                source_system_id = self.source_system_id,
                name             = name,
                email            = email,
                is_active        = True,
            ))
            await self.db.flush()
            return True

    async def _upsert_customer(
        self,
        crm_customer_id: str,
        name: str,
        email: str | None,
        phone: str | None = None,
    ) -> bool:
        """Returns True if created, False if updated."""
        result = await self.db.execute(
            select(Customer).where(
                Customer.tenant_id        == self.tenant_id,
                Customer.crm_customer_id  == crm_customer_id,
                Customer.source_system_id == self.source_system_id,
            )
        )
        customer = result.scalars().first()

        if customer:
            customer.name  = name
            customer.email = email
            customer.phone = phone
            await self.db.flush()
            return False
        else:
            self.db.add(Customer(
                tenant_id        = self.tenant_id,
                crm_customer_id  = crm_customer_id,
                source_system_id = self.source_system_id,
                name             = name,
                email            = email,
                phone            = phone,
            ))
            await self.db.flush()
            return True

    
    # ------------------------------------------------------------------
    # Config-driven sync methods
    # ------------------------------------------------------------------

    async def sync_agents(
        self,
        raw_list: list,
        crm_type: str,
        registry: AdapterRegistry | None = None,
    ) -> tuple[int, int]:
        """
        Sync a list of agents into the agents table.

        Accepts either:
          - List[UnifiedAgent]  from the adapter pattern (new)
          - List[dict]          raw CRM JSON (legacy / backward-compat)

        Returns:
            (created, updated) counts.
        """
        created = updated = 0

        for item in raw_list:
            try:
                if isinstance(item, UnifiedAgent):
                    # ── Unified model path (adapter pattern) ──────────────
                    crm_agent_id = item.id
                    name         = item.full_name
                    email        = item.email
                else:
                    # ── Dict path (legacy) ────────────────────────────────
                    mappings = _get_agent_mappings(crm_type, registry)
                    crm_agent_id, name, email = _extract_agent_fields(item, mappings)

                was_created = await self._upsert_agent(crm_agent_id, name, email)
                if was_created:
                    created += 1
                else:
                    updated += 1

            except Exception as exc:
                item_id = item.id if isinstance(item, UnifiedAgent) else item.get("id")
                logger.error(
                    "Failed to sync %s agent id=%r tenant=%s: %s",
                    crm_type, item_id, self.tenant_id, exc,
                )

        return created, updated

    async def sync_customers(
        self,
        raw_list: list,
        crm_type: str,
        registry: AdapterRegistry | None = None,
    ) -> tuple[int, int]:
        """
        Sync a list of customers into the customers table.

        Accepts either:
          - List[UnifiedCustomer]  from the adapter pattern (new)
          - List[dict]             raw CRM JSON (legacy / backward-compat)

        Returns:
            (created, updated) counts.
        """
        created = updated = 0

        for item in raw_list:
            try:
                if isinstance(item, UnifiedCustomer):
                    # ── Unified model path (adapter pattern) ──────────────
                    crm_customer_id = item.id
                    name            = item.full_name
                    email           = item.email
                    phone           = None   # UnifiedCustomer has no phone field
                else:
                    # ── Dict path (legacy) ────────────────────────────────
                    mappings = _get_agent_mappings(crm_type, registry)
                    crm_customer_id, name, email, phone = _extract_customer_fields(item, mappings)

                was_created = await self._upsert_customer(
                    crm_customer_id, name, email, phone
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

            except Exception as exc:
                item_id = item.id if isinstance(item, UnifiedCustomer) else item.get("id")
                logger.error(
                    "Failed to sync %s customer id=%r tenant=%s: %s",
                    crm_type, item_id, self.tenant_id, exc,
                )

        return created, updated

    # ------------------------------------------------------------------
    # Backward-compatible named methods (delegate to the generic ones)
    # ------------------------------------------------------------------

    async def sync_zammad_agents(self, raw_users: list) -> tuple[int, int]:
        return await self.sync_agents(raw_users, "zammad")

    async def sync_zammad_customers(self, raw_customers: list) -> tuple[int, int]:
        return await self.sync_customers(raw_customers, "zammad")

    async def sync_zammad_companies(self, raw_orgs: list) -> tuple[int, int]:
        return await self.sync_companies(raw_orgs, "zammad")

    async def sync_espo_agents(self, raw_users: list) -> tuple[int, int]:
        return await self.sync_agents(raw_users, "espocrm")

    async def sync_espo_customers(self, raw_contacts: list) -> tuple[int, int]:
        return await self.sync_customers(raw_contacts, "espocrm")

    async def sync_espo_companies(self, raw_accounts: list) -> tuple[int, int]:
        return await self.sync_companies(raw_accounts, "espocrm")


# ---------------------------------------------------------------------------
# Config resolution helpers
# ---------------------------------------------------------------------------

def _get_agent_mappings(
    crm_type: str,
    registry: AdapterRegistry | None,
) -> dict[str, str]:
    """
    Return the agent field_mappings dict for the given CRM type.
    Falls back to sensible defaults if the registry is unavailable or the
    mapping section is empty.
    """
    if registry is not None:
        try:
            config = registry.get_adapter_config(crm_type)
            mappings = config.field_mappings.agent
            if mappings:
                return mappings
        except (AdapterNotFoundError, Exception):
            pass

    _defaults: dict[str, dict[str, str]] = {
        "zammad": {
            "id": "id",
            "first_name": "firstname",
            "last_name": "lastname",
            "email": "?email",
        },
        "espocrm": {
            "id": "id",
            "first_name": "firstName",
            "last_name": "lastName",
            "email": "?emailAddress",
        },
    }
    return _defaults.get(crm_type.lower(), {"id": "id"})


def _get_org_mappings(
    crm_type: str,
    registry: AdapterRegistry | None,
) -> dict[str, str]:
    """
    Return the organization field_mappings dict for the given CRM type.
    """
    if registry is not None:
        try:
            config = registry.get_adapter_config(crm_type)
            mappings = config.field_mappings.organization
            if mappings:
                return mappings
        except (AdapterNotFoundError, Exception):
            pass

    _defaults: dict[str, dict[str, str]] = {
        "zammad": {
            "id": "id",
            "name": "name",
        },
        "espocrm": {
            "id": "id",
            "name": "name",
            "email": "?emailAddress",
            "phone": "?phoneNumber",
        },
    }
    return _defaults.get(crm_type.lower(), {"id": "id", "name": "name"})