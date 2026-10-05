"""Pack MVP tables: users, missions, decisions, journals, XP, quests, replays.

Revision ID: 20260921_0001
Revises:
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260921_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64)),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("wolf_rank", sa.String(32), nullable=False, server_default="SCOUT"),
        sa.Column("pack_xp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discipline_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("streak_updated_on", sa.Date()),
        sa.Column("last_process_at", sa.DateTime(timezone=True)),
        sa.Column("notification_prefs", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),
    )
    op.create_table(
        "missions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("cci_setup_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("quality_tier", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("thesis_summary", sa.Text(), nullable=False),
        sa.Column("evidence_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("lifecycle_state", sa.String(32), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("outcome_code", sa.String(32)),
        sa.Column("outcome_payload_json", postgresql.JSONB()),
        sa.Column("source", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("disclaimer", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("cci_setup_id", name="uq_missions_cci_setup_id"),
    )
    op.create_table(
        "mission_lifecycle_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("mission_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column("cci_event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload_json", postgresql.JSONB()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("cci_event_id", name="uq_lifecycle_cci_event_id"),
    )
    op.create_table(
        "user_mission_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("mission_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "mission_id", name="uq_user_mission_decision"),
        sa.CheckConstraint("decision IN ('TRACK','I_TOOK_THIS','WATCH_ONLY','NO_TRADE')", name="ck_decision_value"),
    )
    op.create_table(
        "journals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("mission_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("missions.id"), nullable=False),
        sa.Column("emotional_state", sa.String(32)),
        sa.Column("process_notes", sa.Text(), nullable=False),
        sa.Column("followed_plan", sa.Text()),
        sa.Column("self_reported_result", sa.String(32)),
        sa.Column("reason", sa.Text()),
        sa.Column("lesson", sa.Text()),
        sa.Column("screenshot_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "mission_id", name="uq_user_mission_journal"),
    )
    op.create_table(
        "xp_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("ref_type", sa.String(64)),
        sa.Column("ref_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_xp_ledger_idempotency_key"),
    )
    op.create_index("ix_xp_ledger_user_id", "xp_ledger", ["user_id"])
    op.create_index("ix_xp_ledger_created_at", "xp_ledger", ["created_at"])
    op.create_table(
        "quests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("cadence", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("rule_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("xp_reward", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("code", name="uq_quests_code"),
    )
    op.create_table(
        "quest_completions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("quest_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("quests.id"), nullable=False),
        sa.Column("period_key", sa.String(32), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "quest_id", "period_key", name="uq_quest_period"),
    )
    op.create_table(
        "achievements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("rarity", sa.String(32), nullable=False),
        sa.Column("rule_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("xp_reward", sa.Integer(), nullable=False),
        sa.UniqueConstraint("code", name="uq_achievements_code"),
    )
    op.create_table(
        "user_achievements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("achievement_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("achievements.id"), nullable=False),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "achievement_id", name="uq_user_achievement"),
    )
    op.create_table(
        "replay_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_mission_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("missions.id")),
        sa.Column("fixture_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("teaching_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("rubric_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("source_mission_id", name="uq_replay_source_mission"),
    )
    op.create_table(
        "replay_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("replay_challenges.id"), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("detail_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("idempotency_key", name="uq_replay_attempt_idempotency"),
    )


def downgrade() -> None:
    for name in (
        "replay_attempts",
        "replay_challenges",
        "user_achievements",
        "achievements",
        "quest_completions",
        "quests",
        "xp_ledger",
        "journals",
        "user_mission_decisions",
        "mission_lifecycle_events",
        "missions",
        "users",
    ):
        op.drop_table(name)
