from flask import Blueprint, jsonify, request

from auth.security import can_access_base_id, can_manage_operational_data, login_required, resolve_base_id, resolve_company_id, role_level, scoped_query
from database.database import db
from models.client import Client
from models.route import LogisticRoute, RouteStop
from models.vehicle import Vehicle

route_bp = Blueprint("route_bp", __name__)


@route_bp.route("/routes", methods=["GET"])
@login_required
def list_routes():
    return jsonify({"routes": [route.to_dict() for route in scoped_query(LogisticRoute).all()]})


@route_bp.route("/routes", methods=["POST"])
@login_required
def create_route():
    if not can_manage_operational_data(request.current_user):
        return jsonify({"error": "Motoristas nao podem criar rotas"}), 403
    data = request.get_json() or {}
    company_id = resolve_company_id(data)
    if not company_id:
        return jsonify({"error": "company_id e obrigatorio"}), 400
    requested_base_id = data.get("base_id")
    base_id = resolve_base_id(data, company_id)
    if requested_base_id not in (None, "") and not can_access_base_id(request.current_user, requested_base_id, company_id):
        return jsonify({"error": "Base fora da permissao do usuario"}), 403
    if role_level(request.current_user) < 3 and not request.current_user.is_master and not base_id:
        return jsonify({"error": "Usuario sem base atribuida"}), 403

    vehicle = Vehicle.query.filter_by(id=data.get("vehicle_id"), company_id=company_id).first()
    if not vehicle:
        return jsonify({"error": "Veiculo nao encontrado para esta empresa"}), 404
    if base_id and vehicle.base_id and vehicle.base_id != base_id:
        return jsonify({"error": "Veiculo pertence a outra base"}), 400

    route = LogisticRoute(
        company_id=company_id,
        base_id=base_id,
        vehicle_id=vehicle.id,
        name=data.get("name", "Nova rota"),
        driver_name=data.get("driver_name"),
        status=data.get("status", "planned"),
        start_time=data.get("start_time") or "08:00",
        estimated_distance_km=data.get("estimated_distance_km", 0),
        estimated_duration_minutes=data.get("estimated_duration_minutes", 0),
    )
    db.session.add(route)
    db.session.flush()

    for index, stop in enumerate(data.get("stops", []), start=1):
        client = Client.query.filter_by(id=stop.get("client_id"), company_id=company_id).first()
        if not client:
            db.session.rollback()
            return jsonify({"error": "Cliente da parada nao pertence a esta empresa"}), 400
        if base_id and client.base_id and client.base_id != base_id:
            db.session.rollback()
            return jsonify({"error": "Cliente da parada pertence a outra base"}), 400
        try:
            service_minutes = int(stop.get("service_minutes") or 10)
        except (TypeError, ValueError):
            service_minutes = 10
        db.session.add(RouteStop(
            route_id=route.id,
            client_id=client.id,
            sequence=stop.get("sequence", index),
            planned_arrival=stop.get("planned_arrival"),
            service_minutes=max(1, min(240, service_minutes)),
        ))

    db.session.commit()
    return jsonify({"route": route.to_dict()}), 201
