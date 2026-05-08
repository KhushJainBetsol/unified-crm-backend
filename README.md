# Unified CRM Backend

FastAPI backend for the Unified CRM platform. This service handles authentication support, tenant-aware CRM integrations, credential storage, webhook ingestion, and scheduled sync jobs.

## What This Backend Needs

Before you run the app, make sure you have:

- Python 3.12.10
- PostgreSQL
- Keycloak
- Infisical credentials
- A reachable frontend URL
- SMTP settings if you want email features to work

## Project Layout

- `app/main.py` - FastAPI application entrypoint
- `app/core/` - database, settings, auth, logging, and Keycloak helpers
- `app/credentials/` - Infisical-backed credential management
- `app/routes/` - API routers
- `app/services/` - business logic, sync jobs, schedulers, and provisioning
- `app/integrations/webhooks/` - webhook ingestion and setup helpers
- `alembic/` - database migrations

## Quick Start

### 1. Create and activate a virtual environment

```bash
python3.12 -m venv venv
source venv/bin/activate
```

If your system exposes Python 3.12 as `python3`, use `python3 -m venv venv` instead.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your environment file

Copy `.env.example` to `.env` and fill in the real values for your environment.

### 4. Apply database migrations

```bash
alembic upgrade head
```

### 5. Start the API

```bash
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 6. Verify the service

```bash
curl http://localhost:8000/health
```

Open the interactive API docs at `http://localhost:8000/docs`.

## Sample Environment File

The repository includes a full `.env.example` file. At minimum, the app requires these values:

- `DATABASE_URL`
- `SECRET_KEY`
- `INFISICAL_CLIENT_ID`
- `INFISICAL_CLIENT_SECRET`
- `INFISICAL_PROJECT_ID`

Recommended local defaults are already included in `.env.example`, so you can start from that file and only replace the secrets and machine-specific URLs.

## Environment Variables

### Required

| Variable | Purpose | Where to get it |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL connection string used by SQLAlchemy | Create it from your Postgres host, port, database, username, and password. Format: `postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DBNAME` |
| `SECRET_KEY` | JWT signing secret for legacy auth helpers | Generate a long random value locally, for example with `openssl rand -hex 32` |
| `INFISICAL_CLIENT_ID` | Infisical service account client ID | Infisical project or service account settings |
| `INFISICAL_CLIENT_SECRET` | Infisical service account secret | Infisical project or service account settings |
| `INFISICAL_PROJECT_ID` | Infisical project identifier | Infisical project settings |

### Commonly Used

| Variable | Purpose | Where to get it |
| --- | --- | --- |
| `KEYCLOAK_URL` | Base URL for your Keycloak server | Your Keycloak deployment URL, for example `http://localhost:8080` |
| `KEYCLOAK_REALM` | Realm used by the backend | The realm name you created in Keycloak, or `unified-crm` for local dev |
| `KEYCLOAK_CLIENT_ID` | Frontend-facing client ID | Keycloak client configuration for the frontend app |
| `KEYCLOAK_ADMIN_CLIENT_ID` | Confidential client used for admin API calls | Keycloak client configuration for the backend admin client |
| `KEYCLOAK_ADMIN_CLIENT_SECRET` | Secret for the admin client | Keycloak client credentials screen |
| `FRONTEND_URL` | Public frontend URL used in invite links | Your frontend dev server or deployed frontend URL |
| `WEBHOOK_BASE_URL` | Public backend base URL used to build webhook URLs | Use the externally reachable backend URL, or `http://localhost:8000` for local testing |
| `WEBHOOK_TENANT_ID` | Tenant UUID used by webhook seeding | Copy the tenant UUID from your database after creating the tenant |
| `ALLOWED_ORIGINS` | CORS origins allowed by the API | Set this to your frontend origin(s) |
| `SUPER_ADMIN_EMAIL` | Optional super admin bootstrap email | Your internal admin email address |

### Optional / Feature-Specific

| Variable | Purpose | Where to get it |
| --- | --- | --- |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Email delivery settings | Your SMTP provider or mail service |
| `ESPO_BASE_URL`, `ESPO_API_KEY` | Legacy EspoCRM compatibility values | Only needed for older flows or scripts that still read legacy env vars |
| `ZAMMAD_BASE_URL`, `ZAMMAD_API_TOKEN` | Legacy Zammad compatibility values | Only needed for older flows or scripts that still read legacy env vars |
| `SYNC_INTERVAL_MINUTES` | Scheduler interval for CRM sync jobs | Set to the frequency you want, default is 15 minutes |
| `CRM_CONFIG_DIR` | Adapter config directory | Usually leave as `app/config` |
| `CRM_ADAPTER_ENGINE` | Adapter engine selector | Usually leave as `new` |
| `APP_NAME`, `APP_VERSION`, `ENVIRONMENT`, `DEBUG`, `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS` | App metadata and auth defaults | Usually leave at the defaults unless you are changing runtime behavior |

## Example `.env`

```env
APP_NAME=UnifiedCRM
APP_VERSION=0.1.0
ENVIRONMENT=development
DEBUG=false

DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/unified_crm
SECRET_KEY=change-this-to-a-long-random-string

ALLOWED_ORIGINS=http://localhost:5173,http://localhost:3000

KEYCLOAK_URL=http://localhost:8080
KEYCLOAK_REALM=unified-crm
KEYCLOAK_CLIENT_ID=crm-frontend
KEYCLOAK_ADMIN_CLIENT_ID=crm-admin-api
KEYCLOAK_ADMIN_CLIENT_SECRET=change-me
FRONTEND_URL=http://localhost:5173
WEBHOOK_BASE_URL=http://localhost:8000
SUPER_ADMIN_EMAIL=admin@example.com

CRM_CONFIG_DIR=app/config
CRM_ADAPTER_ENGINE=new
SYNC_INTERVAL_MINUTES=15

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=

WEBHOOK_TENANT_ID=
ESPO_BASE_URL=
ESPO_API_KEY=
ZAMMAD_BASE_URL=
ZAMMAD_API_TOKEN=

INFISICAL_CLIENT_ID=
INFISICAL_CLIENT_SECRET=
INFISICAL_PROJECT_ID=
INFISICAL_ENVIRONMENT=dev
INFISICAL_HOST=https://app.infisical.com
INFISICAL_SECRET_PATH=/
```

## How To Get Each Value

### PostgreSQL

Create a database and user, then build `DATABASE_URL` from the connection details. If you are running locally, a URL like `postgresql+asyncpg://postgres:postgres@localhost:5432/unified_crm` is fine.

### SECRET_KEY

Generate a secure random string locally:

```bash
openssl rand -hex 32
```

### Keycloak

1. Create or reuse a realm, then set `KEYCLOAK_REALM` to that realm name.
2. Set `KEYCLOAK_URL` to your Keycloak base URL.
3. Create the frontend client and copy its client ID into `KEYCLOAK_CLIENT_ID`.
4. Create a confidential admin client for backend-to-Keycloak calls and copy its ID and secret into `KEYCLOAK_ADMIN_CLIENT_ID` and `KEYCLOAK_ADMIN_CLIENT_SECRET`.

### Infisical

1. Create or open your Infisical project.
2. Create a machine/service account or client credential pair.
3. Copy the client ID, client secret, and project ID into the three required `INFISICAL_*` variables.
4. Leave `INFISICAL_ENVIRONMENT`, `INFISICAL_HOST`, and `INFISICAL_SECRET_PATH` at their defaults unless your Infisical deployment is custom.

### Frontend URL

Set `FRONTEND_URL` to the URL where the UI is running. Local development is usually `http://localhost:5173`.

### Webhook Base URL

`WEBHOOK_BASE_URL` should be the URL the external CRM can reach. Use `http://localhost:8000` only for local testing. For a real deployment, use the public API URL or a reverse proxy URL.

### Tenant UUID

`WEBHOOK_TENANT_ID` is the UUID of the tenant row in your database. If it is not set, webhook CRM seeding is skipped and you can create integrations manually.

### SMTP

Get these values from your SMTP provider. If you do not use email features, you can leave them empty.

## Database Setup Notes

- Run `alembic upgrade head` before the first server start.
- The app also creates tables on startup, but migrations are still the source of truth.
- On startup the app seeds lookup tables such as source systems, ticket priorities, and ticket statuses.
- If `WEBHOOK_TENANT_ID` is set, CRM integrations can also be seeded automatically.

## Useful Endpoints

- `GET /health` - service health check
- `GET /` - basic root response
- `GET /docs` - Swagger UI
- `GET /redoc` - ReDoc
- `GET /api/v1/auth/realm-config` - Keycloak realm selection for the frontend
- `GET /api/v1/auth/me` - current authenticated user details
- `POST /api/v1/integrations/` - provision a CRM integration
- `GET /api/v1/integrations/{integration_id}/webhook-url` - return the computed webhook URL and setup instructions

## Frontend Integration

This backend is designed to work with the React + Vite frontend you described. Keep the frontend and backend settings aligned:

- `VITE_API_BASE_URL` in the frontend should point to this backend, usually `http://localhost:8000/api/v1`.
- `VITE_KEYCLOAK_URL` should match backend `KEYCLOAK_URL`.
- `VITE_KEYCLOAK_REALM` should match backend `KEYCLOAK_REALM`.
- `VITE_KEYCLOAK_CLIENT` should match backend `KEYCLOAK_CLIENT_ID`.
- The frontend origin must be included in backend `ALLOWED_ORIGINS`.
- Keycloak must allow the frontend redirect URI and web origin for the Vite dev server, usually `http://localhost:3000/*` or the port used in your frontend repo.

The backend exposes the discovery endpoints the frontend needs:

- `GET /api/v1/auth/realm-config` lets the frontend resolve the correct Keycloak realm.
- `GET /api/v1/config/crms` returns CRM metadata, auth options, webhook instructions, and supported capabilities.
- `GET /api/v1/auth/me` returns the authenticated user profile after login.

When you wire the two apps together, use the frontend README’s `VITE_*` variables as the client-side mirror of the backend settings above. That keeps auth, API calls, and tenant-aware realm resolution consistent across both projects.

## Webhooks And CRM Setup

If you are setting up CRM webhooks, see:

- `WEBHOOK_SETUP.md` for the end-to-end testing workflow
- `README_WEBHOOK_FINAL.md` for the webhook URL implementation summary

## Troubleshooting

- If startup fails immediately, check `DATABASE_URL` first.
- If the app stops during startup with an Infisical error, verify all three required `INFISICAL_*` variables.
- If Keycloak login or auth calls fail, check `KEYCLOAK_URL`, `KEYCLOAK_REALM`, and `KEYCLOAK_ADMIN_CLIENT_SECRET`.
- If the frontend cannot call the API from the browser, update `ALLOWED_ORIGINS`.
- If webhook URLs are wrong in a deployed environment, update `WEBHOOK_BASE_URL`.

## Run Commands Summary

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```