# """
# app/integrations/normalizer/config/loader.py

# Loads and caches CRM mapping config from TOML files.

# Why TOML:
#   - Python 3.11+ has tomllib in the standard library (no extra package)
#   - Human-readable, supports comments, ideal for config files
#   - Mappings can be changed without touching any Python code

# Usage:
#     from app.integrations.normalizer.config.loader import get_zammad_mappings, get_espo_mappings

#     cfg = get_zammad_mappings()
#     status = cfg.status.get("open", cfg.fallback_status)
#     priority = cfg.priority_id.get("2", cfg.fallback_priority)
# """

# from __future__ import annotations

# import sys
# from dataclasses import dataclass, field
# from functools import lru_cache
# from pathlib import Path

# # tomllib is built-in from Python 3.11
# # For Python 3.9 / 3.10 install: pip install tomli
# if sys.version_info >= (3, 11):
#     import tomllib
# else:
#     try:
#         import tomllib  # type: ignore[no-redef]
#     except ImportError:
#         import tomli as tomllib  # type: ignore[no-redef]  # pip install tomli


# CONFIG_DIR = Path(__file__).parent


# @dataclass
# class ZammadMappings:
#     status:        dict[str, str]
#     priority_name: dict[str, str]  # "2 normal" → "normal"
#     priority_id:   dict[int, str]  # 2 → "normal"
#     fallback_status:   str
#     fallback_priority: str | None


# @dataclass
# class EspoMappings:
#     status:            dict[str, str]
#     priority:          dict[str, str]
#     fallback_status:   str
#     fallback_priority: str | None


# @lru_cache(maxsize=1)
# def get_zammad_mappings() -> ZammadMappings:
#     """
#     Load and cache Zammad mappings from zammad_mappings.toml.
#     Cached — file is read once per process.
#     """
#     path = CONFIG_DIR / "zammad_mappings.toml"
#     with open(path, "rb") as f:
#         raw = tomllib.load(f)

#     # priority_id keys are strings in TOML — convert to int for lookup
#     priority_id = {
#         int(k): v
#         for k, v in raw.get("priority_id", {}).items()
#     }

#     fallback_priority = raw["fallbacks"].get("priority") or None

#     return ZammadMappings(
#         status=raw.get("status", {}),
#         priority_name=raw.get("priority_name", {}),
#         priority_id=priority_id,
#         fallback_status=raw["fallbacks"].get("status", "open"),
#         fallback_priority=fallback_priority,
#     )


# @lru_cache(maxsize=1)
# def get_espo_mappings() -> EspoMappings:
#     """
#     Load and cache EspoCRM mappings from espo_mappings.toml.
#     Cached — file is read once per process.
#     """
#     path = CONFIG_DIR / "espo_mappings.toml"
#     with open(path, "rb") as f:
#         raw = tomllib.load(f)

#     fallback_priority = raw["fallbacks"].get("priority") or None

#     return EspoMappings(
#         status=raw.get("status", {}),
#         priority=raw.get("priority", {}),
#         fallback_status=raw["fallbacks"].get("status", "open"),
#         fallback_priority=fallback_priority,
#     )
