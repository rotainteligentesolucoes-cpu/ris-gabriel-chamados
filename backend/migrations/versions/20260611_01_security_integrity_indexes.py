"""Security and integrity indexes.

Revision ID: 20260611_01
Revises: None
"""
from alembic import op
import sqlalchemy as sa


revision = "20260611_01"
down_revision = None
branch_labels = None
depends_on = None


INDEXES = [
    ("ix_companies_active", "companies", "active"),
    ("ix_company_bases_company_active", "company_bases", "company_id, active"),
    ("ix_company_bases_parent", "company_bases", "parent_base_id"),
    ("ix_users_company_base_role", "users", "company_id, base_id, role"),
    ("ix_users_company_active", "users", "company_id, active"),
    ("ix_clients_company_base_active", "clients", "company_id, base_id, active"),
    ("ix_clients_company_name", "clients", "company_id, name"),
    ("ix_vehicles_company_base_active", "vehicles", "company_id, base_id, active"),
    ("ix_routes_company_date", "logistic_routes", "company_id, planned_date"),
    ("ix_routes_company_base_date", "logistic_routes", "company_id, base_id, planned_date"),
    ("ix_routes_driver_date", "logistic_routes", "driver_user_id, planned_date"),
    ("ix_routes_company_status", "logistic_routes", "company_id, status"),
    ("ix_route_stops_route_sequence", "route_stops", "route_id, sequence"),
    ("ix_route_stops_route_status", "route_stops", "route_id, status"),
    ("ix_route_stops_client", "route_stops", "client_id"),
    ("ix_manual_locations_company_base_active", "manual_locations", "company_id, base_id, active"),
    ("ix_stop_observations_route_created", "stop_observations", "route_id, created_at"),
    ("ix_route_transfers_company_created", "route_transfers", "company_id, created_at"),
    ("ix_route_breaks_route_active", "route_breaks", "route_id, active"),
    ("ix_report_snapshots_company_created", "report_snapshots", "company_id, created_at"),
    ("ix_telemetry_route_captured", "telemetry_events", "route_id, captured_at"),
    ("ix_telemetry_company_captured", "telemetry_events", "company_id, captured_at"),
    ("ix_telemetry_vehicle_captured", "telemetry_events", "vehicle_id, captured_at"),
    ("ix_stop_proofs_route_captured", "stop_proofs", "route_id, captured_at"),
    ("ix_stop_proofs_stop", "stop_proofs", "route_stop_id"),
    ("ix_merchandises_company_base_status", "merchandises", "company_id, base_id, status"),
    ("ix_merchandises_company_barcode", "merchandises", "company_id, barcode"),
    ("ix_merchandises_route_stop", "merchandises", "route_id, route_stop_id"),
    ("ix_merchandise_scans_route_created", "merchandise_scans", "route_id, created_at"),
    ("ix_merchandise_scans_company_code", "merchandise_scans", "company_id, scanned_code"),
    ("ix_merchandise_scans_merchandise", "merchandise_scans", "merchandise_id"),
    ("ix_public_contacts_type_created", "public_contact_requests", "request_type, created_at"),
    ("ix_company_subscriptions_company_status", "company_subscriptions", "company_id, status"),
    ("ix_audit_logs_company_created", "audit_logs", "company_id, created_at"),
    ("ix_audit_logs_user_created", "audit_logs", "user_id, created_at"),
    ("ix_platform_events_severity_created", "platform_events", "severity, created_at"),
    ("ix_platform_events_resolved_created", "platform_events", "resolved, created_at"),
]


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for name, table, columns in INDEXES:
        if table not in tables:
            continue
        available = {column["name"] for column in inspector.get_columns(table)}
        required = {column.strip() for column in columns.split(",")}
        if not required.issubset(available):
            continue
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")


def downgrade():
    for name, _table, _columns in reversed(INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
