from flask import Blueprint, jsonify, request

from auth.security import master_required
from database.database import db
from models.company import Company

company_bp = Blueprint("company_bp", __name__)


@company_bp.route("/companies", methods=["GET"])
@master_required
def list_companies():
    return jsonify({"companies": [company.to_dict() for company in Company.query.all()]})


@company_bp.route("/companies", methods=["POST"])
@master_required
def create_company():
    data = request.get_json() or {}
    if not data.get("name") or not data.get("email"):
        return jsonify({"error": "name e email sao obrigatorios"}), 400

    if Company.query.filter_by(email=data["email"].strip().lower()).first():
        return jsonify({"error": "Email de empresa ja cadastrado"}), 400

    company = Company(name=data["name"], email=data["email"].strip().lower(), active=data.get("active", True))
    db.session.add(company)
    db.session.commit()
    return jsonify({"company": company.to_dict()}), 201
