"""Rolling workspace memory and recurring auto-join assignments."""

import json
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "meetings",
        sa.Column("auto_join", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("meetings") as batch:
        batch.alter_column("auto_join", existing_type=sa.Boolean(), server_default=None)
    op.create_table(
        "meeting_memories",
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "meeting_series",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "calendar_id",
            sa.Uuid(),
            sa.ForeignKey("meeting_calendars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_series_id", sa.String(1024), nullable=False),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agenda", sa.Text(), nullable=False),
        sa.Column("auto_join", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "calendar_id", "provider_series_id", name="uq_meeting_series"
        ),
    )
    # Preserve recent existing notes inside the single string before retiring their columns.
    connection = op.get_bind()
    now = datetime.now(UTC)
    rows = connection.execute(
        sa.text(
            "SELECT c.organization_id, c.notes, m.starts_at FROM meeting_captures c "
            "JOIN meetings m ON m.id = c.meeting_id WHERE c.notes IS NOT NULL ORDER BY m.starts_at"
        )
    )
    contexts = {}
    for org, notes, started in rows:
        if started.date() < now.date() - timedelta(days=13):
            continue
        if isinstance(notes, str):
            notes = json.loads(notes)
        lines = [
            notes.get("summary", ""),
            *notes.get("decisions", []),
            *notes.get("action_items", []),
        ]
        contexts.setdefault(org, []).append(
            started.date().isoformat() + "\n" + "\n".join(lines)
        )
    table = sa.table(
        "meeting_memories",
        sa.column("organization_id", sa.Uuid()),
        sa.column("context"),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for org, parts in contexts.items():
        connection.execute(
            table.insert().values(
                organization_id=org, context="\n\n".join(parts), updated_at=now
            )
        )
    op.alter_column("meeting_captures", "notes_status", new_column_name="memory_status")
    op.alter_column("meeting_captures", "notes_error", new_column_name="memory_error")
    for name in ("notes", "notes_attempted_at", "transcript_status"):
        op.drop_column("meeting_captures", name)
    op.drop_table("meeting_jobs")


def downgrade():
    raise RuntimeError(
        "Rolling memory replaces raw transcripts; restore a backup to downgrade."
    )
