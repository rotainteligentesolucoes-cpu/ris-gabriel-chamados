from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from auth.security import clear_login_failures, create_token, login_block_seconds, login_required, master_required, register_login_failure, resolve_company_id, start_user_session
from database.database import db
from models.company import Company
from models.user import User

user_bp = Blueprint("user_bp", __name__)


@user_bp.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    blocked_seconds = login_block_seconds(email)
    if blocked_seconds:
        return jsonify({"error": "Muitas tentativas. Aguarde alguns minutos e tente novamente."}), 429

    user = User.query.filter_by(email=email).first()
    if not user or not user.active or not check_password_hash(user.password, password):
        register_login_failure(email)
        return jsonify({"error": "Email ou senha invalidos"}), 401

    clear_login_failures(email)
    start_user_session(user)
    db.session.commit()
    return jsonify({"token": create_token(user), "user": user.to_dict()})


@user_bp.route("/auth/me", methods=["GET"])
@login_required
def me():
    return jsonify({"user": request.current_user.to_dict()})


@user_bp.route("/users", methods=["POST"])
@master_required
def create_user():
    data = request.get_json() or {}
    required = ["name", "email", "password", "role"]
    missing = [field for field in required if not data.get(field)]
    if missing:
        return jsonify({"error": "Campos obrigatorios", "fields": missing}), 400

    if User.query.filter_by(email=data["email"].strip().lower()).first():
        return jsonify({"error": "Email ja cadastrado"}), 400

    company_id = resolve_company_id(data)
    role = data["role"]
    if role != "master" and not company_id:
        return jsonify({"error": "company_id e obrigatorio para usuarios de empresa"}), 400

    if company_id and not Company.query.get(company_id):
        return jsonify({"error": "Empresa nao encontrada"}), 404

    user = User(
        company_id=company_id,
        name=data["name"],
        email=data["email"].strip().lower(),
        password=generate_password_hash(data["password"]),
        role=role,
        active=data.get("active", True),
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({"user": user.to_dict()}), 201


@user_bp.route("/users", methods=["GET"])
@login_required
def list_users():
    user = request.current_user
    query = User.query
    if not user.is_master:
        query = query.filter_by(company_id=user.company_id)
    elif request.args.get("company_id", type=int):
        query = query.filter_by(company_id=request.args.get("company_id", type=int))
    return jsonify({"users": [item.to_dict() for item in query.all()]})
