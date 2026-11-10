"""sender auth

Ergebnis der Absenderprüfung (SPF/DKIM/DMARC aus Authentication-Results) je E-Mail:
pass | fail | none. Bestehende Mails bleiben NULL (unbekannt).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-10 22:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("sender_auth", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("email_messages", "sender_auth")
