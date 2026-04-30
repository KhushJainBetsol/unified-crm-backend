/**
 * ProvisionCredentialsForm.jsx  ─  v10 · Webhook URL Integration
 * ─────────────────────────────────────────────────────────────────────────────
 * Changes from v9:
 *  • CopyableField now uses actual webhook_url from backend response
 *  • Added WebhookDisplay component showing webhook_uuid and webhook_url
 *  • After successful provision, displays webhook setup instructions
 *  • Added "Retrieve Webhook URL" section for existing integrations
 *  • Webhook URL copy button with clipboard feedback
 *  • CRM-specific setup instructions from backend
 *
 * Dependencies: react-hook-form  zod  @hookform/resolvers/zod  @tanstack/react-query
 */

import { useState, useCallback, useEffect } from 'react';
import { useForm, useFieldArray } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';

import {
  useCrmConfigs,
  useActiveIntegrations,
  useIntegrationStatus,
  useProvisionIntegration,
  useUpdateCredentials,
  useDeprovisionIntegration,
  useTestConnection,
  useGetWebhookUrl,
  transformFormToPayload,
} from '../services/integrationService';
import { useAuth } from '../context/AuthContext';

import './ProvisionCredentialsForm.css';

// ─────────────────────────────────────────────────────────────────────────────
// HARDCODED SOURCE SYSTEM ID → CRM KEY MAP
// TODO: replace with dynamic mapping from CrmInfoSchema.source_system_id
// ─────────────────────────────────────────────────────────────────────────────

const SOURCE_SYSTEM_ID_TO_CRM_KEY = {
  1: 'zammad',
  2: 'espocrm',
};

// ─────────────────────────────────────────────────────────────────────────────
// ZOD SCHEMAS
// ─────────────────────────────────────────────────────────────────────────────

const webhookEventSchema = z.object({
  event:  z.string().min(1, 'Event name is required'),
  secret: z.string().min(1, 'Secret is required'),
});

const TOKEN_LIKE_AUTH_TYPES = ['api_token', 'bearer_token', 'access_token', 'api_key', 'hmac'];
const TOKEN_LIKE_RENDER     = new Set(TOKEN_LIKE_AUTH_TYPES);

function buildCrmEntrySchema(crmConfigs) {
  return z
    .object({
      crm_type:           z.string().min(1),
      base_url:           z.string().min(1, 'Base URL is required').url('Must be a valid URL'),
      auth_type:          z.string().min(1, 'Select an auth method'),
      cred_token:         z.string().optional(),
      cred_username:      z.string().optional(),
      cred_password:      z.string().optional(),
      cred_access_token:  z.string().optional(),
      cred_refresh_token: z.string().optional(),
      cred_token_type:    z.string().optional(),
      cred_expires_at:    z.string().optional(),
      cred_client_id:     z.string().optional(),
      cred_client_secret: z.string().optional(),
      enable_webhooks:    z.boolean().default(false),
      webhook_secret:     z.string().optional(),
      per_event_secrets:  z.array(webhookEventSchema).optional(),
    })
    .superRefine((d, ctx) => {
      if (TOKEN_LIKE_AUTH_TYPES.includes(d.auth_type) && !d.cred_token?.trim()) {
        ctx.addIssue({ code: 'custom', path: ['cred_token'],
          message: d.auth_type === 'hmac' ? 'API Key is required' : 'Token / key is required' });
      }
      if (d.auth_type === 'basic_auth') {
        if (!d.cred_username?.trim())
          ctx.addIssue({ code: 'custom', path: ['cred_username'], message: 'Username is required' });
        if (!d.cred_password?.trim())
          ctx.addIssue({ code: 'custom', path: ['cred_password'], message: 'Password is required' });
      }
      if (d.auth_type === 'oauth2' && !d.cred_access_token?.trim()) {
        ctx.addIssue({ code: 'custom', path: ['cred_access_token'], message: 'Access token is required' });
      }
      if (d.enable_webhooks) {
        const crmCfg = crmConfigs?.find((c) => c.crm_key === d.crm_type);
        if (crmCfg?.webhook_model === 'shared' && !d.webhook_secret?.trim())
          ctx.addIssue({ code: 'custom', path: ['webhook_secret'], message: 'Webhook secret is required' });
        if (crmCfg?.webhook_model === 'per_event' && (!d.per_event_secrets || d.per_event_secrets.length === 0))
          ctx.addIssue({ code: 'custom', path: ['per_event_secrets'], message: 'Add at least one event secret' });
      }
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// DEFAULT ENTRY
// ─────────────────────────────────────────────────────────────────────────────

function makeCrmEntry(crmKey, crmConfigs) {
  const cfg = crmConfigs?.find((c) => c.crm_key === crmKey);
  return {
    crm_type: crmKey, base_url: '',
    auth_type: cfg?.supported_auth_options?.[0]?.value ?? '',
    cred_token: '', cred_username: '', cred_password: '',
    cred_access_token: '', cred_refresh_token: '', cred_token_type: 'Bearer',
    cred_expires_at: '', cred_client_id: '', cred_client_secret: '',
    enable_webhooks: false, webhook_secret: '', per_event_secrets: [],
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// UTILITY COMPONENTS
// ─────────────────────────────────────────────────────────────────────────────

function ErrMsg({ msg }) {
  if (!msg) return null;
  return (
    <span className="pcf-errmsg">
      <svg width="12" height="12" fill="none" viewBox="0 0 16 16">
        <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.5" />
        <path d="M8 5v3.5M8 11h.01" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
      {msg}
    </span>
  );
}

function Field({ label, required, optional, hint, error, children, style }) {
  return (
    <div className="pcf-field" style={style}>
      {label && (
        <label className="pcf-lbl">
          {label}
          {required && <span className="pcf-req">*</span>}
          {optional && <span className="pcf-opt">optional</span>}
        </label>
      )}
      {children}
      {hint && !error && <span className="pcf-hint">{hint}</span>}
      <ErrMsg msg={error} />
    </div>
  );
}

/** Renders a failed_checks array as a structured log — used in error alerts */
function ChecksLog({ checks }) {
  if (!checks || checks.length === 0) return null;
  return (
    <ul className="pcf-checks-log">
      {checks.map((check, idx) => (
        <li key={idx} className="pcf-checks-log-item">
          <svg width="9" height="9" fill="none" viewBox="0 0 14 14" style={{ flexShrink: 0, marginTop: 1 }}>
            <path d="M2 2l10 10M12 2L2 12" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
          </svg>
          {check}
        </li>
      ))}
    </ul>
  );
}

// SVG icons
const BackArrowSVG = () => (
  <svg fill="none" viewBox="0 0 16 16" stroke="currentColor" strokeWidth="2">
    <path d="M10 3l-6 5 6 5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
const PlusSVG = () => (
  <svg width="11" height="11" fill="none" viewBox="0 0 14 14">
    <path d="M7 1v12M1 7h12" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
  </svg>
);
const InfoSVG = () => (
  <svg fill="none" viewBox="0 0 16 16" stroke="currentColor" strokeWidth="1.5">
    <circle cx="8" cy="8" r="7" /><path d="M8 5v4.5M8 12h.01" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
const ChevronSVG = () => (
  <svg fill="none" viewBox="0 0 16 16" stroke="currentColor" strokeWidth="2">
    <path d="M6 5l4 3-4 3" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
const BeakerSVG = () => (
  <svg width="13" height="13" fill="none" viewBox="0 0 20 20" stroke="currentColor" strokeWidth="1.8">
    <path strokeLinecap="round" strokeLinejoin="round" d="M9 3H5a2 2 0 00-2 2v4a2 2 0 00.586 1.414L8 14.828A2 2 0 009.414 15.414L15 9.828A2 2 0 0015.414 8.414L11 4H9V3zM9 3v1M6 9h.01M14 7h.01" />
    <path strokeLinecap="round" strokeLinejoin="round" d="M7 3V2a1 1 0 012 0v1" />
    <path strokeLinecap="round" strokeLinejoin="round" d="M3.5 12c0 2 1.5 4 6.5 4s6.5-2 6.5-4" />
  </svg>
);
const SaveSVG = () => (
  <svg width="13" height="13" fill="none" viewBox="0 0 16 16" stroke="currentColor" strokeWidth="1.8">
    <path strokeLinecap="round" strokeLinejoin="round" d="M13 2H5a1 1 0 00-1 1v10a1 1 0 001 1h8a1 1 0 001-1V2zM10 2v4H6V2M6 9h4" />
  </svg>
);
const ResetSVG = () => (
  <svg width="12" height="12" fill="none" viewBox="0 0 16 16" stroke="currentColor" strokeWidth="1.8">
    <path strokeLinecap="round" strokeLinejoin="round" d="M1.5 4.5A6.5 6.5 0 0114 8M14.5 11.5A6.5 6.5 0 012 8" />
    <path strokeLinecap="round" strokeLinejoin="round" d="M1.5 1.5v3h3M14.5 14.5v-3h-3" />
  </svg>
);

// ─────────────────────────────────────────────────────────────────────────────
// SKELETON LOADER
// ─────────────────────────────────────────────────────────────────────────────

function SkeletonCard() {
  return (
    <div className="pcf-crm-card pcf-crm-card--skeleton" aria-hidden="true">
      <div className="pcf-skeleton pcf-skeleton--name" />
      <div className="pcf-skeleton pcf-skeleton--desc" />
      <div style={{ display: 'flex', gap: 5, marginTop: 10 }}>
        <div className="pcf-skeleton pcf-skeleton--tag" />
        <div className="pcf-skeleton pcf-skeleton--tag" />
      </div>
    </div>
  );
}
function CrmGridSkeleton() {
  return (
    <div className="pcf-crm-grid">
      {[0, 1, 2, 3].map((i) => <SkeletonCard key={i} />)}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// WEBHOOK URL DISPLAY  (NEW COMPONENT)
// ─────────────────────────────────────────────────────────────────────────────

function WebhookDisplay({ webhookData, crmDisplayName }) {
  const [copied, setCopied] = useState(false);
  
  if (!webhookData) return null;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(webhookData.webhook_url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (e) {
      console.error('Failed to copy:', e);
    }
  };

  return (
    <div className="pcf-webhook-display">
      <div className="pcf-webhook-header">
        <div className="pcf-webhook-ico">
          <svg width="14" height="14" fill="none" viewBox="0 0 16 16">
            <path d="M2 12c0-2 1.5-3.5 3.5-4L9 4.5M14 4c0 2-1.5 3.5-3.5 4L7 12"
              stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </div>
        <div>
          <h3 className="pcf-webhook-title">Webhook Configuration</h3>
          <p className="pcf-webhook-subtitle">Use the URL below to configure webhooks in {crmDisplayName}</p>
        </div>
      </div>

      <div className="pcf-webhook-body">
        {/* Webhook URL */}
        <Field label="Webhook Endpoint URL" hint="Copy this URL to your CRM webhook settings">
          <div className="pcf-copyable-wrapper">
            <input type="text" value={webhookData.webhook_url} readOnly 
              className="pcf-input pcf-copyable-input" />
            <button type="button" className={`pcf-copy-btn${copied ? ' pcf-copy-btn--copied' : ''}`}
              onClick={handleCopy} title="Copy to clipboard">
              {copied ? (
                <><svg width="14" height="14" fill="currentColor" viewBox="0 0 16 16">
                  <path d="M13.5 2.5L6 9l-4-4" stroke="currentColor" strokeWidth="2" fill="none"
                    strokeLinecap="round" strokeLinejoin="round" /></svg>Copied!</>
              ) : (
                <><svg width="12" height="12" fill="none" viewBox="0 0 16 16">
                  <path d="M6 1H2a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7M14 1H9v4h5"
                    stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>Copy</>
              )}
            </button>
          </div>
        </Field>

        {/* Webhook UUID Info */}
        <Field label="Webhook UUID" hint="Unique identifier for this integration's webhook endpoint">
          <div className="pcf-info-box">
            <code className="pcf-code">{webhookData.webhook_uuid}</code>
          </div>
        </Field>

        {/* CRM-Specific Instructions */}
        {webhookData.instructions && (
          <HelpBox 
            instructions={webhookData.instructions.split('\n').filter(l => l.trim())}
            title={`${crmDisplayName} Setup Instructions`}
          />
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// COPYABLE FIELD  (UPDATED)
// ─────────────────────────────────────────────────────────────────────────────

function CopyableField({ webhookUrl, label, hint }) {
  const [copied, setCopied] = useState(false);
  
  if (!webhookUrl) {
    return (
      <Field label={label} hint={hint || 'Webhook URL will appear here after saving'}>
        <div className="pcf-copyable-wrapper">
          <input type="text" value="" placeholder="Loading..." readOnly className="pcf-input pcf-copyable-input" />
        </div>
      </Field>
    );
  }

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(webhookUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (e) {
      console.error('Failed to copy:', e);
    }
  };

  return (
    <Field label={label} hint={hint || 'Copy this URL and paste it into your CRM webhook configuration'}>
      <div className="pcf-copyable-wrapper">
        <input type="text" value={webhookUrl} readOnly className="pcf-input pcf-copyable-input" />
        <button type="button" className={`pcf-copy-btn${copied ? ' pcf-copy-btn--copied' : ''}`}
          onClick={handleCopy} title="Copy to clipboard">
          {copied ? (
            <><svg width="14" height="14" fill="currentColor" viewBox="0 0 16 16">
              <path d="M13.5 2.5L6 9l-4-4" stroke="currentColor" strokeWidth="2" fill="none"
                strokeLinecap="round" strokeLinejoin="round" /></svg>Copied!</>
          ) : (
            <><svg width="12" height="12" fill="none" viewBox="0 0 16 16">
              <path d="M6 1H2a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7M14 1H9v4h5"
                stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>Copy</>
          )}
        </button>
      </div>
    </Field>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// HELP BOX
// ─────────────────────────────────────────────────────────────────────────────

function HelpBox({ instructions, title }) {
  const [isOpen, setIsOpen] = useState(false);
  
  // Parse instructions if they're a single string
  const instructionsList = typeof instructions === 'string'
    ? instructions.split('\n').filter(l => l.trim())
    : Array.isArray(instructions) ? instructions : [];

  if (!instructionsList || instructionsList.length === 0) return null;
  
  return (
    <div className="pcf-help">
      <button type="button" className="pcf-help-trigger" onClick={() => setIsOpen(!isOpen)}>
        <div className="pcf-help-ico"><InfoSVG /></div>
        <span className="pcf-help-title">{title}</span>
        <span className={`pcf-help-chevron${isOpen ? ' pcf-help-chevron--open' : ''}`}><ChevronSVG /></span>
      </button>
      {isOpen && (
        <div className="pcf-help-body">
          <ol className="pcf-help-steps">
            {instructionsList.map((step, idx) => (
              <li key={idx} className="pcf-help-step">
                <span className="pcf-help-step-num">{idx + 1}</span>
                <span className="pcf-help-step-text">{step}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// CREDENTIAL FIELD GROUPS
// ─────────────────────────────────────────────────────────────────────────────

function TokenCredFields({ register, errors, authType, disabled }) {
  const err = errors?.cred_token?.message;
  return (
    <Field label={authType === 'hmac' ? 'API Key' : 'API Token / Key'} required error={err}
      hint={authType === 'hmac' ? 'The API Key used to authenticate outbound calls to the CRM' : undefined}>
      <input {...register('cred_token')} type="password" autoComplete="new-password"
        placeholder="••••••••••••••••••••"
        className={`pcf-input pcf-secret${err ? ' has-err' : ''}`} disabled={disabled} />
    </Field>
  );
}

function BasicCredFields({ register, errors, disabled }) {
  return (
    <div className="pcf-g2">
      <Field label="Username" required error={errors?.cred_username?.message}>
        <input {...register('cred_username')} type="text" placeholder="admin_user"
          className={`pcf-input${errors?.cred_username ? ' has-err' : ''}`} disabled={disabled} />
      </Field>
      <Field label="Password" required error={errors?.cred_password?.message}>
        <input {...register('cred_password')} type="password" autoComplete="new-password"
          placeholder="••••••••" className={`pcf-input pcf-secret${errors?.cred_password ? ' has-err' : ''}`}
          disabled={disabled} />
      </Field>
    </div>
  );
}

function OAuth2CredFields({ register, errors, disabled }) {
  return (
    <>
      <Field label="Access Token" required error={errors?.cred_access_token?.message}>
        <input {...register('cred_access_token')} type="password" autoComplete="new-password"
          placeholder="••••••••••••••••••••"
          className={`pcf-input pcf-secret${errors?.cred_access_token ? ' has-err' : ''}`} disabled={disabled} />
      </Field>
      <div className="pcf-g2">
        <Field label="Refresh Token" optional>
          <input {...register('cred_refresh_token')} type="password" autoComplete="new-password"
            placeholder="••••••••••••••" className="pcf-input pcf-secret" disabled={disabled} />
        </Field>
        <Field label="Token Type" optional hint='Default: "Bearer"'>
          <input {...register('cred_token_type')} type="text" placeholder="Bearer" className="pcf-input" disabled={disabled} />
        </Field>
        <Field label="Client ID" optional>
          <input {...register('cred_client_id')} type="text" placeholder="client_id" className="pcf-input" disabled={disabled} />
        </Field>
        <Field label="Client Secret" optional>
          <input {...register('cred_client_secret')} type="password" autoComplete="new-password"
            placeholder="••••••••" className="pcf-input pcf-secret" disabled={disabled} />
        </Field>
      </div>
      <Field label="Expires At" optional hint="Unix timestamp — leave blank for non-expiring tokens"
        error={errors?.cred_expires_at?.message}>
        <input {...register('cred_expires_at')} type="number" placeholder="1893456000" className="pcf-input" disabled={disabled} />
      </Field>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// WEBHOOK COMPONENTS
// ─────────────────────────────────────────────────────────────────────────────

function WebhookShared({ register, errors, webhookUrl, crmDisplayName, webhookInstructions }) {
  return (
    <div className="pcf-wh-box">
      <p className="pcf-wh-box-title">
        <svg width="12" height="12" fill="none" viewBox="0 0 16 16">
          <path d="M2 12c0-2 1.5-3.5 3.5-4L9 4.5M14 4c0 2-1.5 3.5-3.5 4L7 12"
            stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        Webhook — Shared Secret
      </p>
      <CopyableField webhookUrl={webhookUrl} label="Webhook Endpoint URL" />
      <Field label="Shared Webhook Secret" required error={errors?.webhook_secret?.message}>
        <input {...register('webhook_secret')} type="password" autoComplete="new-password"
          placeholder="••••••••••••••••"
          className={`pcf-input pcf-secret${errors?.webhook_secret ? ' has-err' : ''}`} />
      </Field>
      <span className="pcf-hint">Your CRM sends this in a header (e.g., <code>X-Hub-Signature</code>).</span>
      {webhookInstructions?.length > 0 && (
        <div style={{ marginTop: 4 }}>
          <HelpBox instructions={webhookInstructions} title={`${crmDisplayName} Webhook Setup`} />
        </div>
      )}
    </div>
  );
}

function WebhookPerEvent({ register, control, errors, webhookUrl, crmDisplayName, webhookInstructions }) {
  const { fields, append, remove } = useFieldArray({ control, name: 'per_event_secrets' });
  return (
    <div className="pcf-wh-box">
      <p className="pcf-wh-box-title">
        <svg width="12" height="12" fill="none" viewBox="0 0 16 16">
          <path d="M2 12c0-2 1.5-3.5 3.5-4L9 4.5M14 4c0 2-1.5 3.5-3.5 4L7 12"
            stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        Webhooks — Per-Event Secrets
      </p>
      <CopyableField webhookUrl={webhookUrl} label="Webhook Endpoint URL" />
      <span className="pcf-hint">Different secrets per event type. Add one row per event.</span>
      {fields.length > 0 && (
        <div className="pcf-kv-list">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 32px', gap: 8 }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-muted)' }}>Event</span>
            <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-muted)' }}>Secret</span>
            <span />
          </div>
          {fields.map((f, i) => {
            const e = errors?.per_event_secrets?.[i];
            return (
              <div key={f.id} className="pcf-kv-row">
                <input {...register(`per_event_secrets.${i}.event`)} type="text"
                  placeholder="e.g., Lead.create" className={`pcf-input${e?.event ? ' has-err' : ''}`} />
                <input {...register(`per_event_secrets.${i}.secret`)} type="password"
                  autoComplete="new-password" placeholder="secret"
                  className={`pcf-input pcf-secret${e?.secret ? ' has-err' : ''}`} />
                <button type="button" className="pcf-kv-del" onClick={() => remove(i)} aria-label="Remove row">×</button>
              </div>
            );
          })}
        </div>
      )}
      <button type="button" className="pcf-add-row" onClick={() => append({ event: '', secret: '' })}>
        <PlusSVG /> Add Event Secret
      </button>
      <ErrMsg msg={typeof errors?.per_event_secrets?.message === 'string'
        ? errors.per_event_secrets.message : errors?.per_event_secrets?.root?.message} />
      {webhookInstructions?.length > 0 && (
        <div style={{ marginTop: 4 }}>
          <HelpBox instructions={webhookInstructions} title={`${crmDisplayName} Webhook Setup`} />
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// PAGE 1: CRM SELECTION GRID
// ─────────────────────────────────────────────────────────────────────────────

function PageSelectCrm({ crmConfigs, crmStatuses, isLoadingConfigs, configsError, onSelectCrm, onClose, modal }) {
  return (
    <>
      <div className="pcf-hdr">
        <div className="pcf-hdr-l">
          <div className="pcf-ico">
            <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
            </svg>
          </div>
          <div>
            <h2 className="pcf-ttl">CRM Integrations</h2>
            <p className="pcf-sub">Select a CRM system to configure</p>
          </div>
        </div>
        {modal && (
          <button className="pcf-close" type="button" onClick={onClose} aria-label="Close">
            <svg width="13" height="13" fill="none" viewBox="0 0 16 16">
              <path d="M2 2l12 12M14 2L2 14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </button>
        )}
      </div>

      <div className="pcf-body">
        <div className="pcf-sec">
          <p className="pcf-sec-lbl">Available CRM Systems</p>

          {isLoadingConfigs && <CrmGridSkeleton />}

          {configsError && !isLoadingConfigs && (
            <div className="pcf-alert pcf-alert--err">
              <span className="pcf-alert-ico">!</span>
              <div>
                <strong>Failed to load CRM configurations</strong><br />
                <span className="pcf-alert-sub">{configsError.message || 'Please try again.'}</span>
              </div>
            </div>
          )}

          {!isLoadingConfigs && !configsError && crmConfigs?.length === 0 && (
            <div className="pcf-alert pcf-alert--ok">
              <span className="pcf-alert-ico">✓</span>
              <div>
                <strong>No CRM systems available</strong><br />
                <span className="pcf-alert-sub">Contact your administrator to add CRM adapters.</span>
              </div>
            </div>
          )}

          {!isLoadingConfigs && !configsError && crmConfigs?.length > 0 && (
            <div className="pcf-crm-grid">
              {crmConfigs.map((crm) => {
                const statusEntry = crmStatuses?.[crm.crm_key];
                const status = statusEntry?.status ?? statusEntry;
                const cardClass = ['pcf-crm-card',
                  status === 'success' ? 'pcf-crm-card--success' : '',
                  status === 'error'   ? 'pcf-crm-card--error'   : '',
                ].filter(Boolean).join(' ');

                return (
                  <button key={crm.crm_key} type="button" className={cardClass}
                    onClick={() => onSelectCrm(crm.crm_key)}>
                    <div className="pcf-crm-card-body">
                      <div className="pcf-crm-card-header">
                        <span className="pcf-crm-card-name">{crm.display_name}</span>
                        {status === 'success' && (
                          <span className="pcf-crm-status-badge pcf-crm-status-badge--ok">
                            <svg width="11" height="11" fill="none" viewBox="0 0 14 14">
                              <path d="M2 7l3.5 3.5L12 3" stroke="currentColor" strokeWidth="2"
                                strokeLinecap="round" strokeLinejoin="round" /></svg>
                            Integrated
                          </span>
                        )}
                        {status === 'error' && (
                          <span className="pcf-crm-status-badge pcf-crm-status-badge--err">
                            <svg width="10" height="10" fill="none" viewBox="0 0 14 14">
                              <path d="M2 2l10 10M12 2L2 12" stroke="currentColor" strokeWidth="2"
                                strokeLinecap="round" /></svg>
                            Error
                          </span>
                        )}
                      </div>
                      <span className="pcf-crm-card-desc">{crm.description}</span>
                      <div className="pcf-crm-card-tags">
                        {crm.supported_auth_options?.map((at) => (
                          <span key={at.value} className="pcf-crm-tag">{at.label}</span>
                        ))}
                        <span className="pcf-crm-tag">Webhooks</span>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {modal && (
        <div className="pcf-ftr">
          <button className="pcf-btn-cancel" type="button" onClick={onClose}>Close</button>
        </div>
      )}
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// PAGE 2: CONFIGURATION FORM  (UPDATED)
// ─────────────────────────────────────────────────────────────────────────────

function PageConfigureCrm({ activeCrm, crmConfigs, integrationId, onBack, onSuccess, onError, onClearStatus, onClose, modal }) {
  const crmConfig = crmConfigs?.find((c) => c.crm_key === activeCrm);

  // ── Gatekeeper state ──────────────────────────────────────────────────────
  const [testStatus,    setTestStatus]    = useState(null);
  const [testError,     setTestError]     = useState(null);
  const [submitEnabled, setSubmitEnabled] = useState(false);
  const [fieldsLocked,  setFieldsLocked]  = useState(false);
  const [successData,   setSuccessData]   = useState(null);

  // ── Read-only / locked state ──────────────────────────────────────────────
  const [isLocked, setIsLocked] = useState(!!integrationId);

  // ── Fetch existing integration status ──────────────────────────────────────
  const { data: statusData } = useIntegrationStatus(integrationId, {
    enabled: !!integrationId,
  });

  // ── Fetch webhook URL for existing integration (NEW) ──────────────────────
  const { data: webhookUrlData } = useGetWebhookUrl(integrationId, {
    enabled: !!integrationId && isLocked,
  });

  // ── Mutations ─────────────────────────────────────────────────────────────
  const provisionMutation = useProvisionIntegration({
    onSuccess: (responseData) => {
      setSuccessData(responseData);
      setTimeout(() => { onSuccess?.(responseData); onBack(); }, 1500);
    },
  });

  const updateMutation = useUpdateCredentials({
    onSuccess: (responseData) => {
      setSuccessData(responseData);
      setTimeout(() => { onSuccess?.(responseData); onBack(); }, 1500);
    },
  });

  const deprovisionMutation = useDeprovisionIntegration({
    onSuccess: () => {
      onClearStatus?.(activeCrm);
      onBack();
    },
  });

  const testMutation = useTestConnection();

  // Derive error/loading state
  const activeSaveMutation = integrationId ? updateMutation : provisionMutation;
  const isSubmitting = activeSaveMutation.isPending;
  const isDeprovisioning = deprovisionMutation.isPending;
  const isTesting    = testMutation.isPending;
  const apiError     = activeSaveMutation.error?.message    ?? null;
  const failedChecks = activeSaveMutation.error?.failedChecks ?? null;
  const errorStatus  = activeSaveMutation.error?.status     ?? null;

  // ── Form ──────────────────────────────────────────────────────────────────
  const { register, handleSubmit, control, watch, setValue, reset, formState: { errors } } = useForm({
    resolver: zodResolver(buildCrmEntrySchema(crmConfigs)),
    defaultValues: makeCrmEntry(activeCrm, crmConfigs),
  });

  const authType = watch('auth_type');
  const enableWh = watch('enable_webhooks');

  // ── Pre-fill from status data ─────────────────────────────────────────────
  useEffect(() => {
    if (!statusData) return;
    reset({
      ...makeCrmEntry(activeCrm, crmConfigs),
      base_url:  statusData.base_url  ?? '',
      auth_type: statusData.auth_type ?? '',
    });
  }, [statusData]); // eslint-disable-line react-hooks/exhaustive-deps

  const helpInstructions    = crmConfig?.auth_instructions?.[authType] ?? null;
  const webhookInstructions = crmConfig?.webhook_instructions ?? null;
  const webhookModel        = crmConfig?.webhook_model ?? 'shared';

  // ── Gatekeeper ────────────────────────────────────────────────────────────
  const handleFieldChange = useCallback(() => {
    if (testStatus === 'success') {
      setTestStatus(null); setTestError(null);
      setSubmitEnabled(false); setFieldsLocked(false);
    }
  }, [testStatus]);

  const registerWithReset = (name, options) => {
    const reg = register(name, options);
    return { ...reg, onChange: (e) => { handleFieldChange(); return reg.onChange(e); } };
  };

  useEffect(() => {
    if (activeCrm) {
      setValue('crm_type', activeCrm);
    }
  }, [activeCrm, setValue]);

  // ── Reset handler ─────────────────────────────────────────────────────────
  const handleReset = () => {
    reset(makeCrmEntry(activeCrm, crmConfigs));
    setTestStatus(null);
    setTestError(null);
    setSuccessData(null);
    setSubmitEnabled(false);
    setFieldsLocked(false);
    setIsLocked(false);
  };

  const buildPayload = (formValues) => transformFormToPayload({
    ...formValues,
    _webhookModel: webhookModel,
    crm_type: activeCrm ?? formValues.crm_type,
  });

  // ── Test Connection ───────────────────────────────────────────────────────
  const handleTestConnection = handleSubmit((formValues) => {
    const payload = buildPayload(formValues);
    setTestStatus(null); setTestError(null);
    testMutation.mutate(payload, {
      onSuccess: () => { setTestStatus('success'); setSubmitEnabled(true); setFieldsLocked(true); },
      onError: (err) => {
        setTestStatus('error');
        setTestError({ message: err.message ?? 'Connection test failed.',
          failedChecks: err.failedChecks ?? null, status: err.status ?? null });
        setSubmitEnabled(false);
      },
    });
  });

  // ── Submit ────────────────────────────────────────────────────────────────
  const onSubmit = (formValues) => {
    const payload = buildPayload(formValues);
    if (integrationId) {
      updateMutation.mutate({ integrationId, payload });
    } else {
      provisionMutation.mutate(payload);
    }
  };

  if (!crmConfig) return null;
  const isDisabled = isSubmitting || isTesting || isDeprovisioning;

  // ── Disconnect ────────────────────────────────────────────────────────────
  const handleDisconnect = () => {
    if (!integrationId) return;
    deprovisionMutation.mutate({ integrationId, wipe: true });
  };

  return (
    <>
      {/* Header */}
      <div className="pcf-hdr">
        <div className="pcf-hdr-l">
          <button type="button" className="pcf-hdr-back" onClick={onBack}
            disabled={isDisabled} aria-label="Go back">
            <BackArrowSVG />
          </button>
          <div className="pcf-ico">
            <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
            </svg>
          </div>
          <div>
            <h2 className="pcf-ttl">Configure {crmConfig.display_name}</h2>
            <p className="pcf-sub">Set up your integration credentials</p>
          </div>
        </div>
        {modal && (
          <button className="pcf-close" type="button" onClick={onClose}
            aria-label="Close" disabled={isDisabled}>
            <svg width="13" height="13" fill="none" viewBox="0 0 16 16">
              <path d="M2 2l12 12M14 2L2 14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </button>
        )}
      </div>

      <form onSubmit={handleSubmit(onSubmit)} noValidate>
        <input type="hidden" {...register('crm_type')} />
        <div className="pcf-body">

          {/* Success banner with webhook display (NEW) */}
          {successData && (
            <>
              <div className="pcf-alert pcf-alert--ok pcf-alert--prominent">
                <span className="pcf-alert-ico">✓</span>
                <div>
                  <strong>{crmConfig.display_name} connection saved!</strong><br />
                  <span className="pcf-alert-sub">
                    Integration ID: <code className="pcf-code">{successData.integration_id}</code>
                  </span>
                </div>
              </div>

              {/* Webhook Display Section (NEW) */}
              {successData.webhook_uuid && (
                <WebhookDisplay 
                  webhookData={{
                    webhook_uuid: successData.webhook_uuid,
                    webhook_url: successData.webhook_url,
                    instructions: successData.instructions || crmConfig.webhook_instructions
                  }}
                  crmDisplayName={crmConfig.display_name}
                />
              )}
            </>
          )}

          {/* Form — hidden after success */}
          {!successData && (
            <div className="pcf-cfg-form">

              {/* Base URL */}
              <Field label="Instance Base URL" required
                hint={`Tenant-specific ${crmConfig.display_name} URL`}
                error={errors.base_url?.message}>
                <input {...registerWithReset('base_url')} type="url"
                  placeholder={crmConfig.default_base_url}
                  className={`pcf-input${errors.base_url ? ' has-err' : ''}`}
                  disabled={fieldsLocked || isLocked} />
              </Field>

              {/* Auth type */}
              <div>
                <p className="pcf-lbl" style={{ marginBottom: 10 }}>
                  Primary Auth Method <span className="pcf-req">*</span>
                </p>
                <div className="pcf-radio-group">
                  {crmConfig.supported_auth_options?.map((at) => {
                    const selected = authType === at.value;
                    return (
                      <label key={at.value}
                        className={`pcf-radio-item${selected ? ' pcf-radio-item--selected' : ''}${(fieldsLocked || isLocked) ? ' pcf-radio-item--disabled' : ''}`}>
                        <input type="radio" value={at.value} checked={selected} disabled={fieldsLocked || isLocked}
                          onChange={() => { if (!fieldsLocked && !isLocked) { handleFieldChange(); setValue('auth_type', at.value, { shouldValidate: true }); } }} />
                        <div className="pcf-radio-dot"><div className="pcf-radio-dot-inner" /></div>
                        <span className="pcf-radio-icon">{at.icon}</span>
                        <span className="pcf-radio-label">{at.label}</span>
                      </label>
                    );
                  })}
                </div>
                <ErrMsg msg={errors.auth_type?.message} />
              </div>

              {helpInstructions && (
                <HelpBox instructions={helpInstructions} title="How to get this credential" />
              )}

              {/* Credential fields */}
              {authType && (
                <div className="pcf-cred-sub">
                  {TOKEN_LIKE_RENDER.has(authType) && (
                    <TokenCredFields register={registerWithReset} errors={errors}
                      authType={authType} disabled={fieldsLocked || isLocked} />
                  )}
                  {authType === 'basic_auth' && (
                    <BasicCredFields register={registerWithReset} errors={errors} disabled={fieldsLocked || isLocked} />
                  )}
                  {authType === 'oauth2' && (
                    <OAuth2CredFields register={registerWithReset} errors={errors} disabled={fieldsLocked || isLocked} />
                  )}
                </div>
              )}

              {/* Webhook toggle */}
              <div>
                <div className="pcf-wh-toggle-row">
                  <div className="pcf-wh-toggle-info">
                    <span className="pcf-wh-toggle-title">Enable Webhook Support</span>
                    <span className="pcf-wh-toggle-desc">
                      {webhookModel === 'shared'
                        ? 'Configure a shared secret for all inbound webhook events (Zammad-style)'
                        : 'Configure per-event secrets for inbound webhooks (EspoCRM-style)'}
                    </span>
                  </div>
                  <div className="pcf-toggle-wrap">
                    <label className="pcf-toggle">
                      <input type="checkbox" {...register('enable_webhooks')}
                        onChange={(e) => { handleFieldChange(); register('enable_webhooks').onChange(e); }} />
                      <span className="pcf-toggle-track" /><span className="pcf-toggle-thumb" />
                    </label>
                  </div>
                </div>

                {enableWh && webhookModel === 'shared' && (
                  <div style={{ marginTop: 14 }}>
                    <WebhookShared register={register} errors={errors} webhookUrl=""
                      crmDisplayName={crmConfig.display_name} webhookInstructions={webhookInstructions} />
                  </div>
                )}
                {enableWh && webhookModel === 'per_event' && (
                  <div style={{ marginTop: 14 }}>
                    <WebhookPerEvent register={register} control={control} errors={errors}
                      webhookUrl="" crmDisplayName={crmConfig.display_name}
                      webhookInstructions={webhookInstructions} />
                  </div>
                )}
              </div>

            </div>
          )}

          {/* ── Alert area ─────────────────────────────────────────────────── */}
          {!successData && (
            <div className="pcf-alerts-area">
              {/* Locked / read-only info banner */}
              {isLocked && (
                <div className="pcf-alert pcf-alert--info">
                  <span className="pcf-alert-ico">🔒</span>
                  <div>
                    <strong>Integration active and connected</strong><br />
                    <span className="pcf-alert-sub">
                      This integration is active and connected. Click <strong>Reset</strong> to enter
                      new credentials or <strong>Disconnect</strong> to remove it.
                    </span>
                  </div>
                </div>
              )}

              {/* Webhook URL Display for existing integration (NEW) */}
              {isLocked && webhookUrlData && (
                <WebhookDisplay
                  webhookData={webhookUrlData}
                  crmDisplayName={crmConfig.display_name}
                />
              )}

              {/* Test success */}
              {!isLocked && testStatus === 'success' && (
                <div className="pcf-alert pcf-alert--ok">
                  <span className="pcf-alert-ico">✓</span>
                  <div>
                    <strong>Connection test passed!</strong><br />
                    <span className="pcf-alert-sub">
                      Credentials verified — you can now save the integration.
                    </span>
                  </div>
                </div>
              )}

              {/* Test error */}
              {!isLocked && testStatus === 'error' && testError && (
                <div className="pcf-alert pcf-alert--err">
                  <span className="pcf-alert-ico">!</span>
                  <div>
                    <strong>{testError.status === 403 ? 'Insufficient permissions' : testError.status === 422 ? 'Invalid CRM configuration' : 'Connection test failed'}</strong><br />
                    <span className="pcf-alert-sub">{testError.message}</span>
                    <ChecksLog checks={testError.failedChecks} />
                  </div>
                </div>
              )}

              {/* Provision / update error */}
              {!isLocked && apiError && (
                <div className="pcf-alert pcf-alert--err">
                  <span className="pcf-alert-ico">!</span>
                  <div>
                    <strong>{errorStatus === 403 ? 'Insufficient permissions' : errorStatus === 422 ? 'Invalid CRM configuration' : 'Submission failed'}</strong><br />
                    <span className="pcf-alert-sub">{apiError}</span>
                    <ChecksLog checks={failedChecks} />
                  </div>
                </div>
              )}

              {/* 502 hint */}
              {!isLocked && errorStatus === 502 && (
                <div className="pcf-alert pcf-alert--warn">
                  <span className="pcf-alert-ico">⚠</span>
                  <span className="pcf-alert-sub">
                    The CRM rejected the credentials after saving. Verify that the token has the
                    required permissions and that the base URL is correct.
                  </span>
                </div>
              )}
            </div>
          )}

        </div>{/* /pcf-body */}

        {/* Footer: [Back][Reset] ··· [Test Connection][Save … Connection] | [Disconnect] */}
        <div className="pcf-cfg-footer">
          <div className="pcf-footer-left">
            <button type="button" className="pcf-btn-cancel" onClick={onBack} disabled={isDisabled}>
              Back
            </button>
            <button type="button" className="pcf-btn-reset" onClick={handleReset}
              disabled={isDisabled} title={isLocked ? 'Unlock form to enter new credentials' : 'Clear all fields and start over'}>
              <ResetSVG />
              Reset
            </button>
          </div>

          {!successData && !isLocked && (
            <div className="pcf-footer-right">
              <button type="button" className="pcf-btn-test" onClick={handleTestConnection}
                disabled={isDisabled}>
                {isTesting
                  ? <><span className="pcf-spinner pcf-spinner--dark" />Testing…</>
                  : <><BeakerSVG />{testStatus === 'success' ? 'Retest Connection' : 'Test Connection'}</>}
              </button>

              <button className="pcf-btn-submit" type="submit"
                disabled={isDisabled || !submitEnabled}
                title={!submitEnabled ? 'Run a successful connection test first' : undefined}>
                {isSubmitting
                  ? <><span className="pcf-spinner" />Saving…</>
                  : <><SaveSVG />Save {crmConfig.display_name} Connection</>}
              </button>
            </div>
          )}

          {!successData && isLocked && integrationId && (
            <div className="pcf-footer-right">
              <button type="button" className="pcf-btn-danger" onClick={handleDisconnect}
                disabled={isDeprovisioning}
                title="Permanently remove this integration and wipe stored credentials">
                {isDeprovisioning
                  ? <><span className="pcf-spinner pcf-spinner--danger" />Disconnecting…</>
                  : <>
                      <svg width="13" height="13" fill="none" viewBox="0 0 16 16" stroke="currentColor" strokeWidth="1.8">
                        <path strokeLinecap="round" strokeLinejoin="round"
                          d="M10 6L6 10M6 6l4 4M2 8a6 6 0 1112 0A6 6 0 012 8z" />
                      </svg>
                      Disconnect
                    </>}
              </button>
            </div>
          )}
        </div>
      </form>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// MAIN COMPONENT
// ─────────────────────────────────────────────────────────────────────────────

export default function ProvisionCredentialsForm({ onSuccess, onClose, modal = true }) {
  const { user } = useAuth();
  const tenantId = user?.tenant_id ?? null;

  const [step, setStep]               = useState('SELECT_CRM');
  const [activeCrm, setActiveCrm]     = useState(null);
  const [crmStatuses, setCrmStatuses] = useState({});

  const { data: configsData, isLoading: isLoadingConfigs, error: configsError } = useCrmConfigs();
  const crmConfigs = configsData?.crms ?? null;

  const { data: activeIntegrationsData } = useActiveIntegrations(tenantId);

  useEffect(() => {
    if (!activeIntegrationsData) return;

    const activeIds = activeIntegrationsData.active_source_system_ids ?? [];
    if (activeIds.length === 0) return;

    setCrmStatuses((prev) => {
      const seeded = {};
      activeIds.forEach((sourceSystemId) => {
        const crmKey = SOURCE_SYSTEM_ID_TO_CRM_KEY[sourceSystemId];
        if (!crmKey || prev[crmKey]) return;
        seeded[crmKey] = {
          status: 'success',
          integration_id: null,
        };
      });
      return Object.keys(seeded).length > 0 ? { ...prev, ...seeded } : prev;
    });
  }, [activeIntegrationsData]);

  const handleSelectCrm           = (key)  => { setActiveCrm(key); setStep('CONFIGURE_CRM'); };
  const handleBackToCrmSelection  = ()     => { setActiveCrm(null); setStep('SELECT_CRM'); };
  const handleProvisionSuccess    = (resp) => { setCrmStatuses((p) => ({ ...p, [resp.crm_type]: { status: 'success', integration_id: resp.integration_id } })); onSuccess?.(resp); };
  const handleProvisionError      = (key)  => { setCrmStatuses((p) => ({ ...p, [key]: { status: 'error', integration_id: null } })); };
  const handleClearCrmStatus      = (key)  => { setCrmStatuses((p) => { const next = { ...p }; delete next[key]; return next; }); };
  const handleClose               = ()     => { setStep('SELECT_CRM'); setActiveCrm(null); onClose?.(); };

  const content = step === 'SELECT_CRM' ? (
    <PageSelectCrm crmConfigs={crmConfigs} crmStatuses={crmStatuses}
      isLoadingConfigs={isLoadingConfigs} configsError={configsError}
      onSelectCrm={handleSelectCrm} onClose={handleClose} modal={modal} />
  ) : (
    <PageConfigureCrm activeCrm={activeCrm} crmConfigs={crmConfigs}
      integrationId={crmStatuses[activeCrm]?.integration_id ?? null}
      onBack={handleBackToCrmSelection} onSuccess={handleProvisionSuccess}
      onError={handleProvisionError} onClearStatus={handleClearCrmStatus}
      onClose={handleClose} modal={modal} />
  );

  return (
    <div className="pcf">
      {modal ? (
        <div className="pcf-overlay" role="dialog" aria-modal="true" aria-label="CRM Integrations"
          onClick={(e) => e.target === e.currentTarget && handleClose()}>
          <div className="pcf-panel pcf-panel--modal">{content}</div>
        </div>
      ) : (
        <div className="pcf-panel pcf-panel--card">{content}</div>
      )}
    </div>
  );
}
