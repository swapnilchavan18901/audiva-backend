"""Add private per-user Google connections and short-lived OAuth state."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("meeting_google_connection") as batch:
        batch.alter_column(
            "singleton_key",
            existing_type=sa.Integer(),
            nullable=True,
            server_default=None,
        )
        batch.add_column(sa.Column("user_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_calendar_user", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_unique_constraint("uq_calendar_user", ["user_id"])
    op.create_table(
        "calendar_oauth_states",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("verifier_ciphertext", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.String(500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    # User connections and their calendar history cannot fit the old singleton schema.
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT count(*) FROM meeting_google_connection WHERE singleton_key IS NULL"
        )
    ):
        raise RuntimeError("Remove user calendar connections before downgrading.")
    op.drop_table("calendar_oauth_states")
    with op.batch_alter_table("meeting_google_connection") as batch:
        batch.drop_constraint("uq_calendar_user", type_="unique")
        batch.drop_constraint("fk_calendar_user", type_="foreignkey")
        batch.drop_column("user_id")
        batch.alter_column(
            "singleton_key",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )
