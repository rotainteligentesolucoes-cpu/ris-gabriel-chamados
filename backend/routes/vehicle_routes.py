from flask import Blueprint, jsonify, request

from auth.security import login_required, resolve_company_id, scoped_query
from database.database import db
from models.vehicle import Vehicle

vehicle_bp = Blueprint("vehicle_bp", __name__)

DEFAULT_SPEEDS = {
    "caminhao": 60,
    "van": 80,
    "carro": 80,
    "moto": 70,
}


@vehicle_bp.route("/vehicles", methods=["GET"])
@login_required
def list_vehicles():
    return jsonify({"vehicles": [vehicle.to_dict() for vehicle in scoped_query(Vehicle).all()]})


@vehicle_bp.route("/vehicles", methods=["POST"])
@login_required
def create_vehicle():
    data = request.get_json() or {}
    company_id = resolve_company_id(data)
    vehicle_type = data.get("vehicle_type")

    if not company_id:
        return jsonify({"error": "company_id e obrigatorio"}), 400
    if vehicle_type not in DEFAULT_SPEEDS:
        return jsonify({"error": "vehicle_type invalido", "allowed": list(DEFAULT_SPEEDS)}), 400

    vehicle = Vehicle(
        company_id=company_id,
        name=data.get("name") or vehicle_type.title(),
        vehicle_type=vehicle_type,
        average_speed_kmh=data.get("average_speed_kmh") or DEFAULT_SPEEDS[vehicle_type],
        active=data.get("active", True),
    )
    db.session.add(vehicle)
    db.session.commit()
    return jsonify({"vehicle": vehicle.to_dict()}), 201
