# Frontend Integration Service Updates

## Required Changes to `integrationService.js`

This guide shows what needs to be added to your integration service to support the new webhook URL features.

---

## 1. Add Webhook URL Query Hook

Add this new hook to fetch the webhook URL for an existing integration:

```javascript
/**
 * useGetWebhookUrl
 * Fetches the webhook URL for an existing integration
 * 
 * GET /api/v1/integrations/{integration_id}/webhook-url
 * 
 * Returns:
 * {
 *   integration_id: UUID,
 *   webhook_uuid: UUID,
 *   webhook_url: string,
 *   crm_type: string,
 *   instructions: string
 * }
 */
export function useGetWebhookUrl(integrationId, options = {}) {
  return useQuery({
    queryKey: ['webhook-url', integrationId],
    queryFn: async () => {
      if (!integrationId) throw new Error('Integration ID is required');
      
      const response = await fetch(
        `/api/v1/integrations/${integrationId}/webhook-url`,
        {
          method: 'GET',
          headers: {
            'Authorization': `Bearer ${getAuthToken()}`,
            'Content-Type': 'application/json',
          },
        }
      );

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to fetch webhook URL');
      }

      return response.json();
    },
    staleTime: 1000 * 60 * 5, // 5 minutes
    ...options,
  });
}
```

---

## 2. Update Provision Response Handling

Update your `useProvisionIntegration` mutation to extract webhook data:

```javascript
export function useProvisionIntegration(options = {}) {
  return useMutation({
    mutationFn: async (payload) => {
      const response = await fetch('/api/v1/integrations/', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${getAuthToken()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const error = await response.json();
        const err = new Error(error.detail || 'Provision failed');
        err.failedChecks = error.failed_checks;
        err.status = response.status;
        throw err;
      }

      const data = await response.json();
      
      // ✨ NEW: Ensure webhook data is included in response
      return {
        integration_id: data.integration_id,
        crm_type: data.crm_type,
        auth_type: data.auth_type,
        base_url: data.base_url,
        webhook_uuid: data.webhook_uuid,           // ← NEW
        webhook_url: data.webhook_url,             // ← NEW (computed property)
        instructions: data.instructions,           // ← NEW (if available)
        is_active: data.is_active,
        has_credentials: data.has_credentials,
        has_webhook_secrets: data.has_webhook_secrets,
        created_at: data.created_at,
        updated_at: data.updated_at,
      };
    },
    ...options,
  });
}
```

---

## 3. Update Success Response in Form

When the provision response is received, the component now expects:

```javascript
// In PageConfigureCrm onSuccess handler:
const provisionMutation = useProvisionIntegration({
  onSuccess: (responseData) => {
    // responseData now includes:
    // - webhook_uuid: string (UUID)
    // - webhook_url: string (full URL to register in CRM)
    // - instructions: string (CRM-specific setup instructions)
    
    setSuccessData(responseData);
    setTimeout(() => { 
      onSuccess?.(responseData); 
      onBack(); 
    }, 1500);
  },
});
```

---

## 4. Complete Updated integrationService.js Section

```javascript
import { useQuery, useMutation } from '@tanstack/react-query';

// ─────────────────────────────────────────────────────────────────────────────
// HELPER: Get Auth Token
// ─────────────────────────────────────────────────────────────────────────────

function getAuthToken() {
  // Adjust based on your auth implementation
  return localStorage.getItem('authToken') || '';
}

// ─────────────────────────────────────────────────────────────────────────────
// QUERIES
// ─────────────────────────────────────────────────────────────────────────────

/**
 * useCrmConfigs
 * Fetches available CRM configurations
 * GET /api/v1/config/crms
 */
export function useCrmConfigs() {
  return useQuery({
    queryKey: ['crm-configs'],
    queryFn: async () => {
      const response = await fetch('/api/v1/config/crms', {
        headers: { 'Authorization': `Bearer ${getAuthToken()}` },
      });
      if (!response.ok) throw new Error('Failed to load CRM configs');
      return response.json();
    },
    staleTime: 1000 * 60 * 30, // 30 minutes
  });
}

/**
 * useActiveIntegrations
 * Fetches list of active integrations for current tenant
 * GET /tenant-source-systems/active?tenant_id={uuid}
 */
export function useActiveIntegrations(tenantId) {
  return useQuery({
    queryKey: ['active-integrations', tenantId],
    queryFn: async () => {
      const response = await fetch(
        `/tenant-source-systems/active?tenant_id=${tenantId}`,
        { headers: { 'Authorization': `Bearer ${getAuthToken()}` } }
      );
      if (!response.ok) throw new Error('Failed to load active integrations');
      return response.json();
    },
    enabled: !!tenantId,
    staleTime: 1000 * 60 * 5, // 5 minutes
  });
}

/**
 * useIntegrationStatus
 * Fetches status of a specific integration
 * GET /api/v1/integrations/{integration_id}/credentials/status
 */
export function useIntegrationStatus(integrationId, options = {}) {
  return useQuery({
    queryKey: ['integration-status', integrationId],
    queryFn: async () => {
      const response = await fetch(
        `/api/v1/integrations/${integrationId}/credentials/status`,
        { headers: { 'Authorization': `Bearer ${getAuthToken()}` } }
      );
      if (!response.ok) throw new Error('Failed to load integration status');
      return response.json();
    },
    enabled: !!integrationId,
    staleTime: 1000 * 60 * 2, // 2 minutes
    ...options,
  });
}

/**
 * useGetWebhookUrl  ← NEW
 * Fetches webhook URL and setup instructions for an integration
 * GET /api/v1/integrations/{integration_id}/webhook-url
 */
export function useGetWebhookUrl(integrationId, options = {}) {
  return useQuery({
    queryKey: ['webhook-url', integrationId],
    queryFn: async () => {
      if (!integrationId) throw new Error('Integration ID is required');
      
      const response = await fetch(
        `/api/v1/integrations/${integrationId}/webhook-url`,
        { headers: { 'Authorization': `Bearer ${getAuthToken()}` } }
      );

      if (!response.ok) throw new Error('Failed to fetch webhook URL');
      return response.json();
    },
    staleTime: 1000 * 60 * 5, // 5 minutes
    ...options,
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// MUTATIONS
// ─────────────────────────────────────────────────────────────────────────────

/**
 * useProvisionIntegration
 * Creates a new CRM integration with credentials
 * POST /api/v1/integrations/
 */
export function useProvisionIntegration(options = {}) {
  return useMutation({
    mutationFn: async (payload) => {
      const response = await fetch('/api/v1/integrations/', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${getAuthToken()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const error = await response.json();
        const err = new Error(error.detail || 'Provision failed');
        err.failedChecks = error.failed_checks;
        err.status = response.status;
        throw err;
      }

      const data = await response.json();
      
      // ✨ Include webhook data in response
      return {
        integration_id: data.integration_id,
        crm_type: data.crm_type,
        auth_type: data.auth_type,
        base_url: data.base_url,
        key_version: data.key_version,
        webhook_uuid: data.webhook_uuid,     // ← NEW
        webhook_url: data.webhook_url,       // ← NEW (computed property from response)
        is_active: data.is_active,
        has_credentials: data.has_credentials,
        has_webhook_secrets: data.has_webhook_secrets,
        created_at: data.created_at,
        updated_at: data.updated_at,
      };
    },
    ...options,
  });
}

/**
 * useUpdateCredentials
 * Updates credentials for an existing integration
 * PATCH /api/v1/integrations/{integration_id}/credentials
 */
export function useUpdateCredentials(options = {}) {
  return useMutation({
    mutationFn: async ({ integrationId, payload }) => {
      const response = await fetch(
        `/api/v1/integrations/${integrationId}/credentials`,
        {
          method: 'PATCH',
          headers: {
            'Authorization': `Bearer ${getAuthToken()}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(payload),
        }
      );

      if (!response.ok) {
        const error = await response.json();
        const err = new Error(error.detail || 'Update failed');
        err.failedChecks = error.failed_checks;
        err.status = response.status;
        throw err;
      }

      const data = await response.json();
      return {
        integration_id: data.integration_id,
        crm_type: data.crm_type,
        auth_type: data.auth_type,
        base_url: data.base_url,
        webhook_uuid: data.webhook_uuid,     // ← NEW
        webhook_url: data.webhook_url,       // ← NEW
        is_active: data.is_active,
        has_credentials: data.has_credentials,
        has_webhook_secrets: data.has_webhook_secrets,
      };
    },
    ...options,
  });
}

/**
 * useTestConnection
 * Tests connection to a CRM
 * POST /api/v1/integrations/check-connection
 */
export function useTestConnection(options = {}) {
  return useMutation({
    mutationFn: async (payload) => {
      const response = await fetch('/api/v1/integrations/check-connection', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${getAuthToken()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const error = await response.json();
        const err = new Error(error.detail || 'Connection test failed');
        err.failedChecks = error.failed_checks;
        err.status = response.status;
        throw err;
      }

      return response.json();
    },
    ...options,
  });
}

/**
 * useDeprovisionIntegration
 * Removes/disables an integration
 * DELETE /api/v1/integrations/{integration_id}/credentials?wipe=true
 */
export function useDeprovisionIntegration(options = {}) {
  return useMutation({
    mutationFn: async ({ integrationId, wipe = false }) => {
      const response = await fetch(
        `/api/v1/integrations/${integrationId}/credentials?wipe=${wipe}`,
        {
          method: 'DELETE',
          headers: { 'Authorization': `Bearer ${getAuthToken()}` },
        }
      );

      if (!response.ok) throw new Error('Deprovision failed');
      return response.json();
    },
    ...options,
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// PAYLOAD TRANSFORMATION
// ─────────────────────────────────────────────────────────────────────────────

/**
 * transformFormToPayload
 * Transforms form data into API payload format
 */
export function transformFormToPayload(formData) {
  const {
    crm_type,
    base_url,
    auth_type,
    cred_token,
    cred_username,
    cred_password,
    cred_access_token,
    cred_refresh_token,
    cred_token_type,
    cred_expires_at,
    cred_client_id,
    cred_client_secret,
    enable_webhooks,
    webhook_secret,
    per_event_secrets,
    _webhookModel,
  } = formData;

  // Build credentials based on auth_type
  let credentials = {};

  if (['api_token', 'bearer_token', 'access_token', 'api_key', 'hmac'].includes(auth_type)) {
    credentials = { auth_type, token: cred_token };
  } else if (auth_type === 'basic_auth') {
    credentials = { auth_type, username: cred_username, password: cred_password };
  } else if (auth_type === 'oauth2') {
    credentials = {
      auth_type,
      access_token: cred_access_token,
      refresh_token: cred_refresh_token,
      token_type: cred_token_type,
      expires_at: cred_expires_at ? parseInt(cred_expires_at) : null,
      client_id: cred_client_id,
      client_secret: cred_client_secret,
    };
  }

  const payload = {
    crm_type,
    base_url,
    credentials,
  };

  // Add webhook secrets if enabled
  if (enable_webhooks) {
    if (_webhookModel === 'shared' && webhook_secret) {
      payload.webhook_secret = webhook_secret;
    } else if (_webhookModel === 'per_event' && per_event_secrets?.length > 0) {
      payload.per_event_secrets = per_event_secrets.reduce((acc, item) => {
        acc[item.event] = item.secret;
        return acc;
      }, {});
    }
  }

  return payload;
}
```

---

## 5. Usage in ProvisionCredentialsForm Component

The component now uses the webhook data like this:

```javascript
// After successful provision:
const provisionMutation = useProvisionIntegration({
  onSuccess: (responseData) => {
    // responseData.webhook_uuid → displayed in WebhookDisplay component
    // responseData.webhook_url → used in copy-to-clipboard
    // responseData.instructions → shown in help box
    
    setSuccessData(responseData);
    setTimeout(() => { onSuccess?.(responseData); onBack(); }, 1500);
  },
});

// For existing integrations:
const { data: webhookUrlData } = useGetWebhookUrl(integrationId, {
  enabled: !!integrationId && isLocked,
});

// Display webhook URL:
{isLocked && webhookUrlData && (
  <WebhookDisplay
    webhookData={webhookUrlData}
    crmDisplayName={crmConfig.display_name}
  />
)}
```

---

## 6. Response Structure

The backend now returns this in the provision response:

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
  "created_at": "2024-04-30T10:00:00Z",
  "updated_at": "2024-04-30T10:00:00Z"
}
```

And when fetching the webhook URL via GET /webhook-url:

```json
{
  "integration_id": "550e8400-e29b-41d4-a716-446655440000",
  "webhook_uuid": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "webhook_url": "http://localhost:8000/webhooks/ingest/6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "crm_type": "espocrm",
  "instructions": "In EspoCRM admin panel:\n1. Go to Admin → Integrations → Webhooks\n2. Create a new webhook with URL: ..."
}
```

---

## 7. Summary of Changes

| Change | File | What It Does |
|--------|------|-------------|
| Add `useGetWebhookUrl` hook | integrationService.js | Query webhook URL for existing integrations |
| Update provision response | integrationService.js | Include webhook_uuid and webhook_url |
| Add WebhookDisplay component | ProvisionCredentialsForm.jsx | Show webhook URL with copy button |
| Update CopyableField | ProvisionCredentialsForm.jsx | Use actual webhook_url from backend |
| Add webhook display after success | ProvisionCredentialsForm.jsx | Show webhook setup instructions |
| Fetch webhook URL for locked integration | ProvisionCredentialsForm.jsx | Display URL when viewing existing integration |

---

## 8. Testing Checklist

After making these changes, test:

- [ ] Provision new EspoCRM integration
- [ ] Verify webhook_uuid appears in success response
- [ ] Copy webhook URL button works
- [ ] CRM-specific instructions display
- [ ] Go back and return to same integration
- [ ] Webhook URL loads from GET /webhook-url endpoint
- [ ] Provision new Zammad integration
- [ ] Verify Zammad-specific instructions display
- [ ] Test webhook URL copy functionality
- [ ] Verify URL format is correct: `http://localhost:8000/webhooks/ingest/{webhook_uuid}`

---

## Next: Register Webhooks in CRMs

Once the frontend displays the webhook URL, the admin should:

1. **EspoCRM:**
   - Copy the webhook URL
   - Go to Admin → Integrations → Webhooks
   - Create new webhook with the URL
   - Set events: Case.create, Case.update, Case.delete
   - Save

2. **Zammad:**
   - Copy the webhook URL
   - Go to Admin → Webhooks
   - Create new webhook with the URL
   - Set events: ticket.create, ticket.update
   - Save

See [WEBHOOK_TESTING_COMPLETE_GUIDE.md](./WEBHOOK_TESTING_COMPLETE_GUIDE.md) for full testing instructions.
