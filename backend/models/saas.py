from database.database import db


class SubscriptionPlan(db.Model):
    __tablename__ = "subscription_plans"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    monthly_price = db.Column(db.Float, default=0)
    max_companies = db.Column(db.Integer, nullable=True)
    max_bases = db.Column(db.Integer, nullable=True)
    max_users = db.Column(db.Integer, nullable=True)
    max_routes_month = db.Column(db.Integer, nullable=True)
    features_json = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "monthly_price": self.monthly_price,
            "max_companies": self.max_companies,
            "max_bases": self.max_bases,
            "max_users": self.max_users,
            "max_routes_month": self.max_routes_month,
            "features_json": self.features_json,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CompanySubscription(db.Model):
    __tablename__ = "company_subscriptions"
    __table_args__ = (db.Index("ix_company_subscriptions_company_status", "company_id", "status"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("subscription_plans.id"), nullable=True)
    status = db.Column(db.String(40), default="trial", nullable=False)
    billing_email = db.Column(db.String(255), nullable=True)
    start_date = db.Column(db.String(10), nullable=True)
    end_date = db.Column(db.String(10), nullable=True)
    renewal_day = db.Column(db.Integer, nullable=True)
    payment_method = db.Column(db.String(20), default="pix", nullable=False)
    payment_status = db.Column(db.String(30), default="pending", nullable=False)
    payment_reference = db.Column(db.String(120), nullable=True)
    last_payment_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    company = db.relationship("Company")
    plan = db.relationship("SubscriptionPlan")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "plan_id": self.plan_id,
            "status": self.status,
            "billing_email": self.billing_email,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "renewal_day": self.renewal_day,
            "payment_method": self.payment_method,
            "payment_status": self.payment_status,
            "payment_reference": self.payment_reference,
            "last_payment_at": self.last_payment_at.isoformat() if self.last_payment_at else None,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (
        db.Index("ix_audit_logs_company_created", "company_id", "created_at"),
        db.Index("ix_audit_logs_user_created", "user_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action = db.Column(db.String(120), nullable=False)
    resource_type = db.Column(db.String(120), nullable=True)
    resource_id = db.Column(db.String(80), nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "user_id": self.user_id,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "ip_address": self.ip_address,
            "details": self.details,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PlatformEvent(db.Model):
    __tablename__ = "platform_events"
    __table_args__ = (
        db.Index("ix_platform_events_severity_created", "severity", "created_at"),
        db.Index("ix_platform_events_resolved_created", "resolved", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    severity = db.Column(db.String(30), default="info", nullable=False)
    category = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=True)
    path = db.Column(db.String(500), nullable=True)
    method = db.Column(db.String(12), nullable=True)
    status_code = db.Column(db.Integer, nullable=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    resolved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "message": self.message,
            "path": self.path,
            "method": self.method,
            "status_code": self.status_code,
            "company_id": self.company_id,
            "user_id": self.user_id,
            "resolved": self.resolved,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
