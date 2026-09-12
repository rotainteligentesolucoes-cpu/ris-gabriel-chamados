import os
import hashlib
from datetime import datetime, timedelta, timezone
from functools import wraps
from time import time
from uuid import uuid4

import jwt
from flask import jsonify, request
from sqlalchemy import false

from models.user import User
from models.company import CompanyBase


ROLE_LEVELS = {
    "driver": 0, "motorista": 0,
    "operator": 1, "operador": 1, "operational": 1, "operacional": 1,
    "manager": 2, "gerente": 2, "admin": 2,
    "ceo": 3, "owner": 3, "proprietario": 3,
    "master": 4,
}


def role_level(user):
    return ROLE_LEVELS.get((getattr(user, "role", "") or "").strip().lower(), 0)


def can_manage_operational_data(user):
    return bool(user.is_master or role_level(user) >= 1)


def can_access_base_id(user, base_id, company_id=None):
    if not base_id:
        return bool(user.is_master or role_level(user) >= 3)
    base = CompanyBase.query.get(base_id)
    if not base:
        return False
    if user.is_master:
        return not company_id or base.company_id == company_id
    if base.company_id != user.company_id or (company_id and base.company_id != company_id):
        return False
    if role_level(user) >= 3:
        return True
    return bool(user.base_id and user.base_id == base.id)


def _jwt_secret():
    return os.getenv("JWT_SECRET", "ris_jwt_enterprise_2026_multiempresa_chave_segura_local")


LOGIN_ATTEMPTS = {}
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))
LOGIN_LOCK_SECONDS = int(os.getenv("LOGIN_LOCK_SECONDS", "900"))
MIN_PASSWORD_LENGTH = int(os.getenv("MIN_PASSWORD_LENGTH", "10"))
_REDIS_CLIENT = None
_REDIS_URL = None


def request_ip():
    cloudflare_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cloudflare_ip:
        return cloudflare_ip
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or request.remote_addr or "unknown"


def login_attempt_key(email):
    normalized_email = (email or "").strip().lower() or "sem-email"
    return f"{request_ip()}::{normalized_email}"


def password_validation_error(password):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Use uma senha com pelo menos {MIN_PASSWORD_LENGTH} caracteres."
    return None


def _redis_client():
    """Return the shared rate-limit store when Redis is configured and reachable."""
    global _REDIS_CLIENT, _REDIS_URL
    redis_url = os.getenv("RATE_LIMIT_REDIS_URL", "").strip() or os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None
    if _REDIS_CLIENT is not None and _REDIS_URL == redis_url:
        return _REDIS_CLIENT
    try:
        import redis

        client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        _REDIS_CLIENT = client
        _REDIS_URL = redis_url
        return client
    except Exception:
        _REDIS_CLIENT = None
        _REDIS_URL = None
        return None


def _redis_login_keys(email):
    digest = hashlib.sha256(login_attempt_key(email).encode("utf-8")).hexdigest()
    return f"ris:login:attempts:{digest}", f"ris:login:lock:{digest}"


def login_block_seconds(email):
    client = _redis_client()
    if client is not None:
        _attempts_key, lock_key = _redis_login_keys(email)
        try:
            ttl = client.ttl(lock_key)
            return max(0, int(ttl or 0))
        except Exception:
            pass
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
    client = _redis_client()
    if client is not None:
        attempts_key, lock_key = _redis_login_keys(email)
        try:
            pipeline = client.pipeline()
            pipeline.incr(attempts_key)
            pipeline.ttl(attempts_key)
            count, ttl = pipeline.execute()
            if int(ttl) < 0:
                client.expire(attempts_key, LOGIN_WINDOW_SECONDS)
            if int(count) >= LOGIN_MAX_ATTEMPTS:
                client.set(lock_key, "1", ex=LOGIN_LOCK_SECONDS)
                return LOGIN_LOCK_SECONDS
            return 0
        except Exception:
            pass
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
    client = _redis_client()
    if client is not None:
        attempts_key, lock_key = _redis_login_keys(email)
        try:
            client.delete(attempts_key, lock_key)
        except Exception:
            pass
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
            query = query.filter_by(company_id=company_id)
            base_id = request.args.get("base_id", type=int)
            if base_id and hasattr(model, "base_id"):
                query = query.filter_by(base_id=base_id)
            return query
        return query
    query = query.filter_by(company_id=user.company_id)
    if role_level(user) == 0 and getattr(model, "__tablename__", None) == "logistic_routes":
        return query.filter_by(driver_user_id=user.id)
    if hasattr(model, "base_id") and role_level(user) < 3:
        if not user.base_id:
            return query.filter(false())
        query = query.filter_by(base_id=user.base_id)
    return query


def resolve_company_id(data):
    user = request.current_user
    if user.is_master:
        return data.get("company_id")
    return user.company_id


def resolve_base_id(data, company_id):
    user = request.current_user
    requested = data.get("base_id")
    if role_level(user) < 3 and not user.is_master and user.base_id:
        requested = user.base_id
    if requested in (None, ""):
        return None
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        return None
    return requested if can_access_base_id(user, requested, company_id) else None

