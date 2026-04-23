import sys
import os
import asyncio

# This forces Alembic to look in your root folder (unified-crm-backend) for imports
sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), '..')))

from logging.config import fileConfig
from sqlalchemy import pool
from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 1. Import your Base from the core/base.py file
from app.core.base import Base 
# 2. Import ALL models so Alembic can resolve foreign keys
import app.models.tenant
import app.models.tenant_realm
import app.models.source_system
import app.models.tenant_source_systems
import app.models.ticket_status
import app.models.ticket_priority
import app.models.permission
import app.models.role_permission
import app.models.dashboard_user
import app.models.company
import app.models.customer
import app.models.agent
import app.models.ticket
import app.models.ticket_sync_log
import app.models.user_agent_mapping
import app.models.ticket_comment
import app.models.invitation
import app.models.activity_log # 3. Import your async engine from your database config
import app.models.crm_integrations
from app.core.database import engine

# 4. Point Alembic to the metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """Run the actual migration inside a synchronous context."""
    context.configure(
        connection=connection, 
        target_metadata=target_metadata,
        compare_type=True  # Important: Helps Alembic detect column type changes
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    """Connect to the database asynchronously, then run migrations."""
    connectable = engine

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()