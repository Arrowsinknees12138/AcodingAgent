"""CI sandbox command-time budget.

Revision ID: 6b25a1ci0001
Revises: 2c077d327024
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "6b25a1ci0001"
down_revision = "2c077d327024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sandbox_budgets",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("max_ms", sa.Integer(), nullable=False),
        sa.Column("consumed_ms", sa.Integer(), nullable=False),
        sa.Column("reserved_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "max_ms > 0 and consumed_ms >= 0 and reserved_ms >= 0",
            name="ck_sandbox_budget_nonnegative",
        ),
    )
    op.create_index("ix_sandbox_budgets_tenant_id", "sandbox_budgets", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("sandbox_budgets")
