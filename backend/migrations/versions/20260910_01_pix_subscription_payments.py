"""PIX-only subscription payment fields.

Revision ID: 20260910_01
Revises: 20260611_01
"""
from alembic import op
import sqlalchemy as sa


revision = "20260910_01"
down_revision = "20260611_01"
branch_labels = None
depends_on = None


COLUMNS = (
    ("payment_method", sa.Column("payment_method", sa.String(length=20), nullable=False, server_default="pix")),
    ("payment_status", sa.Column("payment_status", sa.String(length=30), nullable=False, server_default="pending")),
    ("payment_reference", sa.Column("payment_reference", sa.String(length=120), nullable=True)),
    ("last_payment_at", sa.Column("last_payment_at", sa.DateTime(), nullable=True)),
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "company_subscriptions" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("company_subscriptions")}
    for name, column in COLUMNS:
        if name not in existing:
            op.add_column("company_subscriptions", column)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "company_subscriptions" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("company_subscriptions")}
    for name, _column in reversed(COLUMNS):
        if name in existing:
            op.drop_column("company_subscriptions", name)
