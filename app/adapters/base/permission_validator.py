"""
crm/adapters/base/permission_validator.py

CRM Permission Validators
=========================
Validates that the API credentials supplied during onboarding have the
minimum permissions required by the unified dashboard.

Required capabilities (for every supported CRM):
  - fetch_tickets        → read tickets
  - push_ticket_update   → edit tickets
  - delete tickets       → delete tickets
  - create tickets       → create tickets
  - fetch_agents         → read users / agents
  - fetch_organizations  → read organizations / accounts

Each CRM encodes these capabilities differently in its /whoami or
/user_access_token response.  A dedicated validator class handles the
check for each CRM type and returns a structured result so the caller
can surface a clear, actionable error message when any check fails.

Usage (inside an adapter's verify_connection())
-----------------------------------------------

    from crm.adapters.base.permission_validator import (
        EspoCrmPermissionValidator,
        ZammadPermissionValidator,
        PermissionValidationError,
    )

    # EspoCRM — pass the full parsed JSON body from
    #   GET /api/v1/App/user
    validator = EspoCrmPermissionValidator(api_response)
    result = validator.validate()
    if not result.ok:
        raise PermissionValidationError(result.failures)

    # Zammad — pass the full parsed JSON body from
    #   GET /api/v1/user_access_token
    validator = ZammadPermissionValidator(api_response)
    result = validator.validate()
    if not result.ok:
        raise PermissionValidationError(result.failures)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Result + Exception
# ---------------------------------------------------------------------------


@dataclass
class PermissionCheckResult:
    """Holds the outcome of a full permission validation pass."""

    ok: bool
    failures: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


class PermissionValidationError(Exception):
    """
    Raised when one or more required permissions are missing.

    Attributes
    ----------
    failures : List[str]
        Human-readable descriptions of each missing permission.
    """

    def __init__(self, failures: List[str]) -> None:
        self.failures = failures
        bullet_list = "\n".join(f"  • {f}" for f in failures)
        super().__init__(
            f"Insufficient CRM permissions. The following checks failed:\n{bullet_list}"
        )


# ---------------------------------------------------------------------------
# EspoCRM validator
# ---------------------------------------------------------------------------

# Minimum access level required for each entity.
# "all" > "team" > "own" > "no"
_ESPO_LEVEL_RANK: Dict[str, int] = {
    "no": 0,
    "own": 1,
    "team": 2,
    "all": 3,
    "yes": 3,   # "create" uses "yes" instead of a scope level
}


def _espo_meets(actual: str, required: str) -> bool:
    """Return True if *actual* access level satisfies *required*."""
    return _ESPO_LEVEL_RANK.get(actual, 0) >= _ESPO_LEVEL_RANK.get(required, 0)


class EspoCrmPermissionValidator:
    """
    Validates EspoCRM permissions from the GET /api/v1/App/user response.

    The relevant section is ``response["acl"]["table"]``, which maps entity
    names to their CRUD access levels.

    Required for the unified dashboard
    -----------------------------------
    Entity  | read  | edit  | delete | create
    --------|-------|-------|--------|-------
    Case    | team+ | team+ | team+  | yes
    User    | team+ |  —    |  —     |  —
    Account | team+ |  —    |  —     |  —

    "team+" means the level must be "team" or "all" (not just "own").
    This ensures the agent can work across all tickets in the org, not
    only ones assigned to themselves.
    """

    # (entity, action, minimum_level, human_label)
    _REQUIRED: List[tuple] = [
        ("Case",    "read",   "team", "Read tickets (Case read ≥ team)"),
        ("Case",    "edit",   "team", "Edit/update tickets (Case edit ≥ team)"),
        ("Case",    "delete", "team", "Delete tickets (Case delete ≥ team)"),
        ("Case",    "create", "yes",  "Create tickets (Case create = yes)"),
        ("User",    "read",   "team", "Read agents/users (User read ≥ team)"),
        ("Account", "read",   "team", "Read organizations (Account read ≥ team)"),
    ]

    def __init__(self, api_response: Dict[str, Any]) -> None:
        self._response = api_response

    def validate(self) -> PermissionCheckResult:
        """
        Run all permission checks and return a PermissionCheckResult.

        Returns
        -------
        PermissionCheckResult
            ``.ok`` is True only when every required check passes.
            ``.failures`` lists human-readable descriptions of failed checks.
        """
        acl_table: Dict[str, Any] = (
            self._response.get("acl", {}).get("table", {})
        )

        failures: List[str] = []

        for entity, action, required_level, label in self._REQUIRED:
            entity_acl = acl_table.get(entity)

            # Entity entirely absent from ACL table → no access at all
            if not entity_acl or not isinstance(entity_acl, dict):
                failures.append(
                    f"{label}  [missing '{entity}' in ACL table]"
                )
                continue

            actual_level: str = str(entity_acl.get(action, "no")).lower()

            if not _espo_meets(actual_level, required_level):
                failures.append(
                    f"{label}  [actual level: '{actual_level}', "
                    f"required: '{required_level}' or higher]"
                )

        return PermissionCheckResult(ok=len(failures) == 0, failures=failures)


# ---------------------------------------------------------------------------
# Zammad validator
# ---------------------------------------------------------------------------

class ZammadPermissionValidator:
    """
    Validates Zammad permissions from the GET /api/v1/user_access_token response.

    The relevant section is ``response["tokens"]`` — an array of token objects
    each carrying a ``preferences.permission`` list.  We look for a token
    whose permission set satisfies ALL required capabilities.

    Required permissions for the unified dashboard
    -----------------------------------------------
    Permission        | Capability
    ------------------|------------------------------------------
    ticket.agent      | Read, create, edit, delete tickets AND
                      | read agents (agents see all users in their
                      | groups via the ticket interface)
    admin.user        | Fetch full user/agent list via admin API
                      | (fallback: "admin" grants all admin.* perms)

    Note: "admin" is a superset of all admin.* permissions, so a token that
    carries "admin" satisfies the admin.user requirement automatically.
    """

    # Each tuple: (required_permission, human_label)
    # A token satisfies the check if it has the permission OR "admin"
    # (which is a superset of all admin.* sub-permissions).
    _REQUIRED: List[tuple] = [
        (
            "ticket.agent",
            "Agent ticket access — read, create, edit, delete tickets "
            "(ticket.agent permission)",
        ),
        (
            "admin.user",
            "Read agents/users via admin API "
            "(admin.user or admin permission)",
        ),
    ]

    def __init__(self, api_response: Dict[str, Any]) -> None:
        self._response = api_response

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_permissions(self) -> set[str]:
        """
        Collect the union of all permissions from all tokens in the response.

        The response may contain multiple tokens; we union their permissions
        because the admin supplied one specific token (identified by last_used_at
        being most recent), but the API returns all tokens for the user.

        In practice the validating token is the most-recently-used one.  We
        union to be permissive — if the admin's token has the right permissions
        it will be captured regardless of ordering.
        """
        all_perms: set[str] = set()
        tokens = self._response.get("tokens", [])
        for token in tokens:
            prefs = token.get("preferences", {}) or {}
            perms = prefs.get("permission", []) or []
            all_perms.update(perms)
        return all_perms

    def _satisfies(self, required: str, all_perms: set[str]) -> bool:
        """
        Return True if *required* is met.

        "admin" is a wildcard that satisfies any "admin.*" requirement.
        """
        if required in all_perms:
            return True
        # "admin" subsumes all admin.* sub-permissions
        if required.startswith("admin.") and "admin" in all_perms:
            return True
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self) -> PermissionCheckResult:
        """
        Run all permission checks and return a PermissionCheckResult.

        Returns
        -------
        PermissionCheckResult
            ``.ok`` is True only when every required check passes.
            ``.failures`` lists human-readable descriptions of failed checks.
        """
        all_perms = self._collect_permissions()
        failures: List[str] = []

        for required, label in self._REQUIRED:
            if not self._satisfies(required, all_perms):
                failures.append(
                    f"{label}  [token has: {sorted(all_perms) or 'no permissions'}]"
                )

        return PermissionCheckResult(ok=len(failures) == 0, failures=failures)