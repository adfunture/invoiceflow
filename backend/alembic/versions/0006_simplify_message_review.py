"""simplify message review

Bestehende E-Mails in Prüfung bereinigen: "N Dokument(e) zur Prüfung" (steht in der
Dokumentliste) und Warnungen zu alten Office-Dateien (.xls/.doc/.ppt) entfallen.
Bleibt kein Grund übrig, ist die E-Mail erledigt.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-09 21:00:00+00:00
"""
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSOLETE = re.compile(r"^(\d+ Dokument\(e\) zur Prüfung|Gefährlicher Anhang „[^“]*\.(xls|doc|ppt)“ blockiert)$", re.I)


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT m.id, m.review_reason, EXISTS (SELECT 1 FROM documents d WHERE d.email_message_id = m.id) AS has_docs "
        "FROM email_messages m WHERE m.status = 'manual_review' AND m.review_reason IS NOT NULL"
    )).all()
    for msg_id, reason, has_docs in rows:
        parts = [p.strip() for p in reason.split(";") if p.strip()]
        kept = [p for p in parts if not _OBSOLETE.match(p)]
        if len(kept) == len(parts):
            continue
        if kept:
            conn.execute(sa.text("UPDATE email_messages SET review_reason = :r WHERE id = :id"),
                         {"r": "; ".join(kept), "id": msg_id})
        else:
            conn.execute(sa.text("UPDATE email_messages SET status = :s, review_reason = NULL WHERE id = :id"),
                         {"s": "processed" if has_docs else "no_invoice", "id": msg_id})


def downgrade() -> None:
    pass  # Bereinigung von Prüfvermerken ist nicht umkehrbar und nicht nötig
