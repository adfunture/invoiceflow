"""Alembic-Umgebung. Die Datenbank-URL kommt aus DATABASE_URL (Settings)."""
from alembic import context
from sqlalchemy import create_engine, pool

from invoiceflow.config import get_settings
from invoiceflow.db.models import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection")
    if connectable is not None:
        context.configure(connection=connectable, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        poolclass=pool.NullPool,
        connect_args={"prepare_threshold": None} if settings.db_disable_prepared_statements else {},
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
