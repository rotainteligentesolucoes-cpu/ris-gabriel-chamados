import os
from datetime import datetime, timedelta, timezone
from functools import wraps
from time import time
from uuid import uuid4

import jwt
from flask import jsonify, request

from models.user import User


def _jwt_secret():
    return os.getenv("JWT_SECRET", "ris_jwt_enterprise_2026_multiempresa_chave_segura_local")


LOGIN_ATTEMPTS = {}
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))
LOGIN_LOCK_SECONDS = int(os.getenv("LOGIN_LOCK_SECONDS", "900"))


def request_ip():
    cloudflare_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cloudflare_ip:
        return cloudflare_ip
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or request.remote_addr or "unknown"


def login_attempt_key(email):
    normalized_email = (email or "").strip().lower() or "sem-email"
    return f"{request_ip()}::{normalized_email}"


def login_block_seconds(email):
    key = login_attempt_key(email)
    item = LOGIN_ATTEMPTS.get(key)
    now = time()
    if not item:
        return 0
    if now - item.get("first_at", now) > LOGIN_WINDOW_SECONDS:
        LOGIN_ATTEMPTS.pop(key, None)
        return 0
    locked_until = item.get("locked_until", 0)
    if locked_until > now:
        return int(locked_until - now)
    return 0


def register_login_failure(email):
    key = login_attempt_key(email)
    now = time()
    item = LOGIN_ATTEMPTS.get(key)
    if not item or now - item.get("first_at", now) > LOGIN_WINDOW_SECONDS:
        item = {"count": 0, "first_at": now, "locked_until": 0}
    item["count"] += 1
    if item["count"] >= LOGIN_MAX_ATTEMPTS:
        item["locked_until"] = now + LOGIN_LOCK_SECONDS
    LOGIN_ATTEMPTS[key] = item
    return login_block_seconds(email)


def clear_login_failures(email):
    LOGIN_ATTEMPTS.pop(login_attempt_key(email), None)


def start_user_session(user):
    session_id = uuid4().hex
    user.active_session_id = session_id
    user.active_session_started_at = datetime.now(timezone.utc)
    user.active_session_ip = request_ip()[:64]
    user.active_session_user_agent = request.headers.get("User-Agent", "")[:255]
    return session_id


def create_token(user):
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "company_id": user.company_id,
        "sid": user.active_session_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=12),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def current_user():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None

    token = header.replace("Bearer ", "", 1).strip()
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    user = User.query.get(int(user_id))
    if not user:
        return None

    token_session_id = payload.get("sid")
    if getattr(user, "active_session_id", None) and token_session_id != user.active_session_id:
        return None

    return user


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not user.active:
            return jsonify({"error": "Nao autenticado"}), 401
        request.current_user = user
        return func(*args, **kwargs)
    return wrapper


def master_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not user.active:
            return jsonify({"error": "Nao autenticado"}), 401
        if not user.is_master:
            return jsonify({"error": "Acesso exclusivo do usuario master"}), 403
        request.current_user = user
        return func(*args, **kwargs)
    return wrapper


def scoped_query(model):
    user = request.current_user
    query = model.query
    if user.is_master:
        company_id = request.args.get("company_id", type=int)
        if company_id:
            return query.filter_by(company_id=company_id)
        return query
    return query.filter_by(company_id=user.company_id)


def resolve_company_id(data):
    user = request.current_user
    if user.is_master:
        return data.get("company_id")
    return user.company_id

