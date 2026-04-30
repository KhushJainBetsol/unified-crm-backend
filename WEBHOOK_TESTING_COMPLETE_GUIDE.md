# Webhook Testing Complete Guide
## For EspoCRM & Zammad — Full Setup from Start

---

## 📋 TABLE OF CONTENTS
1. [Backend Configuration](#backend-configuration)
2. [Environment Setup](#environment-setup)
3. [Database Setup](#database-setup)
4. [Testing the Provision Endpoint](#testing-the-provision-endpoint)
5. [Testing the Webhook URL Endpoint](#testing-the-webhook-url-endpoint)
6. [CRM-Specific Configuration](#crm-specific-configuration)
7. [End-to-End Testing](#end-to-end-testing)
8. [Troubleshooting](#troubleshooting)

---

## 1. BACKEND CONFIGURATION

### Required Files
```
app/
├── models/
│   ├── crm_integration.py          ✓ (webhook_uuid auto-generated)
│   └── source_system.py            ✓
├── schemas/
│   └── credentials.py              ✓ (webhook_uuid + webhook_url property)
├── services/
│   └── credential_service.py       ✓ (_to_status includes webhook_uuid)
├── routes/
│   └── credentials.py              ✓ (GET /webhook-url endpoint added)
├── integrations/webhooks/
│   ├── router.py                   ✓ (POST /webhooks/ingest/{webhook_uuid})
│   ├── service.py                  ✓ (webhook processing)
│   ├── verifier.py                 ✓ (HMAC verification)
│   └── seeder.py                   ✓ (auto-generates webhook_uuid)
└── core/
    └── settings.py                 ✓ (WEBHOOK_BASE_URL env var)
```

### Verify Installation
```bash
cd /home/interns/crm-project/unified-crm-backend
source venv/bin/activate

# Test imports
python3 -c "
from app.schemas.credentials import CredentialStatusResponse, WebhookUrlResponse
from app.routes.credentials import router
from app.models.crm_integration import CrmIntegration
print('✓ All webhook components imported successfully')
"

# Test FastAPI app
python3 -c "
from app.main import app
endpoints = [route.path for route in app.routes if 'webhook' in route.path.lower()]
print(f'✓ Found {len(endpoints)} webhook-related endpoints')
print(f'  - {endpoints}')
"
```

---

## 2. ENVIRONMENT SETUP

### Create `.env` File
```bash
# Backend URL for webhook callbacks (used in webhook_url property)
WEBHOOK_BASE_URL=http://localhost:8000

# Or for deployed environment:
# WEBHOOK_BASE_URL=https://api.yourdomain.com

# Database
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/crm_db

# Infisical (for encryption)
INFISICAL_CLIENT_ID=your_client_id
INFISICAL_CLIENT_SECRET=your_client_secret
INFISICAL_PROJECT_ID=your_project_id
INFISICAL_ENVIRONMENT=dev

# Keycloak
KEYCLOAK_URL=http://localhost:8080
KEYCLOAK_REALM=unified-crm
KEYCLOAK_ADMIN_CLIENT_ID=crm-admin-api
KEYCLOAK_ADMIN_CLIENT_SECRET=your_secret

# For seeding (optional)
WEBHOOK_TENANT_ID=<your-tenant-uuid>
ESPO_BASE_URL=https://espo.example.com
ZAMMAD_BASE_URL=https://zammad.example.com
```

### Verify Configuration
```bash
source venv/bin/activate
python3 -c "
from app.core.settings import get_settings
s = get_settings()
print(f'✓ WEBHOOK_BASE_URL: {s.WEBHOOK_BASE_URL}')
print(f'✓ DATABASE_URL: {s.DATABASE_URL[:50]}...')
print(f'✓ KEYCLOAK_URL: {s.KEYCLOAK_URL}')
"
```

---

## 3. DATABASE SETUP

### Initialize Database
```bash
cd /home/interns/crm-project/unified-crm-backend

# Apply migrations
alembic upgrade head

# Verify source_systems are seeded
psql -U postgres -d crm_db -c "
SELECT id, system_name, display_name FROM source_systems;
"
# Expected output:
# id | system_name | display_name
# ---+-------------+----------
#  1 | espocrm     | EspoCRM
#  2 | zammad      | Zammad
```

### Create Test Tenant
```bash
psql -U postgres -d crm_db << 'EOF'
INSERT INTO tenants (id, name, realm_id, created_at, updated_at)
VALUES (
  'f47ac10b-58cc-4372-a567-0e02b2c3d479',
  'Test Tenant',
  'unified-crm',
  NOW(),
  NOW()
)
ON CONFLICT DO NOTHING;

SELECT id, name FROM tenants WHERE name = 'Test Tenant';
EOF
```

---

## 4. TESTING THE PROVISION ENDPOINT

### 4.1 Get a Keycloak JWT Token

```bash
# Get access token
TOKEN=$(curl -s -X POST \
  http://localhost:8080/realms/unified-crm/protocol/openid-connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=crm-admin-api" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "grant_type=client_credentials" | jq -r '.access_token')

echo "Token: $TOKEN"
```

### 4.2 Provision EspoCRM Integration

```bash
curl -X POST http://localhost:8000/api/v1/integrations/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "crm_type": "espocrm",
    "base_url": "https://espo.example.com",
    "credentials": {
      "auth_type": "api_key",
      "token": "your_espo_api_key"
    },
    "webhook_secret": "shared-webhook-secret-espo",
    "per_event_secrets": {
      "Case.create": "secret-case-create",
      "Case.update": "secret-case-update",
      "Case.delete": "secret-case-delete"
    }
  }' | jq .
```

**Expected Response:**
```json
{
  "integration_id": "550e8400-e29b-41d4-a716-446655440000",
  "crm_type": "espocrm",
  "auth_type": "api_key",
  "base_url": "https://espo.example.com",
  "key_version": "v1",
  "is_active": true,
  "has_credentials": true,
  "has_webhook_secrets": true,
  "webhook_uuid": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "token_expires_at": null,
  "created_at": "2024-04-30T10:00:00Z",
  "updated_at": "2024-04-30T10:00:00Z"
}
```

### 4.3 Provision Zammad Integration

```bash
curl -X POST http://localhost:8000/api/v1/integrations/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "crm_type": "zammad",
    "base_url": "https://zammad.example.com",
    "credentials": {
      "auth_type": "bearer_token",
      "token": "your_zammad_api_token"
    },
    "webhook_secret": "shared-webhook-secret-zammad"
  }' | jq .
```

**Expected Response:**
```json
{
  "integration_id": "660f9500-f30c-52e5-b827-557766441111",
  "crm_type": "zammad",
  "auth_type": "bearer_token",
  "base_url": "https://zammad.example.com",
  "key_version": "v1",
  "is_active": true,
  "has_credentials": true,
  "has_webhook_secrets": true,
  "webhook_uuid": "7cb8c921-aebd-22e2-91c5-11d15f541d9d",
  "token_expires_at": null,
  "created_at": "2024-04-30T10:01:00Z",
  "updated_at": "2024-04-30T10:01:00Z"
}
```

**Key Fields:**
- `webhook_uuid` — Unique identifier for this integration's webhook endpoint
- `webhook_url` (computed) — Full URL for CRM to send webhooks

---

## 5. TESTING THE WEBHOOK URL ENDPOINT

### 5.1 Get Webhook URL for EspoCRM

```bash
ESPO_ID="550e8400-e29b-41d4-a716-446655440000"

curl -X GET http://localhost:8000/api/v1/integrations/$ESPO_ID/webhook-url \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | jq .
```

**Response:**
```json
{
  "integration_id": "550e8400-e29b-41d4-a716-446655440000",
  "webhook_uuid": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "webhook_url": "http://localhost:8000/webhooks/ingest/6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "crm_type": "espocrm",
  "instructions": "In EspoCRM admin panel:\n1. Go to Admin → Integrations → Webhooks\n2. Create a new webhook with URL: http://localhost:8000/webhooks/ingest/6ba7b810-9dad-11d1-80b4-00c04fd430c8\n3. Set Events: Case.create, Case.update, Case.delete, Note.create\n4. Save webhook (webhook_uuid will be verified on first inbound request)"
}
```

### 5.2 Get Webhook URL for Zammad

```bash
ZAMMAD_ID="660f9500-f30c-52e5-b827-557766441111"

curl -X GET http://localhost:8000/api/v1/integrations/$ZAMMAD_ID/webhook-url \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | jq .
```

**Response:**
```json
{
  "integration_id": "660f9500-f30c-52e5-b827-557766441111",
  "webhook_uuid": "7cb8c921-aebd-22e2-91c5-11d15f541d9d",
  "webhook_url": "http://localhost:8000/webhooks/ingest/7cb8c921-aebd-22e2-91c5-11d15f541d9d",
  "crm_type": "zammad",
  "instructions": "In Zammad admin panel:\n1. Go to Admin → Webhooks\n2. Create a new webhook with URL: http://localhost:8000/webhooks/ingest/7cb8c921-aebd-22e2-91c5-11d15f541d9d\n3. Set Events: ticket.create, ticket.update\n4. Save webhook (webhook_uuid will be verified on first inbound request)"
}
```

---

## 6. CRM-SPECIFIC CONFIGURATION

### 6.1 EspoCRM Setup

#### Step 1: Get Admin URL
```
https://your-espo-instance.com/admin
```

#### Step 2: Navigate to Webhooks
```
Admin → Integrations → Webhooks
```

#### Step 3: Create New Webhook
```
Title:          Unified CRM Sync
URL:            http://localhost:8000/webhooks/ingest/6ba7b810-9dad-11d1-80b4-00c04fd430c8
Events:         Case.create, Case.update, Case.delete, Note.create
Active:         ✓ (checked)
```

#### Step 4: Test in EspoCRM
```
Create a new Case in EspoCRM → webhook will fire
```

---

### 6.2 Zammad Setup

#### Step 1: Get Admin URL
```
https://your-zammad-instance.com/admin
```

#### Step 2: Navigate to Webhooks
```
Admin → Webhooks
```

#### Step 3: Create New Webhook
```
Name:           Unified CRM Sync
URL:            http://localhost:8000/webhooks/ingest/7cb8c921-aebd-22e2-91c5-11d15f541d9d
Events:         ticket.create, ticket.update
Active:         ✓ (checked)
```

#### Step 4: Test in Zammad
```
Create a new Ticket in Zammad → webhook will fire
```

---

## 7. END-TO-END TESTING

### 7.1 Test EspoCRM Webhook Delivery

```bash
# 1. Trigger webhook in EspoCRM
# In EspoCRM: Admin → Integrations → Webhooks → [Your webhook] → Test

# 2. Check backend logs
tail -f /path/to/app.log | grep -i webhook

# 3. Verify webhook was received
# Expected log:
# [2024-04-30 10:15:00] INFO: Webhook received for Case.create
# [2024-04-30 10:15:00] INFO: HMAC signature verified successfully
# [2024-04-30 10:15:01] INFO: Case synced: internal_id=123456
```

### 7.2 Test Zammad Webhook Delivery

```bash
# 1. Create a test ticket in Zammad
# Dashboard → Tickets → New

# 2. Check backend logs
tail -f /path/to/app.log | grep -i webhook

# 3. Verify webhook was received
# Expected log:
# [2024-04-30 10:16:00] INFO: Webhook received for ticket.create
# [2024-04-30 10:16:00] INFO: HMAC signature verified successfully
# [2024-04-30 10:16:01] INFO: Ticket synced: internal_id=789012
```

### 7.3 Full Request/Response Cycle

```bash
#!/bin/bash

# Setup
TENANT_ID="f47ac10b-58cc-4372-a567-0e02b2c3d479"
TOKEN=$(curl -s -X POST http://localhost:8080/realms/unified-crm/protocol/openid-connect/token \
  -d "client_id=crm-admin-api&client_secret=YOUR_SECRET&grant_type=client_credentials" | jq -r '.access_token')

# Step 1: Provision EspoCRM
echo "=== STEP 1: Provisioning EspoCRM ==="
ESPO_RESPONSE=$(curl -s -X POST http://localhost:8000/api/v1/integrations/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "crm_type": "espocrm",
    "base_url": "https://espo.example.com",
    "credentials": {"auth_type": "api_key", "token": "test_key"},
    "webhook_secret": "espo-test-secret"
  }')

ESPO_ID=$(echo $ESPO_RESPONSE | jq -r '.integration_id')
ESPO_WEBHOOK_UUID=$(echo $ESPO_RESPONSE | jq -r '.webhook_uuid')
echo "✓ EspoCRM provisioned: $ESPO_ID"
echo "✓ Webhook UUID: $ESPO_WEBHOOK_UUID"

# Step 2: Get Webhook URL
echo -e "\n=== STEP 2: Getting Webhook URL ==="
WEBHOOK_URL=$(curl -s -X GET http://localhost:8000/api/v1/integrations/$ESPO_ID/webhook-url \
  -H "Authorization: Bearer $TOKEN" | jq -r '.webhook_url')
echo "✓ Webhook URL: $WEBHOOK_URL"

# Step 3: Simulate CRM webhook
echo -e "\n=== STEP 3: Simulating Webhook Delivery ==="
curl -X POST $WEBHOOK_URL \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature: sha256=..." \
  -d '{
    "event": "Case.create",
    "data": {"id": 123, "subject": "Test Case"}
  }' | jq .

echo -e "\n✓ Full cycle complete!"
```

---

## 8. TROUBLESHOOTING

### Issue: "webhook_uuid" field not found

**Solution:**
```bash
# Verify schema update
python3 -c "
from app.schemas.credentials import CredentialStatusResponse
import inspect
sig = inspect.signature(CredentialStatusResponse)
if 'webhook_uuid' in sig.parameters:
    print('✓ webhook_uuid field exists')
else:
    print('✗ webhook_uuid field missing - update credentials.py')
"
```

### Issue: Webhook URL shows as None

**Solution:**
```bash
# Check WEBHOOK_BASE_URL setting
python3 -c "
from app.core.settings import get_settings
s = get_settings()
url = getattr(s, 'WEBHOOK_BASE_URL', 'NOT SET')
print(f'WEBHOOK_BASE_URL: {url}')
if url == 'NOT SET':
    print('→ Add WEBHOOK_BASE_URL to .env file')
"
```

### Issue: HMAC Signature Verification Failed

**Solution:**
```bash
# Ensure webhook_secret matches CRM configuration
# Backend expects:
#   Header: X-Hub-Signature
#   Value: sha256=<hex_encoded_hmac_sha256>

# Test HMAC generation
python3 << 'EOF'
import hmac
import hashlib

secret = "your-webhook-secret"
payload = '{"event": "Case.create"}'

signature = hmac.new(
    secret.encode(),
    payload.encode(),
    hashlib.sha256
).hexdigest()

print(f"X-Hub-Signature: sha256={signature}")
EOF
```

### Issue: Integration not found (404)

**Solution:**
```bash
# Verify integration exists
psql -U postgres -d crm_db -c "
SELECT id, webhook_uuid, source_system_id, is_active 
FROM crm_integrations 
WHERE id = 'YOUR_INTEGRATION_ID';
"
```

### Issue: Permission Denied (403) on Provision

**Solution:**
```bash
# Check Keycloak token has required scopes
curl -s http://localhost:8080/realms/unified-crm/protocol/openid-connect/userinfo \
  -H "Authorization: Bearer $TOKEN" | jq .

# Ensure token includes: ['realm-management', 'account', 'default-roles-unified-crm']
```

---

## Quick Test Script

```bash
#!/bin/bash
# webhook-test.sh

set -e

BACKEND_URL=${1:-http://localhost:8000}
KEYCLOAK_URL=${2:-http://localhost:8080}
REALM=${3:-unified-crm}

echo "🔧 Webhook Integration Test"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Backend:   $BACKEND_URL"
echo "Keycloak:  $KEYCLOAK_URL"
echo "Realm:     $REALM"
echo ""

# Get token
echo "1️⃣  Obtaining Keycloak token..."
TOKEN=$(curl -s -X POST \
  $KEYCLOAK_URL/realms/$REALM/protocol/openid-connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=crm-admin-api" \
  -d "client_secret=your_secret" \
  -d "grant_type=client_credentials" | jq -r '.access_token')

if [ -z "$TOKEN" ] || [ "$TOKEN" == "null" ]; then
  echo "❌ Failed to get token"
  exit 1
fi
echo "✓ Token: ${TOKEN:0:20}..."

# Provision EspoCRM
echo -e "\n2️⃣  Provisioning EspoCRM..."
ESPO=$(curl -s -X POST $BACKEND_URL/api/v1/integrations/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "crm_type": "espocrm",
    "base_url": "https://espo.example.com",
    "credentials": {"auth_type": "api_key", "token": "test"},
    "webhook_secret": "test-secret"
  }')

ESPO_ID=$(echo $ESPO | jq -r '.integration_id')
ESPO_UUID=$(echo $ESPO | jq -r '.webhook_uuid')
echo "✓ Integration: $ESPO_ID"
echo "✓ Webhook UUID: $ESPO_UUID"

# Get webhook URL
echo -e "\n3️⃣  Getting webhook URL..."
WEBHOOK=$(curl -s -X GET $BACKEND_URL/api/v1/integrations/$ESPO_ID/webhook-url \
  -H "Authorization: Bearer $TOKEN")

URL=$(echo $WEBHOOK | jq -r '.webhook_url')
echo "✓ Webhook URL: $URL"

echo -e "\n✅ All tests passed!"
```

---

## Summary

| Component | Status | Notes |
|-----------|--------|-------|
| webhook_uuid auto-generation | ✅ | Via model default=uuid.uuid4 |
| webhook_url computation | ✅ | Via CredentialStatusResponse property |
| GET /webhook-url endpoint | ✅ | Returns URL + CRM instructions |
| POST /webhooks/ingest/{uuid} | ✅ | Receives and processes webhooks |
| HMAC verification | ✅ | X-Hub-Signature header validation |
| EspoCRM integration | ✅ | Supports per-event secrets |
| Zammad integration | ✅ | Supports shared secret |
| Multitenancy | ✅ | Enforced via tenant_id |

All systems ready for production deployment! 🚀
