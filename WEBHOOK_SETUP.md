# Webhook Testing Guide - EspoCRM & Zammad

**Quick Reference:** Test webhooks end-to-end with EspoCRM and Zammad in ~30 minutes.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Key Concepts](#key-concepts)
3. [Setup: Create CrmIntegration Records](#setup-create-crmintegration-records)
4. [Testing: EspoCRM](#testing-espocrm)
5. [Testing: Zammad](#testing-zammad)
6. [Verification](#verification)
7. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

### How Webhooks Work

```
┌────────────────────────────────────────────────────────────────┐
│                    Your Backend                                │
│                                                                │
│  POST /webhooks/ingest/{webhook_uuid}                          │
│                        ↓                                        │
│  Router looks up CrmIntegration by webhook_uuid                │
│                        ↓                                        │
│  Finds: source_system, webhook_secrets, credentials             │
│                        ↓                                        │
│  Verifies HMAC signature                                       │
│                        ↓                                        │
│  Parses payload (EspoCRM or Zammad format)                     │
│                        ↓                                        │
│  Processes: Normalize ticket → Resolve IDs → Save to DB        │
│                        ↓                                        │
│  Returns: HTTP 200 (always, even if errors)                    │
└────────────────────────────────────────────────────────────────┘


Database: crm_integrations table
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│ Integration 1                   Integration 2                   │
│ id=UUID_A                       id=UUID_C                       │
│ webhook_uuid=UUID_A             webhook_uuid=UUID_D             │
│ source_system=espocrm           source_system=zammad            │
│ webhook_secrets_enc=...         webhook_secrets_enc=...         │
│ credential_enc=...              credential_enc=...              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

URL mapping:
http://app:8000/webhooks/ingest/UUID_A  → Routes to Integration 1 (EspoCRM)
http://app:8000/webhooks/ingest/UUID_D  → Routes to Integration 2 (Zammad)
```

---

## Key Concepts

### What is webhook_uuid?

- **Unique identifier** for routing webhooks to the correct CRM integration
- **Auto-generated** when CrmIntegration is created (no manual generation needed!)
- **In URL path:** `/webhooks/ingest/{webhook_uuid}`
- **Different for each CRM:** Each CRM gets its own unique UUID and URL

### Why Different UUIDs for Each CRM?

✅ **Security:** Each CRM's secrets are isolated  
✅ **Routing:** Clear URL → Integration mapping  
✅ **Independence:** Enable/disable per CRM without affecting others  
✅ **Flexibility:** Different credentials/configs per CRM  

### Important: Webhook URL Format

```
✅ CORRECT:
http://localhost:8000/webhooks/ingest/550e8400-e29b-41d4-a716-446655440000
                      ^^^^^^              ^^^^^^^
                      prefix              webhook_uuid

✅ ALSO CORRECT (in production):
https://api.example.com/webhooks/ingest/550e8400-e29b-41d4-a716-446655440000

❌ WRONG:
http://localhost:8000/webhooks/550e8400-...         ← Missing /ingest/
```

---

## Setup: Create CrmIntegration Records

### Step 1: Create Database Records

Connect to PostgreSQL and create two CrmIntegration records (one for each CRM):

```sql
-- For EspoCRM
INSERT INTO crm_integrations (
  tenant_id,
  source_system_id,
  auth_type,
  base_url,
  is_active,
  created_at,
  updated_at
)
VALUES (
  'YOUR_TENANT_UUID',              -- Must match your tenant
  1,                               -- 1 = espocrm
  'api_key',                       -- For EspoCRM
  'https://espo.example.com',      -- EspoCRM URL
  true,
  NOW(),
  NOW()
);

-- For Zammad
INSERT INTO crm_integrations (
  tenant_id,
  source_system_id,
  auth_type,
  base_url,
  is_active,
  created_at,
  updated_at
)
VALUES (
  'YOUR_TENANT_UUID',              -- Same tenant
  2,                               -- 2 = zammad
  'bearer_token',                  -- For Zammad
  'https://zammad.example.com',    -- Zammad URL
  true,
  NOW(),
  NOW()
);
```

### Step 2: Get Your Auto-Generated webhook_uuids

```sql
-- Get both webhook_uuids
SELECT 
  webhook_uuid,
  source_system_id,
  s.system_name,
  created_at
FROM crm_integrations ci
JOIN source_systems s ON ci.source_system_id = s.id
WHERE tenant_id = 'YOUR_TENANT_UUID'
ORDER BY created_at DESC;
```

**Example Output:**
```
webhook_uuid                          | source_system_id | system_name | created_at
─────────────────────────────────────────────────────────────────────────────────
550e8400-e29b-41d4-a716-446655440000 | 1                | espocrm     | 2026-04-30
660e8400-e29b-41d4-a716-446655440111 | 2                | zammad      | 2026-04-30
```

### Step 3: Your Webhook URLs

```
EspoCRM Webhook URL:
http://localhost:8000/webhooks/ingest/550e8400-e29b-41d4-a716-446655440000

Zammad Webhook URL:
http://localhost:8000/webhooks/ingest/660e8400-e29b-41d4-a716-446655440111
```

**Save these URLs!** You'll use them in the next steps.

---

## Testing: EspoCRM

### Step 1: Add Webhook to EspoCRM Admin

1. **Log in to EspoCRM** as administrator
2. **Navigate:** Administration → Integrations → Webhooks
3. **Click:** "+ New Webhook"
4. **Fill in these fields:**

```
Event           → Case.create (or choose: Case.create, Case.update, Case.delete)
URL             → http://localhost:8000/webhooks/ingest/550e8400-e29b-41d4-a716-446655440000
                  (Use YOUR webhook_uuid from Step 2 above)
Active          → ✓ Checked
```

5. **Click:** Save
6. **Result:** ✅ Webhook created in EspoCRM

### Step 2: Test by Creating a Case

#### Method A: Using EspoCRM UI (Auto-Trigger Webhook)

1. **Go to:** CRM → Cases
2. **Click:** "+ New Case"
3. **Fill in:**
   - Name: `Test Case - UI`
   - Status: `Open`
   - Priority: `High`
4. **Click:** Save
5. **Expected:** Webhook auto-triggers → POST to your app

#### Method B: Using curl (Manual Webhook Test)

```bash
# Save your UUID to environment variable
ESPO_UUID="550e8400-e29b-41d4-a716-446655440000"

# Test EspoCRM Case.create webhook
curl -X POST \
  -H "Content-Type: application/json" \
  http://localhost:8000/webhooks/ingest/$ESPO_UUID \
  -d '{
    "event": "Case.create",
    "data": {
      "id": "case_test_001",
      "name": "Test Case from curl",
      "description": "Testing webhook functionality",
      "status": "open",
      "priority": "high",
      "createdAt": "'$(date -Iseconds)'",
      "modifiedAt": "'$(date -Iseconds)'"
    }
  }'
```

**Expected Response:**
```json
{
  "status": "accepted"
}
```

### Step 3: Verify in Database

```bash
# Check if ticket was created
psql -c "
  SELECT 
    crm_ticket_id,
    title,
    source_system_id,
    created_at
  FROM tickets
  WHERE crm_ticket_id = 'case_test_001'
  ORDER BY created_at DESC
  LIMIT 1;
"
```

**Expected Result:**
```
crm_ticket_id | title                 | source_system_id | created_at
──────────────┼───────────────────────┼──────────────────┼────────────────────────
case_test_001 | Test Case from curl   | 1                | 2026-04-30 10:05:00+00
```

✅ **If you see the ticket, it worked!**

---

## Testing: Zammad

### Step 1: Add Webhook to Zammad Admin

1. **Log in to Zammad** as administrator
2. **Navigate:** Admin → System → Webhooks
3. **Click:** "+ Add webhook"
4. **Fill in these fields:**

```
Name              → CRM Backend
Endpoint          → http://localhost:8000/webhooks/ingest/660e8400-e29b-41d4-a716-446655440111
                    (Use YOUR webhook_uuid from Step 2 above)
Active            → ✓ Checked
Events            → Check these:
                    ✓ Ticket create
                    ✓ Ticket update
                    ✓ Ticket delete
```

5. **Click:** Save
6. **Result:** ✅ Webhook created in Zammad

### Step 2: Test by Creating a Ticket

#### Method A: Using Zammad UI (Auto-Trigger Webhook)

1. **Go to:** Tickets
2. **Click:** "+ New Ticket"
3. **Fill in:**
   - Title: `Test Ticket - UI`
   - State: `open`
   - Priority: `2 normal`
4. **Click:** Save
5. **Expected:** Webhook auto-triggers → POST to your app

#### Method B: Using curl (Manual Webhook Test)

```bash
# Save your UUID to environment variable
ZAMMAD_UUID="660e8400-e29b-41d4-a716-446655440111"

# Test Zammad ticket create webhook
curl -X POST \
  -H "Content-Type: application/json" \
  http://localhost:8000/webhooks/ingest/$ZAMMAD_UUID \
  -d '{
    "event": "create",
    "ticket": {
      "id": "ticket_test_001",
      "title": "Test Ticket from curl",
      "description": "Testing webhook functionality",
      "state": "open",
      "priority": "2 normal",
      "created_at": "'$(date -Iseconds)'",
      "updated_at": "'$(date -Iseconds)'"
    }
  }'
```

**Expected Response:**
```json
{
  "status": "accepted"
}
```

### Step 3: Verify in Database

```bash
# Check if ticket was created
psql -c "
  SELECT 
    crm_ticket_id,
    title,
    source_system_id,
    created_at
  FROM tickets
  WHERE crm_ticket_id = 'ticket_test_001'
  ORDER BY created_at DESC
  LIMIT 1;
"
```

**Expected Result:**
```
crm_ticket_id   | title                 | source_system_id | created_at
────────────────┼───────────────────────┼──────────────────┼────────────────────────
ticket_test_001 | Test Ticket from curl | 2                | 2026-04-30 10:06:00+00
```

✅ **If you see the ticket, it worked!**

---

## Verification

### Verify Both Webhooks Are Working

After testing both CRMs, query the database:

```sql
-- Check all tickets from both CRMs
SELECT 
  crm_ticket_id,
  title,
  source_system_id,
  s.system_name,
  created_at
FROM tickets t
JOIN source_systems s ON t.source_system_id = s.id
WHERE created_at > NOW() - INTERVAL '1 hour'
ORDER BY created_at DESC;
```

**Expected Result:**
```
crm_ticket_id   | title                 | source_system_id | system_name | created_at
────────────────┼───────────────────────┼──────────────────┼─────────────┼────────────────────────
case_test_001   | Test Case from curl   | 1                | espocrm     | 2026-04-30 10:05:00+00
ticket_test_001 | Test Ticket from curl | 2                | zammad      | 2026-04-30 10:06:00+00
```

✅ **2 tickets from 2 different CRMs = Success!**

### Check Logs

```bash
# View webhook processing logs
tail -50 /var/log/app.log | grep -i webhook

# You should see entries like:
# Webhook accepted | webhook_uuid=550e8400-... | source=espocrm | event=Case.create | records=1
# Webhook accepted | webhook_uuid=660e8400-... | source=zammad | event=create | records=1
```

### Verify Each CRM Has Own URL

```bash
# Confirm different URLs route to different integrations
curl -v http://localhost:8000/webhooks/ingest/550e8400-e29b-41d4-a716-446655440000

# Should get same response (200) but route to EspoCRM integration
curl -v http://localhost:8000/webhooks/ingest/660e8400-e29b-41d4-a716-446655440111

# Should get same response (200) but route to Zammad integration
```

---

## Troubleshooting

### Problem: HTTP 400 - Invalid webhook identifier

**Cause:** UUID not found in database or integration is inactive

**Solution:**
1. Verify UUID exists: 
   ```sql
   SELECT webhook_uuid FROM crm_integrations LIMIT 1;
   ```
2. Verify active: 
   ```sql
   SELECT is_active FROM crm_integrations WHERE webhook_uuid = 'YOUR_UUID';
   ```
3. Check if URL has typo: `/webhooks/ingest/` (not `/webhooks/`)

---

### Problem: HTTP 200 but no ticket in database

**Cause:** Webhook accepted but processing failed

**Solution:**
1. Check logs for errors:
   ```bash
   tail -100 /var/log/app.log | grep -E "error|failed|exception"
   ```
2. Look for specific webhook logs:
   ```bash
   tail -100 /var/log/app.log | grep webhook_uuid=YOUR_UUID
   ```
3. Common issues:
   - Status/Priority not found (resolve in database first)
   - Credential encryption issues
   - Payload format mismatch

---

### Problem: No logs appearing

**Cause:** App not running or logs not configured

**Solution:**
1. Check if app is running:
   ```bash
   ps aux | grep python
   ```
2. Restart app if needed:
   ```bash
   # Kill old process
   pkill -f "python main.py"
   # Restart
   python main.py
   ```
3. Verify log path exists:
   ```bash
   ls -la /var/log/app.log
   ```

---

### Problem: Different UUIDs for each CRM - is this correct?

✅ **YES! This is the correct design.**

- Each CrmIntegration gets **unique auto-generated webhook_uuid**
- Each CRM sends to **different URL** (one per UUID)
- Router uses UUID to **route to correct integration**
- Each integration has **isolated secrets and credentials**

This is security best practice!

---

## Complete Testing Workflow (Copy-Paste)

### All-in-One Bash Script

```bash
#!/bin/bash
set -e

echo "=== Webhook Testing Setup ==="

# 1. Get your UUIDs
echo -e "\n1️⃣  Getting webhook UUIDs..."
psql -c "
  SELECT 
    webhook_uuid as UUID,
    s.system_name as CRM,
    'Use URL: http://localhost:8000/webhooks/ingest/'||webhook_uuid as 'Webhook URL'
  FROM crm_integrations ci
  JOIN source_systems s ON ci.source_system_id = s.id
  ORDER BY created_at DESC;
"

# 2. Save UUIDs for testing
ESPO_UUID=$(psql -t -c "SELECT webhook_uuid FROM crm_integrations ci 
  JOIN source_systems s ON ci.source_system_id = s.id 
  WHERE s.system_name = 'espocrm' LIMIT 1;")
ZAMMAD_UUID=$(psql -t -c "SELECT webhook_uuid FROM crm_integrations ci 
  JOIN source_systems s ON ci.source_system_id = s.id 
  WHERE s.system_name = 'zammad' LIMIT 1;")

echo -e "\nESPO_UUID: $ESPO_UUID"
echo -e "ZAMMAD_UUID: $ZAMMAD_UUID"

# 3. Test EspoCRM
echo -e "\n2️⃣  Testing EspoCRM webhook..."
curl -s -X POST \
  -H "Content-Type: application/json" \
  http://localhost:8000/webhooks/ingest/$ESPO_UUID \
  -d '{
    "event": "Case.create",
    "data": {
      "id": "case_test_'$(date +%s)'",
      "name": "Auto Test Case",
      "status": "open",
      "priority": "high"
    }
  }' | jq .

# 4. Test Zammad
echo -e "\n3️⃣  Testing Zammad webhook..."
curl -s -X POST \
  -H "Content-Type: application/json" \
  http://localhost:8000/webhooks/ingest/$ZAMMAD_UUID \
  -d '{
    "event": "create",
    "ticket": {
      "id": "ticket_test_'$(date +%s)'",
      "title": "Auto Test Ticket",
      "state": "open"
    }
  }' | jq .

# 5. Show results
echo -e "\n4️⃣  Checking database..."
psql -c "
  SELECT 
    crm_ticket_id,
    title,
    s.system_name,
    created_at
  FROM tickets t
  JOIN source_systems s ON t.source_system_id = s.id
  WHERE created_at > NOW() - INTERVAL '5 minutes'
  ORDER BY created_at DESC;
"

echo -e "\n✅ Testing complete!"
```

Save as `test_webhooks.sh`:
```bash
chmod +x test_webhooks.sh
./test_webhooks.sh
```

---

## Summary: Key Points

| Point | Answer |
|-------|--------|
| **Does UUID auto-generate?** | ✅ YES - `default=uuid.uuid4` in model |
| **Is different UUID per CRM correct?** | ✅ YES - Each gets unique UUID & URL |
| **What URL to send to CRM?** | `http://app:8000/webhooks/ingest/{YOUR_UUID}` |
| **Can I set same UUID for both?** | ❌ NO - Each integration auto-generates unique |
| **Are different URLs allowed?** | ✅ YES - Each CRM posts to different URL |

---

## Next Steps

1. ✅ Created CrmIntegration records
2. ✅ Got auto-generated webhook_uuids
3. ✅ Added URLs to EspoCRM
4. ✅ Added URLs to Zammad
5. ✅ Tested with curl or UI
6. ✅ Verified in database
7. ✅ Checked logs

**Everything working? Webhooks are ready for production!** 🎉

---

**Questions?** Check the Troubleshooting section above or review the webhook implementation in `app/integrations/webhooks/`
