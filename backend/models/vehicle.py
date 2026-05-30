from database.database import db


class Vehicle(db.Model):
    __tablename__ = "vehicles"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    base_id = db.Column(db.Integer, db.ForeignKey("company_bases.id"), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    vehicle_type = db.Column(db.String(50), nullable=False)
    average_speed_kmh = db.Column(db.Integer, nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company", back_populates="vehicles")
    base = db.relationship("CompanyBase")
    routes = db.relationship("LogisticRoute", back_populates="vehicle", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "base_id": self.base_id,
            "base": self.base.to_dict() if self.base else None,
            "name": self.name,
            "vehicle_type": self.vehicle_type,
            "average_speed_kmh": self.average_speed_kmh,
            "active": self.active,
        }
