from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash

from database.database import db
from models.user import User

user_bp = Blueprint("user_bp", __name__)

@user_bp.route("/register", methods=["POST"])
def register():

    data = request.json

    if User.query.filter_by(email=data["email"]).first():
        return jsonify({
            "error": "Email já cadastrado"
        }), 400

    hashed_password = generate_password_hash(data["password"])

    user = User(
        name=data["name"],
        email=data["email"],
        password=hashed_password,
        company=data.get("company", ""),
        is_admin=True
    )

    db.session.add(user)
    db.session.commit()

    return jsonify({
        "message": "Usuário criado com sucesso",
        "user": user.to_dict()
    })