#!/usr/bin/env python3
"""
scripts/inspect_infisical_sdk.py
=================================
Inspects the installed infisical-python SDK and prints the exact
parameter names accepted by each Options class.

Run this FIRST before using the manager:
    python scripts/inspect_infisical_sdk.py

This tells us whether the SDK uses snake_case or camelCase params.
"""
import sys
import inspect

print("\n🔍  Infisical SDK Parameter Inspector\n")

try:
    import infisical_client
    print(f"✅  infisical_client found")
    print(f"    Location : {infisical_client.__file__}")

    # Try to get version
    try:
        print(f"    Version  : {infisical_client.__version__}")
    except AttributeError:
        print(f"    Version  : (no __version__ attr)")
except ImportError:
    print("❌  infisical_client not installed. Run: pip install infisical-python")
    sys.exit(1)

print()

# Classes to inspect
classes_to_check = [
    "GetSecretOptions",
    "CreateSecretOptions",
    "UpdateSecretOptions",
    "DeleteSecretOptions",
    "ClientSettings",
    "AuthenticationOptions",
    "UniversalAuthMethod",
    "InfisicalClient",
]

for class_name in classes_to_check:
    cls = None

    # Try top-level import first
    cls = getattr(infisical_client, class_name, None)

    # Try models submodule
    if cls is None:
        try:
            from infisical_client import models
            cls = getattr(models, class_name, None)
        except ImportError:
            pass

    if cls is None:
        print(f"  ⚠️  {class_name:<30} — NOT FOUND")
        continue

    try:
        sig = inspect.signature(cls.__init__)
        params = [
            p for p in sig.parameters.keys()
            if p != "self"
        ]
        print(f"  ✅  {class_name:<30} → params: {params}")
    except Exception as exc:
        print(f"  ❓  {class_name:<30} — could not inspect: {exc}")

print()
print("─" * 60)
print("  Copy the param names above into manager.py")
print("─" * 60)
print()