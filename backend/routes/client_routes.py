from flask import Blueprint, jsonify, request

from auth.security import can_access_base_id, can_manage_operational_data, login_required, resolve_base_id, resolve_company_id, role_level, scoped_query
from database.database import db
from models.client import Client

client_bp = Blueprint("client_bp", __name__)


@client_bp.route("/clients", methods=["GET"])
@login_required
def list_clients():
    return jsonify({"clients": [client.to_dict() for client in scoped_query(Client).all()]})


@client_bp.route("/clients", methods=["POST"])
@login_required
def create_client():
    if not can_manage_operational_data(request.current_user):
        return jsonify({"error": "Motoristas nao podem criar clientes"}), 403
    data = request.get_json() or {}
    company_id = resolve_company_id(data)
    required = ["name", "latitude", "longitude"]
    missing = [field for field in required if data.get(field) is None]
    if missing:
        return jsonify({"error": "Campos obrigatorios", "fields": missing}), 400
    if not company_id:
        return jsonify({"error": "company_id e obrigatorio"}), 400
    requested_base_id = data.get("base_id")
    base_id = resolve_base_id(data, company_id)
    if requested_base_id not in (None, "") and not can_access_base_id(request.current_user, requested_base_id, company_id):
        return jsonify({"error": "Base fora da permissao do usuario"}), 403
    if role_level(request.current_user) < 3 and not request.current_user.is_master and not base_id:
        return jsonify({"error": "Usuario sem base atribuida"}), 403

    client = Client(
        company_id=company_id,
        base_id=base_id,
        name=data["name"],
        email=data.get("email"),
        latitude=float(data["latitude"]),
        longitude=float(data["longitude"]),
        delivery_window_start=data.get("delivery_window_start"),
        delivery_window_end=data.get("delivery_window_end"),
        priority=data.get("priority", "media"),
        active=data.get("active", True),
    )
    db.session.add(client)
    db.session.commit()
    return jsonify({"client": client.to_dict()}), 201
