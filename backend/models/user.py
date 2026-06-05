from database.database import db


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True)
    base_id = db.Column(db.Integer, db.ForeignKey("company_bases.id"), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default="operator", nullable=False)
    can_view_finance = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    active_session_id = db.Column(db.String(64), nullable=True)
    active_session_started_at = db.Column(db.DateTime, nullable=True)
    active_session_ip = db.Column(db.String(64), nullable=True)
    active_session_user_agent = db.Column(db.String(255), nullable=True)
    terms_accepted_at = db.Column(db.DateTime, nullable=True)
    terms_accepted_version = db.Column(db.String(40), nullable=True)
    terms_accepted_ip = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company", back_populates="users")
    base = db.relationship("CompanyBase")

    @property
    def is_master(self):
        return self.role == "master"

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "company": self.company.to_dict() if self.company else None,
            "base_id": self.base_id,
            "base": self.base.to_dict() if self.base else None,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "can_view_finance": self.can_view_finance,
            "active": self.active,
            "active_session_started_at": self.active_session_started_at.isoformat() if self.active_session_started_at else None,
            "terms_accepted_at": self.terms_accepted_at.isoformat() if self.terms_accepted_at else None,
            "terms_accepted_version": self.terms_accepted_version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
