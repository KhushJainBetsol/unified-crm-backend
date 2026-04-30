#!/usr/bin/env python3
"""
Test webhook_url integration across schema, service, and routes.

This test verifies:
1. CredentialStatusResponse includes webhook_uuid and webhook_url property
2. WebhookUrlResponse schema is properly defined
3. GET /api/v1/integrations/{integration_id}/webhook-url endpoint is registered
4. webhook_url is computed correctly with WEBHOOK_BASE_URL
"""

import asyncio
import uuid
from datetime import datetime
from uuid import UUID

# Import schemas
from app.schemas.credentials import CredentialStatusResponse, WebhookUrlResponse

# Import models
from app.models.crm_integration import CrmIntegration

# Test 1: CredentialStatusResponse schema includes webhook_uuid
def test_credential_status_response_schema():
    """Test that CredentialStatusResponse has webhook_uuid and webhook_url property."""
    print("\n[Test 1] CredentialStatusResponse schema validation")
    
    test_webhook_uuid = uuid.uuid4()
    
    # Create a mock response
    response = CredentialStatusResponse(
        integration_id=uuid.uuid4(),
        crm_type="espocrm",
        auth_type="api_key",
        base_url="https://crm.example.com",
        key_version="v1",
        is_active=True,
        has_credentials=True,
        has_webhook_secrets=True,
        webhook_uuid=test_webhook_uuid,
        token_expires_at=None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    
    # Verify webhook_uuid is set
    assert response.webhook_uuid == test_webhook_uuid, "webhook_uuid not set correctly"
    print(f"  ✓ webhook_uuid: {response.webhook_uuid}")
    
    # Verify webhook_url property is computed
    webhook_url = response.webhook_url
    assert webhook_url is not None, "webhook_url is None"
    assert str(test_webhook_uuid) in webhook_url, "webhook_uuid not in webhook_url"
    assert "/webhooks/ingest/" in webhook_url, "webhook path not in webhook_url"
    print(f"  ✓ webhook_url: {webhook_url}")
    
    # Verify webhook_url format
    expected_pattern = f"/webhooks/ingest/{test_webhook_uuid}"
    assert expected_pattern in webhook_url, f"Expected pattern '{expected_pattern}' not found in '{webhook_url}'"
    print(f"  ✓ webhook_url matches expected pattern")


# Test 2: WebhookUrlResponse schema
def test_webhook_url_response_schema():
    """Test that WebhookUrlResponse schema is properly defined."""
    print("\n[Test 2] WebhookUrlResponse schema validation")
    
    webhook_uuid = uuid.uuid4()
    webhook_url = f"http://localhost:8000/webhooks/ingest/{webhook_uuid}"
    
    response = WebhookUrlResponse(
        integration_id=uuid.uuid4(),
        webhook_uuid=webhook_uuid,
        webhook_url=webhook_url,
        crm_type="espocrm",
        instructions="Test instructions for webhook setup",
    )
    
    assert response.webhook_uuid == webhook_uuid, "webhook_uuid not set"
    assert response.webhook_url == webhook_url, "webhook_url not set"
    assert response.crm_type == "espocrm", "crm_type not set"
    print(f"  ✓ WebhookUrlResponse created successfully")
    print(f"    - integration_id: {response.integration_id}")
    print(f"    - webhook_uuid: {response.webhook_uuid}")
    print(f"    - webhook_url: {response.webhook_url}")
    print(f"    - crm_type: {response.crm_type}")


# Test 3: Router endpoints are registered
def test_router_endpoints():
    """Test that the new webhook-url endpoint is registered."""
    print("\n[Test 3] Router endpoints registration")
    
    from app.routes.credentials import router
    
    endpoints = [
        (route.path, [method for method in route.methods if method != "OPTIONS"])
        for route in router.routes
    ]
    
    print(f"  ✓ Found {len(endpoints)} endpoints:")
    for path, methods in endpoints:
        print(f"    - {methods} {path}")
    
    # Check for webhook-url endpoint
    webhook_url_endpoint = any(
        "webhook-url" in path for path, _ in endpoints
    )
    assert webhook_url_endpoint, "webhook-url endpoint not registered"
    print(f"  ✓ GET /{{integration_id}}/webhook-url endpoint registered")


# Test 4: Mock service flow
def test_mock_service_flow():
    """Test a mock flow of credential provisioning returning webhook_url."""
    print("\n[Test 4] Mock service flow")
    
    # Simulate what _to_status function does
    test_integration_id = uuid.uuid4()
    test_webhook_uuid = uuid.uuid4()
    
    # This simulates the response that would be returned after provisioning
    response = CredentialStatusResponse(
        integration_id=test_integration_id,
        crm_type="zammad",
        auth_type="api_token",
        base_url="https://zammad.example.com",
        key_version="v1",
        is_active=True,
        has_credentials=True,
        has_webhook_secrets=True,
        webhook_uuid=test_webhook_uuid,
        token_expires_at=None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    
    print(f"  ✓ Mock provision response created")
    print(f"    - integration_id: {response.integration_id}")
    print(f"    - webhook_uuid: {response.webhook_uuid}")
    print(f"    - webhook_url: {response.webhook_url}")
    
    # Verify admin would get the webhook_url in the response
    assert hasattr(response, 'webhook_url'), "Response missing webhook_url property"
    print(f"  ✓ Admin can access webhook_url immediately after provisioning")


# Test 5: Different CRM types have correct webhook_url
def test_webhook_url_by_crm_type():
    """Test that webhook_url is correct for different CRM types."""
    print("\n[Test 5] webhook_url for different CRM types")
    
    crm_types = ["espocrm", "zammad", "salesforce"]
    
    for crm_type in crm_types:
        webhook_uuid = uuid.uuid4()
        response = CredentialStatusResponse(
            integration_id=uuid.uuid4(),
            crm_type=crm_type,
            auth_type="api_key",
            base_url=f"https://{crm_type}.example.com",
            key_version="v1",
            is_active=True,
            has_credentials=True,
            has_webhook_secrets=False,
            webhook_uuid=webhook_uuid,
            token_expires_at=None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        
        webhook_url = response.webhook_url
        assert str(webhook_uuid) in webhook_url, f"webhook_uuid not in URL for {crm_type}"
        print(f"  ✓ {crm_type.upper()}: {webhook_url}")


if __name__ == "__main__":
    print("=" * 80)
    print("Webhook URL Integration Tests")
    print("=" * 80)
    
    try:
        test_credential_status_response_schema()
        test_webhook_url_response_schema()
        test_router_endpoints()
        test_mock_service_flow()
        test_webhook_url_by_crm_type()
        
        print("\n" + "=" * 80)
        print("✓ All tests passed!")
        print("=" * 80)
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
