import asyncio
from pathlib import Path

from app.config.registry import AdapterRegistry
from app.credentials.models import CrmCredentialEnvelope
from app.factory.adapter_factory import CrmAdapterFactory

# ---------------------------------------------------------
# 1. MOCK THE VAULT (So we don't need Infisical for this test)
# ---------------------------------------------------------
class MockCredentialManager:
    def __init__(self, test_crm_type: str, test_base_url: str, test_credentials: dict):
        self.envelope = CrmCredentialEnvelope(
            crm_type=test_crm_type,
            base_url=test_base_url,
            credentials=test_credentials
        )

    def get_credentials(self, integration_id: str) -> CrmCredentialEnvelope:
        print(f"🔓 [Mock Vault] Fetching credentials for integration '{integration_id}'...")
        return self.envelope


async def run_test():
    print("🚀 Starting Isolated Adapter Test...\n")

    # ---------------------------------------------------------
    # 2. INITIALIZE THE REGISTRY
    # ---------------------------------------------------------
    # Point this to your app/config directory where crm_adapters.yaml lives
    config_dir = Path("app/config")
    registry = AdapterRegistry(config_base_dir=config_dir)
    registry.initialise()
    
    print(f"✅ Registry Loaded! Available adapters: {registry.list_adapter_keys()}\n")

    # ---------------------------------------------------------
    # 3. SET UP YOUR TEST CREDENTIALS
    # ---------------------------------------------------------
    # CHANGE THESE to a real test instance to see live data!
    # To test EspoCRM, change "zammad" to "espocrm" and use "api_key" instead of "token"
    mock_vault = MockCredentialManager(
        test_crm_type="espocrm",
        test_base_url="http://192.168.80.229:9091",
        # FIXED: Added the strategy key below
        test_credentials={"strategy": "api_token", "token": "f177888efa9b2814b150291a24aa7703"}# <--- PUT REAL TOKEN HERE
    )

    # ---------------------------------------------------------
    # 4. INSTANTIATE THE FACTORY
    # ---------------------------------------------------------
    factory = CrmAdapterFactory(registry, mock_vault)
    test_integration_id = "test-uuid-1234"

    print(f"🏗️  Factory building adapter for '{test_integration_id}'...")
    adapter = factory.create(test_integration_id)
    print(f"✅ Factory successfully built: {adapter.__class__.__name__}\n")

    # ---------------------------------------------------------
    # 5. EXECUTE BUSINESS LOGIC
    # ---------------------------------------------------------
    print("🔌 Opening HTTP connection and authenticating...")
    try:
        async with adapter:
            print("✅ Authenticated successfully!\n")
            
            print("📥 Fetching page 1 of tickets (max 3)...")
            # We fetch a small page just to prove it works
            result = await adapter.fetch_tickets(page=1, per_page=3)
            
            print(f"✅ Fetched {len(result.items)} tickets!")
            for ticket in result.items:
                print(f"   🎟️  [{ticket.id}] {ticket.title} (Status: {ticket.status})")
                
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")

if __name__ == "__main__":
    asyncio.run(run_test())