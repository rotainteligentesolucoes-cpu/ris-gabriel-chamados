from database.database import db


class LogisticRoute(db.Model):
    __tablename__ = "logistic_routes"
    __table_args__ = (
        db.Index("ix_routes_company_date", "company_id", "planned_date"),
        db.Index("ix_routes_company_base_date", "company_id", "base_id", "planned_date"),
        db.Index("ix_routes_driver_date", "driver_user_id", "planned_date"),
        db.Index("ix_routes_company_status", "company_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    base_id = db.Column(db.Integer, db.ForeignKey("company_bases.id"), nullable=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False)
    driver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    driver_name = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(50), default="planned", nullable=False)
    planned_date = db.Column(db.String(10), nullable=True)
    start_time = db.Column(db.String(5), default="08:00", nullable=False)
    actual_start_time = db.Column(db.String(5), nullable=True)
    actual_end_time = db.Column(db.String(5), nullable=True)
    estimated_distance_km = db.Column(db.Float, default=0)
    estimated_duration_minutes = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company", back_populates="routes")
    base = db.relationship("CompanyBase")
    vehicle = db.relationship("Vehicle", back_populates="routes")
    driver_user = db.relationship("User")
    stops = db.relationship("RouteStop", back_populates="route", cascade="all, delete-orphan", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "base_id": self.base_id,
            "base": self.base.to_dict() if self.base else None,
            "vehicle": self.vehicle.to_dict() if self.vehicle else None,
            "driver_user_id": self.driver_user_id,
            "name": self.name,
            "driver_name": self.driver_name,
            "status": self.status,
            "planned_date": self.planned_date,
            "start_time": self.start_time,
            "actual_start_time": self.actual_start_time,
            "actual_end_time": self.actual_end_time,
            "estimated_distance_km": self.estimated_distance_km,
            "estimated_duration_minutes": self.estimated_duration_minutes,
            "stops": [stop.to_dict() for stop in sorted(self.stops, key=lambda item: item.sequence)],
        }


class RouteStop(db.Model):
    __tablename__ = "route_stops"
    __table_args__ = (
        db.Index("ix_route_stops_route_sequence", "route_id", "sequence"),
        db.Index("ix_route_stops_route_status", "route_id", "status"),
        db.Index("ix_route_stops_client", "client_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("logistic_routes.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    planned_arrival = db.Column(db.String(5), nullable=True)
    service_minutes = db.Column(db.Integer, default=10, nullable=False)
    service_description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), default="pendente", nullable=False)

    route = db.relationship("LogisticRoute", back_populates="stops")
    client = db.relationship("Client", back_populates="stops")

    def to_dict(self):
        return {
            "id": self.id,
            "route_id": self.route_id,
            "client_id": self.client_id,
            "sequence": self.sequence,
            "planned_arrival": self.planned_arrival,
            "service_minutes": self.service_minutes,
            "service_description": self.service_description,
            "status": self.status,
            "client": self.client.to_dict() if self.client else None,
        }
