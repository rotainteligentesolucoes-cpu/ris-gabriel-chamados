from database.database import db


class ManualLocation(db.Model):
    __tablename__ = "manual_locations"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    base_id = db.Column(db.Integer, db.ForeignKey("company_bases.id"), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    location_type = db.Column(db.String(50), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    base = db.relationship("CompanyBase")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "base_id": self.base_id,
            "name": self.name,
            "location_type": self.location_type,
            "latitude": round(float(self.latitude), 6),
            "longitude": round(float(self.longitude), 6),
            "notes": self.notes,
            "active": self.active,
        }


class StopObservation(db.Model):
    __tablename__ = "stop_observations"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    route_id = db.Column(db.Integer, db.ForeignKey("logistic_routes.id"), nullable=False)
    route_stop_id = db.Column(db.Integer, db.ForeignKey("route_stops.id"), nullable=True)
    observation_type = db.Column(db.String(80), nullable=False)
    description = db.Column(db.Text, nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    route = db.relationship("LogisticRoute")
    route_stop = db.relationship("RouteStop")
    created_by = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "route_id": self.route_id,
            "route_stop_id": self.route_stop_id,
            "observation_type": self.observation_type,
            "description": self.description,
            "latitude": round(float(self.latitude), 6) if self.latitude is not None else None,
            "longitude": round(float(self.longitude), 6) if self.longitude is not None else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class RouteTransfer(db.Model):
    __tablename__ = "route_transfers"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    route_stop_id = db.Column(db.Integer, db.ForeignKey("route_stops.id"), nullable=False)
    from_route_id = db.Column(db.Integer, db.ForeignKey("logistic_routes.id"), nullable=False)
    to_route_id = db.Column(db.Integer, db.ForeignKey("logistic_routes.id"), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    route_stop = db.relationship("RouteStop")
    from_route = db.relationship("LogisticRoute", foreign_keys=[from_route_id])
    to_route = db.relationship("LogisticRoute", foreign_keys=[to_route_id])
    created_by = db.relationship("User")


class RouteBreak(db.Model):
    __tablename__ = "route_breaks"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    route_id = db.Column(db.Integer, db.ForeignKey("logistic_routes.id"), nullable=False)
    after_route_stop_id = db.Column(db.Integer, db.ForeignKey("route_stops.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False, default="Intervalo intrajornada")
    break_type = db.Column(db.String(50), nullable=False, default="intrajornada")
    duration_minutes = db.Column(db.Integer, nullable=False, default=60)
    source = db.Column(db.String(30), nullable=False, default="manual")
    notes = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    route = db.relationship("LogisticRoute")
    after_stop = db.relationship("RouteStop")
    created_by = db.relationship("User")


class ReportSnapshot(db.Model):
    __tablename__ = "report_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    report_type = db.Column(db.String(80), nullable=False)
    summary = db.Column(db.Text, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    created_by = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "title": self.title,
            "report_type": self.report_type,
            "summary": self.summary,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TelemetryEvent(db.Model):
    __tablename__ = "telemetry_events"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    route_id = db.Column(db.Integer, db.ForeignKey("logistic_routes.id"), nullable=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=True)
    driver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    speed_kmh = db.Column(db.Float, nullable=True)
    ignition_on = db.Column(db.Boolean, default=True)
    battery_percent = db.Column(db.Integer, nullable=True)
    signal_quality = db.Column(db.String(30), nullable=True)
    source = db.Column(db.String(50), default="manual")
    notes = db.Column(db.Text, nullable=True)
    captured_at = db.Column(db.DateTime, server_default=db.func.now())
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    route = db.relationship("LogisticRoute")
    vehicle = db.relationship("Vehicle")
    driver = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "route_id": self.route_id,
            "vehicle_id": self.vehicle_id,
            "driver_user_id": self.driver_user_id,
            "latitude": round(float(self.latitude), 6),
            "longitude": round(float(self.longitude), 6),
            "speed_kmh": self.speed_kmh,
            "ignition_on": self.ignition_on,
            "battery_percent": self.battery_percent,
            "signal_quality": self.signal_quality,
            "source": self.source,
            "notes": self.notes,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
        }


class StopProof(db.Model):
    __tablename__ = "stop_proofs"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    route_id = db.Column(db.Integer, db.ForeignKey("logistic_routes.id"), nullable=False)
    route_stop_id = db.Column(db.Integer, db.ForeignKey("route_stops.id"), nullable=False)
    driver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    proof_type = db.Column(db.String(40), nullable=False)
    receiver_name = db.Column(db.String(255), nullable=True)
    receiver_document = db.Column(db.String(80), nullable=True)
    invoice_number = db.Column(db.String(120), nullable=True)
    invoice_link = db.Column(db.String(1000), nullable=True)
    invoice_file_filename = db.Column(db.String(255), nullable=True)
    fiscal_status = db.Column(db.String(40), default="pendente", nullable=True)
    notes = db.Column(db.Text, nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    photo_filename = db.Column(db.String(255), nullable=True)
    signature_data = db.Column(db.Text, nullable=True)
    captured_at = db.Column(db.DateTime, server_default=db.func.now())
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    route = db.relationship("LogisticRoute")
    route_stop = db.relationship("RouteStop")
    driver = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "route_id": self.route_id,
            "route_stop_id": self.route_stop_id,
            "driver_user_id": self.driver_user_id,
            "proof_type": self.proof_type,
            "receiver_name": self.receiver_name,
            "receiver_document": self.receiver_document,
            "invoice_number": self.invoice_number,
            "invoice_link": self.invoice_link,
            "invoice_file_filename": self.invoice_file_filename,
            "fiscal_status": self.fiscal_status,
            "notes": self.notes,
            "latitude": round(float(self.latitude), 6) if self.latitude is not None else None,
            "longitude": round(float(self.longitude), 6) if self.longitude is not None else None,
            "photo_filename": self.photo_filename,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
        }


class Merchandise(db.Model):
    __tablename__ = "merchandises"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    base_id = db.Column(db.Integer, db.ForeignKey("company_bases.id"), nullable=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    route_id = db.Column(db.Integer, db.ForeignKey("logistic_routes.id"), nullable=True)
    route_stop_id = db.Column(db.Integer, db.ForeignKey("route_stops.id"), nullable=True)
    invoice_code = db.Column(db.String(120), nullable=True)
    order_code = db.Column(db.String(120), nullable=True)
    volume = db.Column(db.String(80), nullable=True)
    weight_kg = db.Column(db.Float, default=0)
    cubic_meters = db.Column(db.Float, default=0)
    batch_code = db.Column(db.String(120), nullable=True)
    barcode = db.Column(db.String(255), nullable=False)
    qr_code = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(40), default="pendente", nullable=False)
    notes = db.Column(db.Text, nullable=True)
    last_scanned_at = db.Column(db.DateTime, nullable=True)
    last_scanned_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    base = db.relationship("CompanyBase")
    client = db.relationship("Client")
    route = db.relationship("LogisticRoute")
    route_stop = db.relationship("RouteStop")
    last_scanned_by = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "base_id": self.base_id,
            "client_id": self.client_id,
            "route_id": self.route_id,
            "route_stop_id": self.route_stop_id,
            "invoice_code": self.invoice_code,
            "order_code": self.order_code,
            "volume": self.volume,
            "weight_kg": self.weight_kg,
            "cubic_meters": self.cubic_meters,
            "batch_code": self.batch_code,
            "barcode": self.barcode,
            "qr_code": self.qr_code,
            "status": self.status,
            "notes": self.notes,
            "last_scanned_at": self.last_scanned_at.isoformat() if self.last_scanned_at else None,
        }


class MerchandiseScan(db.Model):
    __tablename__ = "merchandise_scans"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    merchandise_id = db.Column(db.Integer, db.ForeignKey("merchandises.id"), nullable=True)
    route_id = db.Column(db.Integer, db.ForeignKey("logistic_routes.id"), nullable=True)
    route_stop_id = db.Column(db.Integer, db.ForeignKey("route_stops.id"), nullable=True)
    scanned_code = db.Column(db.String(255), nullable=False)
    scan_action = db.Column(db.String(40), nullable=False)
    result_status = db.Column(db.String(40), nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    photo_filename = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    scanned_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    company = db.relationship("Company")
    merchandise = db.relationship("Merchandise")
    route = db.relationship("LogisticRoute")
    route_stop = db.relationship("RouteStop")
    scanned_by = db.relationship("User")


class PublicContactRequest(db.Model):
    __tablename__ = "public_contact_requests"

    id = db.Column(db.Integer, primary_key=True)
    request_type = db.Column(db.String(40), default="contato", nullable=True)
    name = db.Column(db.String(255), nullable=False)
    contact = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    company_document = db.Column(db.String(120), nullable=True)
    financial_responsible = db.Column(db.String(255), nullable=True)
    address_proof_filename = db.Column(db.String(255), nullable=True)
    document_proof_filename = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(30), default="novo")
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "request_type": self.request_type,
            "name": self.name,
            "contact": self.contact,
            "subject": self.subject,
            "message": self.message,
            "company_document": self.company_document,
            "financial_responsible": self.financial_responsible,
            "address_proof_filename": self.address_proof_filename,
            "document_proof_filename": self.document_proof_filename,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PublicInsight(db.Model):
    __tablename__ = "public_insights"

    id = db.Column(db.Integer, primary_key=True)
    insight_key = db.Column(db.String(40), unique=True, nullable=False)
    title = db.Column(db.String(120), nullable=False)
    intro = db.Column(db.Text, nullable=False)
    items = db.Column(db.Text, nullable=False)
    source_title = db.Column(db.String(255), nullable=True)
    source_url = db.Column(db.String(500), nullable=True)
    source_date = db.Column(db.String(40), nullable=True)
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    def to_public_dict(self):
        return {
            "title": self.title,
            "intro": self.intro,
            "items": [line.strip() for line in (self.items or "").splitlines() if line.strip()],
            "source_title": self.source_title or "",
            "source_url": self.source_url or "",
            "source_date": self.source_date or "",
        }
