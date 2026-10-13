"""model call cache

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-13 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_call_cache",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hits", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_model_call_cache")),
    )
    op.add_column(
        "eval_runs",
        sa.Column("use_cache", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "run_items",
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("run_items", "cache_hit")
    op.drop_column("eval_runs", "use_cache")
    op.drop_table("model_call_cache")
