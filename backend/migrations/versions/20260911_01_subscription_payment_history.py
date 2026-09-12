"""Subscription payment history and decimal monetary values.

Revision ID: 20260911_01
Revises: 20260910_01
"""
from alembic import op
import sqlalchemy as sa


revision = "20260911_01"
down_revision = "20260910_01"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "subscription_payments" not in tables:
        op.create_table(
            "subscription_payments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("subscription_id", sa.Integer(), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("billing_period", sa.String(length=7), nullable=False),
            sa.Column("due_date", sa.String(length=10), nullable=False),
            sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column("payment_method", sa.String(length=20), server_default="pix", nullable=False),
            sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
            sa.Column("reference", sa.String(length=120), nullable=True),
            sa.Column("paid_at", sa.DateTime(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["subscription_id"], ["company_subscriptions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("subscription_id", "billing_period", name="uq_subscription_payment_period"),
        )
        op.create_index("ix_subscription_payments_company_due", "subscription_payments", ["company_id", "due_date"])
        op.create_index("ix_subscription_payments_status_due", "subscription_payments", ["status", "due_date"])

    if "subscription_plans" in tables:
        columns = {column["name"]: column for column in inspector.get_columns("subscription_plans")}
        price_column = columns.get("monthly_price")
        if price_column and not isinstance(price_column["type"], sa.Numeric):
            if bind.dialect.name == "sqlite":
                with op.batch_alter_table("subscription_plans") as batch_op:
                    batch_op.alter_column(
                        "monthly_price",
                        existing_type=price_column["type"],
                        type_=sa.Numeric(precision=12, scale=2),
                        existing_nullable=price_column.get("nullable", True),
                    )
            else:
                op.alter_column(
                    "subscription_plans",
                    "monthly_price",
                    existing_type=price_column["type"],
                    type_=sa.Numeric(precision=12, scale=2),
                    existing_nullable=price_column.get("nullable", True),
                    postgresql_using="monthly_price::numeric(12,2)",
                )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "subscription_payments" in inspector.get_table_names():
        op.drop_index("ix_subscription_payments_status_due", table_name="subscription_payments")
        op.drop_index("ix_subscription_payments_company_due", table_name="subscription_payments")
        op.drop_table("subscription_payments")
