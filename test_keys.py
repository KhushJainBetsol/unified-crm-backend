import os
from dotenv import load_dotenv

# Try importing the v2 SDK
try:
    from infisical_client import InfisicalClient, ClientSettings, AuthenticationOptions, UniversalAuthMethod, GetSecretOptions
except ImportError:
    from infisical_client import InfisicalClient, ClientSettings
    from infisical_client.models import AuthenticationOptions, UniversalAuthMethod, GetSecretOptions

load_dotenv()

CLIENT_ID = os.getenv("INFISICAL_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("INFISICAL_CLIENT_SECRET", "").strip()
PROJECT_ID = os.getenv("INFISICAL_PROJECT_ID", "").strip()
HOST = os.getenv("INFISICAL_HOST", "http://192.168.80.229:6002").strip()

print(f"🔌 Connecting to {HOST}...")
print(f"📁 Project ID: {PROJECT_ID}")

# 1. Connect
client = InfisicalClient(
    settings=ClientSettings(
        auth=AuthenticationOptions(
            universal_auth=UniversalAuthMethod(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET
            )
        ),
        site_url=HOST
    )
)

print("✅ Auth successful. Brute-forcing the secret location...\n")

# 2. Brute force combinations
environments_to_try = ["dev", "development", "Development", "prod"]
paths_to_try = ["/", "", "/app"]

found = False

for env in environments_to_try:
    for path in paths_to_try:
        try:
            secret = client.getSecret(
                options=GetSecretOptions(
                    project_id=PROJECT_ID,
                    environment=env,
                    path=path,
                    secret_name="ACTIVE_KEY_VERSION",
                    type="shared" # Explicitly specifying shared
                )
            )
            val = getattr(secret, "secretValue", None)
            if val:
                print("🎉 SUCCESS! WE FOUND IT!")
                print("──────────────────────────────────────────")
                print(f"👉 The EXACT Environment Slug is : '{env}'")
                print(f"👉 The EXACT Path is           : '{path}'")
                print(f"👉 The Secret Value is         : '{val}'")
                print("──────────────────────────────────────────")
                print("Update your .env file to match these exactly!")
                found = True
                break
        except Exception:
            # Failed this combination, moving to next silently
            pass
    if found:
        break

if not found:
    print("❌ All combinations failed.")
    print("Check 1: Are you absolutely sure the secret is named ACTIVE_KEY_VERSION with NO spaces at the end?")
    print("Check 2: Is the Machine Identity (Access Control) added to THIS specific project?")