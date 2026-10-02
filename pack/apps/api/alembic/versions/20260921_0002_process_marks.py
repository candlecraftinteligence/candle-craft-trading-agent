"""Process marks and Pack oath timestamp.

Revision ID: 20260921_0002
Revises: 20260921_0001
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260921_0002"
down_revision = "20260921_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("oath_accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "process_marks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("mission_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "mission_id", "kind", name="uq_process_mark"),
        sa.CheckConstraint("kind IN ('evidence','review')", name="ck_process_mark_kind"),
    )


def downgrade() -> None:
    op.drop_table("process_marks")
    op.drop_column("users", "oath_accepted_at")
