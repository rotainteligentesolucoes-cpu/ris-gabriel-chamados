from database.database import db


class Company(db.Model):
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    users = db.relationship("User", back_populates="company", lazy=True)
    bases = db.relationship("CompanyBase", back_populates="company", lazy=True)
    clients = db.relationship("Client", back_populates="company", lazy=True)
    vehicles = db.relationship("Vehicle", back_populates="company", lazy=True)
    routes = db.relationship("LogisticRoute", back_populates="company", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CompanyBase(db.Model):
    __tablename__ = "company_bases"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    parent_base_id = db.Column(db.Integer, db.ForeignKey("company_bases.id"), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    base_type = db.Column(db.String(40), default="base", nullable=False)
    city = db.Column(db.String(255), nullable=True)
    address = db.Column(db.String(500), nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company", back_populates="bases")
    parent = db.relationship("CompanyBase", remote_side=[id])

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "parent_base_id": self.parent_base_id,
            "name": self.name,
            "base_type": self.base_type,
            "city": self.city,
            "address": self.address,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
