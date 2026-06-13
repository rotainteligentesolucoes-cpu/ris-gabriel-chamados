from database.database import db


class Client(db.Model):
    __tablename__ = "clients"
    __table_args__ = (
        db.Index("ix_clients_company_base_active", "company_id", "base_id", "active"),
        db.Index("ix_clients_company_name", "company_id", "name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    base_id = db.Column(db.Integer, db.ForeignKey("company_bases.id"), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=True)
    address = db.Column(db.String(500), nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    delivery_window_start = db.Column(db.String(5), nullable=True)
    delivery_window_end = db.Column(db.String(5), nullable=True)
    priority = db.Column(db.String(20), default="media", nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company", back_populates="clients")
    base = db.relationship("CompanyBase")
    stops = db.relationship("RouteStop", back_populates="client", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "base_id": self.base_id,
            "base": self.base.to_dict() if self.base else None,
            "name": self.name,
            "email": self.email,
            "address": self.address,
            "latitude": round(float(self.latitude), 6),
            "longitude": round(float(self.longitude), 6),
            "delivery_window_start": self.delivery_window_start,
            "delivery_window_end": self.delivery_window_end,
            "priority": self.priority,
            "active": self.active,
        }
