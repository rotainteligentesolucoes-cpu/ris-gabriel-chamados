import os
from io import BytesIO
import json
import unicodedata
from decimal import Decimal
from functools import wraps
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from urllib.parse import urlparse
from uuid import UUID, uuid4

from flask import Blueprint, current_app, redirect, render_template_string, request, send_file, session, url_for
from dotenv import load_dotenv
from markupsafe import escape
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import folium
import requests
from sqlalchemy import false
from sqlalchemy import or_

load_dotenv()

from auth.security import clear_login_failures, login_block_seconds, register_login_failure, start_user_session
from database.database import db
from models.client import Client
from models.company import Company, CompanyBase
from models.operation import ManualLocation, Merchandise, MerchandiseScan, PublicContactRequest, PublicInsight, ReportSnapshot, RouteBreak, RouteTransfer, StopObservation, StopProof, TelemetryEvent
from models.route import LogisticRoute, RouteStop
from models.saas import AuditLog, CompanySubscription, PlatformEvent, SubscriptionPlan
from services.security_alerts import route_alert_recipient_emails, send_route_access_alert_async
from models.user import User
from models.vehicle import Vehicle
from storage.files import save_bytes, save_upload, send_stored_file

web_bp = Blueprint("web_bp", __name__)


@web_bp.route("/service-worker.js")
def pwa_service_worker():
    response = send_file(os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "service-worker.js"), mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response

COR_TRAJETO = "#1f5eff"
COR_PONTO = "#d92d20"
ROUTE_COLORS = ["#1f5eff", "#f97316", "#16a34a", "#7c3aed", "#dc2626", "#0891b2", "#ca8a04", "#be185d", "#0f766e", "#4338ca"]
BASE_LATITUDE = -29.684200
BASE_LONGITUDE = -53.806900
DEFAULT_SERVICE_MINUTES = 10
ORS_API_KEY = os.getenv("ORS_API_KEY", "")
MAX_SPREADSHEET_MB = int(os.getenv("MAX_SPREADSHEET_MB", "8"))
MAX_PROOF_FILE_MB = int(os.getenv("MAX_PROOF_FILE_MB", "10"))
ALLOWED_SPREADSHEET_EXTENSIONS = {".xlsx"}
ALLOWED_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_INVOICE_EXTENSIONS = {".pdf", ".xml", ".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_ONBOARDING_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
WORKDAY_LIMIT_MINUTES = 10 * 60
INTRADAY_BREAK_MINUTES = 60
INTRADAY_BREAK_WINDOW_START = 4 * 60
INTRADAY_BREAK_WINDOW_END = 6 * 60
INTRADAY_BREAK_REQUIRED_AFTER = 6 * 60
INTERJOURNEY_REST_MINUTES = 11 * 60
TERMS_VERSION = "2026-05-29"

ROLE_LEVELS = {
    "motorista": 0,
    "driver": 0,
    "operator": 1,
    "operador": 1,
    "admin_empresa": 2,
    "manager": 2,
    "gerente": 2,
    "ceo": 3,
    "proprietario": 3,
    "owner": 3,
    "master": 4,
}


def is_master_user(user):
    return bool(user and (getattr(user, "is_master", False) or (getattr(user, "role", "") or "").strip().lower() == "master"))


def same_id(left, right):
    if left is None or right is None:
        return False
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def role_level(user):
    return ROLE_LEVELS.get((user.role or "operator").lower(), 1)


def can_see_admin(user):
    return role_level(user) >= 2


def can_see_finance(user):
    if not user:
        return False
    if role_level(user) >= 3:
        return True
    return bool(getattr(user, "can_view_finance", False) and can_see_admin(user) and not is_driver(user))


def can_grant_finance_access(user):
    return bool(user and (is_master_user(user) or role_level(user) >= 3))


def role_can_receive_finance(role):
    level = ROLE_LEVELS.get((role or "").lower(), 1)
    return 2 <= level < 3


def finance_access_label(user):
    if role_level(user) >= 3:
        return "<span class='badge badge-success'>Padrao do perfil</span>"
    if getattr(user, "can_view_finance", False):
        return "<span class='badge badge-warning'>Liberado pelo CEO</span>"
    return "<span class='muted'>Sem valores</span>"


def can_see_master(user):
    return is_master_user(user)


def can_manage_collaborators(user):
    return bool(user and not is_driver(user) and (is_master_user(user) or can_see_admin(user)))


def can_manage_bases(user):
    return bool(user and not is_driver(user) and (is_master_user(user) or can_see_finance(user) or (can_see_admin(user) and not getattr(user, "base_id", None))))


def is_driver(user):
    return (user.role or "").lower() in ("motorista", "driver")


ROLE_LABELS = {
    "driver": "Motorista",
    "motorista": "Motorista",
    "operator": "Operacional",
    "operador": "Operacional",
    "manager": "Gerente",
    "gerente": "Gerente",
    "admin_empresa": "Gerente",
    "ceo": "CEO / Proprietario",
    "proprietario": "CEO / Proprietario",
    "owner": "CEO / Proprietario",
    "master": "Master",
}


def role_label(role):
    return ROLE_LABELS.get((role or "operator").lower(), role or "Operacional")


def collaborator_role_options(user, selected="operator"):
    roles = [("operator", "Operacional"), ("driver", "Motorista")]
    if can_grant_finance_access(user):
        roles.append(("manager", "Gerente"))
        roles.append(("ceo", "CEO / Proprietario"))
    if user.is_master:
        roles.append(("master", "Master"))
    return "".join(
        f"<option value='{value}' {'selected' if selected == value else ''}>{label}</option>"
        for value, label in roles
    )


def can_assign_role(user, role):
    role = (role or "").lower()
    if user.is_master:
        return role in ROLE_LEVELS
    if role_level(user) >= 3:
        return role in ("operator", "operador", "driver", "motorista", "manager", "gerente", "admin_empresa", "ceo", "proprietario", "owner")
    if can_see_admin(user):
        return role in ("operator", "operador", "driver", "motorista")
    return False


def format_coord(value):
    return f"{float(value):.6f}"


def format_money(value):
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def uploaded_file_size(file_storage):
    stream = file_storage.stream
    current = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(current)
    return size


def validate_upload(file_storage, allowed_extensions, max_mb):
    if not file_storage or not file_storage.filename:
        return None, "Arquivo nao informado."
    filename = secure_filename(file_storage.filename)
    extension = os.path.splitext(filename)[1].lower()
    if extension not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        return None, f"Tipo de arquivo nao permitido. Use: {allowed}."
    max_bytes = max_mb * 1024 * 1024
    if uploaded_file_size(file_storage) > max_bytes:
        return None, f"Arquivo maior que {max_mb} MB."
    return filename, None


def sanitize_external_url(value):
    value = (value or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return value


def route_color(value):
    try:
        index = int(value) - 1
    except (TypeError, ValueError):
        index = 0
    return ROUTE_COLORS[index % len(ROUTE_COLORS)]


def display_date(value):
    if not value:
        return "-"
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return value


def route_sort_key(route):
    return (
        route.planned_date or "9999-12-31",
        route_display_name(route),
        route.id,
    )


def route_free_label(route):
    if route.driver_name:
        return route.driver_name
    if route.driver_user:
        return route.driver_user.name
    return "Equipe/regiao a definir"


def stop_service_minutes(stop):
    value = getattr(stop, "service_minutes", None)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_SERVICE_MINUTES
    return max(1, value or DEFAULT_SERVICE_MINUTES)


def stop_service_description(stop):
    return (getattr(stop, "service_description", None) or "").strip()


def operational_day_from_routes(routes, requested_day=None, selected_route=None):
    if selected_route and selected_route.planned_date:
        return selected_route.planned_date
    if requested_day:
        try:
            datetime.strptime(requested_day, "%Y-%m-%d")
            return requested_day
        except ValueError:
            pass
    today = datetime.now().date().isoformat()
    planned_days = sorted({route.planned_date for route in routes if route.planned_date})
    if today in planned_days:
        return today
    future_days = [day for day in planned_days if day >= today]
    return future_days[0] if future_days else today


def web_login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("web_bp.login_page"))
        user = User.query.get(session["user_id"])
        if not user or not user.active:
            session.clear()
            return redirect(url_for("web_bp.login_page"))
        active_session_id = getattr(user, "active_session_id", None)
        if active_session_id and session.get("session_id") != active_session_id:
            session.clear()
            return redirect(url_for("web_bp.login_page", sessao="encerrada"))
        request.web_user = user
        denied_route = unauthorized_route_requested_by_driver(user)
        if denied_route:
            record_unauthorized_driver_route_access(user, denied_route)
            return layout(
                "Acesso negado",
                "<div class='card'><h2>Esta rota nao pertence ao seu login.</h2>"
                "<p class='muted'>A tentativa foi registrada e a equipe responsavel sera notificada.</p></div>",
                "motorista",
            ), 403
        return func(*args, **kwargs)
    return wrapper


def current_web_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)


def audit_event(action, user=None, company_id=None, resource_type=None, resource_id=None, details=None):
    try:
        if user and not company_id:
            company_id = user.company_id
        db.session.add(AuditLog(
            company_id=company_id,
            user_id=user.id if user else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            ip_address=request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", request.remote_addr),
            user_agent=(request.headers.get("User-Agent") or "")[:255],
            details=details,
        ))
    except Exception:
        pass


def _request_integer(name):
    values = [request.view_args or {}, request.args, request.form]
    payload = request.get_json(silent=True) if request.is_json else {}
    if isinstance(payload, dict):
        values.append(payload)
    for source in values:
        value = source.get(name)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def requested_route_record():
    for field in ("route_id", "from_route_id", "to_route_id"):
        route_id = _request_integer(field)
        if route_id:
            route = LogisticRoute.query.get(route_id)
            if route:
                return route

    stop_id = _request_integer("route_stop_id") or _request_integer("stop_id")
    if stop_id:
        stop = RouteStop.query.get(stop_id)
        if stop:
            return LogisticRoute.query.get(stop.route_id)

    proof_id = _request_integer("proof_id")
    if proof_id:
        proof = StopProof.query.get(proof_id)
        if proof:
            return LogisticRoute.query.get(proof.route_id)

    filename = (request.view_args or {}).get("filename")
    if filename:
        safe_name = secure_filename(filename)
        proof = StopProof.query.filter(
            (StopProof.photo_filename == safe_name) | (StopProof.invoice_file_filename == safe_name)
        ).first()
        if proof:
            return LogisticRoute.query.get(proof.route_id)
    return None


def unauthorized_route_requested_by_driver(user):
    if not is_driver(user):
        return None
    route = requested_route_record()
    if route and route.driver_user_id != user.id:
        return route
    return None


def record_unauthorized_driver_route_access(user, route):
    ip_address = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", request.remote_addr)
    details = (
        f"Motorista {user.name} ({user.email}, ID {user.id}) tentou acessar a rota "
        f"{route.id} atribuida ao usuario {route.driver_user_id or 'nao definido'}. "
        f"Metodo: {request.method}; caminho: {request.full_path[:500]}; IP: {ip_address}."
    )
    audit_event(
        "driver_unauthorized_route_access",
        user=user,
        company_id=route.company_id or user.company_id,
        resource_type="logistic_route",
        resource_id=route.id,
        details=details,
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    recipients = route_alert_recipient_emails(User, route.company_id, route.base_id)
    subject = f"[RIS] Tentativa de acesso indevido a rota {route.id}"
    body = (
        "O RIS bloqueou uma tentativa de acesso a uma rota atribuida a outro motorista.\n\n"
        f"Motorista: {user.name}\n"
        f"Login: {user.email}\n"
        f"ID do motorista: {user.id}\n"
        f"Rota acessada: {route.id} - {route.name or 'Sem nome'}\n"
        f"Motorista responsavel pela rota: {route.driver_user_id or 'Nao atribuido'}\n"
        f"Data/hora UTC: {datetime.utcnow().isoformat()}Z\n"
        f"IP: {ip_address}\n"
        f"Endereco solicitado: {request.url}\n\n"
        "A tentativa foi bloqueada e registrada na auditoria do sistema."
    )
    send_route_access_alert_async(recipients, subject, body, current_app.logger)


BACKUP_MODELS = [
    ("companies", Company),
    ("company_bases", CompanyBase),
    ("users", User),
    ("clients", Client),
    ("vehicles", Vehicle),
    ("manual_locations", ManualLocation),
    ("logistic_routes", LogisticRoute),
    ("route_stops", RouteStop),
    ("route_breaks", RouteBreak),
    ("route_transfers", RouteTransfer),
    ("stop_observations", StopObservation),
    ("stop_proofs", StopProof),
    ("telemetry_events", TelemetryEvent),
    ("merchandises", Merchandise),
    ("merchandise_scans", MerchandiseScan),
    ("report_snapshots", ReportSnapshot),
    ("public_contacts", PublicContactRequest),
    ("public_insights", PublicInsight),
    ("subscription_plans", SubscriptionPlan),
    ("company_subscriptions", CompanySubscription),
    ("audit_logs", AuditLog),
    ("platform_events", PlatformEvent),
]

SENSITIVE_BACKUP_FIELDS = {
    "password",
    "active_session_id",
    "active_session_ip",
    "active_session_user_agent",
    "terms_accepted_ip",
    "signature_data",
}


def backup_directory():
    directory = os.getenv("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.dirname(__file__)), "backups")
    os.makedirs(directory, exist_ok=True)
    return directory


def backup_file_path(filename):
    safe_name = secure_filename(filename or "")
    if not safe_name.startswith("ris_backup_") or not safe_name.endswith(".json"):
        return None
    directory = backup_directory()
    path = os.path.abspath(os.path.join(directory, safe_name))
    if not path.startswith(os.path.abspath(directory)):
        return None
    return path


def latest_backup_file():
    directory = backup_directory()
    filenames = [
        name for name in os.listdir(directory)
        if name.startswith("ris_backup_") and name.endswith(".json")
    ]
    if not filenames:
        return None, None, None
    filenames.sort(reverse=True)
    filename = filenames[0]
    path = os.path.join(directory, filename)
    return filename, path, datetime.fromtimestamp(os.path.getmtime(path))


def serialize_backup_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def serialize_backup_row(item):
    data = {}
    for column in item.__table__.columns:
        key = column.name
        value = getattr(item, key)
        data[key] = "[protegido]" if key in SENSITIVE_BACKUP_FIELDS else serialize_backup_value(value)
    return data


def generate_logical_backup(user=None):
    generated_at = datetime.utcnow()
    payload = {
        "app": "RIS SaaS",
        "backup_type": "logical_json",
        "generated_at": generated_at.isoformat() + "Z",
        "generated_by": getattr(user, "email", None),
        "environment": os.getenv("ENV", "development"),
        "tables": {},
    }
    for table_name, model in BACKUP_MODELS:
        payload["tables"][table_name] = [serialize_backup_row(row) for row in model.query.all()]

    filename = f"ris_backup_{generated_at.strftime('%Y%m%d_%H%M%S')}.json"
    path = os.path.join(backup_directory(), filename)
    with open(path, "w", encoding="utf-8") as backup_file:
        json.dump(payload, backup_file, ensure_ascii=False, indent=2)
    if os.getenv("STORAGE_BACKEND", "local").strip().lower() == "s3":
        save_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "backups", filename, "application/json")
    return filename, path, payload


def scoped_query(model, user):
    query = model.query
    if is_driver(user) and model == LogisticRoute:
        return query.filter_by(driver_user_id=user.id)
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
    if hasattr(model, "base_id") and role_level(user) < 3:
        if not getattr(user, "base_id", None):
            return query.filter(false())
        query = query.filter_by(base_id=user.base_id)
    return query


def scoped_route_record_query(model, user, route_field="route_id"):
    """Scope records that inherit company/base access from a linked route."""
    query = model.query
    route_column = getattr(model, route_field, None)
    if route_column is None:
        return query.filter(false())
    allowed_routes = scoped_query(LogisticRoute, user).with_entities(LogisticRoute.id)
    return query.filter(route_column.in_(allowed_routes))


def can_access_company(user, company_id):
    if is_master_user(user):
        return True
    return same_id(getattr(user, "company_id", None), company_id)


def can_access_base(user, base_id):
    if not base_id:
        return bool(is_master_user(user) or role_level(user) >= 3)
    base = CompanyBase.query.get(base_id)
    if not base:
        return False
    if is_master_user(user):
        return True
    if not same_id(base.company_id, getattr(user, "company_id", None)):
        return False
    if role_level(user) >= 3:
        return True
    return bool(getattr(user, "base_id", None) and same_id(user.base_id, base_id))


def company_dependency_counts(company_id):
    return {
        "bases": CompanyBase.query.filter_by(company_id=company_id).count(),
        "colaboradores": User.query.filter_by(company_id=company_id).count(),
        "clientes": Client.query.filter_by(company_id=company_id).count(),
        "veiculos": Vehicle.query.filter_by(company_id=company_id).count(),
        "rotas": LogisticRoute.query.filter_by(company_id=company_id).count(),
        "mercadorias": Merchandise.query.filter_by(company_id=company_id).count(),
        "comprovantes": StopProof.query.filter_by(company_id=company_id).count(),
        "telemetria": TelemetryEvent.query.filter_by(company_id=company_id).count(),
        "locais": ManualLocation.query.filter_by(company_id=company_id).count(),
    }


def company_can_be_deleted(company_id):
    return not any(company_dependency_counts(company_id).values())


def base_dependency_counts(base_id):
    return {
        "sub-bases": CompanyBase.query.filter_by(parent_base_id=base_id).count(),
        "colaboradores": User.query.filter_by(base_id=base_id).count(),
        "clientes": Client.query.filter_by(base_id=base_id).count(),
        "veiculos": Vehicle.query.filter_by(base_id=base_id).count(),
        "rotas": LogisticRoute.query.filter_by(base_id=base_id).count(),
        "mercadorias": Merchandise.query.filter_by(base_id=base_id).count(),
        "locais": ManualLocation.query.filter_by(base_id=base_id).count(),
    }


def base_can_be_deleted(base_id):
    return not any(base_dependency_counts(base_id).values())


def company_base_options(company_id, selected_id=None, include_all=False):
    bases = CompanyBase.query.filter_by(company_id=company_id, active=True).order_by(CompanyBase.base_type.desc(), CompanyBase.name).all()
    options = "<option value=''>Todas as bases</option>" if include_all else "<option value=''>Sem base definida</option>"
    for base in bases:
        label = f"{base.name} ({'Matriz' if base.base_type == 'matriz' else 'Base'})"
        options += f"<option value='{base.id}' {'selected' if selected_id == base.id else ''}>{escape(label)}</option>"
    return options


def infer_base_id_from_clients(clients, user=None):
    if user and getattr(user, "base_id", None) and role_level(user) < 3:
        return user.base_id
    base_ids = {client.base_id for client in clients if getattr(client, "base_id", None)}
    return next(iter(base_ids)) if len(base_ids) == 1 else None


def can_access_route(user, route):
    if is_driver(user):
        return route.driver_user_id == user.id
    return can_access_company(user, route.company_id) and can_access_base(user, route.base_id)


def distancia_reta_km(points):
    total = 0
    for i in range(len(points) - 1):
        lat1, lon1 = points[i]
        lat2, lon2 = points[i + 1]
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        total += 6371 * 2 * asin(sqrt(a))
    return total


def percurso_pelas_ruas(points):
    """Recebe pontos [lat, lon] e devolve linha Folium [lat, lon] pelas ruas."""
    fallback = {
        "line": points,
        "distance_km": distancia_reta_km(points),
        "real_route": False,
        "message": "Percurso em linha reta: API de rotas indisponivel.",
    }
    if len(points) < 2:
        return fallback

    coordinates = [[lon, lat] for lat, lon in points]
    if ORS_API_KEY and ORS_API_KEY != "SUA_CHAVE_OPENROUTESERVICE":
        try:
            response = requests.post(
                "https://api.openrouteservice.org/v2/directions/driving-car/geojson",
                json={"coordinates": coordinates},
                headers={"Authorization": ORS_API_KEY, "Content-Type": "application/json"},
                timeout=20,
            )
            data = response.json()
            if "features" in data:
                geometry = data["features"][0]["geometry"]["coordinates"]
                summary = data["features"][0].get("properties", {}).get("summary", {})
                return {
                    "line": [[lat, lon] for lon, lat in geometry],
                    "distance_km": (summary.get("distance") or 0) / 1000,
                    "real_route": True,
                    "message": "Percurso calculado pelas ruas.",
                }
            fallback["message"] = f"Percurso em linha reta: API nao retornou trajeto ({data.get('error', data)})."
        except Exception as exc:
            fallback["message"] = f"Percurso em linha reta: falha ao chamar API de rotas ({exc})."

    if len(points) <= 25:
        try:
            coord_text = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
            response = requests.get(
                f"https://router.project-osrm.org/route/v1/driving/{coord_text}",
                params={"overview": "full", "geometries": "geojson", "steps": "false"},
                timeout=20,
            )
            data = response.json()
            if data.get("code") == "Ok" and data.get("routes"):
                route_data = data["routes"][0]
                geometry = route_data["geometry"]["coordinates"]
                return {
                    "line": [[lat, lon] for lon, lat in geometry],
                    "distance_km": (route_data.get("distance") or 0) / 1000,
                    "real_route": True,
                    "message": "Percurso calculado por ruas/rodovias.",
                }
            fallback["message"] = f"Percurso em linha reta: roteador publico nao retornou trajeto ({data.get('message') or data.get('code')})."
        except Exception as exc:
            fallback["message"] = f"Percurso em linha reta: roteador publico indisponivel ({exc})."
    else:
        fallback["message"] = "Percurso em linha reta: rota com muitos pontos para roteador publico."
    return fallback


def route_distance(route):
    stops = sorted(route.stops, key=lambda item: item.sequence)
    points = [[BASE_LATITUDE, BASE_LONGITUDE]]
    for stop in stops:
        points.append([stop.client.latitude, stop.client.longitude])
    points.append([BASE_LATITUDE, BASE_LONGITUDE])
    return distancia_reta_km(points) if len(points) > 1 else 0


def route_cost(route):
    distance = route.estimated_distance_km or route_distance(route)
    speed = route.vehicle.average_speed_kmh if route.vehicle else 60
    base_by_type = {
        "moto": 1.35,
        "carro": 2.10,
        "van": 3.20,
        "caminhao": 5.40,
    }
    km_cost = base_by_type.get((route.vehicle.vehicle_type if route.vehicle else "van"), 3.20)
    time_cost = (distance / speed) * 35 if speed else 0
    return (distance * km_cost) + time_cost


def tempo_formatado(minutes):
    minutes = int(round(minutes))
    h = minutes // 60
    m = minutes % 60
    if h == 0:
        return f"{m} min"
    return f"{h}h {m:02d}min"


def ai_insights(user):
    routes = scoped_query(LogisticRoute, user).all()
    clients = scoped_query(Client, user).all()
    vehicles = scoped_query(Vehicle, user).all()
    high = [client for client in clients if (client.priority or "").lower() == "alta"]
    no_window = [client for client in clients if not client.delivery_window_start or not client.delivery_window_end]
    planned = [route for route in routes if (route.status or "").lower() in ("planned", "planejada")]
    insights = []
    if high:
        insights.append(f"{len(high)} clientes de alta prioridade devem entrar primeiro na validacao das rotas.")
    if no_window:
        insights.append(f"{len(no_window)} clientes ainda precisam de janela de entrega completa.")
    if planned:
        insights.append(f"{len(planned)} rotas planejadas podem ser recalculadas antes da saida.")
    if vehicles and routes:
        slow = min(vehicles, key=lambda item: item.average_speed_kmh)
        insights.append(f"Use {slow.name} ({slow.average_speed_kmh} km/h) em rotas curtas, volumosas ou menos urgentes.")
    if can_see_finance(user) and routes:
        total = sum(route_cost(route) for route in routes)
        insights.append(f"Custo operacional estimado das rotas visiveis: {format_money(total)}.")
    if not insights:
        insights.append("Operacao sem alertas automaticos neste momento.")
    return insights


def layout(title, body, active="dashboard"):
    user = current_web_user()
    is_master_user = bool(user and user.is_master)
    company_name = user.company.name if user and user.company else ("Todas as empresas" if is_master_user else "Empresa")
    base_name = user.base.name if user and getattr(user, "base", None) else ("Todas as bases" if is_master_user or (user and role_level(user) >= 3) else "Base nao definida")
    role = role_label(user.role) if user else ""
    brand_subtitle = "Administracao da plataforma" if is_master_user else "Operacao logistica"
    context_label = "Camada atual" if is_master_user else "Perfil"

    def active_class(name):
        return "active" if active == name else ""

    def nav_section(title, links, open_section=True):
        open_attr = " open" if open_section else ""
        return f'<details class="nav-section"{open_attr}><summary>{title}</summary>{links}</details>'

    admin_links = ""
    finance_links = ""
    master_links = ""
    operational_links = ""
    if user and is_driver(user):
        operational_links = nav_section("Motorista", f'<a class="{active_class("motorista")}" href="/motorista">Minha rota</a>')
    else:
        operational_items = f"""
            <a class="{active_class('dashboard')}" href="/dashboard">Dashboard</a>
            <a class="{active_class('implantacao')}" href="/web/implantacao">Implantacao guiada</a>
            <a class="{active_class('demo_comercial')}" href="/web/demo-comercial">Demo comercial</a>
            <a class="{active_class('operacao')}" href="/web/operacao">Gestao operacional</a>
            <a class="{active_class('simulacao')}" href="/web/simulacao">Simulacao de rotas</a>
            <a class="{active_class('clientes')}" href="/web/clientes">Clientes</a>
            <a class="{active_class('mercadorias')}" href="/web/mercadorias">Mercadorias</a>
            <a class="{active_class('fiscal')}" href="/web/fiscal">Fiscal / Comprovantes</a>
            <a class="{active_class('relatorios')}" href="/web/relatorios">Historico de rotas</a>
        """
        support_items = f"""
            <a class="{active_class('telemetria')}" href="/web/telemetria">Telemetria</a>
            <a class="{active_class('veiculos')}" href="/web/veiculos">Veiculos</a>
            <a class="{active_class('locais')}" href="/web/locais">Oficinas e apoio</a>
        """
        operational_open = active in ("dashboard", "implantacao", "demo_comercial", "operacao", "simulacao", "clientes", "mercadorias", "fiscal", "relatorios", "areas")
        support_open = active in ("telemetria", "veiculos", "locais")
        operational_links = nav_section("Operacional", operational_items, operational_open)
        operational_links += nav_section("Apoio operacional", support_items, support_open)
    if user and can_see_admin(user):
        admin_items = f"""
            <a class="{active_class('bases')}" href="/web/bases">Bases / Unidades</a>
            <a class="{active_class('colaboradores')}" href="/web/colaboradores">Colaboradores</a>
            <a class="{active_class('relatorios')}" href="/web/relatorios">Relatorios 90 dias</a>
        """
        admin_links = nav_section("Gerencia", admin_items, active in ("bases", "colaboradores", "relatorios"))
    if user and can_see_finance(user):
        finance_items = f"""
            <a class="{active_class('financeiro')}" href="/web/financeiro">Valores</a>
        """
        finance_links = nav_section("CEO", finance_items, active in ("financeiro",))
    if user and can_see_master(user):
        master_items = f"""
            <a class="{active_class('contatos')}" href="/web/contatos">Contato publico geral</a>
            <a class="{active_class('insights_publicos')}" href="/web/insights-publicos">Insights publicos</a>
            <a class="{active_class('saude_ia')}" href="/web/saude-ia">Saude e IA</a>
            <a class="{active_class('saas')}" href="/web/saas-producao">SaaS / Producao</a>
            <a class="{active_class('empresas')}" href="/web/empresas">Empresas</a>
            <a class="{active_class('senhas')}" href="/web/redefinir-senha">Redefinir senhas</a>
        """
        master_links = nav_section("Master", master_items, active in ("contatos", "insights_publicos", "saude_ia", "saas", "empresas", "senhas"))
    account_links = nav_section("Conta", '<a href="/logout">Sair</a>', False)

    return f"""
    <!doctype html>
    <html lang="pt-br">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta name="theme-color" content="#031426">
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-title" content="RIS">
        <link rel="manifest" href="/static/manifest.webmanifest">
        <link rel="apple-touch-icon" href="/static/img/pwa-icon-192.png">
        <title>{title} - RIS</title>
        <style>
            :root {{
                --bg:#eef3f8; --sidebar:#101828; --sidebar-soft:#1d2939; --card:#fff;
                --ink:#182235; --muted:#667085; --line:#d7e0ec; --primary:#1f5eff;
                --danger:#d92d20; --success:#14804a; --panel:#f8fafc;
            }}
            * {{ box-sizing:border-box; }}
            body {{ margin:0; background:var(--bg); color:var(--ink); font-family:Arial, sans-serif; }}
            .shell {{ min-height:100vh; display:flex; }}
            .sidebar {{ width:260px; height:100vh; background:var(--sidebar); color:white; padding:18px 14px; position:fixed; inset:0 auto 0 0; overflow-y:auto; overscroll-behavior:contain; scrollbar-width:thin; scrollbar-color:#475467 #101828; }}
            .sidebar::-webkit-scrollbar {{ width:8px; }}
            .sidebar::-webkit-scrollbar-track {{ background:#101828; }}
            .sidebar::-webkit-scrollbar-thumb {{ background:#475467; border-radius:999px; }}
            .menu-toggle {{ position:fixed; left:270px; top:14px; z-index:20; border:1px solid var(--line); background:white; color:var(--sidebar); border-radius:8px; padding:8px 10px; font-weight:900; cursor:pointer; box-shadow:0 6px 18px rgba(16,24,40,.12); }}
            .brand {{ font-size:28px; font-weight:800; margin-bottom:2px; }}
            .brand-sub {{ color:#98a2b3; font-size:12px; margin-bottom:16px; }}
            .nav-section {{ border:1px solid rgba(255,255,255,.08); border-radius:8px; margin:8px 0; background:rgba(255,255,255,.02); overflow:hidden; }}
            .nav-section summary {{ list-style:none; cursor:pointer; color:#98a2b3; font-size:11px; text-transform:uppercase; font-weight:900; padding:9px 10px; letter-spacing:.03em; display:flex; align-items:center; justify-content:space-between; }}
            .nav-section summary::-webkit-details-marker {{ display:none; }}
            .nav-section summary:after {{ content:'+'; font-size:15px; color:#d0d5dd; }}
            .nav-section[open] summary:after {{ content:'-'; }}
            .nav a {{ display:block; color:#d0d5dd; text-decoration:none; padding:9px 11px; border-radius:8px; margin-bottom:3px; font-weight:700; line-height:1.18; white-space:normal; }}
            .nav a:hover, .nav a.active {{ background:var(--sidebar-soft); color:white; }}
            .nav-section a {{ margin:0 7px 5px; }}
            .main {{ margin-left:260px; width:calc(100% - 260px); padding:26px; }}
            body.sidebar-collapsed .sidebar {{ transform:translateX(-100%); }}
            body.sidebar-collapsed .main {{ margin-left:0; width:100%; }}
            body.sidebar-collapsed .menu-toggle {{ left:14px; }}
            .sidebar,.main,.menu-toggle {{ transition:all .18s ease; }}
            .topbar {{ display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:18px; }}
            h1 {{ margin:0; font-size:30px; }} h2 {{ margin:0 0 14px; font-size:20px; }}
            .muted {{ color:var(--muted); }} .user-box {{ text-align:right; font-size:14px; }} .user-box span {{ display:block; color:var(--muted); margin-top:4px; }}
            .grid {{ display:grid; gap:16px; }} .cards {{ grid-template-columns:repeat(4, minmax(0, 1fr)); }} .two {{ grid-template-columns:1.25fr .75fr; align-items:start; }}
            .card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:18px; box-shadow:0 8px 24px rgba(16,24,40,.06); }}
            .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
            .metric {{ margin-top:8px; font-size:30px; font-weight:800; }}
            .table {{ width:100%; border-collapse:collapse; min-width:760px; }} .table-wrap {{ width:100%; overflow-x:auto; }} .table th,.table td {{ padding:12px; border-bottom:1px solid var(--line); text-align:left; font-size:14px; vertical-align:top; }}
            .table th {{ color:var(--muted); font-size:12px; text-transform:uppercase; }}
            .badge {{ display:inline-block; padding:5px 9px; border-radius:999px; background:#eef4ff; color:#1849a9; font-weight:800; font-size:12px; }}
            .badge-danger {{ background:#fef3f2; color:var(--danger); }} .badge-success {{ background:#ecfdf3; color:var(--success); }} .badge-warning {{ background:#fff7ed; color:#c2410c; }}
            .btn {{ display:inline-block; border:0; border-radius:8px; background:var(--primary); color:white; padding:9px 13px; text-decoration:none; font-weight:800; cursor:pointer; }}
            .btn-light {{ background:#eef4ff; color:#1849a9; }} .ai-list {{ margin:0; padding-left:18px; line-height:1.7; }} iframe {{ max-width:100%; }} select,input,button {{ max-width:100%; }}
            .inline-edit-grid {{ display:grid; grid-template-columns:1.3fr 1fr .85fr .85fr auto auto; gap:10px; align-items:end; }}
            .inline-edit-grid label,.checkline {{ font-size:12px; font-weight:800; color:#344054; }}
            .inline-edit-grid input,.inline-edit-grid select {{ width:100%; border:1px solid var(--line); border-radius:6px; padding:8px; }}
            .checkline {{ display:flex; align-items:center; gap:7px; }}
            .checkline input {{ width:auto; }}
            @media (max-width:900px) {{ .shell {{ display:block; }} .sidebar {{ position:static; width:100%; height:auto; max-height:none; overflow:visible; }} .menu-toggle {{ display:none; }} .main {{ margin-left:0; width:100%; padding:14px; }} .cards,.two,.inline-edit-grid {{ grid-template-columns:1fr; }} .topbar {{ display:block; }} .user-box {{ text-align:left; margin-top:12px; }} .card {{ padding:14px; }} .metric {{ font-size:24px; }} h1 {{ font-size:24px; }} h2 {{ font-size:18px; }} .btn {{ width:100%; text-align:center; margin-top:6px; }} iframe {{ height:420px !important; }} }}
        </style>
    </head>
    <body>
        <button class="menu-toggle" type="button" title="Mostrar ou ocultar menu lateral">☰</button>
        <div class="shell">
            <aside class="sidebar">
            <div class="brand">RIS</div><div class="brand-sub">{brand_subtitle}</div>
                <nav class="nav">
                    <a class="{active_class('areas')}" href="/areas">Areas de trabalho</a>
                    {operational_links}
                    {admin_links}{master_links}{finance_links}
                    {account_links}
                </nav>
            </aside>
            <main class="main">
                <div class="topbar"><div><h1>{title}</h1><p class="muted">{context_label}: {role} | {company_name} | {base_name}</p></div><div class="user-box"><strong>{user.name if user else ''}</strong><span>{company_name}</span><span>{base_name}</span></div></div>
                {body}
            </main>
        </div>
        <script>
            if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {{
                window.addEventListener('load', function () {{
                    navigator.serviceWorker.register('/service-worker.js').catch(function () {{}});
                }});
            }}
        </script>
        <script>
            (function () {{
                const key = 'ris.sidebar.collapsed';
                const btn = document.querySelector('.menu-toggle');
                function apply() {{
                    document.body.classList.toggle('sidebar-collapsed', localStorage.getItem(key) === 'true');
                }}
                apply();
                if (btn) btn.addEventListener('click', function () {{
                    localStorage.setItem(key, localStorage.getItem(key) === 'true' ? 'false' : 'true');
                    apply();
                    setTimeout(function () {{ window.dispatchEvent(new Event('resize')); }}, 220);
                }});
            }})();
        </script>
    </body>
    </html>
    """


INSIGHT_ORDER = ["planejamento", "pessoas", "otimizacao", "seguranca", "desempenho", "logistica", "dados", "gestao"]


def public_insights_payload():
    rows = PublicInsight.query.all()
    data = {row.insight_key: row.to_public_dict() for row in rows}
    fallback = {
        "planejamento": {
            "title": "Planejamento",
            "intro": "Planejar deixou de ser apenas montar uma sequencia: agora envolve capacidade, jornada, custo, risco e janela de atendimento antes da execucao.",
            "items": ["Use simulacao para decidir quantidade de rotas, tipo de veiculo, tempo de atendimento e viabilidade operacional antes de oficializar o dia."],
            "source_title": "Gartner Supply Chain Technology Trends 2025",
            "source_url": "https://www.gartner.com/en/newsroom/press-releases/2025-03-18-gartner-identifies-top-supply-chain-technology-trends-for-2025",
            "source_date": "Base 2025 | revisar mensalmente",
        },
        "pessoas": {
            "title": "Pessoas",
            "intro": "A melhor rota tambem protege quem executa: previsibilidade, pausas corretas e menos retrabalho aumentam seguranca, qualidade e engajamento.",
            "items": ["Motorista, operacional, gerencia e CEO trabalham com a mesma verdade operacional, cada um dentro da sua permissao."],
            "source_title": "WEF Thriving Workplaces 2025",
            "source_url": "https://www.weforum.org/publications/thriving-workplaces-how-employers-can-improve-productivity-and-change-lives/",
            "source_date": "Base 2025 | revisar mensalmente",
        },
        "otimizacao": {
            "title": "Otimizacao de Rotas",
            "intro": "A otimizacao moderna combina menor custo, SLA, prioridade, cubagem, peso, janela, descanso legal e historico real de atendimento.",
            "items": ["Rotas acima de 10 horas devem ser quebradas ou continuar em nova jornada com descanso interjornada minimo."],
            "source_title": "DHL Logistics Trend Radar 7.0",
            "source_url": "https://www.dhl.com/us-en/home/insights-and-innovation/insights/logistics-trend-radar.html",
            "source_date": "Radar 7.0 | revisar mensalmente",
        },
        "seguranca": {
            "title": "Seguranca e Conformidade",
            "intro": "SaaS competitivo precisa proteger dados, acessos, comprovantes, termos aceitos, auditoria e rastreabilidade de quem fez cada acao.",
            "items": ["Login por empresa, base, perfil e sessao reduz risco de acesso indevido e fortalece a operacao do cliente."],
            "source_title": "RIS Seguranca 1",
            "source_url": "/termos-de-uso",
            "source_date": "Controle interno | revisar mensalmente",
        },
        "desempenho": {
            "title": "Desempenho e Resultados",
            "intro": "Indicadores bons nao servem so para olhar o passado: eles mostram atraso, produtividade, custo, divergencia e oportunidade de melhoria.",
            "items": ["Exportacao para Excel/PDF e relatorios por 90 dias ajudam o cliente a provar resultado e corrigir gargalos."],
            "source_title": "Gartner Digital Logistics Management",
            "source_url": "https://emt.gartnerweb.com/en/articles/supply-chain-logistics-management",
            "source_date": "Pesquisa 2025 | revisar mensalmente",
        },
        "logistica": {
            "title": "Logistica Eficiente",
            "intro": "A logistica eficiente une roteirizacao, mercadorias, comprovante fiscal, assinatura, foto, motorista no celular e torre de controle.",
            "items": ["Conferencia por QR Code ou codigo de barras reduz mercadoria fora da rota, duplicidade e divergencia no carregamento."],
            "source_title": "Gartner Warehouse Technology Trends 2025",
            "source_url": "https://www.gartner.com/en/documents/6526702",
            "source_date": "Base 2025 | revisar mensalmente",
        },
        "dados": {
            "title": "Inteligencia de Dados",
            "intro": "Dados de GPS, tempo de atendimento, bateria, chegada, saida, cubagem, peso e divergencias formam a base para IA operacional.",
            "items": ["A ferramenta pode aprender por cliente e por rota para sugerir tempos mais realistas a cada ciclo de operacao."],
            "source_title": "McKinsey: Gen AI reshaping supply chains",
            "source_url": "https://www.mckinsey.com/capabilities/operations/our-insights/beyond-automation-how-gen-ai-is-reshaping-supply-chains",
            "source_date": "Base 2025 | revisar mensalmente",
        },
        "gestao": {
            "title": "Gestao e Controle",
            "intro": "Controle bom e simples: a matriz ve tudo, cada base ve sua operacao e cada perfil enxerga somente o que precisa executar.",
            "items": ["Master acompanha empresas, bases, colaboradores, auditoria, saude do sistema e suporte sem expor area interna aos clientes."],
            "source_title": "RIS SaaS Multiempresa",
            "source_url": "/web/implantacao",
            "source_date": "Controle interno | revisar mensalmente",
        },
    }
    for key, value in fallback.items():
        data.setdefault(key, value)
    return {key: data[key] for key in INSIGHT_ORDER}


@web_bp.route("/login", methods=["GET", "POST"])
def login_page():
    error = ""
    contact_notice = ""
    if request.args.get("contato") == "enviado":
        contact_notice = "<div class='contact-ok'>Solicitacao enviada. Nossa equipe retornara pelo contato informado.</div>"
    if request.args.get("sessao") == "encerrada":
        error = "Sessao encerrada: este usuario entrou em outra maquina ou navegador."
    side_state = " login-open contact-open" if contact_notice else (" login-open" if error else "")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        blocked_seconds = login_block_seconds(email)
        if blocked_seconds:
            minutes = max(1, blocked_seconds // 60)
            error = f"Muitas tentativas. Aguarde {minutes} min e tente novamente."
        else:
            user = User.query.filter_by(email=email).first()
            if user and user.active and check_password_hash(user.password, password):
                if user.company and not user.company.active and not user.is_master:
                    error = "Empresa adormecida. Procure o responsavel da plataforma para reativar o acesso."
                    side_state = " login-open"
                    return render_template_string(LOGIN_TEMPLATE, error=error, contact_notice=contact_notice, side_state=side_state, insights_json=json.dumps(public_insights_payload(), ensure_ascii=False))
                if not request.form.get("accept_terms"):
                    error = "Para acessar, aceite os Termos de Uso e Licenca da plataforma."
                    side_state = " login-open"
                    return render_template_string(LOGIN_TEMPLATE, error=error, contact_notice=contact_notice, side_state=side_state, insights_json=json.dumps(public_insights_payload(), ensure_ascii=False))
                clear_login_failures(email)
                user.terms_accepted_at = datetime.utcnow()
                user.terms_accepted_version = TERMS_VERSION
                user.terms_accepted_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", request.remote_addr)
                session_id = start_user_session(user)
                audit_event("login_success", user=user, resource_type="user", resource_id=user.id, details=f"Termos aceitos: {TERMS_VERSION}")
                db.session.commit()
                session.clear()
                session.permanent = True
                session["user_id"] = user.id
                session["session_id"] = session_id
                if is_driver(user):
                    return redirect(url_for("web_bp.driver_page"))
                return redirect(url_for("web_bp.areas_page"))
            wait_seconds = register_login_failure(email)
            if wait_seconds:
                error = "Muitas tentativas. Aguarde alguns minutos antes de tentar novamente."
            else:
                error = "Email ou senha invalidos."
    if error and "login-open" not in side_state:
        side_state += " login-open"
    insights_json = json.dumps(public_insights_payload(), ensure_ascii=False)
    return render_template_string(LOGIN_TEMPLATE, error=error, contact_notice=contact_notice, side_state=side_state, insights_json=insights_json)


LOGIN_TEMPLATE = """
    <!doctype html>
    <html lang="pt-br">
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#031426">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="RIS">
    <link rel="manifest" href="/static/manifest.webmanifest">
    <link rel="apple-touch-icon" href="/static/img/pwa-icon-192.png">
    <title>Login - RIS</title>
    <style>
        * { box-sizing:border-box; }
        body {
            margin:0; min-height:100vh; overflow:hidden; color:white; font-family:Arial, sans-serif;
            background:
                radial-gradient(circle at 24% -8%, rgba(95,174,238,.42), transparent 33%),
                radial-gradient(circle at 66% 42%, rgba(25,110,190,.16), transparent 36%),
                linear-gradient(118deg, #031426 0%, #073765 48%, #021124 100%);
        }
        .wrap {
            min-height:100vh; position:relative; padding:34px 78px 26px 88px;
            display:grid; grid-template-columns:minmax(620px, 1fr) 320px; gap:54px; align-items:center;
        }
        .wrap:before {
            content:""; position:absolute; inset:0; pointer-events:none;
            background:
                linear-gradient(132deg, transparent 0 58%, rgba(130,202,255,.18) 58.15%, transparent 58.65% 76%, rgba(130,202,255,.14) 76.15%, transparent 76.65%),
                radial-gradient(circle at 82% 18%, rgba(124,205,255,.62) 0 3px, transparent 4px),
                radial-gradient(circle at 90% 38%, rgba(124,205,255,.52) 0 3px, transparent 4px),
                radial-gradient(circle at 75% 72%, rgba(124,205,255,.42) 0 3px, transparent 4px);
            opacity:.8;
        }
        .network {
            position:absolute; right:0; top:0; width:46%; height:100%; opacity:.38;
            background:
                linear-gradient(24deg, transparent 12%, rgba(130,202,255,.38) 12.1%, transparent 12.5% 62%, rgba(130,202,255,.22) 62.2%, transparent 62.6%),
                linear-gradient(152deg, transparent 0 42%, rgba(130,202,255,.2) 42.1%, transparent 42.45%);
        }
        .stage { position:relative; z-index:1; min-height:650px; }
        .map-shape {
            position:absolute; left:282px; top:18px; width:440px; height:315px; opacity:.12;
            background:#d7ecff;
            clip-path:polygon(5% 36%,18% 22%,33% 22%,42% 7%,58% 24%,76% 19%,96% 38%,90% 58%,73% 64%,68% 88%,47% 82%,34% 66%,15% 69%);
        }
        .compass {
            position:absolute; left:36px; top:112px; width:356px; height:356px; border-radius:50%;
            border:2px solid rgba(133,238,149,.72);
            background:
                repeating-conic-gradient(from -8deg, rgba(124,226,148,.35) 0deg 1deg, transparent 1deg 8deg),
                radial-gradient(circle, transparent 0 44%, rgba(40,105,255,.16) 45% 47%, transparent 48% 100%);
            box-shadow:0 0 34px rgba(31,94,255,.44), inset 0 0 24px rgba(34,197,94,.18);
        }
        .compass:before {
            content:""; position:absolute; inset:28px; border:2px solid rgba(130,238,151,.42); border-radius:50%;
            box-shadow:inset 0 0 0 22px rgba(31,94,255,.08);
        }
        .compass:after {
            content:""; position:absolute; left:50%; top:50%; width:444px; height:2px;
            background:linear-gradient(90deg, transparent, #b6ff86 18%, #75f7b6 54%, transparent);
            transform:translate(-50%,-50%) rotate(-28deg);
            box-shadow:0 0 14px #82ffc8;
        }
        .ring { position:absolute; inset:68px; border:1px solid rgba(72,126,255,.75); border-radius:50%; }
        .rose { position:absolute; left:50%; top:50%; width:278px; height:278px; transform:translate(-50%,-50%); }
        .rose:before {
            content:""; position:absolute; left:50%; top:50%; width:34px; height:278px; transform:translate(-50%,-50%);
            clip-path:polygon(50% 0, 61% 44%, 100% 50%, 61% 56%, 50% 100%, 39% 56%, 0 50%, 39% 44%);
            background:linear-gradient(#eaffff, #266ad7);
            filter:drop-shadow(0 0 13px rgba(116,232,255,.95));
        }
        .beam {
            position:absolute; left:50%; top:50%; width:334px; height:12px; transform-origin:left center; transform:rotate(-32deg);
            background:linear-gradient(90deg, #eaffff 0, #70ffe3 54%, transparent 100%);
            filter:drop-shadow(0 0 14px #98fff0);
        }
        .center-dot {
            position:absolute; left:50%; top:50%; width:48px; height:48px; border-radius:50%; transform:translate(-50%,-50%);
            background:radial-gradient(circle, #f2fff8 0 20%, #6cf9a7 36%, rgba(34,197,94,.12) 70%);
            box-shadow:0 0 32px #7dffc7;
        }
        .route-line {
            position:absolute; left:332px; top:328px; width:340px; height:170px; pointer-events:none;
            background:
                linear-gradient(155deg, transparent 0 47%, rgba(116,255,161,.8) 47.2% 47.8%, transparent 48%),
                linear-gradient(0deg, transparent 0 74%, rgba(116,255,161,.8) 74.2% 75%, transparent 75.2%);
        }
        .route-line:before, .route-line:after {
            content:""; position:absolute; width:8px; height:8px; border-radius:50%; background:#70ff9c; box-shadow:0 0 12px #70ff9c;
        }
        .route-line:before { left:6px; bottom:34px; }
        .route-line:after { right:0; top:40px; }
        .word { position:absolute; border:0; background:transparent; color:white; padding:0; cursor:pointer; text-align:center; font:inherit; font-weight:900; line-height:1.1; text-shadow:0 2px 8px rgba(0,0,0,.62); }
        .word small { display:block; color:#9cff9c; font-size:22px; text-align:center; margin-top:4px; }
        .word:hover { color:#dfffee; filter:drop-shadow(0 0 10px rgba(112,255,190,.72)); }
        .word:focus-visible { outline:2px solid #70ff9c; outline-offset:5px; border-radius:6px; }
        .w-result { left:26px; top:78px; }
        .w-strategy { left:314px; top:68px; }
        .w-data { left:398px; top:214px; }
        .w-logistics { left:310px; top:388px; }
        .w-economy { left:26px; top:386px; color:#eef7ff; }
        .w-people { left:508px; top:318px; color:#e9ffe9; }
        .brand { position:absolute; left:92px; top:500px; }
        .brand strong { display:block; font-size:76px; line-height:.86; font-weight:900; letter-spacing:0; text-shadow:0 7px 18px rgba(0,0,0,.66); }
        .brand span { display:block; margin-top:13px; font-size:18px; font-weight:900; text-shadow:0 3px 10px rgba(0,0,0,.58); }
        .side { position:relative; z-index:2; width:320px; }
        .panel, .contact-panel {
            width:320px; border:1px solid rgba(255,255,255,.28); border-radius:9px;
            background:linear-gradient(145deg, rgba(255,255,255,.16), rgba(255,255,255,.08));
            box-shadow:0 18px 55px rgba(0,0,0,.38); backdrop-filter:blur(13px);
        }
        .panel { padding:20px 24px; }
        label { display:block; color:#dbeafe; font-size:12px; margin:0 0 5px; }
        input, textarea {
            width:100%; border:1px solid rgba(112,255,139,.86); border-radius:7px;
            background:rgba(5,20,38,.62); color:white; padding:9px 11px; font-size:14px;
            margin-bottom:9px; box-shadow:0 0 14px rgba(34,197,94,.23);
        }
        input { height:34px; }
        textarea { min-height:50px; resize:vertical; font-family:Arial, sans-serif; }
        input::placeholder, textarea::placeholder { color:#a8bfd3; }
        .password-row { position:relative; }
        .password-row:after { content:""; position:absolute; right:13px; top:34px; width:16px; height:9px; border:2px solid rgba(112,255,139,.65); border-top:0; border-radius:0 0 12px 12px; transform:rotate(-20deg); opacity:.75; }
        .btn-main, .contact-toggle, .contact-submit {
            width:100%; height:36px; border:0; border-radius:7px; color:white; font-weight:900; cursor:pointer;
            background:linear-gradient(90deg,#1f5eff,#66e85e); box-shadow:0 8px 24px rgba(31,94,255,.34);
        }
        .contact-toggle {
            margin-top:8px; background:rgba(219,234,254,.1); border:1px solid rgba(255,255,255,.26);
            box-shadow:none; color:#e9f5ff;
        }
        .link, .first { display:block; text-align:right; color:white; font-size:12px; font-weight:800; text-decoration:none; }
        .link { margin:-2px 0 7px; }
        .first { margin-bottom:11px; color:#dbeafe; }
        .terms-check { display:flex; align-items:flex-start; gap:8px; margin:7px 0 12px; color:#dbeafe; font-size:11px; line-height:1.35; }
        .terms-check input { width:auto; height:auto; margin:2px 0 0; box-shadow:none; }
        .terms-check a { color:#9cff9c; font-weight:900; text-decoration:none; }
        .error { color:#ffb4a8; font-size:12px; font-weight:800; min-height:0; margin-bottom:4px; }
        .contact-panel {
            margin-top:10px; padding:0 20px; max-height:0; overflow:hidden; opacity:0; pointer-events:none;
            transform:translateY(-10px); transition:max-height .26s ease, opacity .2s ease, transform .2s ease, padding .2s ease;
            background:linear-gradient(145deg, rgba(6,18,34,.78), rgba(18,45,74,.7));
        }
        .side.contact-open .contact-panel { max-height:410px; opacity:1; pointer-events:auto; transform:translateY(0); padding:13px 20px; }
        .contact-title { font-weight:900; margin-bottom:8px; }
        .contact-ok { color:#bbf7d0; font-size:12px; font-weight:800; margin:0 0 8px; }
        .lgpd {
            position:fixed; right:18px; bottom:12px; color:#dbeafe; background:rgba(255,255,255,.1);
            border:1px solid rgba(255,255,255,.18); border-radius:6px; padding:6px 10px; font-size:11px;
        }
        .insight-modal {
            position:fixed; inset:0; z-index:20; display:none; place-items:center; padding:24px;
            background:rgba(2,8,23,.58); backdrop-filter:blur(4px);
        }
        .insight-modal.open { display:grid; }
        .insight-card {
            width:min(520px, 100%); border:1px solid rgba(132,255,183,.36); border-radius:12px;
            background:linear-gradient(145deg, rgba(7,24,46,.96), rgba(22,58,92,.94));
            box-shadow:0 26px 80px rgba(0,0,0,.52); padding:24px; color:#f8fbff;
        }
        .insight-card h2 { margin:0 0 8px; font-size:25px; }
        .insight-card p { margin:0 0 14px; color:#cfe3f7; line-height:1.5; }
        .insight-source { display:flex; gap:8px; flex-wrap:wrap; align-items:center; font-size:12px; }
        .insight-source a { color:#9cff9c; font-weight:900; text-decoration:none; }
        .insight-card ul { margin:0; padding-left:18px; color:#e8f4ff; line-height:1.65; }
        .insight-card li::marker { color:#70ff9c; }
        .insight-actions { display:flex; justify-content:space-between; gap:12px; align-items:center; margin-top:18px; }
        .insight-tag { color:#9cff9c; font-size:12px; font-weight:900; text-transform:uppercase; }
        .insight-close { width:auto; height:auto; padding:9px 14px; border:1px solid rgba(255,255,255,.24); border-radius:7px; background:rgba(255,255,255,.1); color:white; font-weight:900; cursor:pointer; }
        @media (max-width:940px) {
            body { overflow:auto; }
            .wrap { grid-template-columns:1fr; padding:22px 18px; gap:18px; }
            .stage { min-height:530px; transform:scale(.82); transform-origin:top center; }
            .side, .panel, .contact-panel { width:100%; max-width:360px; margin-left:auto; margin-right:auto; }
            .lgpd { position:static; margin:0 auto 14px; width:max-content; }
        }
        @media (min-width:941px) and (max-height:720px) {
            .wrap { padding:18px 58px 16px 70px; grid-template-columns:minmax(560px, 1fr) 320px; gap:42px; }
            .stage { min-height:560px; transform:scale(.86); transform-origin:center left; }
            .side { transform:translateY(-6px); }
            .brand { top:485px; }
        }
        body {
            background:#031426 url('/static/img/login-rota-mapa.png') center center / cover no-repeat fixed;
        }
        .wrap {
            grid-template-columns:minmax(0, 1fr) 320px;
            gap:34px;
            background:linear-gradient(90deg, rgba(2,8,23,.06), rgba(2,8,23,.18) 72%, rgba(2,8,23,.34));
        }
        .wrap:before, .network, .map-shape, .compass, .route-line, .brand { display:none; }
        .stage {
            position:absolute;
            inset:0;
            min-height:100vh;
            transform:none;
            pointer-events:none;
        }
        .side {
            grid-column:2;
            align-self:center;
            justify-self:end;
        }
        .panel, .contact-panel {
            background:linear-gradient(145deg, rgba(13,39,68,.72), rgba(9,25,46,.56));
            border-color:rgba(216,237,255,.35);
        }
        .word {
            position:fixed;
            display:block;
            min-width:120px;
            min-height:72px;
            color:transparent;
            text-shadow:none;
            border-radius:20px;
            pointer-events:auto;
        }
        .word small { display:none; }
        .word:hover, .word:focus-visible {
            color:transparent;
            outline:2px solid rgba(112,255,190,.8);
            outline-offset:4px;
            background:rgba(34,197,94,.07);
            filter:none;
        }
        .w-planning { left:15%; top:18%; width:18%; height:22%; }
        .w-people { left:43%; top:8%; width:18%; height:16%; }
        .w-optimization { left:69%; top:13%; width:23%; height:20%; }
        .w-security { left:78%; top:40%; width:19%; height:18%; }
        .w-performance { left:66%; top:67%; width:24%; height:21%; }
        .w-logistics { left:39%; top:73%; width:23%; height:18%; }
        .w-data { left:13%; top:66%; width:24%; height:18%; }
        .w-management { left:2%; top:40%; width:23%; height:20%; }
        @media (max-width:940px) {
            body { background-position:center top; }
            .wrap {
                min-height:100vh;
                grid-template-columns:1fr;
                align-items:end;
                padding:22px 16px 24px;
            }
            .stage { transform:none; min-height:100vh; }
            .side { grid-column:1; justify-self:center; transform:none; }
            .word { display:none; }
        }
        .side {
            position:fixed;
            right:22px;
            top:18px;
            width:320px;
            z-index:8;
            align-self:unset;
            justify-self:unset;
        }
        .login-fab {
            width:auto;
            min-width:132px;
            height:38px;
            border:1px solid rgba(216,237,255,.42);
            border-radius:999px;
            background:linear-gradient(135deg, rgba(14,54,92,.86), rgba(6,20,38,.74));
            color:#f8fbff;
            font-weight:900;
            cursor:pointer;
            box-shadow:0 12px 34px rgba(0,0,0,.38), inset 0 0 18px rgba(112,255,190,.08);
            backdrop-filter:blur(10px);
            float:right;
        }
        .login-fab:hover, .login-fab:focus-visible {
            outline:2px solid rgba(112,255,190,.82);
            outline-offset:3px;
        }
        .panel {
            display:none;
            clear:both;
            margin-top:48px;
        }
        .side.login-open .panel {
            display:block;
            animation:loginIn .18s ease-out;
        }
        @keyframes loginIn {
            from { opacity:0; transform:translateY(-8px); }
            to { opacity:1; transform:translateY(0); }
        }
        .password-row:after { display:none; }
        .password-row input { padding-right:58px; }
        .password-toggle {
            position:absolute;
            right:8px;
            top:25px;
            width:42px;
            height:24px;
            border:1px solid rgba(112,255,139,.72);
            border-radius:999px;
            background:rgba(3,16,31,.78);
            color:#dfffee;
            font-size:11px;
            font-weight:900;
            cursor:pointer;
        }
        .contact-panel {
            display:none;
            clear:both;
        }
        .side.login-open.contact-open .contact-panel {
            display:block;
            max-height:410px;
            opacity:1;
            pointer-events:auto;
            transform:translateY(0);
            padding:13px 20px;
        }
        @media (max-width:940px) {
            .side {
                right:14px;
                top:14px;
                width:min(360px, calc(100vw - 28px));
            }
            .panel, .contact-panel { width:100%; }
        }
    </style>
    </head>
    <body>
        <div class="wrap">
            <div class="network"></div>
            <section class="stage">
                <div class="map-shape"></div>
                <div class="compass"><div class="ring"></div><div class="rose"></div><div class="beam"></div><div class="center-dot"></div></div>
                <div class="route-line"></div>
                <button class="word w-planning" type="button" data-insight="planejamento" title="Ver insight de planejamento">PLANEJAMENTO</button>
                <button class="word w-people" type="button" data-insight="pessoas" title="Ver insight de pessoas">PESSOAS</button>
                <button class="word w-optimization" type="button" data-insight="otimizacao" title="Ver insight de otimizacao de rotas">OTIMIZACAO DE ROTAS</button>
                <button class="word w-security" type="button" data-insight="seguranca" title="Ver insight de seguranca e conformidade">SEGURANCA E CONFORMIDADE</button>
                <button class="word w-performance" type="button" data-insight="desempenho" title="Ver insight de desempenho e resultados">DESEMPENHO E RESULTADOS</button>
                <button class="word w-logistics" type="button" data-insight="logistica" title="Ver insight de logistica eficiente">LOGISTICA EFICIENTE</button>
                <button class="word w-data" type="button" data-insight="dados" title="Ver insight de inteligencia de dados">INTELIGENCIA DE DADOS</button>
                <button class="word w-management" type="button" data-insight="gestao" title="Ver insight de gestao e controle">GESTAO E CONTROLE</button>
                <div class="brand"><strong>ROTA</strong><span>INTELIGENTE SOLUCOES</span></div>
            </section>
            <div class="side{{ side_state }}" id="loginSide">
                <button class="login-fab" type="button" id="loginToggle">Acesso / Login</button>
                <form class="panel" method="post">
                    <div class="error">{{ error }}</div>
                    <label>E-mail ou Utilizador</label>
                    <input name="email" type="text" autocomplete="username" required>
                    <div class="password-row">
                        <label>Palavra-passe</label>
                        <input id="passwordInput" name="password" type="password" autocomplete="current-password" required>
                        <button class="password-toggle" type="button" id="passwordToggle" aria-label="Mostrar senha">Ver</button>
                    </div>
                    <a class="link" href="/esqueci-senha">Esqueceu a senha?</a>
                    <a class="first" href="/primeiro-acesso">Ativar Conta (Primeiro Acesso)</a>
                    <label class="terms-check">
                        <input name="accept_terms" type="checkbox" required>
                        <span>Declaro que li e aceito os <a href="/termos-de-uso" target="_blank" rel="noopener">Termos de Uso e Licenca</a>, incluindo proibicao de copiar, revender, sublicenciar, engenharia reversa ou compartilhar acesso sem consentimento.</span>
                    </label>
                    <button class="btn-main" type="submit">ENTRAR NA PLATAFORMA</button>
                    <button class="contact-toggle" type="button" id="contactToggle">Contato comercial</button>
                </form>
                <form class="contact-panel" method="post" action="/contato">
                    <div class="contact-title">Solicitar contato comercial</div>
                    {{ contact_notice|safe }}
                    <label>Nome</label>
                    <input name="name" type="text" autocomplete="name" required placeholder="Seu nome">
                    <label>Contato</label>
                    <input name="contact" type="text" required placeholder="Telefone ou e-mail">
                    <label>Assunto</label>
                    <input name="subject" type="text" required value="Quero conhecer a plataforma">
                    <label>Mensagem</label>
                    <textarea name="message" required>Tenho interesse em conhecer a Rota Inteligente Solucoes e receber mais informacoes.</textarea>
                    <button class="contact-submit" type="submit">ENVIAR SOLICITACAO</button>
                </form>
            </div>
        </div>
        <div class="insight-modal" id="insightModal" aria-hidden="true">
            <div class="insight-card" role="dialog" aria-modal="true" aria-labelledby="insightTitle">
                <h2 id="insightTitle">Insight</h2>
                <p id="insightIntro"></p>
                <p class="insight-source"><span id="insightSourceText"></span><a id="insightSourceLink" href="#" target="_blank" rel="noopener">Abrir fonte</a></p>
                <ul id="insightList"></ul>
                <div class="insight-actions">
                    <span class="insight-tag">Novidades em logistica</span>
                    <button class="insight-close" type="button" id="insightClose">Fechar</button>
                </div>
            </div>
        </div>
        <div class="lgpd">LGPD Ready | Conexao Criptografada</div>
        <script>
            if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
                window.addEventListener('load', function () {
                    navigator.serviceWorker.register('/service-worker.js').catch(function () {});
                });
            }
            const loginToggle = document.getElementById('loginToggle');
            const toggle = document.getElementById('contactToggle');
            const side = document.getElementById('loginSide');
            const passwordInput = document.getElementById('passwordInput');
            const passwordToggle = document.getElementById('passwordToggle');
            if (loginToggle && side) {
                loginToggle.addEventListener('click', function () {
                    side.classList.toggle('login-open');
                    if (!side.classList.contains('login-open')) {
                        side.classList.remove('contact-open');
                    }
                });
            }
            if (toggle && side) {
                toggle.addEventListener('click', function () {
                    side.classList.add('login-open');
                    side.classList.toggle('contact-open');
                });
            }
            if (passwordToggle && passwordInput) {
                passwordToggle.addEventListener('click', function () {
                    const visible = passwordInput.type === 'text';
                    passwordInput.type = visible ? 'password' : 'text';
                    passwordToggle.textContent = visible ? 'Ver' : 'Ocultar';
                    passwordToggle.setAttribute('aria-label', visible ? 'Mostrar senha' : 'Ocultar senha');
                    passwordInput.focus();
                });
            }
            const insightContent = {{ insights_json|safe }};
            const modal = document.getElementById('insightModal');
            const title = document.getElementById('insightTitle');
            const intro = document.getElementById('insightIntro');
            const list = document.getElementById('insightList');
            const sourceText = document.getElementById('insightSourceText');
            const sourceLink = document.getElementById('insightSourceLink');
            function openInsight(key) {
                const data = insightContent[key];
                if (!data || !modal) return;
                title.textContent = data.title;
                intro.textContent = data.intro;
                list.innerHTML = data.items.map(item => '<li>' + item + '</li>').join('');
                sourceText.textContent = 'Fonte: ' + (data.source_title || 'RIS') + (data.source_date ? ' | ' + data.source_date : '');
                if (data.source_url) {
                    sourceLink.href = data.source_url;
                    sourceLink.style.display = 'inline';
                } else {
                    sourceLink.style.display = 'none';
                }
                modal.classList.add('open');
                modal.setAttribute('aria-hidden', 'false');
            }
            function closeInsight() {
                if (!modal) return;
                modal.classList.remove('open');
                modal.setAttribute('aria-hidden', 'true');
            }
            document.querySelectorAll('[data-insight]').forEach(function (button) {
                button.addEventListener('click', function () {
                    openInsight(button.dataset.insight);
                });
            });
            document.getElementById('insightClose').addEventListener('click', closeInsight);
            modal.addEventListener('click', function (event) {
                if (event.target === modal) closeInsight();
            });
            document.addEventListener('keydown', function (event) {
                if (event.key === 'Escape') closeInsight();
            });
        </script>
    </body>
    </html>
    """


@web_bp.route("/termos-de-uso")
def terms_of_use_page():
    return render_template_string(f"""
    <!doctype html>
    <html lang="pt-br">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Termos de Uso e Licenca - RIS</title>
        <style>
            body {{ margin:0; background:#eef3f8; color:#182235; font-family:Arial,sans-serif; line-height:1.55; }}
            main {{ max-width:920px; margin:0 auto; padding:32px 18px; }}
            .card {{ background:white; border:1px solid #d7e0ec; border-radius:8px; padding:24px; box-shadow:0 8px 24px rgba(16,24,40,.06); }}
            h1 {{ margin-top:0; }} h2 {{ margin-top:24px; font-size:19px; }}
            .badge {{ display:inline-block; padding:5px 9px; border-radius:999px; background:#eef4ff; color:#1849a9; font-weight:900; font-size:12px; }}
            .notice {{ border-left:4px solid #1f5eff; background:#f5f8ff; padding:12px 14px; border-radius:6px; }}
            a {{ color:#1849a9; font-weight:800; }}
        </style>
    </head>
    <body>
        <main>
            <section class="card">
                <span class="badge">Versao {TERMS_VERSION}</span>
                <h1>Termos de Uso e Licenca da Plataforma RIS</h1>
                <p class="notice">Este texto cria uma barreira comercial e tecnica para uso da plataforma. A versao final deve ser revisada por advogado antes de venda em escala.</p>

                <h2>1. Propriedade intelectual</h2>
                <p>A plataforma RIS, seus fluxos operacionais, telas, codigo, estrutura de dados, layouts, textos, marcas, relatorios, logica de roteirizacao e organizacao comercial pertencem a Rota Inteligente Solucoes ou ao titular autorizado. O acesso ao sistema nao transfere propriedade intelectual ao usuario, empresa cliente, colaborador, parceiro ou terceiro.</p>

                <h2>2. Licenca de uso limitada</h2>
                <p>O usuario recebe apenas uma licenca limitada, temporaria, nao exclusiva, nao transferivel e revogavel para usar a plataforma conforme o plano contratado e as permissoes liberadas.</p>

                <h2>3. Proibicoes</h2>
                <p>E proibido copiar, reproduzir, vender, revender, sublicenciar, alugar, emprestar, ceder acesso, compartilhar senhas, capturar telas para replicacao comercial, realizar engenharia reversa, extrair banco de dados, automatizar raspagem de dados ou criar produto concorrente baseado no uso interno da plataforma sem autorizacao expressa e por escrito.</p>

                <h2>4. Acesso por usuario</h2>
                <p>Cada login e individual. A plataforma pode registrar data, IP, dispositivo, navegador, sessao ativa e aceite destes termos para protecao contra uso indevido, compartilhamento de acesso e investigacao de incidentes.</p>

                <h2>5. Dados do cliente</h2>
                <p>Os dados operacionais inseridos pelo cliente pertencem ao cliente, mas a tecnologia, metodologia, interface, codigo e funcionamento da plataforma continuam protegidos. O cliente deve usar os dados conforme LGPD e conforme suas responsabilidades comerciais.</p>

                <h2>6. Suspensao e encerramento</h2>
                <p>O acesso podera ser suspenso ou encerrado em caso de tentativa de copia, revenda, compartilhamento indevido, quebra de confidencialidade, uso fora do plano contratado ou risco a seguranca da plataforma.</p>

                <h2>7. Confidencialidade</h2>
                <p>Informacoes internas, funcionalidades ainda nao publicas, regras de negocio, precos, telas administrativas e recursos de suporte devem ser tratados como confidenciais.</p>

                <h2>8. Consentimento</h2>
                <p>Ao marcar aceite no login e acessar a plataforma, o usuario confirma que leu, compreendeu e concorda com estes termos.</p>

                <p><a href="/login">Voltar ao login</a></p>
            </section>
        </main>
    </body>
    </html>
    """)


@web_bp.route("/logout")
def logout_page():
    user = current_web_user()
    if user and session.get("session_id") == getattr(user, "active_session_id", None):
        user.active_session_id = None
        user.active_session_started_at = None
        user.active_session_ip = None
        user.active_session_user_agent = None
        db.session.commit()
    session.clear()
    return redirect(url_for("web_bp.login_page"))


@web_bp.route("/contato", methods=["POST"])
def public_contact_page():
    name = request.form.get("name", "").strip()
    contact = request.form.get("contact", "").strip()
    subject = request.form.get("subject", "").strip()
    message = request.form.get("message", "").strip()
    if name and contact and subject and message:
        from database.database import db
        db.session.add(PublicContactRequest(
            name=name[:255],
            contact=contact[:255],
            subject=subject[:255],
            message=message,
            status="novo",
        ))
        db.session.commit()
    return redirect(url_for("web_bp.login_page", contato="enviado"))


@web_bp.route("/")
def root_page():
    return redirect(url_for("web_bp.login_page"))


@web_bp.route("/areas")
@web_login_required
def areas_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))

    def module_card(title, description, href, icon, color, area):
        return f"""
        <a class="module-card" href="{href}" data-area="{area}">
            <span class="module-icon" style="background:{color};">{icon}</span>
            <span>
                <strong>{title}</strong>
                <small>{description}</small>
            </span>
        </a>
        """

    def mvp_step(number, title, description, href):
        return f"""
        <a class="mvp-step" href="{href}">
            <span>{number}</span>
            <strong>{title}</strong>
            <small>{description}</small>
        </a>
        """

    mvp_steps = [
        mvp_step("1", "Login e perfis", "Cada usuario entra direto no perfil liberado: operacional, gerencia, CEO ou motorista.", "/areas"),
        mvp_step("2", "Gestao operacional", "Torre do dia com rotas, mapa, linha do tempo, alertas, intervalos e execucao.", "/web/operacao"),
        mvp_step("3", "Simulacao / importacao / exportacao", "Importa planilha, simula rotas, ajusta custo/tempo e exporta o resultado.", "/web/simulacao"),
        mvp_step("4", "Motorista no celular", "Responsavel assume a rota, registra chegada, saida, observacoes, telemetria e comprovantes.", "/motorista"),
        mvp_step("5", "Dashboard e relatorio", "Cliente acompanha indicadores, clientes atendidos, rotas recentes e exporta Excel.", "/dashboard"),
    ]

    operational_cards = [
        module_card("Implantacao Guiada", "Checklist comercial para ativar empresa, bases, colaboradores, clientes, rotas, execucao e relatorios.", "/web/implantacao", "IG", "#1f5eff", "Operacional"),
        module_card("Demonstracao Comercial", "Roteiro guiado para simular, oficializar, operar, abrir motorista e exportar relatorio.", "/web/demo-comercial", "DC", "#0f766e", "Operacional"),
        module_card("Gestao operacional", "Acompanhar mapas, pontos, previsoes e execucao das rotas.", "/web/operacao", "GO", "#1f5eff", "Operacional"),
        module_card("Dashboard Operacional", "Indicadores, graficos e exportacao operacional para Excel.", "/dashboard", "DB", "#38bdf8", "Operacional"),
        module_card("Simulacao de Rotas", "Simule sequencias, tipos de veiculo, tempo e custo antes de gravar a operacao.", "/web/simulacao", "SR", "#f59e0b", "Operacional"),
        module_card("Cadastro de Clientes", "Cadastrar nome do cliente, latitude e longitude para montar rotas.", "/web/clientes", "CL", "#84cc16", "Operacional"),
        module_card("Mercadorias e Lotes", "Cadastrar, importar, conferir por QR Code/codigo de barras e rastrear divergencias.", "/web/mercadorias", "ML", "#7c3aed", "Operacional"),
        module_card("Fiscal e Comprovantes", "Consultar nota, assinatura, foto, documento do recebedor e prova de entrega/coleta.", "/web/fiscal", "FC", "#0f766e", "Operacional"),
    ]

    support_cards = [
        module_card("Telemetria", "Acompanhar no mapa o caminho GPS, horario, latitude, longitude e velocidade.", "/web/telemetria", "TL", "#0ea5e9", "Apoio operacional"),
        module_card("Veiculos", "Cadastrar e editar veiculos, tipo, velocidade media e status operacional.", "/web/veiculos", "VE", "#8b5cf6", "Apoio operacional"),
        module_card("Oficinas e Apoio", "Cadastrar oficinas, pontos de apoio e locais externos usados pela operacao.", "/web/locais", "OA", "#f97316", "Apoio operacional"),
    ]

    admin_cards = []
    if can_see_admin(user):
        admin_cards = [
            module_card("Bases e Unidades", "Cadastrar matriz, bases e filiais para separar usuarios, rotas e clientes.", "/web/bases", "BU", "#0f766e", "ADM / Operacional"),
            module_card("Colaboradores", "Cadastrar operadores, motoristas e usuarios da empresa conforme permissao.", "/web/colaboradores", "US", "#2563eb", "ADM / Operacional"),
            module_card("Relatorios 90 Dias", "Gerar e consultar snapshots operacionais armazenados.", "/web/relatorios", "RL", "#06b6d4", "ADM / Operacional"),
        ]

    finance_cards = []
    if can_see_finance(user):
        finance_cards = [
            module_card("Contabilidade e Valores", "Visualizar custos operacionais estimados e visao financeira das rotas.", "/web/financeiro", "CV", "#ef4444", "Operacional / ADM / Contabilidade"),
            module_card("Exportar Dashboard", "Exportar indicadores e dados operacionais para Excel.", "/web/exportar-dashboard.xlsx", "EX", "#14b8a6", "Operacional / ADM / Contabilidade"),
        ]

    sections = [
        ("Operacional", "Rotina diaria, monitoramento e execucao das rotas.", "".join(operational_cards)),
        ("Apoio operacional", "Telemetria, veiculos, oficinas e pontos de apoio no mesmo grupo. Descanso fica junto dos intervalos da rota.", "".join(support_cards)),
    ]
    if admin_cards:
        sections.append(("ADM / Operacional", "Cadastros, apoio de rota e relatorios administrativos.", "".join(admin_cards)))
    if finance_cards:
        sections.append(("Operacional / ADM / Contabilidade", "Valores, custos e exportacoes para proprietario ou CEO.", "".join(finance_cards)))

    section_html = "".join(
        f"""
        <section class="area-section">
            <h2>{title}</h2>
            <p>{subtitle}</p>
            <div class="module-list">{cards}</div>
        </section>
        """
        for title, subtitle, cards in sections
    )

    body = f"""
    <style>
        .area-home {{ max-width:960px; margin:0 auto; }}
        .mvp-panel {{ background:#fff; border:1px solid #c9d8f4; border-radius:8px; padding:16px; margin-bottom:14px; box-shadow:0 10px 28px rgba(31,94,255,.08); }}
        .mvp-panel h2 {{ margin:0; font-size:20px; color:#0b1f3a; }}
        .mvp-panel p {{ margin:6px 0 14px; color:#52627a; font-size:13px; }}
        .mvp-flow {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; }}
        .mvp-step {{ display:grid; gap:6px; align-content:start; min-height:142px; border:1px solid #d9e3f5; border-radius:8px; padding:12px; color:#182235; text-decoration:none; background:linear-gradient(180deg,#f8fbff,#fff); }}
        .mvp-step:hover {{ border-color:#1f5eff; box-shadow:0 8px 18px rgba(31,94,255,.12); }}
        .mvp-step span {{ display:grid; place-items:center; width:28px; height:28px; border-radius:999px; background:#1f5eff; color:white; font-weight:900; }}
        .mvp-step strong {{ font-size:14px; line-height:1.2; }}
        .mvp-step small {{ color:#667085; line-height:1.35; }}
        .area-section {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:16px; margin-bottom:14px; }}
        .area-section h2 {{ margin:0; font-size:18px; color:#182235; }}
        .area-section p {{ margin:5px 0 12px; color:#667085; font-size:13px; }}
        .module-list {{ display:grid; gap:10px; }}
        .module-card {{ display:grid; grid-template-columns:48px 1fr; gap:12px; align-items:center; color:#182235; text-decoration:none; border:1px solid #e4e7ec; border-radius:8px; padding:12px; background:#fbfcfe; }}
        .module-card:hover {{ border-color:#1f5eff; background:#f5f8ff; }}
        .module-icon {{ display:grid; place-items:center; width:42px; height:42px; border-radius:6px; color:#fff; font-weight:900; font-size:13px; }}
        .module-card strong {{ display:block; font-size:15px; margin-bottom:4px; }}
        .module-card small {{ display:block; color:#667085; line-height:1.35; }}
        @media (max-width:900px) {{ .mvp-flow {{ grid-template-columns:1fr; }} }}
        @media (max-width:700px) {{ .area-home {{ max-width:100%; }} .module-card {{ grid-template-columns:42px 1fr; }} }}
    </style>
    <div class="area-home">
        <section class="mvp-panel">
            <h2>Pacote comercial para demonstracao</h2>
            <p>Fluxo principal para vender a ideia: acesso por perfil, planejamento, controle, execucao no celular e relatorio para o cliente.</p>
            <div class="mvp-flow">{''.join(mvp_steps)}</div>
        </section>
        {section_html}
    </div>
    """
    return layout("Areas de trabalho", body, "areas")


@web_bp.route("/web/implantacao")
@web_login_required
def guided_implementation_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))

    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    selected_company_id = request.args.get("company_id", type=int)
    if not selected_company_id:
        selected_company_id = user.company_id if not user.is_master else (companies[0].id if companies else None)
    if selected_company_id and not can_access_company(user, selected_company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "implantacao"), 403

    selected_company = Company.query.get(selected_company_id) if selected_company_id else None
    selected_base_id = request.args.get("base_id", type=int)
    if getattr(user, "base_id", None) and role_level(user) < 3:
        selected_base_id = user.base_id
    if selected_base_id and not can_access_base(user, selected_base_id):
        selected_base_id = None

    def filtered_query(model):
        query = model.query
        if hasattr(model, "company_id") and selected_company_id:
            query = query.filter_by(company_id=selected_company_id)
        if selected_base_id and hasattr(model, "base_id"):
            query = query.filter_by(base_id=selected_base_id)
        return query

    base_query = CompanyBase.query.filter_by(company_id=selected_company_id, active=True) if selected_company_id else CompanyBase.query.filter_by(active=True)
    if selected_base_id:
        base_query = base_query.filter_by(id=selected_base_id)
    base_count = base_query.count()

    collaborator_count = filtered_query(User).filter(User.active == True, User.role != "master").count()
    driver_count = filtered_query(User).filter(User.active == True, User.role.in_(("driver", "motorista"))).count()
    client_count = filtered_query(Client).filter(Client.active == True).count()
    vehicle_count = filtered_query(Vehicle).filter(Vehicle.active == True).count()
    merchandise_count = filtered_query(Merchandise).count()
    official_routes_query = filtered_query(LogisticRoute).filter(LogisticRoute.planned_date.isnot(None))
    official_routes_count = official_routes_query.count()
    today = datetime.now().date().isoformat()
    today_routes_count = official_routes_query.filter(LogisticRoute.planned_date == today).count()
    completed_routes_count = official_routes_query.filter(LogisticRoute.status.in_(("completed", "concluida", "finalizada", "realizada"))).count()
    reports_count = ReportSnapshot.query.filter_by(company_id=selected_company_id).count() if selected_company_id else 0
    monitoring_url = os.getenv("UPTIME_MONITOR_URL", "").strip() or os.getenv("MONITORING_URL", "").strip()
    monitoring_done = bool(monitoring_url)

    company_done = bool(selected_company and selected_company.active)
    bases_done = base_count > 0
    collaborators_done = collaborator_count > 0
    clients_done = client_count > 0
    simulation_ready = clients_done and vehicle_count > 0
    official_done = official_routes_count > 0
    execution_done = today_routes_count > 0 or completed_routes_count > 0
    reports_done = reports_count > 0

    steps = [
        {
            "key": "empresa",
            "title": "Empresa",
            "description": "Dados da empresa ativos para operar como cliente SaaS.",
            "count": selected_company.name if selected_company else "Nao selecionada",
            "done": company_done,
            "ready": company_done,
            "href": "/web/empresas" if user.is_master else "/areas",
            "action": "Revisar empresa" if user.is_master else "Ver areas",
        },
        {
            "key": "bases",
            "title": "Bases / unidades",
            "description": "Matriz, filial ou base operacional separando rotas, usuarios e clientes.",
            "count": f"{base_count} cadastrada(s)",
            "done": bases_done,
            "ready": company_done,
            "href": "/web/bases",
            "action": "Cadastrar base",
        },
        {
            "key": "colaboradores",
            "title": "Colaboradores",
            "description": "Operadores, gerentes, motoristas e responsaveis por perfil.",
            "count": f"{collaborator_count} ativo(s) | {driver_count} motorista(s)",
            "done": collaborators_done,
            "ready": bases_done or company_done,
            "href": "/web/colaboradores",
            "action": "Cadastrar colaborador",
        },
        {
            "key": "clientes",
            "title": "Clientes / pontos",
            "description": "Nome, endereco, latitude, longitude, janela e prioridade de atendimento.",
            "count": f"{client_count} ponto(s)",
            "done": clients_done,
            "ready": collaborators_done or bases_done,
            "href": "/web/clientes",
            "action": "Cadastrar ponto",
        },
        {
            "key": "simulacao",
            "title": "Simulacao de rotas",
            "description": "Importar planilha, calcular quantidade de rotas, custo, tempo e veiculo ideal.",
            "count": f"{vehicle_count} veiculo(s) disponivel(is)",
            "done": official_done,
            "ready": simulation_ready,
            "href": "/web/simulacao",
            "action": "Simular rotas",
        },
        {
            "key": "rotas",
            "title": "Rotas oficiais",
            "description": "Transformar a simulacao em rota oficial por data de execucao.",
            "count": f"{official_routes_count} oficial(is)",
            "done": official_done,
            "ready": simulation_ready,
            "href": "/web/operacao",
            "action": "Abrir rotas",
        },
        {
            "key": "execucao",
            "title": "Execucao",
            "description": "Gestao operacional, motorista no celular, mercadorias, comprovantes e telemetria.",
            "count": f"{today_routes_count} rota(s) hoje | {merchandise_count} mercadoria(s)",
            "done": execution_done,
            "ready": official_done,
            "href": "/web/operacao",
            "action": "Abrir gestao",
        },
        {
            "key": "relatorios",
            "title": "Relatorios",
            "description": "Exportar dados e manter historico operacional para o cliente acompanhar resultado.",
            "count": f"{reports_count} salvo(s)",
            "done": reports_done,
            "ready": execution_done or official_done,
            "href": "/web/relatorios",
            "action": "Gerar relatorio",
        },
    ]

    done_count = sum(1 for step in steps if step["done"])
    progress = int(round((done_count / len(steps)) * 100))
    next_step = next((step for step in steps if not step["done"] and step["ready"]), None)
    if not next_step:
        next_step = next((step for step in steps if not step["done"]), steps[-1])

    company_options = ""
    if user.is_master:
        company_options = "".join(
            f"<option value='{company.id}' {'selected' if company.id == selected_company_id else ''}>{escape(company.name)}</option>"
            for company in companies
        )
        if not company_options:
            company_options = "<option value=''>Nenhuma empresa cadastrada</option>"
    base_selector = ""
    if selected_company_id and (user.is_master or role_level(user) >= 2):
        base_selector = f"""
            <label>Base / unidade</label>
            <select name="base_id">
                {company_base_options(selected_company_id, selected_base_id, include_all=True)}
            </select>
        """

    filter_form = ""
    if user.is_master or (selected_company_id and role_level(user) >= 2):
        company_field = ""
        if user.is_master:
            company_field = f"""
                <label>Empresa</label>
                <select name="company_id">{company_options}</select>
            """
        filter_form = f"""
        <form class="implant-filter" method="get">
            {company_field}
            {base_selector}
            <button class="btn" type="submit">Atualizar visao</button>
        </form>
        """

    def status_badge(step):
        if step["done"]:
            return "<span class='implant-badge done'>Concluido</span>"
        if step["ready"]:
            return "<span class='implant-badge next'>Proximo passo</span>"
        return "<span class='implant-badge wait'>Pendente</span>"

    def step_card(index, step):
        return f"""
        <article class="implant-step {'is-done' if step['done'] else ''}">
            <div class="step-number">{index}</div>
            <div class="step-main">
                <div class="step-head">
                    <h3>{step['title']}</h3>
                    {status_badge(step)}
                </div>
                <p>{step['description']}</p>
                <strong>{escape(step['count'])}</strong>
            </div>
            <a class="step-action" href="{step['href']}">{step['action']}</a>
        </article>
        """

    step_cards = "".join(step_card(index, step) for index, step in enumerate(steps, start=1))
    launch_ready = all(step["done"] for step in steps[:7])
    launch_badge = "Pronto para vender" if launch_ready else "Implantacao em andamento"
    launch_text = "O cliente ja tem estrutura operacional para demonstracao e uso acompanhado." if launch_ready else "Complete o proximo passo para transformar os modulos em uma jornada comercial clara."
    pilot_items = [
        ("Empresa cliente real", company_done, "Cadastrar a empresa real que vai validar a RIS fora do ambiente de demonstracao."),
        ("Base e usuarios", bases_done and collaborators_done, "Criar base/unidade, operador, gestor e motorista para testar permissoes por perfil."),
        ("Pontos reais", clients_done, "Cadastrar clientes com endereco, latitude, longitude, janela e prioridade reais."),
        ("Rota oficial", official_done, "Gerar uma rota oficial com data definida para acompanhar a operacao."),
        ("Teste em celular", execution_done, "Validar login do motorista, execucao, comprovantes e leitura em rede 4G/5G."),
        ("Relatorio final", reports_done, "Salvar relatorio para comparar promessa, execucao e resultado do cliente."),
        ("Monitor externo", monitoring_done, "Manter UptimeRobot, Better Stack ou Grafana consultando /readyz a cada 5 minutos."),
    ]
    pilot_done_count = sum(1 for _, done, _ in pilot_items if done)
    pilot_progress = int(round((pilot_done_count / len(pilot_items)) * 100))
    pilot_cards = "".join(
        f"""
        <div class="pilot-card {'ok' if done else 'pending'}">
            <span>{'OK' if done else 'Pendente'}</span>
            <strong>{title}</strong>
            <p>{description}</p>
        </div>
        """
        for title, done, description in pilot_items
    )
    empty_company_notice = ""
    if user.is_master and not companies:
        empty_company_notice = """
        <div class="card" style="margin-bottom:16px; border-color:#f59e0b;">
            <h2>Nenhuma empresa cadastrada</h2>
            <p class="muted">Este ambiente de producao esta limpo. Cadastre a primeira empresa cliente para liberar o checklist de implantacao.</p>
            <a class="btn" href="/web/empresas">Cadastrar primeira empresa</a>
        </div>
        """

    body = f"""
    <style>
        .implant-hero {{ display:grid; grid-template-columns:1.1fr .9fr; gap:16px; margin-bottom:16px; }}
        .implant-progress {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:18px; box-shadow:0 8px 24px rgba(16,24,40,.06); }}
        .implant-progress h2 {{ margin:0; font-size:22px; }}
        .implant-progress p {{ margin:8px 0 14px; color:#667085; line-height:1.45; }}
        .progress-row {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:8px; font-weight:900; }}
        .progress-track {{ height:12px; border-radius:999px; background:#e4e7ec; overflow:hidden; }}
        .progress-fill {{ width:{progress}%; height:100%; background:linear-gradient(90deg,#1f5eff,#22c55e); }}
        .implant-next {{ background:#0b1f3a; color:white; border-radius:8px; padding:18px; box-shadow:0 8px 24px rgba(16,24,40,.12); }}
        .implant-next small {{ display:inline-block; background:rgba(255,255,255,.14); padding:5px 9px; border-radius:999px; font-weight:900; margin-bottom:10px; }}
        .implant-next h2 {{ margin:0 0 8px; color:white; }}
        .implant-next p {{ color:#d0d5dd; line-height:1.45; }}
        .implant-next .btn {{ margin-top:10px; background:#22c55e; color:#07111f; }}
        .implant-filter {{ display:grid; grid-template-columns:1fr 1fr auto; gap:10px; align-items:end; background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:14px; margin-bottom:16px; }}
        .implant-filter label {{ display:block; font-size:12px; font-weight:900; color:#344054; margin-bottom:5px; }}
        .implant-filter select {{ width:100%; border:1px solid #d7e0ec; border-radius:8px; padding:9px; background:white; }}
        .implant-grid {{ display:grid; gap:10px; }}
        .implant-step {{ display:grid; grid-template-columns:42px 1fr auto; align-items:center; gap:12px; background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:14px; box-shadow:0 6px 18px rgba(16,24,40,.04); }}
        .implant-step.is-done {{ border-color:#b7e4c7; background:#fbfffd; }}
        .step-number {{ display:grid; place-items:center; width:34px; height:34px; border-radius:999px; background:#eef4ff; color:#1849a9; font-weight:900; }}
        .is-done .step-number {{ background:#ecfdf3; color:#14804a; }}
        .step-head {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
        .step-head h3 {{ margin:0; font-size:17px; }}
        .step-main p {{ margin:5px 0 6px; color:#667085; line-height:1.4; }}
        .step-main strong {{ color:#182235; }}
        .implant-badge {{ display:inline-block; padding:4px 8px; border-radius:999px; font-size:11px; font-weight:900; }}
        .implant-badge.done {{ background:#ecfdf3; color:#14804a; }}
        .implant-badge.next {{ background:#fff7ed; color:#c2410c; }}
        .implant-badge.wait {{ background:#f2f4f7; color:#667085; }}
        .step-action {{ display:inline-block; border-radius:8px; padding:9px 12px; text-decoration:none; background:#eef4ff; color:#1849a9; font-weight:900; white-space:nowrap; }}
        .implant-summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:16px 0; }}
        .summary-card {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:14px; }}
        .summary-card span {{ color:#667085; font-size:12px; font-weight:900; text-transform:uppercase; }}
        .summary-card strong {{ display:block; margin-top:6px; font-size:22px; }}
        .pilot-panel {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:16px; margin:16px 0; box-shadow:0 6px 18px rgba(16,24,40,.04); }}
        .pilot-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; }}
        .pilot-head h2 {{ margin:0 0 6px; }}
        .pilot-head p {{ margin:0; color:#667085; line-height:1.45; }}
        .pilot-score {{ min-width:150px; text-align:right; font-weight:900; color:#1849a9; }}
        .pilot-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
        .pilot-card {{ border:1px solid #d7e0ec; border-radius:8px; padding:12px; background:#f8fafc; }}
        .pilot-card span {{ display:inline-block; border-radius:999px; padding:4px 8px; font-size:11px; font-weight:900; margin-bottom:8px; background:#fff7ed; color:#c2410c; }}
        .pilot-card.ok span {{ background:#ecfdf3; color:#14804a; }}
        .pilot-card strong {{ display:block; color:#182235; }}
        .pilot-card p {{ margin:6px 0 0; color:#667085; line-height:1.35; font-size:13px; }}
        .infra-note {{ display:grid; grid-template-columns:1fr auto; gap:12px; align-items:center; border:1px solid #fed7aa; background:#fff7ed; border-radius:8px; padding:12px; margin-top:12px; }}
        .infra-note strong {{ color:#9a3412; }}
        .infra-note p {{ margin:4px 0 0; color:#7c2d12; line-height:1.4; }}
        .infra-note a {{ white-space:nowrap; }}
        @media (max-width:900px) {{
            .implant-hero,.implant-filter,.implant-summary,.pilot-grid,.infra-note {{ grid-template-columns:1fr; }}
            .implant-step {{ grid-template-columns:34px 1fr; }}
            .step-action {{ grid-column:2; width:100%; text-align:center; }}
            .pilot-score {{ text-align:left; }}
        }}
    </style>
    {empty_company_notice}
    {filter_form}
    <section class="implant-hero">
        <div class="implant-progress">
            <h2>Painel de implantacao SaaS</h2>
            <p>Checklist para transformar a ferramenta em produto vendavel: empresa, bases, equipe, pontos, rotas, execucao e relatorios em uma unica jornada.</p>
            <div class="progress-row"><span>{done_count} de {len(steps)} etapas concluidas</span><strong>{progress}%</strong></div>
            <div class="progress-track"><div class="progress-fill"></div></div>
        </div>
        <div class="implant-next">
            <small>{launch_badge}</small>
            <h2>{next_step['title']}</h2>
            <p>{launch_text}</p>
            <a class="btn" href="{next_step['href']}">{next_step['action']}</a>
        </div>
    </section>
    <section class="implant-summary">
        <div class="summary-card"><span>Bases</span><strong>{base_count}</strong></div>
        <div class="summary-card"><span>Colaboradores</span><strong>{collaborator_count}</strong></div>
        <div class="summary-card"><span>Clientes / pontos</span><strong>{client_count}</strong></div>
        <div class="summary-card"><span>Rotas oficiais</span><strong>{official_routes_count}</strong></div>
    </section>
    <section class="pilot-panel">
        <div class="pilot-head">
            <div>
                <h2>Piloto com cliente real</h2>
                <p>Use este bloco para conduzir a primeira operacao acompanhada: configurar cliente, rodar uma rota real, colher feedback e transformar isso em material comercial.</p>
            </div>
            <div class="pilot-score">{pilot_done_count} de {len(pilot_items)} prontos<br>{pilot_progress}%</div>
        </div>
        <div class="pilot-grid">{pilot_cards}</div>
        <div class="infra-note">
            <div>
                <strong>Atencao antes de vender como 24/7</strong>
                <p>O dominio HTTPS esta correto, mas se o web service da Render estiver no plano Free ele pode hibernar por inatividade. Para cliente pagante, ajuste o Instance Type do servico ris-saas para Starter ou superior.</p>
            </div>
            <a class="btn btn-secondary" href="/web/saas-producao">Ver prontidao SaaS</a>
        </div>
    </section>
    <section class="implant-grid">{step_cards}</section>
    """
    return layout("Implantacao guiada", body, "implantacao")


@web_bp.route("/web/saas-producao", methods=["GET", "POST"])
@web_login_required
def saas_production_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Area interna de administracao da plataforma.</h2></div>", "saas"), 403

    notice = ""
    if request.method == "POST":
        company_id = request.form.get("company_id", type=int)
        plan_id = request.form.get("plan_id", type=int)
        status = request.form.get("status", "trial").strip() or "trial"
        company = Company.query.get(company_id)
        plan = SubscriptionPlan.query.get(plan_id) if plan_id else None
        if company:
            subscription = CompanySubscription.query.filter_by(company_id=company.id).first()
            if not subscription:
                subscription = CompanySubscription(company_id=company.id)
                db.session.add(subscription)
            subscription.plan_id = plan.id if plan else None
            subscription.status = status[:40]
            subscription.billing_email = request.form.get("billing_email", "").strip() or company.email
            subscription.renewal_day = request.form.get("renewal_day", type=int)
            subscription.notes = request.form.get("notes", "").strip() or None
            audit_event("subscription_update", user=user, company_id=company.id, resource_type="company_subscription", resource_id=company.id, details=f"Plano: {plan.name if plan else 'sem plano'} | Status: {status}")
            db.session.commit()
            notice = "<div class='badge badge-success'>Assinatura atualizada.</div>"
        else:
            notice = "<div class='badge badge-danger'>Empresa nao encontrada.</div>"

    database_url = os.getenv("DATABASE_URL", "sqlite:///ris.db")
    using_postgres = database_url.startswith(("postgresql://", "postgres://"))
    env = os.getenv("ENV", "development")
    backup_enabled = os.getenv("BACKUP_ENABLED", "").strip().lower() in ("1", "true", "sim", "yes")
    backup_filename, backup_path, backup_at = latest_backup_file()
    backup_size = os.path.getsize(backup_path) if backup_path and os.path.exists(backup_path) else 0
    automatic_backup = (
        PlatformEvent.query
        .filter_by(category="backup", title="Backup automatico diario concluido", resolved=True)
        .order_by(PlatformEvent.created_at.desc())
        .first()
    )
    automatic_backup_at = automatic_backup.created_at if automatic_backup else None
    automatic_backup_ok = bool(
        automatic_backup_at and automatic_backup_at >= datetime.utcnow() - timedelta(hours=36)
    )
    recent_backup_ok = bool(
        backup_at and backup_at >= datetime.utcnow() - timedelta(hours=36) and backup_size > 0
    )
    if automatic_backup and automatic_backup.path:
        automatic_filename = secure_filename(os.path.basename(automatic_backup.path))
        if automatic_filename and (not backup_at or automatic_backup_at >= backup_at):
            backup_filename = automatic_filename
            backup_path = backup_file_path(automatic_filename)
            backup_at = automatic_backup_at
            backup_size = os.path.getsize(backup_path) if backup_path and os.path.exists(backup_path) else 0
    monitoring_enabled = bool(os.getenv("MONITORING_URL", "").strip() or os.getenv("UPTIME_MONITOR_URL", "").strip())
    secret_ready = os.getenv("SECRET_KEY", "") not in ("", "ris_enterprise_2026")
    jwt_ready = os.getenv("JWT_SECRET", "") not in ("", "ris_jwt_enterprise_2026_multiempresa_chave_segura_local")
    master_password_ready = os.getenv("MASTER_PASSWORD", "RIS@12345") != "RIS@12345"
    audit_count = AuditLog.query.count()
    plan_count = SubscriptionPlan.query.filter_by(active=True).count()
    subscription_count = CompanySubscription.query.count()
    company_count = Company.query.count()
    render_instance_type = os.getenv("RENDER_INSTANCE_TYPE", "").strip() or os.getenv("RENDER_PLAN", "").strip()
    render_instance_label = render_instance_type or "Verificar no painel da Render"
    render_instance_ready = bool(render_instance_type and render_instance_type.lower() not in ("free", "starter-free"))

    readiness = [
        ("Banco PostgreSQL em cloud", using_postgres, "Configurar DATABASE_URL com PostgreSQL gerenciado antes da venda em escala."),
        ("Backup automatico diario", backup_enabled, "Rotina interna diaria configurada; acompanhe a ultima execucao no painel e mantenha tambem o backup nativo do Render em producao."),
        ("Ambiente producao separado", env == "production", "Rodar producao com ENV=production e manter ambiente local apenas para testes."),
        ("Logs de erro e auditoria", audit_count >= 0, f"Auditoria criada. Eventos registrados: {audit_count}."),
        ("Monitoramento 24/7", monitoring_enabled, f"Monitor externo deve consultar {default_readyz_url} e validar status=ready, database=ok e environment=production."),
        ("Painel de saude", True, "Endpoints ativos: /healthz e /readyz."),
        ("Controle de planos/assinaturas", plan_count > 0, f"Planos ativos: {plan_count}. Assinaturas cadastradas: {subscription_count}."),
        ("Login por empresa/base/perfil", True, "Multiempresa, bases/unidades e perfis ja estao estruturados."),
        ("Termos e licenca", True, f"Termos ativos na versao {TERMS_VERSION} com aceite registrado no usuario."),
        ("Segredos de producao", secret_ready and jwt_ready and master_password_ready, "Trocar SECRET_KEY, JWT_SECRET e MASTER_PASSWORD antes de ativar producao."),
    ]
    ready_count = sum(1 for _, done, _ in readiness if done)
    progress = int(round((ready_count / len(readiness)) * 100))

    checklist_html = "".join(
        f"""
        <div class="saas-check {'ok' if done else 'pending'}">
            <span>{'✓' if done else '!'}</span>
            <div>
                <strong>{title}</strong>
                <small>{description}</small>
            </div>
        </div>
        """
        for title, done, description in readiness
    )

    plans = SubscriptionPlan.query.order_by(SubscriptionPlan.monthly_price, SubscriptionPlan.name).all()
    subscriptions = CompanySubscription.query.order_by(CompanySubscription.created_at.desc()).all()
    companies = Company.query.order_by(Company.name).all()
    plan_options = "".join(f"<option value='{plan.id}'>{escape(plan.name)} - {format_money(plan.monthly_price)}</option>" for plan in plans)
    company_options = "".join(f"<option value='{company.id}'>{escape(company.name)}</option>" for company in companies)
    subscription_rows = "".join(
        f"""
        <tr>
            <td>{escape(subscription.company.name if subscription.company else '-')}</td>
            <td>{escape(subscription.plan.name if subscription.plan else 'Sem plano')}</td>
            <td><span class="badge">{escape(subscription.status)}</span></td>
            <td>{escape(subscription.billing_email or '-')}</td>
            <td>{subscription.renewal_day or '-'}</td>
        </tr>
        """
        for subscription in subscriptions
    ) or "<tr><td colspan='5'>Nenhuma assinatura cadastrada.</td></tr>"

    audit_rows = "".join(
        f"""
        <tr>
            <td>{log.created_at.strftime('%d/%m/%Y %H:%M') if log.created_at else '-'}</td>
            <td>{escape(log.action)}</td>
            <td>{escape(log.user.name if log.user else '-')}</td>
            <td>{escape(log.company.name if log.company else '-')}</td>
            <td>{escape(log.ip_address or '-')}</td>
        </tr>
        """
        for log in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(12).all()
    ) or "<tr><td colspan='5'>Nenhum evento registrado ainda.</td></tr>"
    if backup_enabled and (automatic_backup_ok or recent_backup_ok):
        backup_status = "Automatico ativo"
        backup_status_class = "badge-success"
    elif backup_at:
        backup_status = "Manual gerado"
        backup_status_class = "badge"
    elif backup_enabled:
        backup_status = "Automatico sem backup recente"
        backup_status_class = "badge-danger"
    else:
        backup_status = "Pendente"
        backup_status_class = "badge-danger"
    backup_at_text = backup_at.strftime("%d/%m/%Y %H:%M") if backup_at else "Nenhum backup gerado ainda"
    backup_size_text = f"{backup_size / 1024:.1f} KB" if backup_size else "-"
    backup_download = f"<a class='btn btn-secondary' href='/web/backup/baixar?file={escape(backup_filename)}'>Baixar ultimo backup</a>" if backup_filename else ""

    body = f"""
    <style>
        .saas-hero {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }}
        .saas-progress {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:18px; }}
        .saas-progress h2 {{ margin:0; }}
        .bar {{ height:12px; background:#e4e7ec; border-radius:999px; overflow:hidden; margin:12px 0; }}
        .fill {{ height:100%; width:{progress}%; background:linear-gradient(90deg,#1f5eff,#22c55e); }}
        .saas-checks {{ display:grid; gap:10px; }}
        .saas-check {{ display:grid; grid-template-columns:34px 1fr; gap:10px; align-items:start; background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:12px; }}
        .saas-check span {{ display:grid; place-items:center; width:28px; height:28px; border-radius:999px; font-weight:900; }}
        .saas-check.ok span {{ background:#ecfdf3; color:#14804a; }}
        .saas-check.pending span {{ background:#fff7ed; color:#c2410c; }}
        .saas-check strong {{ display:block; }}
        .saas-check small {{ display:block; color:#667085; margin-top:4px; line-height:1.35; }}
        .saas-form {{ display:grid; grid-template-columns:1fr 1fr 1fr 90px 1fr auto; gap:10px; align-items:end; }}
        .saas-form label {{ display:block; font-size:12px; color:#344054; font-weight:900; margin-bottom:5px; }}
        .saas-form input,.saas-form select {{ width:100%; border:1px solid #d7e0ec; border-radius:8px; padding:9px; }}
        .mono {{ font-family:Consolas, monospace; font-size:13px; background:#f8fafc; border:1px solid #d7e0ec; border-radius:8px; padding:10px; }}
        .backup-panel {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:12px 0; }}
        .backup-box {{ border:1px solid #d7e0ec; border-radius:8px; padding:12px; background:#f8fafc; }}
        .backup-box span {{ display:block; color:#667085; font-size:12px; font-weight:900; text-transform:uppercase; margin-bottom:5px; }}
        .backup-actions {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }}
        .monitor-panel {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:16px; }}
        .monitor-box {{ border:1px solid #d7e0ec; border-radius:8px; padding:12px; background:#f8fafc; }}
        .monitor-box span {{ display:block; color:#667085; font-size:12px; font-weight:900; text-transform:uppercase; margin-bottom:5px; }}
        .infra-panel {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-top:16px; }}
        .infra-box {{ border:1px solid #d7e0ec; border-radius:8px; padding:12px; background:#f8fafc; }}
        .infra-box.warning {{ border-color:#fed7aa; background:#fff7ed; }}
        .infra-box span {{ display:block; color:#667085; font-size:12px; font-weight:900; text-transform:uppercase; margin-bottom:5px; }}
        .infra-box.warning span,.infra-box.warning strong {{ color:#9a3412; }}
        @media (max-width:1100px) {{ .saas-hero,.saas-form {{ grid-template-columns:1fr; }} }}
        @media (max-width:800px) {{ .backup-panel,.monitor-panel,.infra-panel {{ grid-template-columns:1fr; }} }}
    </style>
    <section class="saas-hero">
        <div class="saas-progress">
            <h2>Prontidao SaaS profissional</h2>
            <p class="muted">Base estrutural para vender com estabilidade: cloud, banco, backup, auditoria, planos, monitoramento e termos.</p>
            <div class="bar"><div class="fill"></div></div>
            <strong>{ready_count} de {len(readiness)} itens prontos ({progress}%)</strong>
            <div class="mono" style="margin-top:12px;">Health: /healthz<br>Ready: /readyz<br>Ambiente: {escape(env)}<br>Empresas cadastradas: {company_count}</div>
        </div>
        <div class="saas-checks">{checklist_html}</div>
    </section>

    <section class="card" style="margin-top:16px;">
        <h2>Monitoramento 24/7</h2>
        <p class="muted">Configure UptimeRobot, Better Stack ou Grafana Cloud para validar o endpoint de prontidao da producao. O monitor interno do Codex foi pausado porque o ambiente local gerou falso alerta TLS.</p>
        <div class="monitor-panel">
            <div class="monitor-box"><span>Endpoint oficial</span><strong class="mono">{default_readyz_url}</strong></div>
            <div class="monitor-box"><span>Monitor externo</span><strong>{'Configurado' if monitoring_enabled else 'Pendente'}</strong></div>
            <div class="monitor-box"><span>URL configurada</span><strong class="mono">{escape(monitoring_target)}</strong></div>
            <div class="monitor-box"><span>Resposta esperada</span><strong class="mono">database=ok | environment=production | status=ready</strong></div>
        </div>
    </section>

    <section class="card" style="margin-top:16px;">
        <h2>Infraestrutura 24/7</h2>
        <p class="muted">Dominio customizado e HTTPS confirmam acesso seguro, mas a disponibilidade continua dependendo do plano do web service. Instancia Free pode hibernar por inatividade.</p>
        <div class="infra-panel">
            <div class="infra-box"><span>Dominio publico</span><strong class="mono">https://app.rotainteligentesolucoes.com.br</strong></div>
            <div class="infra-box"><span>Render web service</span><strong>{escape(render_instance_label)}</strong></div>
            <div class="infra-box {'warning' if not render_instance_ready else ''}"><span>Acao antes do cliente pagante</span><strong>{'Sem hibernacao configurada' if render_instance_ready else 'Trocar Free por Starter ou superior'}</strong></div>
        </div>
    </section>

    <section class="card" style="margin-top:16px;">
        <h2>Backup e restauracao</h2>
        <p class="muted">Backup logico da plataforma para contingencia. O arquivo protege campos sensiveis e deve ser baixado para um local seguro. Em producao, mantenha tambem o backup nativo do PostgreSQL no Render.</p>
        <div class="backup-panel">
            <div class="backup-box"><span>Status</span><strong class="badge {backup_status_class}">{backup_status}</strong></div>
            <div class="backup-box"><span>Ultimo backup</span><strong>{backup_at_text}</strong></div>
            <div class="backup-box"><span>Arquivo</span><strong>{escape(backup_filename or '-')}</strong></div>
            <div class="backup-box"><span>Tamanho</span><strong>{backup_size_text}</strong></div>
        </div>
        <div class="backup-actions">
            <form method="post" action="/web/backup/gerar">
                <button class="btn" type="submit">Gerar backup agora</button>
            </form>
            {backup_download}
        </div>
    </section>

    <section class="card">
        <h2>Controle de planos e assinaturas</h2>
        {notice}
        <form class="saas-form" method="post">
            <div><label>Empresa</label><select name="company_id" required>{company_options}</select></div>
            <div><label>Plano</label><select name="plan_id">{plan_options}</select></div>
            <div><label>Status</label><select name="status"><option value="trial">Trial</option><option value="active">Ativa</option><option value="past_due">Pendente</option><option value="paused">Pausada</option><option value="cancelled">Cancelada</option></select></div>
            <div><label>Renova dia</label><input name="renewal_day" type="number" min="1" max="31"></div>
            <div><label>E-mail cobranca</label><input name="billing_email" type="email"></div>
            <button class="btn" type="submit">Salvar</button>
            <div style="grid-column:1/-1;"><label>Observacao</label><input name="notes" placeholder="Contrato, condicao comercial ou observacao interna"></div>
        </form>
        <div class="table-wrap" style="margin-top:14px;">
            <table class="table">
                <thead><tr><th>Empresa</th><th>Plano</th><th>Status</th><th>Cobranca</th><th>Renovacao</th></tr></thead>
                <tbody>{subscription_rows}</tbody>
            </table>
        </div>
    </section>

    <section class="card" style="margin-top:16px;">
        <h2>Auditoria recente</h2>
        <div class="table-wrap">
            <table class="table">
                <thead><tr><th>Data</th><th>Acao</th><th>Usuario</th><th>Empresa</th><th>IP</th></tr></thead>
                <tbody>{audit_rows}</tbody>
            </table>
        </div>
    </section>
    """
    return layout("SaaS / Producao", body, "saas")


@web_bp.route("/web/backup/gerar", methods=["POST"])
@web_login_required
def generate_backup_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Area exclusiva master.</h2></div>", "saas"), 403

    filename, path, payload = generate_logical_backup(user)
    total_rows = sum(len(rows) for rows in payload["tables"].values())
    backup_enabled = os.getenv("BACKUP_ENABLED", "").strip().lower() in ("1", "true", "sim", "yes")
    event_title = "Backup automatico diario concluido" if backup_enabled else "Backup logico gerado"
    event_message = (
        f"Execucao sob demanda da rotina diaria gerou {filename} com {total_rows} registros exportados."
        if backup_enabled
        else f"Arquivo {filename} gerado com {total_rows} registros exportados."
    )
    db.session.add(PlatformEvent(
        severity="info",
        category="backup",
        title=event_title,
        message=event_message,
        path=path,
        user_id=user.id,
        ip_address=request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", request.remote_addr),
        user_agent=(request.headers.get("User-Agent") or "")[:255],
        resolved=True,
    ))
    audit_event(
        "backup_auto_generate" if backup_enabled else "backup_generate",
        user=user,
        resource_type="backup",
        resource_id=filename,
        details=event_message,
    )
    db.session.commit()
    return redirect(url_for("web_bp.saas_production_page"))


@web_bp.route("/web/backup/baixar")
@web_login_required
def download_backup_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Area exclusiva master.</h2></div>", "saas"), 403

    filename = request.args.get("file", "")
    path = backup_file_path(filename)
    storage_backend = os.getenv("STORAGE_BACKEND", "local").strip().lower()
    if (not path or not os.path.exists(path)) and storage_backend != "s3":
        return layout("Backup nao encontrado", "<div class='card'><h2>Arquivo de backup nao encontrado.</h2></div>", "saas"), 404

    audit_event("backup_download", user=user, resource_type="backup", resource_id=secure_filename(filename), details="Download de backup logico.")
    db.session.commit()
    if storage_backend == "s3" and (not path or not os.path.exists(path)):
        return send_stored_file("backups", secure_filename(filename), as_attachment=True, download_name=secure_filename(filename))
    return send_file(path, as_attachment=True, download_name=secure_filename(filename), mimetype="application/json")


@web_bp.route("/web/saude-ia")
@web_login_required
def platform_health_ai_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Area exclusiva master.</h2></div>", "saude_ia"), 403

    now = datetime.now()
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    database_ok = True
    database_message = "Banco respondendo"
    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception as exc:
        database_ok = False
        database_message = str(exc)[:160]

    recent_events = PlatformEvent.query.order_by(PlatformEvent.created_at.desc()).limit(12).all()
    critical_24h = PlatformEvent.query.filter(PlatformEvent.created_at >= last_24h, PlatformEvent.severity == "critical").count()
    warning_24h = PlatformEvent.query.filter(PlatformEvent.created_at >= last_24h, PlatformEvent.severity.in_(("warning", "attention"))).count()
    unresolved_count = PlatformEvent.query.filter_by(resolved=False).count()
    audit_24h = AuditLog.query.filter(AuditLog.created_at >= last_24h).count()

    today = now.date().isoformat()
    future_limit = (now.date() + timedelta(days=2)).isoformat()
    monitored_routes = LogisticRoute.query.filter(
        LogisticRoute.planned_date.isnot(None),
        LogisticRoute.planned_date >= today,
        LogisticRoute.planned_date <= future_limit,
    ).order_by(LogisticRoute.planned_date, LogisticRoute.name).all()

    telemetry_route_ids = {
        item.route_id for item in TelemetryEvent.query.filter(TelemetryEvent.created_at >= last_24h, TelemetryEvent.route_id.isnot(None)).all()
    }
    proof_route_ids = {
        item.route_id for item in StopProof.query.filter(StopProof.created_at >= last_7d).all()
    }
    divergence_count = MerchandiseScan.query.filter(
        MerchandiseScan.created_at >= last_7d,
        MerchandiseScan.result_status.in_(("fora_rota", "duplicado", "divergente", "erro")),
    ).count()

    route_alerts = []
    for route in monitored_routes:
        stop_count = len(route.stops)
        duration = int(route.estimated_duration_minutes or route_work_minutes_without_break(route) or 0)
        pending_count = sum(1 for stop in route.stops if (stop.status or "pendente") != "realizado")
        route_name = route_display_name(route)
        if duration > WORKDAY_LIMIT_MINUTES:
            route_alerts.append(("critico", route, f"{route_name}: previsao acima de 10h ({tempo_formatado(duration)}). Dividir rota ou criar continuidade com interjornada."))
        elif duration > INTRADAY_BREAK_REQUIRED_AFTER and not RouteBreak.query.filter_by(route_id=route.id, active=True).first():
            route_alerts.append(("atencao", route, f"{route_name}: acima de 6h sem intervalo registrado. Inserir intrajornada entre 4h e 6h."))
        if route.planned_date == today and route.id not in telemetry_route_ids:
            route_alerts.append(("critico", route, f"{route_name}: rota do dia sem telemetria recebida."))
        if stop_count and pending_count == stop_count and route.planned_date == today:
            route_alerts.append(("atencao", route, f"{route_name}: todos os pontos ainda pendentes hoje."))
        if route.id not in proof_route_ids and route.planned_date and route.planned_date < today:
            route_alerts.append(("info", route, f"{route_name}: sem comprovantes recentes vinculados."))

    client_profiles = []
    clients = Client.query.filter_by(active=True).all()
    for client in clients:
        stops = list(client.stops or [])
        if not stops:
            continue
        service_values = [stop_service_minutes(stop) for stop in stops]
        avg_service = sum(service_values) / len(service_values)
        route_ids = {stop.route_id for stop in stops}
        realized = sum(1 for stop in stops if (stop.status or "") == "realizado")
        client_profiles.append({
            "client": client,
            "avg_service": avg_service,
            "samples": len(stops),
            "routes": len(route_ids),
            "realized": realized,
        })
    client_profiles.sort(key=lambda item: (item["avg_service"], item["samples"]), reverse=True)

    adaptive_recommendations = []
    for item in client_profiles[:8]:
        if item["avg_service"] > DEFAULT_SERVICE_MINUTES + 5:
            adaptive_recommendations.append(
                f"{item['client'].name}: atendimento medio {int(round(item['avg_service']))} min em {item['samples']} parada(s). Sugestao: usar esse tempo como padrao nas proximas simulacoes."
            )
    long_routes = [route for route in monitored_routes if int(route.estimated_duration_minutes or 0) > WORKDAY_LIMIT_MINUTES]
    if long_routes:
        adaptive_recommendations.append(f"{len(long_routes)} rota(s) futuras acima de 10h. Sugestao: dividir por base/regiao ou programar continuidade com descanso de 11h.")
    if divergence_count:
        adaptive_recommendations.append(f"{divergence_count} divergencia(s) de mercadoria nos ultimos 7 dias. Sugestao: revisar carregamento por rota antes da saida.")
    if warning_24h:
        adaptive_recommendations.append(f"{warning_24h} alerta(s) tecnico(s) nas ultimas 24h. Sugestao: acompanhar estabilidade antes de demonstracao para cliente.")
    if not adaptive_recommendations:
        adaptive_recommendations.append("Sem aprendizado critico no momento. Conforme o uso crescer, a IA melhora previsao de atendimento, risco e produtividade por cliente.")

    event_rows = "".join(
        f"""
        <tr>
            <td><span class='risk {escape(event.severity)}'>{escape(event.severity.upper())}</span></td>
            <td>{escape(event.category)}</td>
            <td><strong>{escape(event.title)}</strong><br><small>{escape((event.message or '-')[:180])}</small></td>
            <td>{escape(event.path or '-')}</td>
            <td>{display_date(event.created_at.strftime('%Y-%m-%d') if event.created_at else '')}</td>
        </tr>
        """
        for event in recent_events
    )
    if not event_rows:
        event_rows = "<tr><td colspan='5'>Nenhum erro tecnico registrado ainda.</td></tr>"

    route_rows = "".join(
        f"""
        <tr>
            <td><span class='risk {severity}'>{severity.upper()}</span></td>
            <td>{escape(route.planned_date or '-')}</td>
            <td><a href='/web/operacao?data={escape(route.planned_date or "")}&route_id={route.id}'>{escape(route_display_name(route))}</a><br><small>{escape(route_free_label(route))}</small></td>
            <td>{escape(message)}</td>
        </tr>
        """
        for severity, route, message in route_alerts[:16]
    )
    if not route_rows:
        route_rows = "<tr><td colspan='4'>Sem alertas operacionais criticos para hoje e proximos dias.</td></tr>"

    profile_rows = "".join(
        f"""
        <tr>
            <td>{escape(item['client'].name)}<br><small>{escape(item['client'].company.name if item['client'].company else '-')}</small></td>
            <td>{int(round(item['avg_service']))} min</td>
            <td>{item['samples']}</td>
            <td>{item['routes']}</td>
            <td>{item['realized']}</td>
        </tr>
        """
        for item in client_profiles[:10]
    )
    if not profile_rows:
        profile_rows = "<tr><td colspan='5'>Ainda nao ha historico suficiente de paradas para aprendizado por cliente.</td></tr>"

    recommendation_items = "".join(f"<li>{escape(item)}</li>" for item in adaptive_recommendations)
    db_status = "ok" if database_ok else "critical"

    body = f"""
    <style>
        .health-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-bottom:16px; }}
        .health-card {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:14px; }}
        .health-card span {{ display:block; color:#667085; font-size:12px; font-weight:900; text-transform:uppercase; }}
        .health-card strong {{ display:block; margin-top:7px; font-size:26px; }}
        .health-card small {{ display:block; margin-top:5px; color:#667085; }}
        .risk {{ display:inline-block; border-radius:999px; padding:4px 8px; font-size:11px; font-weight:900; }}
        .risk.critical,.risk.critico {{ background:#fef3f2; color:#b42318; }}
        .risk.warning,.risk.attention,.risk.atencao {{ background:#fffaeb; color:#b54708; }}
        .risk.info,.risk.ok {{ background:#eef4ff; color:#1849a9; }}
        .ai-panel {{ background:#0b1f3a; color:white; border-radius:8px; padding:18px; }}
        .ai-panel h2 {{ color:white; margin-top:0; }}
        .ai-panel li {{ margin:8px 0; color:#e4e7ec; }}
        .two-wide {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:16px; }}
        @media(max-width:1000px) {{ .health-grid,.two-wide {{ grid-template-columns:1fr; }} }}
    </style>
    <div class='health-grid'>
        <div class='health-card'><span>Banco</span><strong><span class='risk {db_status}'>{'OK' if database_ok else 'ERRO'}</span></strong><small>{escape(database_message)}</small></div>
        <div class='health-card'><span>Erros criticos 24h</span><strong>{critical_24h}</strong><small>{unresolved_count} evento(s) aberto(s)</small></div>
        <div class='health-card'><span>Alertas operacionais</span><strong>{len(route_alerts)}</strong><small>Hoje e proximos 2 dias</small></div>
        <div class='health-card'><span>Auditoria 24h</span><strong>{audit_24h}</strong><small>Eventos de uso registrados</small></div>
    </div>
    <div class='two-wide'>
        <div class='ai-panel'>
            <h2>IA adaptativa operacional</h2>
            <p>Primeira camada de aprendizado: o sistema compara historico, telemetria, comprovantes, tempo de atendimento e risco de jornada para sugerir ajustes.</p>
            <ul>{recommendation_items}</ul>
        </div>
        <div class='card'>
            <h2>Como a IA vai aprender</h2>
            <p class='muted'>A cada rota executada, o sistema passa a refinar tempo por cliente, motorista, base, veiculo, janela, distancia, comprovantes e divergencias.</p>
            <ol>
                <li>Coleta dados reais de atendimento e execucao.</li>
                <li>Compara previsto versus realizado.</li>
                <li>Sugere tempo padrao e risco antes da rota oficial.</li>
                <li>Melhora simulacao e torre de controle com historico.</li>
            </ol>
        </div>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Rotas e operacao em risco</h2>
        <div class='table-wrap'><table class='table'><thead><tr><th>Risco</th><th>Data</th><th>Rota</th><th>Alerta</th></tr></thead><tbody>{route_rows}</tbody></table></div>
    </div>
    <div class='two-wide'>
        <div class='card'>
            <h2>Aprendizado por cliente</h2>
            <div class='table-wrap'><table class='table'><thead><tr><th>Cliente</th><th>Tempo medio</th><th>Amostras</th><th>Rotas</th><th>Realizados</th></tr></thead><tbody>{profile_rows}</tbody></table></div>
        </div>
        <div class='card'>
            <h2>Eventos tecnicos recentes</h2>
            <div class='table-wrap'><table class='table'><thead><tr><th>Nivel</th><th>Categoria</th><th>Evento</th><th>Pagina</th><th>Data</th></tr></thead><tbody>{event_rows}</tbody></table></div>
        </div>
    </div>
    """
    return layout("Saude e IA", body, "saude_ia")


@web_bp.route("/web/bases", methods=["GET", "POST"])
@web_login_required
def company_bases_page():
    user = request.web_user
    if not can_see_admin(user):
        return layout("Acesso negado", "<div class='card'><h2>Bases disponiveis para perfis administrativos autorizados.</h2></div>", "bases"), 403

    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    selected_company_id = request.form.get("company_id", type=int) if request.method == "POST" else request.args.get("company_id", type=int)
    if not selected_company_id:
        selected_company_id = user.company_id if not user.is_master else (companies[0].id if companies else None)
    if selected_company_id and not can_access_company(user, selected_company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "bases"), 403

    message = ""
    if request.method == "POST":
        if not can_manage_bases(user):
            return layout("Acesso negado", "<div class='card'><h2>Seu perfil consulta bases, mas nao cria novas unidades.</h2></div>", "bases"), 403
        name = request.form.get("name", "").strip()
        base_type = request.form.get("base_type", "base").strip().lower()
        parent_base_id = request.form.get("parent_base_id", type=int)
        try:
            if not selected_company_id or not name:
                raise ValueError("Informe empresa e nome da base.")
            if base_type not in ("matriz", "base"):
                base_type = "base"
            if parent_base_id and not can_access_base(user, parent_base_id):
                raise ValueError("Matriz/base superior fora da permissao.")
            existing = CompanyBase.query.filter_by(company_id=selected_company_id, name=name).first()
            if existing:
                raise ValueError("Ja existe uma base com este nome nesta empresa.")
            db.session.add(CompanyBase(
                company_id=selected_company_id,
                parent_base_id=parent_base_id or None,
                name=name,
                base_type=base_type,
                city=request.form.get("city", "").strip() or None,
                address=request.form.get("address", "").strip() or None,
                active=True,
            ))
            db.session.commit()
            return redirect(url_for("web_bp.company_bases_page", company_id=selected_company_id, salvo=1))
        except Exception as exc:
            db.session.rollback()
            message = f"<div class='client-notice danger'>Nao foi possivel salvar. {escape(str(exc))}</div>"

    if request.args.get("salvo") == "1":
        message = "<div class='client-notice ok'>Base/unidade cadastrada com sucesso.</div>"

    company_select = ""
    if user.is_master:
        company_options = "".join(
            f"<option value='{company.id}' {'selected' if company.id == selected_company_id else ''}>{escape(company.name)}</option>"
            for company in companies if company
        )
        company_select = f"""
        <form method='get' class='base-filter'>
            <label>Empresa<select name='company_id'>{company_options}</select></label>
            <button class='btn btn-light' type='submit'>Ver empresa</button>
        </form>
        """
    else:
        company_select = f"<p class='muted'>Empresa: <strong>{escape(user.company.name if user.company else '-')}</strong></p>"

    base_query = CompanyBase.query.filter_by(company_id=selected_company_id) if selected_company_id else CompanyBase.query.filter_by(id=-1)
    if getattr(user, "base_id", None) and role_level(user) < 3:
        base_query = base_query.filter_by(id=user.base_id)
    bases = base_query.order_by(CompanyBase.base_type.desc(), CompanyBase.name).all()
    parent_options = "<option value=''>Sem matriz/base superior</option>" + "".join(
        f"<option value='{base.id}'>{escape(base.name)}</option>"
        for base in bases if base.active
    )
    rows = ""
    for base in bases:
        dependency_summary = ", ".join(f"{label}: {count}" for label, count in base_dependency_counts(base.id).items() if count)
        if can_manage_bases(user):
            status_label = "Adormecer" if base.active else "Reativar"
            base_actions = f"""
            <div class='action-stack'>
                <form method='post' action='/web/bases/status'>
                    <input type='hidden' name='base_id' value='{base.id}'>
                    <input type='hidden' name='company_id' value='{selected_company_id or ""}'>
                    <button class='mini-btn {"danger" if base.active else ""}' type='submit'>{status_label}</button>
                </form>
            """
            if base_can_be_deleted(base.id):
                base_actions += f"""
                <form method='post' action='/web/bases/excluir' onsubmit="return confirm('Excluir esta base? Esta acao so deve ser usada para cadastro criado por engano.');">
                    <input type='hidden' name='base_id' value='{base.id}'>
                    <input type='hidden' name='company_id' value='{selected_company_id or ""}'>
                    <button class='mini-btn danger' type='submit'>Excluir</button>
                </form>
                """
            else:
                base_actions += f"<span class='muted' title='{escape(dependency_summary)}'>Excluir bloqueado</span>"
            base_actions += "</div>"
        else:
            base_actions = "-"
        rows += f"""
        <tr>
            <td><strong>{escape(base.name)}</strong><br><span class='muted'>{escape(base.city or '-')}</span></td>
            <td>{'Matriz' if base.base_type == 'matriz' else 'Base / Unidade'}</td>
            <td>{escape(base.parent.name if base.parent else '-')}</td>
            <td>{escape(base.address or '-')}</td>
            <td><span class='badge {"badge-success" if base.active else "badge-danger"}'>{'Ativa' if base.active else 'Inativa'}</span></td>
            <td>{base_actions}</td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='6'>Nenhuma base cadastrada para esta empresa.</td></tr>"

    form_company_field = f"<input type='hidden' name='company_id' value='{selected_company_id or ''}'>"
    if user.is_master:
        form_company_options = "".join(
            f"<option value='{company.id}' {'selected' if company.id == selected_company_id else ''}>{escape(company.name)}</option>"
            for company in companies if company
        )
        form_company_field = f"<label>Empresa<select name='company_id' required>{form_company_options}</select></label>"
    create_panel = ""
    if can_manage_bases(user):
        create_panel = f"""
        <div class='card'>
            <h2>Nova matriz / base</h2>
            <form method='post' class='base-form'>
                {form_company_field}
                <label>Tipo<select name='base_type'><option value='matriz'>Matriz</option><option value='base'>Base / Unidade</option></select></label>
                <label>Nome<input name='name' required placeholder='Ex: Base Santa Maria'></label>
                <label>Matriz/base superior<select name='parent_base_id'>{parent_options}</select></label>
                <label>Cidade<input name='city' placeholder='Cidade'></label>
                <label>Endereco<input name='address' placeholder='Rua, numero, bairro'></label>
                <button class='btn' type='submit'>Cadastrar base</button>
            </form>
        </div>
        """
    else:
        create_panel = "<div class='card'><h2>Base vinculada</h2><p class='muted'>Seu perfil consulta somente a base associada ao seu usuario.</p></div>"

    body = f"""
    <style>
        .base-grid {{ display:grid; grid-template-columns:.76fr 1.24fr; gap:16px; align-items:start; }}
        .base-form {{ display:grid; gap:10px; }}
        .base-form input,.base-form select,.base-filter select {{ width:100%; border:1px solid #cfd8e3; border-radius:7px; padding:10px; font:inherit; }}
        .base-form label,.base-filter label {{ font-weight:800; color:#0b1f3a; font-size:13px; }}
        .base-filter {{ display:flex; gap:10px; align-items:end; flex-wrap:wrap; margin-bottom:12px; }}
        .client-notice.danger {{ background:#fef3f2; color:#b42318; border:1px solid #fda29b; }}
        @media(max-width:900px) {{ .base-grid {{ grid-template-columns:1fr; }} .base-filter {{ display:block; }} }}
    </style>
    <div class='card' style='margin-bottom:16px;'>
        <h2>Matriz, bases e unidades</h2>
        <p class='muted'>Use esta camada para separar rotas, clientes, veiculos e usuarios por filial/base. CEO ve todas; gerente com base definida ve somente a sua base.</p>
        <p class='muted'>Para cadastrar a empresa contratante, acesse <a href='/web/empresas'>Empresas</a>. Esta tela cadastra matriz, filial e base dentro de uma empresa ja criada.</p>
        {company_select}
        {message}
    </div>
    <div class='base-grid'>
        {create_panel}
        <div class='card'>
            <h2>Bases cadastradas</h2>
            <div class='table-wrap'><table class='table'>
                <thead><tr><th>Base</th><th>Tipo</th><th>Vinculo</th><th>Endereco</th><th>Status</th><th>Acao</th></tr></thead>
                <tbody>{rows}</tbody>
            </table></div>
        </div>
    </div>
    """
    return layout("Bases / Unidades", body, "bases")


@web_bp.route("/web/bases/status", methods=["POST"])
@web_login_required
def company_base_status_page():
    user = request.web_user
    if not can_manage_bases(user):
        return layout("Acesso negado", "<div class='card'><h2>Seu perfil nao pode alterar bases.</h2></div>", "bases"), 403
    base = CompanyBase.query.get_or_404(request.form.get("base_id", type=int))
    if not can_access_base(user, base.id):
        return layout("Acesso negado", "<div class='card'><h2>Base fora da sua permissao.</h2></div>", "bases"), 403
    base.active = not bool(base.active)
    audit_event("base_status_toggle", user=user, company_id=base.company_id, resource_type="company_base", resource_id=base.id, details=f"{base.name}: {'ativa' if base.active else 'inativa'}")
    db.session.commit()
    return redirect(url_for("web_bp.company_bases_page", company_id=request.form.get("company_id", type=int) or base.company_id))


@web_bp.route("/web/bases/excluir", methods=["POST"])
@web_login_required
def company_base_delete_page():
    user = request.web_user
    if not can_manage_bases(user):
        return layout("Acesso negado", "<div class='card'><h2>Seu perfil nao pode excluir bases.</h2></div>", "bases"), 403
    base = CompanyBase.query.get_or_404(request.form.get("base_id", type=int))
    if not can_access_base(user, base.id):
        return layout("Acesso negado", "<div class='card'><h2>Base fora da sua permissao.</h2></div>", "bases"), 403
    company_id = base.company_id
    if base_can_be_deleted(base.id):
        audit_event("base_delete", user=user, company_id=company_id, resource_type="company_base", resource_id=base.id, details=base.name)
        db.session.delete(base)
    else:
        base.active = False
        audit_event("base_sleep_instead_delete", user=user, company_id=company_id, resource_type="company_base", resource_id=base.id, details=base.name)
    db.session.commit()
    return redirect(url_for("web_bp.company_bases_page", company_id=request.form.get("company_id", type=int) or company_id))


@web_bp.route("/web/colaboradores", methods=["GET", "POST"])
@web_login_required
def collaborators_page():
    user = request.web_user
    if not can_manage_collaborators(user):
        return layout("Acesso negado", "<div class='card'><h2>Cadastro de colaboradores disponivel para perfis administrativos autorizados.</h2></div>", "colaboradores"), 403

    message = ""
    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    selected_company_id = request.form.get("company_id", type=int) if request.method == "POST" else request.args.get("company_id", type=int)
    if not selected_company_id:
        selected_company_id = user.company_id if not user.is_master else (companies[0].id if companies else None)
    if selected_company_id and not can_access_company(user, selected_company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "colaboradores"), 403

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "operator").strip().lower()
        base_id = request.form.get("base_id", type=int)
        can_view_finance = can_grant_finance_access(user) and role_can_receive_finance(role) and request.form.get("can_view_finance") == "on"
        try:
            if not selected_company_id and role != "master":
                raise ValueError("Selecione a empresa.")
            if not name or not email or not password:
                raise ValueError("Informe nome, login e senha.")
            if len(password) < 6:
                raise ValueError("Use uma senha com pelo menos 6 caracteres.")
            if not can_assign_role(user, role):
                raise ValueError("Seu perfil nao pode criar este tipo de usuario.")
            if not user.is_master and getattr(user, "base_id", None) and role_level(user) < 3:
                base_id = user.base_id
            if base_id and not can_access_base(user, base_id):
                raise ValueError("Base fora da sua permissao.")
            if not user.is_master and role == "master":
                raise ValueError("Este perfil e restrito a administracao da plataforma.")
            if User.query.filter_by(email=email).first():
                raise ValueError("Este login ja esta cadastrado.")
            company_id = None if role == "master" else selected_company_id
            collaborator = User(
                company_id=company_id,
                base_id=None if role == "master" else base_id,
                name=name,
                email=email,
                password=generate_password_hash(password),
                role=role,
                can_view_finance=can_view_finance,
                active=True,
            )
            db.session.add(collaborator)
            db.session.commit()
            return redirect(url_for("web_bp.collaborators_page", company_id=selected_company_id, salvo=1))
        except Exception as exc:
            db.session.rollback()
            message = f"<div class='client-notice danger'>Nao foi possivel cadastrar. {escape(str(exc))}</div>"

    if request.args.get("salvo") == "1":
        message = "<div class='client-notice ok'>Colaborador cadastrado com sucesso.</div>"
    if request.args.get("status") == "ok":
        message = "<div class='client-notice ok'>Status do colaborador atualizado.</div>"
    if user.is_master and not companies:
        message = """
        <div class='client-notice danger'>
            Nenhuma empresa cliente cadastrada neste ambiente. Cadastre a empresa primeiro para liberar bases e colaboradores.
            <div style='margin-top:8px;'><a class='btn' href='/web/empresas'>Cadastrar empresa cliente</a></div>
        </div>
        """

    company_select = ""
    if user.is_master:
        company_options = "".join(
            f"<option value='{company.id}' {'selected' if company.id == selected_company_id else ''}>{escape(company.name)}</option>"
            for company in companies if company
        )
        if not company_options:
            company_options = "<option value=''>Nenhuma empresa cadastrada</option>"
        company_select = f"""
        <form method='get' class='collab-filter'>
            <label>Empresa<select name='company_id'>{company_options}</select></label>
            <button class='btn btn-light' type='submit'>Ver empresa</button>
        </form>
        """
    else:
        company_select = f"<p class='muted'>Empresa: <strong>{escape(user.company.name if user.company else '-')}</strong></p>"

    rows = ""
    query = User.query
    if selected_company_id:
        query = query.filter_by(company_id=selected_company_id)
    elif not user.is_master:
        query = query.filter_by(company_id=user.company_id)
    if getattr(user, "base_id", None) and role_level(user) < 3:
        query = query.filter_by(base_id=user.base_id)
    users = query.order_by(User.active.desc(), User.role, User.name).all()
    for item in users:
        status_badge = "<span class='badge badge-success'>Ativo</span>" if item.active else "<span class='badge badge-danger'>Inativo</span>"
        action = "-"
        can_change_status = item.id != user.id and (user.is_master or role_level(item) < role_level(user))
        if can_change_status:
            action_label = "Desativar" if item.active else "Reativar"
            action = f"""
            <div class='action-stack'>
                <a class='mini-btn' href='/web/colaboradores/{item.id}/editar?company_id={selected_company_id or ""}'>Editar</a>
                <form method='post' action='/web/colaboradores/status'>
                    <input type='hidden' name='user_id' value='{item.id}'>
                    <input type='hidden' name='company_id' value='{selected_company_id or ""}'>
                    <button class='mini-btn {"danger" if item.active else ""}' type='submit'>{action_label}</button>
                </form>
            </div>
            """
        elif item.id != user.id:
            action = f"<a class='mini-btn' href='/web/colaboradores/{item.id}/editar?company_id={selected_company_id or ''}'>Editar</a>"
        rows += f"""
        <tr>
            <td><strong>{escape(item.name)}</strong><br><span class='muted'>{escape(item.email)}</span></td>
            <td>{escape(item.company.name if item.company else "Plataforma")}</td>
            <td>{escape(item.base.name if item.base else "Todas as bases")}</td>
            <td>{escape(role_label(item.role))}</td>
            <td>{finance_access_label(item)}</td>
            <td>{status_badge}</td>
            <td>{display_date(item.created_at.strftime("%Y-%m-%d") if item.created_at else "")}</td>
            <td>{action}</td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='8'>Nenhum colaborador cadastrado para esta empresa.</td></tr>"

    company_hidden = f"<input type='hidden' name='company_id' value='{selected_company_id or ''}'>"
    company_field = company_hidden
    if user.is_master:
        form_company_options = "".join(
            f"<option value='{company.id}' {'selected' if company.id == selected_company_id else ''}>{escape(company.name)}</option>"
            for company in companies if company
        )
        if not form_company_options:
            form_company_options = "<option value=''>Cadastre uma empresa antes</option>"
        company_field = f"<label>Empresa<select name='company_id' required>{form_company_options}</select></label>"
    base_field = f"<label>Base / unidade<select name='base_id'>{company_base_options(selected_company_id, getattr(user, 'base_id', None), include_all=True)}</select></label>" if selected_company_id else ""
    if not user.is_master and getattr(user, "base_id", None) and role_level(user) < 3:
        base_field = f"<input type='hidden' name='base_id' value='{user.base_id}'><p class='muted'>Base: <strong>{escape(user.base.name if user.base else '-')}</strong></p>"
    finance_access_field = ""
    if can_grant_finance_access(user):
        finance_access_field = """
                <label class='check-line'><input type='checkbox' name='can_view_finance'> Autorizar acesso a valores para gerente</label>
                <p class='muted'>Use somente quando o CEO/proprietario liberar um gerente para ver custos, margens e valores.</p>
        """

    body = f"""
    <style>
        .collab-grid {{ display:grid; grid-template-columns:.82fr 1.18fr; gap:16px; align-items:start; }}
        .collab-form {{ display:grid; gap:10px; }}
        .collab-form input,.collab-form select,.collab-filter select {{ width:100%; border:1px solid #cfd8e3; border-radius:7px; padding:10px; font:inherit; }}
        .collab-form label,.collab-filter label {{ font-weight:800; color:#0b1f3a; font-size:13px; }}
        .collab-form .check-line {{ display:flex; gap:8px; align-items:center; font-weight:800; }}
        .collab-form .check-line input {{ width:auto; }}
        .collab-filter {{ display:flex; gap:10px; align-items:end; flex-wrap:wrap; margin-bottom:12px; }}
        .action-stack {{ display:flex; gap:6px; align-items:center; flex-wrap:wrap; }}
        .action-stack form {{ margin:0; }}
        .client-notice.danger {{ background:#fef3f2; color:#b42318; border:1px solid #fda29b; }}
        @media(max-width:900px) {{ .collab-grid {{ grid-template-columns:1fr; }} .collab-filter {{ display:block; }} }}
    </style>
    <div class='card' style='margin-bottom:16px;'>
        <h2>Colaboradores da empresa</h2>
        <p class='muted'>Gerencia e CEO podem cadastrar usuarios da propria empresa conforme as permissoes liberadas.</p>
        {company_select}
        {message}
    </div>
    <div class='collab-grid'>
        <div class='card'>
            <h2>Novo colaborador</h2>
            <form method='post' class='collab-form'>
                {company_field}
                {base_field}
                <label>Nome<input name='name' required placeholder='Nome do colaborador'></label>
                <label>Email ou login<input name='email' required placeholder='ex: operador01 ou email@empresa.com'></label>
                <label>Senha temporaria<input name='password' type='password' required placeholder='Minimo 6 caracteres'></label>
                <label>Perfil<select name='role'>{collaborator_role_options(user)}</select></label>
                {finance_access_field}
                <button class='btn' type='submit'>Cadastrar colaborador</button>
            </form>
        </div>
        <div class='card'>
            <h2>Usuarios cadastrados</h2>
            <div class='table-wrap'><table class='table'>
                <thead><tr><th>Usuario</th><th>Empresa</th><th>Base</th><th>Perfil</th><th>Valores</th><th>Status</th><th>Criado em</th><th>Acao</th></tr></thead>
                <tbody>{rows}</tbody>
            </table></div>
        </div>
    </div>
    """
    return layout("Colaboradores", body, "colaboradores")


@web_bp.route("/web/colaboradores/<int:collaborator_id>/editar", methods=["GET", "POST"])
@web_login_required
def collaborator_edit_page(collaborator_id):
    user = request.web_user
    if not can_manage_collaborators(user):
        return layout("Acesso negado", "<div class='card'><h2>Cadastro de colaboradores disponivel para perfis administrativos autorizados.</h2></div>", "colaboradores"), 403

    collaborator = User.query.get_or_404(collaborator_id)
    if collaborator.id == user.id:
        return layout("Colaboradores", "<div class='card'><h2>Use redefinir senha ou seu cadastro principal para alterar o proprio usuario.</h2><a class='btn' href='/web/colaboradores'>Voltar</a></div>", "colaboradores"), 400
    if not user.is_master:
        if collaborator.company_id != user.company_id or role_level(collaborator) >= role_level(user):
            return layout("Acesso negado", "<div class='card'><h2>Voce nao pode editar este colaborador.</h2></div>", "colaboradores"), 403
        if getattr(user, "base_id", None) and role_level(user) < 3 and collaborator.base_id != user.base_id:
            return layout("Acesso negado", "<div class='card'><h2>Colaborador fora da sua base.</h2></div>", "colaboradores"), 403

    selected_company_id = request.form.get("company_id", type=int) if request.method == "POST" else (request.args.get("company_id", type=int) or collaborator.company_id)
    message = ""
    if request.method == "POST":
        try:
            name = (request.form.get("name") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            role = (request.form.get("role") or "operator").strip().lower()
            base_id = request.form.get("base_id", type=int)
            password = request.form.get("password", "")
            active = request.form.get("active") == "on"
            can_view_finance = can_grant_finance_access(user) and role_can_receive_finance(role) and request.form.get("can_view_finance") == "on"

            if not name or not email:
                raise ValueError("Informe nome e login.")
            if not can_assign_role(user, role):
                raise ValueError("Seu perfil nao pode atribuir este tipo de usuario.")
            if not user.is_master and role == "master":
                raise ValueError("Perfil master e restrito a administracao da plataforma.")
            if role != "master":
                if not selected_company_id:
                    raise ValueError("Selecione a empresa.")
                if not can_access_company(user, selected_company_id):
                    raise ValueError("Empresa fora da sua permissao.")
            if not user.is_master and getattr(user, "base_id", None) and role_level(user) < 3:
                base_id = user.base_id
            if base_id and not can_access_base(user, base_id):
                raise ValueError("Base fora da sua permissao.")
            existing = User.query.filter(User.email == email, User.id != collaborator.id).first()
            if existing:
                raise ValueError("Este login ja esta cadastrado em outro usuario.")
            if password:
                if len(password) < 6:
                    raise ValueError("Use uma senha com pelo menos 6 caracteres.")
                collaborator.password = generate_password_hash(password)
                collaborator.active_session_id = None
                collaborator.active_session_started_at = None
                collaborator.active_session_ip = None
                collaborator.active_session_user_agent = None

            collaborator.name = name
            collaborator.email = email
            collaborator.role = role
            collaborator.can_view_finance = can_view_finance
            collaborator.company_id = None if role == "master" else selected_company_id
            collaborator.base_id = None if role == "master" else base_id
            collaborator.active = active
            if not active:
                collaborator.active_session_id = None
                collaborator.active_session_started_at = None
                collaborator.active_session_ip = None
                collaborator.active_session_user_agent = None
            audit_event("collaborator_update", user=user, company_id=collaborator.company_id, resource_type="user", resource_id=collaborator.id, details=collaborator.email)
            db.session.commit()
            return redirect(url_for("web_bp.collaborators_page", company_id=collaborator.company_id or selected_company_id, status="ok"))
        except Exception as exc:
            db.session.rollback()
            message = f"<div class='client-notice danger'>Nao foi possivel salvar. {escape(str(exc))}</div>"

    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    company_field = f"<input type='hidden' name='company_id' value='{collaborator.company_id or selected_company_id or ''}'>"
    if user.is_master:
        company_options = "".join(
            f"<option value='{company.id}' {'selected' if company.id == (collaborator.company_id or selected_company_id) else ''}>{escape(company.name)}</option>"
            for company in companies if company
        )
        company_field = f"<label>Empresa<select name='company_id'>{company_options}</select></label>"
    selected_form_company_id = collaborator.company_id or selected_company_id
    base_field = f"<label>Base / unidade<select name='base_id'>{company_base_options(selected_form_company_id, collaborator.base_id, include_all=True)}</select></label>" if selected_form_company_id else ""
    if not user.is_master and getattr(user, "base_id", None) and role_level(user) < 3:
        base_field = f"<input type='hidden' name='base_id' value='{user.base_id}'><p class='muted'>Base: <strong>{escape(user.base.name if user.base else '-')}</strong></p>"
    finance_access_field = ""
    if can_grant_finance_access(user):
        finance_access_field = f"""
            <label class='check-line'><input type='checkbox' name='can_view_finance' {'checked' if getattr(collaborator, 'can_view_finance', False) else ''}> Autorizar acesso a valores para gerente</label>
            <p class='muted'>Quando marcado, gerente autorizado acessa Valores e relatorios financeiros da empresa/base permitida.</p>
        """

    body = f"""
    <style>
        .collab-form {{ display:grid; gap:10px; max-width:620px; }}
        .collab-form input,.collab-form select {{ width:100%; border:1px solid #cfd8e3; border-radius:7px; padding:10px; font:inherit; }}
        .collab-form label {{ font-weight:800; color:#0b1f3a; font-size:13px; }}
        .collab-form .check-line {{ display:flex; gap:8px; align-items:center; }}
        .collab-form .check-line input {{ width:auto; }}
        .client-notice.danger {{ background:#fef3f2; color:#b42318; border:1px solid #fda29b; }}
    </style>
    <div class='card'>
        <h2>Editar colaborador</h2>
        <p class='muted'>Altere gerente, operacional ou motorista. A senha so muda se uma nova senha for informada.</p>
        {message}
        <form method='post' class='collab-form'>
            {company_field}
            {base_field}
            <label>Nome<input name='name' value='{escape(collaborator.name)}' required></label>
            <label>Email ou login<input name='email' value='{escape(collaborator.email)}' required></label>
            <label>Nova senha opcional<input name='password' type='password' placeholder='Preencha somente se quiser trocar'></label>
            <label>Perfil<select name='role'>{collaborator_role_options(user, collaborator.role)}</select></label>
            {finance_access_field}
            <label style='display:flex; gap:8px; align-items:center;'><input type='checkbox' name='active' {'checked' if collaborator.active else ''} style='width:auto;'> Usuario ativo</label>
            <div style='display:flex; gap:10px; flex-wrap:wrap;'>
                <button class='btn' type='submit'>Salvar alteracoes</button>
                <a class='btn btn-light' href='/web/colaboradores?company_id={selected_company_id or collaborator.company_id or ""}'>Voltar</a>
            </div>
        </form>
    </div>
    """
    return layout("Editar colaborador", body, "colaboradores")


@web_bp.route("/web/colaboradores/status", methods=["POST"])
@web_login_required
def collaborator_status_page():
    user = request.web_user
    if not can_manage_collaborators(user):
        return layout("Acesso negado", "<div class='card'><h2>Cadastro de colaboradores disponivel para perfis administrativos autorizados.</h2></div>", "colaboradores"), 403

    collaborator = User.query.get_or_404(request.form.get("user_id", type=int))
    if collaborator.id == user.id:
        return layout("Colaboradores", "<div class='card'><h2>Voce nao pode desativar o proprio usuario.</h2><a class='btn' href='/web/colaboradores'>Voltar</a></div>", "colaboradores"), 400
    if not user.is_master:
        if collaborator.company_id != user.company_id or role_level(collaborator) >= role_level(user):
            return layout("Acesso negado", "<div class='card'><h2>Voce nao pode alterar este colaborador.</h2></div>", "colaboradores"), 403
    collaborator.active = not collaborator.active
    if not collaborator.active:
        collaborator.active_session_id = None
        collaborator.active_session_started_at = None
        collaborator.active_session_ip = None
        collaborator.active_session_user_agent = None
    db.session.commit()
    company_id = request.form.get("company_id", type=int) or collaborator.company_id
    return redirect(url_for("web_bp.collaborators_page", company_id=company_id, status="ok"))


@web_bp.route("/dashboard")
@web_login_required
def dashboard():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))
    visible_routes = scoped_query(LogisticRoute, user).order_by(LogisticRoute.id.desc()).all()
    visible_clients = scoped_query(Client, user).all()
    visible_vehicles = scoped_query(Vehicle, user).all()

    route_count = len(visible_routes)
    client_count = len(visible_clients)
    vehicle_count = len(visible_vehicles)
    company_count = Company.query.count() if user.is_master else 1

    def counts(items, attr_name, fallback="Sem status"):
        data = {}
        for item in items:
            value = getattr(item, attr_name, None) or fallback
            data[value] = data.get(value, 0) + 1
        return data

    def bar_chart(title, data, color="#1f5eff"):
        if not data:
            return f"<div class='card'><h2>{title}</h2><p class='muted'>Sem dados para exibir.</p></div>"
        max_value = max(data.values()) or 1
        rows = ""
        for label, value in sorted(data.items(), key=lambda pair: str(pair[0])):
            width = max(6, int((value / max_value) * 100))
            rows += f"""
            <div style='margin:12px 0;'>
                <div style='display:flex;justify-content:space-between;gap:10px;font-size:13px;font-weight:800;'><span>{label}</span><span>{value}</span></div>
                <div style='height:12px;background:#eef3f8;border-radius:999px;overflow:hidden;margin-top:6px;'><div style='height:100%;width:{width}%;background:{color};'></div></div>
            </div>
            """
        return f"<div class='card'><h2>{title}</h2>{rows}</div>"

    status_chart = bar_chart("Rotas por status", counts(visible_routes, "status"), "#1f5eff")
    vehicle_chart = bar_chart("Veiculos por tipo", counts(visible_vehicles, "vehicle_type", "Sem tipo"), "#14804a")
    priority_chart = bar_chart("Clientes por prioridade", counts(visible_clients, "priority", "Sem prioridade"), "#b54708")

    cost_chart = ""
    if can_see_finance(user):
        cost_data = {route.name: round(route_cost(route), 2) for route in visible_routes}
        cost_chart = bar_chart("Custo estimado por rota", cost_data, "#d92d20")

    route_rows = ""
    recent_routes = visible_routes[:8]
    finance_head = "<th>Custo</th>" if can_see_finance(user) else ""
    for route in recent_routes:
        finance = f"<td>{format_money(route_cost(route))}</td>" if can_see_finance(user) else ""
        route_rows += f"<tr><td>{route_display_name(route)}<br><span class='muted'>{display_date(route.planned_date)}</span></td><td>{route.company.name}</td><td>{route.vehicle.name if route.vehicle else '-'}</td><td>{escape(route_free_label(route))}</td><td><span class='badge'>{route.status}</span></td>{finance}<td><a class='btn btn-light' href='/web/mapa/{route.id}'>Mapa</a></td></tr>"
    attendance_rows = ""
    for route in visible_routes:
        for item in stop_schedule(route):
            stop = item["stop"]
            client = stop.client
            attendance_rows += f"""
            <tr>
                <td><strong>{client.name}</strong><br><span class='muted'>{format_coord(client.latitude)}, {format_coord(client.longitude)}</span></td>
                <td>{route_display_name(route)}<br><span class='muted'>{escape(route_free_label(route))}</span></td>
                <td><span class='badge'>{stop_status_label(stop.status)}</span><br><span class='muted'>Parada {stop.sequence}</span></td>
                <td>Chegada {item['arrival']}<br><span class='muted'>Saida {item['departure']} | Atendimento {item['service_minutes']} min</span></td>
                <td><a class='btn btn-light' href='/web/operacao?route_id={route.id}'>Acompanhar</a></td>
            </tr>
            """
    if not attendance_rows:
        attendance_rows = "<tr><td colspan='5'>Nenhum cliente em rota no momento.</td></tr>"
    insights = "".join(f"<li>{item}</li>" for item in ai_insights(user))

    body = f"""
    <div style='display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap;'>
        <div class='muted'>Indicadores em graficos para acompanhar a operacao.</div>
        <a class='btn' href='/web/exportar-dashboard.xlsx'>Exportar Excel</a>
    </div>
    <div class="grid cards"><div class="card"><div class="muted">Empresas</div><div class="metric">{company_count}</div></div><div class="card"><div class="muted">Rotas</div><div class="metric">{route_count}</div></div><div class="card"><div class="muted">Clientes</div><div class="metric">{client_count}</div></div><div class="card"><div class="muted">Veiculos</div><div class="metric">{vehicle_count}</div></div></div>
    <div class="grid two" style="margin-top:16px;">{status_chart}{vehicle_chart or priority_chart}</div>
    <div class="grid two" style="margin-top:16px;">{priority_chart if vehicle_chart else ''}{cost_chart or '<div class="card"><h2>IA operacional</h2><ul class="ai-list">' + insights + '</ul></div>'}</div>
    <div class="card" style="margin-top:16px;"><h2>Clientes em atendimento</h2><div class='table-wrap'><table class="table"><thead><tr><th>Cliente</th><th>Rota / Equipe</th><th>Status</th><th>Previsao</th><th></th></tr></thead><tbody>{attendance_rows}</tbody></table></div></div>
    <div class="grid two" style="margin-top:16px;"><div class="card"><h2>Rotas recentes</h2><div class='table-wrap'><table class="table"><thead><tr><th>Rota / data</th><th>Empresa</th><th>Veiculo</th><th>Responsavel / regiao / info</th><th>Status</th>{finance_head}<th></th></tr></thead><tbody>{route_rows}</tbody></table></div></div><div class="card"><h2>IA operacional</h2><ul class="ai-list">{insights}</ul></div></div>
    """
    return layout("Dashboard", body, "dashboard")


@web_bp.route("/web/exportar-dashboard.xlsx")
@web_login_required
def export_dashboard_excel():
    user = request.web_user
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Dashboard"
    ws.append(["Indicador", "Valor"])
    visible_routes = scoped_query(LogisticRoute, user).order_by(LogisticRoute.id.desc()).all()
    visible_clients = scoped_query(Client, user).all()
    visible_vehicles = scoped_query(Vehicle, user).all()
    ws.append(["Empresas", Company.query.count() if user.is_master else 1])
    ws.append(["Rotas", len(visible_routes)])
    ws.append(["Clientes", len(visible_clients)])
    ws.append(["Veiculos", len(visible_vehicles)])

    ws_routes = wb.create_sheet("Rotas")
    headers = ["Rota", "Data", "Empresa", "Veiculo", "Responsavel / regiao / info", "Status", "Paradas"]
    if can_see_finance(user):
        headers.append("Custo estimado")
    ws_routes.append(headers)
    for route in visible_routes:
        row = [route_display_name(route), display_date(route.planned_date), route.company.name, route.vehicle.name if route.vehicle else "", route_free_label(route), route.status, len(route.stops)]
        if can_see_finance(user):
            row.append(round(route_cost(route), 2))
        ws_routes.append(row)

    ws_clients = wb.create_sheet("Clientes")
    ws_clients.append(["Cliente", "Empresa", "Endereco", "Prioridade", "Janela inicio", "Janela fim", "Latitude", "Longitude"])
    for client in visible_clients:
        ws_clients.append([client.name, client.company.name, client.address or "", client.priority, client.delivery_window_start, client.delivery_window_end, round(client.latitude, 6), round(client.longitude, 6)])

    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F5EFF")
        for column_cells in sheet.columns:
            max_length = max(len(str(cell.value or "")) for cell in column_cells)
            sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max_length + 3, 42)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="dashboard_ris.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@web_bp.route("/web/empresas", methods=["GET", "POST"])
@web_login_required
def companies_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso administrativo restrito.</h2></div>", "empresas"), 403
    notice = ""
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        if not name or not email:
            notice = "<div class='badge badge-danger'>Informe nome da empresa e e-mail.</div>"
        elif Company.query.filter_by(email=email).first():
            notice = "<div class='badge badge-danger'>Ja existe empresa cadastrada com este e-mail.</div>"
        else:
            company = Company(name=name, email=email, active=True)
            db.session.add(company)
            db.session.flush()
            plan = SubscriptionPlan.query.filter_by(active=True).order_by(SubscriptionPlan.monthly_price.desc()).first()
            db.session.add(CompanySubscription(
                company_id=company.id,
                plan_id=plan.id if plan else None,
                status="trial",
                billing_email=email,
                notes="Empresa criada pelo painel master.",
            ))
            audit_event("company_create", user=user, company_id=company.id, resource_type="company", resource_id=company.id, details=name)
            db.session.commit()
            return redirect(url_for("web_bp.guided_implementation_page", company_id=company.id))
    companies = Company.query.order_by(Company.active.desc(), Company.name).all()
    rows = ""
    for company in companies:
        status_badge = f"<span class='badge {'badge-success' if company.active else 'badge-danger'}'>{'Ativa' if company.active else 'Adormecida'}</span>"
        usage_counts = company_dependency_counts(company.id)
        usage_summary = " | ".join([
            f"Bases {usage_counts['bases']}",
            f"Usuarios {usage_counts['colaboradores']}",
            f"Clientes {usage_counts['clientes']}",
            f"Rotas {usage_counts['rotas']}",
            f"Mercadorias {usage_counts['mercadorias']}",
        ])
        dependency_summary = ", ".join(f"{label}: {count}" for label, count in usage_counts.items() if count)
        status_label = "Adormecer" if company.active else "Reativar"
        actions = f"""
        <div class='action-stack'>
            <a class='mini-btn' href='/web/implantacao?company_id={company.id}'>Implantacao</a>
            <a class='mini-btn' href='/web/bases?company_id={company.id}'>Bases</a>
            <form method='post' action='/web/empresas/status'>
                <input type='hidden' name='company_id' value='{company.id}'>
                <button class='mini-btn {"danger" if company.active else ""}' type='submit'>{status_label}</button>
            </form>
        """
        if company_can_be_deleted(company.id):
            actions += f"""
            <form method='post' action='/web/empresas/excluir' onsubmit="return confirm('Excluir esta empresa? Use somente para cadastro criado por engano.');">
                <input type='hidden' name='company_id' value='{company.id}'>
                <button class='mini-btn danger' type='submit'>Excluir</button>
            </form>
            """
        else:
            actions += f"<span class='muted' title='{escape(dependency_summary)}'>Excluir bloqueado</span>"
        actions += "</div>"
        rows += f"""
        <tr>
            <td>{company.id}</td>
            <td><strong>{escape(company.name)}</strong><br><span class='muted'>{escape(usage_summary)}</span></td>
            <td>{escape(company.email)}</td>
            <td>{status_badge}</td>
            <td>{actions}</td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='5'>Nenhuma empresa cadastrada ainda.</td></tr>"
    body = f"""
    <div class='card'>
        <h2>Cadastrar nova empresa cliente</h2>
        <p class='muted'>Use esta tela para cadastrar a empresa contratante do SaaS. Depois entre em Bases para cadastrar matriz, filiais e unidades desse cliente.</p>
        {notice}
        <form method='post' class='grid' style='margin-top:12px;'>
            <label>Nome da empresa</label><input name='name' required placeholder='Ex.: Transportadora Cliente'>
            <label>E-mail principal / cobranca</label><input name='email' type='email' required placeholder='financeiro@empresa.com.br'>
            <button class='btn' type='submit'>Cadastrar empresa</button>
        </form>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Empresas cadastradas</h2>
        <table class='table'><thead><tr><th>ID</th><th>Empresa</th><th>Email</th><th>Status</th><th>Acao</th></tr></thead><tbody>{rows}</tbody></table>
    </div>
    """
    return layout("Empresas", body, "empresas")


@web_bp.route("/web/empresas/status", methods=["POST"])
@web_login_required
def company_status_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso administrativo restrito.</h2></div>", "empresas"), 403
    company = Company.query.get_or_404(request.form.get("company_id", type=int))
    company.active = not bool(company.active)
    audit_event("company_status_toggle", user=user, company_id=company.id, resource_type="company", resource_id=company.id, details=f"{company.name}: {'ativa' if company.active else 'adormecida'}")
    db.session.commit()
    return redirect(url_for("web_bp.companies_page"))


@web_bp.route("/web/empresas/excluir", methods=["POST"])
@web_login_required
def company_delete_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso administrativo restrito.</h2></div>", "empresas"), 403
    company = Company.query.get_or_404(request.form.get("company_id", type=int))
    if company_can_be_deleted(company.id):
        company_id = company.id
        name = company.name
        CompanySubscription.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        AuditLog.query.filter_by(company_id=company_id).update({"company_id": None}, synchronize_session=False)
        PlatformEvent.query.filter_by(company_id=company_id).update({"company_id": None}, synchronize_session=False)
        db.session.delete(company)
        audit_event("company_delete", user=user, resource_type="company", resource_id=company_id, details=name)
    else:
        company.active = False
        audit_event("company_sleep_instead_delete", user=user, company_id=company.id, resource_type="company", resource_id=company.id, details=company.name)
    db.session.commit()
    return redirect(url_for("web_bp.companies_page"))


@web_bp.route("/web/rotas")
@web_login_required
def routes_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))
    rows = ""
    visible_routes = sorted(scoped_query(LogisticRoute, user).all(), key=route_sort_key)

    def driver_user_options(company_id, selected_id=None):
        drivers = User.query.filter(
            User.company_id == company_id,
            User.active == True,
            User.role.in_(("driver", "motorista")),
        ).order_by(User.name).all()
        options = [f"<option value='' {'selected' if not selected_id else ''}>Sem login vinculado</option>"]
        for driver in drivers:
            selected = "selected" if selected_id == driver.id else ""
            base_label = driver.base.name if getattr(driver, "base", None) else "Todas as bases"
            options.append(f"<option value='{driver.id}' {selected}>{escape(driver.name)} | {escape(driver.email)} | {escape(base_label)}</option>")
        return "".join(options)

    vehicle_options = "".join(
        f"<option value='{vehicle.id}'>{escape(vehicle.name)} | {escape(vehicle.vehicle_type)} | {vehicle.average_speed_kmh} km/h | {escape(vehicle.company.name if vehicle.company else '')}</option>"
        for vehicle in scoped_query(Vehicle, user).filter_by(active=True).order_by(Vehicle.name).all()
    )
    if not vehicle_options:
        vehicle_options = "<option value=''>Cadastre um veiculo antes de criar rota</option>"
    company_field = ""
    if user.is_master:
        company_options = "".join(
            f"<option value='{company.id}'>{escape(company.name)}</option>"
            for company in Company.query.filter_by(active=True).order_by(Company.name).all()
        )
        company_field = f"""
            <label>Empresa
                <select name="company_id" required>{company_options}</select>
            </label>
        """
    for route in visible_routes:
        cost = f"<td>{format_money(route_cost(route))}</td>" if can_see_finance(user) else ""
        rename_form = f"""
        <form method="post" action="/web/rotas/renomear" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            <input type="hidden" name="route_id" value="{route.id}">
            <input name="name" value="{route_display_name(route)}" placeholder="Ex: ROTA 1" style="min-width:130px;">
            <input name="driver_name" value="{escape(route.driver_name or '')}" placeholder="Responsavel, regiao ou info" style="min-width:190px;">
            <select name="driver_user_id" title="Login do motorista vinculado a esta rota" style="min-width:220px;">{driver_user_options(route.company_id, route.driver_user_id)}</select>
            <button class="btn btn-light" type="submit">Salvar</button>
        </form>
        """
        delete_form = f"""
        <form method="post" action="/web/rotas/excluir" onsubmit="return confirm('Excluir esta rota e todos os vinculos operacionais dela? Esta acao nao desfaz.');">
            <input type="hidden" name="route_id" value="{route.id}">
            <button class="btn btn-light danger-action" type="submit">Excluir</button>
        </form>
        """
        rows += f"<tr><td>{rename_form}<span class='muted'>Execucao: {display_date(route.planned_date)}</span></td><td>{route.company.name}</td><td>{route.vehicle.name if route.vehicle else '-'}</td><td>{escape(route_free_label(route))}</td><td>{len(route.stops)}</td><td><span class='badge'>{route.status}</span></td>{cost}<td><div class='route-actions'><a class='btn btn-light' href='/web/mapa/{route.id}'>Abrir mapa</a>{delete_form}</div></td></tr>"
    cost_head = "<th>Custo</th>" if can_see_finance(user) else ""
    timeline = all_routes_timeline(visible_routes)
    body = f"""
    <style>
        .route-monitor-card {{ overflow:hidden; }}
        .tower-scroll {{ overflow:auto; border:1px solid #d7e0ec; background:#fff; min-height:260px; }}
        .tower-time,.tower-row {{ display:grid; grid-template-columns:150px minmax(1200px, 1fr); }}
        .tower-time {{ position:sticky; top:0; z-index:3; background:#fff; border-bottom:1px solid #98a2b3; height:38px; }}
        .tower-axis {{ position:relative; background:repeating-linear-gradient(90deg, #e5e7eb 0 1px, transparent 1px 6.17%); }}
        .tower-axis span {{ position:absolute; top:9px; transform:translateX(-50%); font-size:11px; font-weight:800; color:#172033; }}
        .tower-row {{ min-height:40px; border-bottom:1px solid #98a2b3; }}
        .tower-label {{ padding:7px 10px; border-right:1px solid #98a2b3; background:#f8fafc; font-size:12px; }}
        .tower-label strong {{ display:block; color:#172033; }}
        .tower-label span {{ display:block; color:#1849a9; font-size:10px; margin-top:4px; font-weight:800; }}
        .tower-label small {{ display:block; color:#667085; font-size:10px; margin-top:3px; }}
        .tower-label .workday {{ display:block; margin-top:4px; font-size:10px; font-weight:900; color:#475467; }}
        .tower-label .workday.proximo_limite {{ color:#b54708; }}
        .tower-label .workday.acima_limite {{ color:#b42318; }}
        .tower-label em {{ display:block; margin-top:4px; color:#d92d20; font-size:10px; font-style:normal; font-weight:900; text-transform:uppercase; }}
        .tower-lane {{ position:relative; background:repeating-linear-gradient(90deg, #e5e7eb 0 1px, transparent 1px 6.17%); }}
        .route-green-line {{ position:absolute; top:21px; height:3px; background:#0b8f20; border-radius:999px; }}
        .route-stop-pin {{ position:absolute; top:12px; transform:translateX(-50%); display:grid; place-items:center; min-width:20px; height:17px; border-radius:4px; color:#fff; border:2px solid #0811ad; background:#0811ad; font-size:10px; font-weight:800; text-decoration:none; z-index:2; }}
        .route-stop-pin small {{ position:absolute; right:-10px; top:-9px; display:grid; place-items:center; width:16px; height:16px; border-radius:50%; background:#fff; color:#1849a9; border:1px solid #cfd8e3; font-size:10px; line-height:1; box-shadow:0 1px 5px rgba(16,24,40,.2); }}
        .route-stop-pin.risk-stop {{ animation:riskPulse 1.15s infinite; box-shadow:0 0 0 4px rgba(217,45,32,.22), 0 0 16px rgba(217,45,32,.78); border-color:#fff; }}
        .route-help-bell {{ display:inline-flex; align-items:center; gap:3px; margin-top:6px; padding:2px 6px; border-radius:999px; color:#fff; background:#d92d20; text-decoration:none; font-size:12px; font-weight:900; box-shadow:0 0 0 3px rgba(217,45,32,.12), 0 0 14px rgba(217,45,32,.52); animation:riskPulse 1.15s infinite; }}
        .route-help-bell.atencao {{ background:#f79009; box-shadow:0 0 0 3px rgba(247,144,9,.16), 0 0 14px rgba(247,144,9,.45); }}
        .route-help-bell span {{ display:inline-grid; place-items:center; min-width:15px; height:15px; margin:0; border-radius:999px; background:#fff; color:#d92d20; font-size:10px; }}
        @keyframes riskPulse {{ 0%,100% {{ transform:translateX(-50%) scale(1); }} 50% {{ transform:translateX(-50%) scale(1.08); }} }}
        .route-help-bell {{ animation:bellPulse 1.15s infinite; }}
        @keyframes bellPulse {{ 0%,100% {{ transform:scale(1); }} 50% {{ transform:scale(1.08); }} }}
        .service-block {{ position:absolute; top:7px; height:8px; border-radius:2px; border:1px solid rgba(0,0,0,.35); z-index:1; }}
        .break-block {{ position:absolute; top:26px; height:8px; border-radius:3px; background:#7c3aed; border:1px solid #4c1d95; box-shadow:0 0 0 2px rgba(124,58,237,.12); z-index:2; }}
        .home-icon {{ position:absolute; top:14px; transform:translateX(-50%); color:#344054; font-weight:900; z-index:3; }}
        .home-icon.end-home {{ top:25px; }}
        .truck-icon {{ position:absolute; top:14px; transform:translateX(-50%); width:14px; height:14px; border-radius:3px; background:#d92d20; border:1px solid #7a271a; color:#d92d20; z-index:2; }}
        .done-icon {{ position:absolute; top:9px; transform:translateX(-50%); display:grid; place-items:center; width:20px; height:20px; border-radius:50%; background:#16a34a; color:white; border:2px solid white; font-size:13px; font-weight:900; box-shadow:0 2px 8px rgba(16,24,40,.22); z-index:3; }}
        .tower-legend {{ display:flex; gap:16px; flex-wrap:wrap; margin-top:10px; color:#667085; font-size:12px; }}
        .tower-legend span {{ display:inline-flex; align-items:center; gap:6px; }}
        .tower-legend i {{ display:inline-block; width:12px; height:12px; border-radius:3px; }}
        .route-create-card {{ margin-top:16px; }}
        .route-create-form {{ display:grid; grid-template-columns:repeat(5,minmax(150px,1fr)) auto; gap:10px; align-items:end; }}
        .route-create-form label {{ font-weight:900; color:#0b1f3a; }}
        .route-create-form input,.route-create-form select {{ width:100%; box-sizing:border-box; margin-top:6px; border:1px solid #cbd5e1; border-radius:7px; padding:10px; font:inherit; background:#fff; }}
        .route-actions {{ display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
        .route-actions form {{ margin:0; }}
        .danger-action {{ color:#b42318 !important; border-color:#fda29b !important; background:#fff1f3 !important; }}
        @media (max-width:1100px) {{ .route-create-form {{ grid-template-columns:1fr; }} }}
        @media (max-width:900px) {{ .tower-time,.tower-row {{ grid-template-columns:120px minmax(950px, 1fr); }} }}
    </style>
    {timeline}
    <div class='card route-create-card'>
        <h2>Acrescentar rota</h2>
        <form method="post" action="/web/rotas/criar" class="route-create-form">
            {company_field}
            <label>Data
                <input name="planned_date" type="date" value="{datetime.now().date().isoformat()}" required>
            </label>
            <label>Nome da rota
                <input name="name" placeholder="Ex: ROTA 5 - REGIAO NORTE">
            </label>
            <label>Responsavel / regiao / info
                <input name="driver_name" placeholder="Ex: Equipe A, Centro, Carlos">
            </label>
            <label>Veiculo
                <select name="vehicle_id" required>{vehicle_options}</select>
            </label>
            <label>Inicio
                <input name="start_time" type="time" value="08:00" required>
            </label>
            <button class="btn" type="submit">Criar rota</button>
        </form>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Rotas cadastradas</h2>
        <div class='table-wrap'><table class='table'><thead><tr><th>Rota / info manual</th><th>Empresa</th><th>Veiculo</th><th>Responsavel / regiao / info</th><th>Paradas</th><th>Status</th>{cost_head}<th></th></tr></thead><tbody>{rows}</tbody></table></div>
    </div>
    """
    return layout("Rotas e mapa", body, "rotas")


@web_bp.route("/web/rotas/renomear", methods=["POST"])
@web_login_required
def rename_route_page():
    user = request.web_user
    if not can_see_admin(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso disponivel a partir da gerencia.</h2></div>", "rotas"), 403
    route = LogisticRoute.query.get_or_404(request.form.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Rota de outra empresa.</h2></div>", "rotas"), 403
    name = request.form.get("name", "").strip()
    if name:
        route.name = name[:120]
    driver_user_id = request.form.get("driver_user_id", type=int)
    driver_user = None
    if driver_user_id:
        driver_user = User.query.get(driver_user_id)
        if not driver_user or not is_driver(driver_user) or not driver_user.active:
            return layout("Rotas", "<div class='card'><h2>Motorista invalido ou inativo.</h2><a class='btn' href='/web/rotas'>Voltar</a></div>", "rotas"), 400
        if driver_user.company_id != route.company_id:
            return layout("Rotas", "<div class='card'><h2>Motorista de outra empresa.</h2><a class='btn' href='/web/rotas'>Voltar</a></div>", "rotas"), 400
        if driver_user.base_id and route.base_id and driver_user.base_id != route.base_id:
            return layout("Rotas", "<div class='card'><h2>Motorista de outra base.</h2><a class='btn' href='/web/rotas'>Voltar</a></div>", "rotas"), 400
    route.driver_user_id = driver_user.id if driver_user else None
    route.driver_name = request.form.get("driver_name", "").strip()[:120] or (driver_user.name if driver_user else None)
    from database.database import db
    db.session.commit()
    return redirect(url_for("web_bp.routes_page"))


@web_bp.route("/web/rotas/criar", methods=["POST"])
@web_login_required
def create_route_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))

    company_id = request.form.get("company_id", type=int) if user.is_master else user.company_id
    base_id = request.form.get("base_id", type=int) or None
    if not user.is_master and getattr(user, "base_id", None) and role_level(user) < 3:
        base_id = user.base_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "rotas"), 403
    if base_id and not can_access_base(user, base_id):
        return layout("Acesso negado", "<div class='card'><h2>Base fora da sua permissao.</h2></div>", "rotas"), 403

    vehicle = Vehicle.query.filter_by(id=request.form.get("vehicle_id", type=int), company_id=company_id).first()
    if not vehicle:
        return layout("Criar rota", "<div class='card'><h2>Selecione um veiculo valido para criar a rota.</h2><a class='btn' href='/web/rotas'>Voltar</a></div>", "rotas"), 400

    planned_date = request.form.get("planned_date", "").strip()
    try:
        datetime.strptime(planned_date, "%Y-%m-%d")
    except ValueError:
        planned_date = datetime.now().date().isoformat()

    hour, minute = parse_hhmm(request.form.get("start_time"), "08:00")
    start_time = f"{hour:02d}:{minute:02d}"
    existing_count = LogisticRoute.query.filter_by(company_id=company_id, planned_date=planned_date).count()
    default_name = f"ROTA {existing_count + 1} - {display_date(planned_date)}"
    name = request.form.get("name", "").strip() or default_name
    driver_name = request.form.get("driver_name", "").strip() or "Equipe a definir"

    from database.database import db
    route = LogisticRoute(
        company_id=company_id,
        base_id=base_id or vehicle.base_id,
        vehicle_id=vehicle.id,
        name=name[:120],
        driver_name=driver_name[:120],
        status="planejada",
        planned_date=planned_date,
        start_time=start_time,
    )
    db.session.add(route)
    db.session.flush()
    db.session.add(StopObservation(
        company_id=company_id,
        route_id=route.id,
        observation_type="rota_criada_manual",
        description=f"Rota criada manualmente pela operacao para {display_date(planned_date)}, inicio {start_time}.",
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return redirect(url_for("web_bp.operational_page", route_id=route.id, data=planned_date))


@web_bp.route("/web/rotas/excluir", methods=["POST"])
@web_login_required
def delete_route_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))
    route = LogisticRoute.query.get_or_404(request.form.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Rota de outra empresa.</h2></div>", "rotas"), 403

    from database.database import db
    route_id = route.id
    planned_date = route.planned_date
    RouteBreak.query.filter_by(route_id=route_id).delete(synchronize_session=False)
    StopProof.query.filter_by(route_id=route_id).delete(synchronize_session=False)
    StopObservation.query.filter_by(route_id=route_id).delete(synchronize_session=False)
    RouteTransfer.query.filter((RouteTransfer.from_route_id == route_id) | (RouteTransfer.to_route_id == route_id)).delete(synchronize_session=False)
    for event in TelemetryEvent.query.filter_by(route_id=route_id).all():
        event.route_id = None
    db.session.delete(route)
    db.session.commit()
    return redirect(url_for("web_bp.operational_page", data=planned_date or None))


@web_bp.route("/web/clientes", methods=["GET", "POST"])
@web_login_required
def clients_page():
    user = request.web_user
    from database.database import db
    error = ""
    notice = ""
    show_demo = user.is_master and request.args.get("mostrar_demo") == "1"
    search = request.args.get("q", "").strip().lower()
    if request.args.get("salvo") == "1":
        notice = "<div class='client-notice ok'>Cliente salvo com sucesso.</div>"
    if user.is_master and request.args.get("demo_oculto") == "1":
        notice = "<div class='client-notice ok'>Exemplos de demonstracao foram ocultados da lista operacional.</div>"
    if request.method == "POST":
        try:
            company_id = request.form.get("company_id", type=int) if user.is_master else user.company_id
            base_id = request.form.get("base_id", type=int) or None
            if not user.is_master and getattr(user, "base_id", None) and role_level(user) < 3:
                base_id = user.base_id
            name = request.form.get("name", "").strip()
            latitude = round(float(request.form.get("latitude")), 6)
            longitude = round(float(request.form.get("longitude")), 6)
            if not company_id or not name:
                raise ValueError("Informe empresa e nome do cliente.")
            if not can_access_company(user, company_id):
                return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "clientes"), 403
            if base_id and not can_access_base(user, base_id):
                return layout("Acesso negado", "<div class='card'><h2>Base fora da sua permissao.</h2></div>", "clientes"), 403
            db.session.add(Client(
                company_id=company_id,
                base_id=base_id,
                name=name,
                email=request.form.get("email", "").strip() or None,
                address=request.form.get("address", "").strip() or None,
                latitude=latitude,
                longitude=longitude,
                delivery_window_start=request.form.get("delivery_window_start") or None,
                delivery_window_end=request.form.get("delivery_window_end") or None,
                priority=request.form.get("priority") or "media",
                active=True,
            ))
            db.session.commit()
            return redirect(url_for("web_bp.clients_page", salvo=1))
        except Exception as exc:
            db.session.rollback()
            error = f"Nao foi possivel salvar o cliente. Confira nome, latitude e longitude. Detalhe: {exc}"

    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    company_field = ""
    if user.is_master:
        company_options = "".join(f"<option value='{company.id}'>{company.name}</option>" for company in companies if company)
        company_field = f"<label>Empresa</label><select name='company_id' required>{company_options}</select>"
    else:
        company_field = f"<input type='hidden' name='company_id' value='{user.company_id}'><label>Empresa</label><input value='{user.company.name if user.company else ''}' disabled>"
    selected_form_company_id = companies[0].id if companies and not user.is_master else (companies[0].id if companies else None)
    base_field = ""
    if selected_form_company_id:
        base_field = f"<label>Base / unidade</label><select name='base_id'>{company_base_options(selected_form_company_id, getattr(user, 'base_id', None), include_all=False)}</select>"
    if not user.is_master and getattr(user, "base_id", None) and role_level(user) < 3:
        base_field = f"<input type='hidden' name='base_id' value='{user.base_id}'><label>Base / unidade</label><input value='{user.base.name if user.base else ''}' disabled>"

    def is_demo_client(client):
        return (client.name or "").strip().lower().startswith("cliente demo")

    rows = ""
    client_query = scoped_query(Client, user).filter_by(active=True).order_by(Client.name)
    visible_clients = []
    hidden_demo_count = 0
    for client in client_query.all():
        if is_demo_client(client) and not show_demo:
            hidden_demo_count += 1
            continue
        if search and search not in " ".join([
            client.name or "",
            client.email or "",
            client.address or "",
            client.company.name if client.company else "",
            client.priority or "",
        ]).lower():
            continue
        visible_clients.append(client)
    for client in visible_clients:
        janela = f"{client.delivery_window_start or '-'} - {client.delivery_window_end or '-'}"
        email = client.email or "-"
        address = client.address or "-"
        status = "Ativo" if client.active else "Inativo"
        edit_company_field = f"<input type='hidden' name='company_id' value='{client.company_id}'>"
        if user.is_master:
            edit_company_options = "".join(
                f"<option value='{company.id}' {'selected' if company.id == client.company_id else ''}>{escape(company.name)}</option>"
                for company in companies if company
            )
            edit_company_field = f"<label>Empresa</label><select name='company_id' required>{edit_company_options}</select>"
        edit_base_field = f"<label>Base / unidade</label><select name='base_id'>{company_base_options(client.company_id, client.base_id, include_all=False)}</select>"
        if not user.is_master and getattr(user, "base_id", None) and role_level(user) < 3:
            edit_base_field = f"<input type='hidden' name='base_id' value='{user.base_id}'><label>Base / unidade</label><input value='{user.base.name if user.base else ''}' disabled>"
        actions = f"""
        <details class='client-edit-box'>
            <summary>Editar</summary>
            <form method='post' action='/web/clientes/editar' class='client-edit-form'>
                <input type='hidden' name='client_id' value='{client.id}'>
                {edit_company_field}
                {edit_base_field}
                <label>Nome</label><input name='name' value='{escape(client.name)}' required>
                <label>Email</label><input name='email' type='email' value='{escape(client.email or '')}'>
                <label>Endereco</label><input name='address' value='{escape(client.address or '')}'>
                <div class='grid two'>
                    <div><label>Latitude</label><input name='latitude' value='{format_coord(client.latitude)}' required></div>
                    <div><label>Longitude</label><input name='longitude' value='{format_coord(client.longitude)}' required></div>
                </div>
                <div class='grid two'>
                    <div><label>Janela inicio</label><input name='delivery_window_start' type='time' value='{client.delivery_window_start or ''}'></div>
                    <div><label>Janela fim</label><input name='delivery_window_end' type='time' value='{client.delivery_window_end or ''}'></div>
                </div>
                <label>Prioridade</label>
                <select name='priority'>
                    <option value='alta' {'selected' if client.priority == 'alta' else ''}>Alta</option>
                    <option value='media' {'selected' if client.priority == 'media' else ''}>Media</option>
                    <option value='baixa' {'selected' if client.priority == 'baixa' else ''}>Baixa</option>
                </select>
                <button class='mini-btn' type='submit'>Salvar alteracoes</button>
            </form>
        </details>
        <form method='post' action='/web/clientes/excluir' onsubmit="return confirm('Excluir este cliente da lista operacional?');">
            <input type='hidden' name='client_id' value='{client.id}'>
            <button class='mini-btn danger' type='submit'>Excluir</button>
        </form>
        """
        rows += f"<tr><td><strong>{escape(client.name)}</strong><br><span class='muted'>{escape(email)}</span></td><td>{escape(address)}</td><td>{escape(client.company.name)}</td><td>{escape(client.base.name if client.base else 'Todas as bases')}</td><td>{janela}</td><td><span class='badge'>{escape(client.priority)}</span><br><span class='muted'>{status}</span></td><td>{format_coord(client.latitude)}, {format_coord(client.longitude)}</td><td>{actions}</td></tr>"
    if not rows:
        rows = "<tr><td colspan='8'>Nenhum cliente encontrado nos filtros atuais.</td></tr>"
    demo_filter_link = ""
    if user.is_master:
        demo_filter_link = (
            "<a class='btn btn-light' href='/web/clientes'>Ocultar exemplos</a>"
            if show_demo else
            "<a class='btn btn-light' href='/web/clientes?mostrar_demo=1'>Mostrar exemplos</a>"
        )
    demo_cleanup_form = ""
    if user.is_master:
        demo_cleanup_form = """
                    <form method='post' action='/web/clientes/ocultar-demo' onsubmit="return confirm('Ocultar os clientes de demonstracao da lista operacional?');">
                        <button class='btn btn-light' type='submit'>Ocultar demo gravado</button>
                    </form>
        """
    if user.is_master:
        hidden_demo_text = f"{hidden_demo_count} exemplo(s) demo oculto(s)." if hidden_demo_count else "Nenhum exemplo demo oculto."
        client_tip_card = f"""
        <div class='client-tip'>
            <strong>Organizacao da base</strong>
            <p class='muted'>Os exemplos de demonstracao ficam ocultos por padrao. A liberacao e limpeza desses exemplos fica restrita a administracao da plataforma.</p>
            <p class='muted'>{hidden_demo_text}</p>
        </div>
        """
    else:
        client_tip_card = """
        <div class='client-tip'>
            <strong>Cadastro operacional</strong>
            <p class='muted'>Cadastre apenas os clientes e locais reais da sua empresa. Esses pontos ficam disponiveis para simulacao, montagem de rotas e gestao operacional.</p>
        </div>
        """
    body = f"""
    <link rel='stylesheet' href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'>
    <style>
        #clientPickMap {{ height:360px; border:1px solid #d7e0ec; border-radius:8px; overflow:hidden; }}
        .client-map-help {{ margin:8px 0 0; font-size:13px; color:#667085; }}
        .client-edit-box summary {{ cursor:pointer; color:#1849a9; font-weight:900; }}
        .client-edit-form {{ display:grid; gap:7px; min-width:280px; margin-top:8px; }}
        .client-edit-form input,.client-edit-form select {{ border:1px solid #cfd8e3; border-radius:6px; padding:8px; font:inherit; }}
        .client-edit-form label {{ font-size:12px; color:#0b1f3a; font-weight:800; margin:0; }}
        .client-hero {{ display:grid; grid-template-columns:1.2fr .8fr; gap:16px; margin-bottom:16px; }}
        .client-tip {{ background:#f8fafc; border:1px solid #d7e0ec; border-radius:8px; padding:14px; }}
        .client-notice {{ padding:10px 12px; border-radius:8px; font-weight:800; margin-bottom:12px; }}
        .client-notice.ok {{ background:#ecfdf3; color:#067647; border:1px solid #abefc6; }}
        .client-toolbar {{ display:flex; gap:10px; flex-wrap:wrap; align-items:end; justify-content:space-between; margin-bottom:12px; }}
        .client-toolbar form {{ display:flex; gap:8px; flex-wrap:wrap; align-items:end; }}
        .client-toolbar input {{ border:1px solid #cfd8e3; border-radius:7px; padding:9px; min-width:240px; }}
        @media (max-width:900px) {{ .client-hero {{ grid-template-columns:1fr; }} .client-toolbar,.client-toolbar form {{ display:block; }} .client-toolbar input {{ width:100%; min-width:0; }} }}
    </style>
    <div class='client-hero'>
        <div class='card'>
            <h2>Novo cliente / ponto de atendimento</h2>
            <p class='muted'>Aqui voce cadastra os locais que entram nas rotas: cliente, endereco, coordenadas, janela de atendimento e prioridade.</p>
            <a class='btn' href='#novo-cliente'>Cadastrar novo cliente</a>
        </div>
        {client_tip_card}
    </div>
    <div class='grid two'>
        <div class='card' id='novo-cliente'>
            <h2>Cadastro do cliente</h2>
            <p class='muted'>Clique no mapa no local do cliente para preencher latitude e longitude. Informe tambem o endereco.</p>
            {notice}
            <div class='error' style='color:#d92d20;font-weight:800;margin-bottom:10px;'>{error}</div>
            <form method='post' class='grid'>
                {company_field}
                {base_field}
                <label>Nome do cliente</label><input name='name' required placeholder='Ex: Cliente Centro'>
                <label>Email</label><input name='email' type='email' placeholder='cliente@email.com'>
                <label>Endereco</label><input name='address' placeholder='Rua, numero, bairro, cidade'>
                <label>Selecionar local no mapa</label>
                <div id='clientPickMap'></div>
                <p class='client-map-help'>Ao clicar no mapa, as coordenadas abaixo serao preenchidas com 6 casas decimais.</p>
                <div class='grid two'>
                    <div><label>Latitude</label><input id='clientLatitude' name='latitude' required placeholder='-29.684200'></div>
                    <div><label>Longitude</label><input id='clientLongitude' name='longitude' required placeholder='-53.806900'></div>
                </div>
                <div class='grid two'>
                    <div><label>Janela inicio</label><input name='delivery_window_start' type='time'></div>
                    <div><label>Janela fim</label><input name='delivery_window_end' type='time'></div>
                </div>
                <label>Prioridade</label>
                <select name='priority'><option value='media'>Media</option><option value='alta'>Alta</option><option value='baixa'>Baixa</option></select>
                <button class='btn' type='submit'>Salvar novo cliente</button>
            </form>
        </div>
        <div class='card'>
            <h2>Clientes reais cadastrados</h2>
            <div class='client-toolbar'>
                <form method='get'>
                    <label><strong>Buscar cliente</strong><br><input name='q' value='{escape(search)}' placeholder='Nome, endereco, email ou prioridade'></label>
                    {'<input type="hidden" name="mostrar_demo" value="1">' if show_demo else ''}
                    <button class='btn' type='submit'>Buscar</button>
                </form>
                <div style='display:flex;gap:8px;flex-wrap:wrap;'>
                    {demo_filter_link}
                    {demo_cleanup_form}
                </div>
            </div>
            <div class='table-wrap'><table class='table'><thead><tr><th>Cliente</th><th>Endereco</th><th>Empresa</th><th>Base</th><th>Janela</th><th>Prioridade</th><th>Latitude, Longitude</th><th>Acoes</th></tr></thead><tbody>{rows}</tbody></table></div>
        </div>
    </div>
    <script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'></script>
    <script>
        (function () {{
            const mapEl = document.getElementById('clientPickMap');
            if (!mapEl || !window.L) return;
            const latInput = document.getElementById('clientLatitude');
            const lonInput = document.getElementById('clientLongitude');
            const map = L.map(mapEl).setView([{BASE_LATITUDE}, {BASE_LONGITUDE}], 13);
            L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                maxZoom: 19,
                attribution: '&copy; OpenStreetMap'
            }}).addTo(map);
            let marker = null;
            function setPoint(lat, lon) {{
                latInput.value = Number(lat).toFixed(6);
                lonInput.value = Number(lon).toFixed(6);
                if (marker) marker.setLatLng([lat, lon]);
                else marker = L.marker([lat, lon]).addTo(map);
            }}
            map.on('click', function (event) {{
                setPoint(event.latlng.lat, event.latlng.lng);
            }});
            if (navigator.geolocation) {{
                navigator.geolocation.getCurrentPosition(function (pos) {{
                    map.setView([pos.coords.latitude, pos.coords.longitude], 15);
                }});
            }}
        }})();
    </script>
    """
    return layout("Clientes", body, "clientes")


@web_bp.route("/web/clientes/ocultar-demo", methods=["POST"])
@web_login_required
def hide_demo_clients_page():
    user = request.web_user
    if not user.is_master:
        return layout("Acesso negado", "<div class='card'><h2>Acao exclusiva da administracao da plataforma.</h2></div>", "clientes"), 403
    from database.database import db

    hidden = 0
    clients = scoped_query(Client, user).filter_by(active=True).all()
    for client in clients:
        if (client.name or "").strip().lower().startswith("cliente demo"):
            client.active = False
            hidden += 1
    db.session.commit()
    return redirect(url_for("web_bp.clients_page", demo_oculto=1 if hidden else 0))


@web_bp.route("/web/clientes/editar", methods=["POST"])
@web_login_required
def edit_client_page():
    user = request.web_user
    from database.database import db
    client = Client.query.get_or_404(request.form.get("client_id", type=int))
    if not can_access_company(user, client.company_id):
        return layout("Acesso negado", "<div class='card'><h2>Cliente fora da sua permissao.</h2></div>", "clientes"), 403
    if client.base_id and not can_access_base(user, client.base_id):
        return layout("Acesso negado", "<div class='card'><h2>Cliente fora da sua base.</h2></div>", "clientes"), 403

    company_id = request.form.get("company_id", type=int) if user.is_master else client.company_id
    base_id = request.form.get("base_id", type=int) or None
    if not user.is_master and getattr(user, "base_id", None) and role_level(user) < 3:
        base_id = user.base_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "clientes"), 403
    if base_id and not can_access_base(user, base_id):
        return layout("Acesso negado", "<div class='card'><h2>Base fora da sua permissao.</h2></div>", "clientes"), 403

    try:
        name = request.form.get("name", "").strip()
        latitude = round(float(request.form.get("latitude")), 6)
        longitude = round(float(request.form.get("longitude")), 6)
        if not name:
            raise ValueError("Informe o nome do cliente.")
        client.company_id = company_id
        client.base_id = base_id
        client.name = name
        client.email = request.form.get("email", "").strip() or None
        client.address = request.form.get("address", "").strip() or None
        client.latitude = latitude
        client.longitude = longitude
        client.delivery_window_start = request.form.get("delivery_window_start") or None
        client.delivery_window_end = request.form.get("delivery_window_end") or None
        client.priority = request.form.get("priority") or "media"
        client.active = True
        db.session.commit()
    except Exception:
        db.session.rollback()
        return layout("Clientes", "<div class='card'><h2>Nao foi possivel editar o cliente.</h2><p>Confira nome, latitude e longitude.</p><a class='btn' href='/web/clientes'>Voltar</a></div>", "clientes"), 400
    return redirect(url_for("web_bp.clients_page"))


@web_bp.route("/web/clientes/excluir", methods=["POST"])
@web_login_required
def delete_client_page():
    user = request.web_user
    from database.database import db
    client = Client.query.get_or_404(request.form.get("client_id", type=int))
    if not can_access_company(user, client.company_id):
        return layout("Acesso negado", "<div class='card'><h2>Cliente fora da sua permissao.</h2></div>", "clientes"), 403

    has_route_history = RouteStop.query.filter_by(client_id=client.id).first() is not None
    if has_route_history:
        client.active = False
    else:
        db.session.delete(client)
    db.session.commit()
    return redirect(url_for("web_bp.clients_page"))


MERCHANDISE_STATUSES = {
    "pendente": "Pendente",
    "coletado": "Coletado",
    "carregado": "Carregado",
    "em_transito": "Em transito",
    "entregue": "Entregue",
    "fora_rota": "Fora da rota",
    "duplicado": "Duplicado",
    "divergente": "Divergente",
}


def merchandise_status_label(status):
    return MERCHANDISE_STATUSES.get(status or "pendente", status or "Pendente")


def merchandise_status_badge(status):
    status = status or "pendente"
    css = {
        "entregue": "badge-success",
        "carregado": "badge-success",
        "coletado": "badge-warning",
        "em_transito": "badge-warning",
        "duplicado": "badge-warning",
        "fora_rota": "badge-danger",
        "divergente": "badge-danger",
    }.get(status, "")
    return f"<span class='badge {css}'>{merchandise_status_label(status)}</span>"


def normalize_merchandise_code(value):
    return (value or "").strip()


def merchandise_match_query(company_id, code):
    code = normalize_merchandise_code(code)
    return Merchandise.query.filter(
        Merchandise.company_id == company_id,
        or_(Merchandise.barcode == code, Merchandise.qr_code == code),
    )


def merchandise_route_options(user, selected_id=None):
    query = LogisticRoute.query
    if not user.is_master:
        query = query.filter_by(company_id=user.company_id)
    routes = query.order_by(LogisticRoute.planned_date.desc(), LogisticRoute.id.desc()).limit(300).all()
    return "<option value=''>Sem rota vinculada</option>" + "".join(
        f"<option value='{route.id}' {'selected' if selected_id == route.id else ''}>{escape(route_display_name(route))} | {display_date(route.planned_date)} | {escape(route_free_label(route))}</option>"
        for route in routes
    )


def merchandise_stop_options(company_id, selected_id=None):
    stops = RouteStop.query.join(LogisticRoute, RouteStop.route_id == LogisticRoute.id).filter(LogisticRoute.company_id == company_id).order_by(LogisticRoute.planned_date.desc(), LogisticRoute.id.desc(), RouteStop.sequence).limit(800).all()
    return "<option value=''>Sem parada vinculada</option>" + "".join(
        f"<option value='{stop.id}' {'selected' if selected_id == stop.id else ''}>{escape(route_display_name(stop.route))} | Parada {stop.sequence} - {escape(stop.client.name if stop.client else '-')}</option>"
        for stop in stops
    )


def merchandise_client_options(company_id, selected_id=None):
    clients = Client.query.filter_by(company_id=company_id, active=True).order_by(Client.name).limit(800).all()
    return "".join(
        f"<option value='{client.id}' {'selected' if selected_id == client.id else ''}>{escape(client.name)}</option>"
        for client in clients
    )


def merchandise_loading_dates(company_id):
    dates = [
        item[0] for item in db.session.query(LogisticRoute.planned_date)
        .filter(LogisticRoute.company_id == company_id, LogisticRoute.planned_date.isnot(None))
        .distinct()
        .order_by(LogisticRoute.planned_date.desc())
        .all()
        if item[0]
    ]
    today = datetime.now().date().isoformat()
    if today not in dates:
        dates.insert(0, today)
    return dates


def loading_routes_for_day(company_id, selected_date):
    query = LogisticRoute.query.filter_by(company_id=company_id)
    if selected_date:
        query = query.filter_by(planned_date=selected_date)
    return query.order_by(LogisticRoute.name, LogisticRoute.id).all()


def route_loading_items(route):
    if not route:
        return []
    return Merchandise.query.filter_by(route_id=route.id).order_by(Merchandise.route_stop_id, Merchandise.client_id, Merchandise.id).all()


def loading_summary(items, route=None):
    total = len(items)
    loaded_statuses = {"carregado", "coletado", "em_transito", "entregue"}
    loaded = len([item for item in items if (item.status or "") in loaded_statuses])
    pending = len([item for item in items if (item.status or "pendente") == "pendente"])
    divergences = len([item for item in items if (item.status or "") in {"divergente", "fora_rota"}])
    duplicate_scans = 0
    out_of_route_scans = 0
    if route:
        duplicate_scans = MerchandiseScan.query.filter_by(route_id=route.id, result_status="duplicado").count()
        out_of_route_scans = MerchandiseScan.query.filter_by(route_id=route.id, result_status="fora_rota").count()
    return {
        "total": total,
        "loaded": loaded,
        "pending": pending,
        "divergences": divergences + out_of_route_scans,
        "duplicates": duplicate_scans,
        "weight": sum(float(item.weight_kg or 0) for item in items),
        "cubic": sum(float(item.cubic_meters or 0) for item in items),
    }


def route_loading_options(routes, selected_route_id=None):
    if not routes:
        return "<option value=''>Nenhuma rota nesta data</option>"
    return "".join(
        f"<option value='{route.id}' {'selected' if selected_route_id == route.id else ''}>{escape(route_display_name(route))} | {escape(route_free_label(route))}</option>"
        for route in routes
    )


@web_bp.route("/web/mercadorias", methods=["GET", "POST"])
@web_login_required
def merchandises_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))

    selected_company_id = request.form.get("company_id", type=int) or request.args.get("company_id", type=int)
    if not selected_company_id:
        selected_company_id = user.company_id if not user.is_master else (Company.query.order_by(Company.name).first().id if Company.query.first() else None)
    if not selected_company_id or not can_access_company(user, selected_company_id):
        empty_state = """
        <div class='card'>
            <h2>Selecione ou cadastre uma empresa para usar Mercadorias.</h2>
            <p class='muted'>
                Esta tela controla as mercadorias do estoque e da operacao: codigo da NF, pedido, cliente,
                volume, peso, cubagem, lote, codigo de barras ou QR Code, rota vinculada e status
                como pendente, carregado, em transito, entregue ou divergente.
            </p>
            <p class='muted'>
                Primeiro cadastre a empresa contratante e seus clientes/pontos. Depois as mercadorias
                poderao ser cadastradas manualmente, importadas por planilha e conferidas por leitor,
                camera ou celular do motorista.
            </p>
            <div style='display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;'>
                <a class='btn' href='/web/empresas'>Cadastrar empresa</a>
                <a class='btn btn-light' href='/web/implantacao'>Abrir implantacao guiada</a>
            </div>
        </div>
        """
        return layout("Mercadorias", empty_state, "mercadorias"), 403

    message = ""
    if request.method == "POST":
        client = Client.query.get(request.form.get("client_id", type=int))
        route = LogisticRoute.query.get(request.form.get("route_id", type=int)) if request.form.get("route_id") else None
        stop = RouteStop.query.get(request.form.get("route_stop_id", type=int)) if request.form.get("route_stop_id") else None
        barcode = normalize_merchandise_code(request.form.get("barcode") or request.form.get("qr_code"))
        if not client or client.company_id != selected_company_id or not barcode:
            message = "<div class='panel' style='border-color:#fda29b;color:#b42318;font-weight:800;'>Informe cliente e codigo de barras/QR Code.</div>"
        elif route and route.company_id != selected_company_id:
            message = "<div class='panel' style='border-color:#fda29b;color:#b42318;font-weight:800;'>Rota pertence a outra empresa.</div>"
        elif stop and (stop.route.company_id != selected_company_id or (route and stop.route_id != route.id)):
            message = "<div class='panel' style='border-color:#fda29b;color:#b42318;font-weight:800;'>Parada invalida para a rota selecionada.</div>"
        else:
            existing = merchandise_match_query(selected_company_id, barcode).first()
            if existing:
                message = "<div class='panel' style='border-color:#fda29b;color:#b42318;font-weight:800;'>Codigo ja cadastrado para outra mercadoria.</div>"
            else:
                try:
                    merchandise = Merchandise(
                        company_id=selected_company_id,
                        client_id=client.id,
                        route_id=route.id if route else (stop.route_id if stop else None),
                        route_stop_id=stop.id if stop else None,
                        invoice_code=request.form.get("invoice_code", "").strip() or None,
                        order_code=request.form.get("order_code", "").strip() or None,
                        volume=request.form.get("volume", "").strip() or None,
                        weight_kg=float(request.form.get("weight_kg") or 0),
                        cubic_meters=float(request.form.get("cubic_meters") or 0),
                        batch_code=request.form.get("batch_code", "").strip() or None,
                        barcode=barcode,
                        qr_code=normalize_merchandise_code(request.form.get("qr_code")) or barcode,
                        status=request.form.get("status") or "pendente",
                        notes=request.form.get("notes", "").strip() or None,
                    )
                    db.session.add(merchandise)
                    db.session.commit()
                    message = "<div class='panel' style='border-color:#abefc6;color:#067647;font-weight:800;'>Mercadoria cadastrada com sucesso.</div>"
                except ValueError:
                    db.session.rollback()
                    message = "<div class='panel' style='border-color:#fda29b;color:#b42318;font-weight:800;'>Peso e cubagem precisam ser numeros validos.</div>"

    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    company_options = "".join(
        f"<option value='{company.id}' {'selected' if company.id == selected_company_id else ''}>{escape(company.name)}</option>"
        for company in companies if company
    )
    status_filter = request.args.get("status", "")
    route_filter = request.args.get("route_id", type=int)
    search = request.args.get("q", "").strip().lower()

    query = Merchandise.query.filter_by(company_id=selected_company_id)
    if status_filter:
        query = query.filter_by(status=status_filter)
    if route_filter:
        query = query.filter_by(route_id=route_filter)
    merchandises = query.order_by(Merchandise.created_at.desc(), Merchandise.id.desc()).limit(500).all()
    if search:
        merchandises = [
            item for item in merchandises
            if search in " ".join([
                item.invoice_code or "",
                item.order_code or "",
                item.client.name if item.client else "",
                item.batch_code or "",
                item.barcode or "",
                item.qr_code or "",
                item.notes or "",
            ]).lower()
        ]

    summary_query = scoped_query(Merchandise, user).filter_by(company_id=selected_company_id)
    total = summary_query.count()
    pending = summary_query.filter_by(status="pendente").count()
    delivered = summary_query.filter_by(status="entregue").count()
    divergent = summary_query.filter_by(status="divergente").count()

    route_options = merchandise_route_options(user, route_filter)
    route_form_options = merchandise_route_options(user)
    client_options = merchandise_client_options(selected_company_id)
    stop_options = merchandise_stop_options(selected_company_id)
    status_options = "".join(f"<option value='{key}'>{label}</option>" for key, label in MERCHANDISE_STATUSES.items())
    filter_status_options = "<option value=''>Todos</option>" + "".join(
        f"<option value='{key}' {'selected' if status_filter == key else ''}>{label}</option>"
        for key, label in MERCHANDISE_STATUSES.items()
    )

    rows = ""
    for item in merchandises:
        route_name = route_display_name(item.route) if item.route else "-"
        stop_name = f"Parada {item.route_stop.sequence}" if item.route_stop else "-"
        scanned = item.last_scanned_at.strftime("%d/%m/%Y %H:%M") if item.last_scanned_at else "-"
        rows += f"""
        <tr>
            <td><strong>{escape(item.client.name if item.client else '-')}</strong><br><span class='muted'>NF {escape(item.invoice_code or '-')} | Pedido {escape(item.order_code or '-')}</span></td>
            <td>{escape(item.volume or '-')}<br><span class='muted'>{item.weight_kg or 0:.2f} kg | {item.cubic_meters or 0:.3f} m3</span></td>
            <td>{escape(item.batch_code or '-')}</td>
            <td><strong>{escape(item.barcode)}</strong><br><span class='muted'>QR {escape(item.qr_code or '-')}</span></td>
            <td>{escape(route_name)}<br><span class='muted'>{escape(stop_name)}</span></td>
            <td>{merchandise_status_badge(item.status)}<br><span class='muted'>Ultima leitura: {scanned}</span></td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='6'>Nenhuma mercadoria encontrada.</td></tr>"

    recent_scans = scoped_route_record_query(MerchandiseScan, user).filter_by(company_id=selected_company_id).order_by(MerchandiseScan.created_at.desc(), MerchandiseScan.id.desc()).limit(80).all()
    scan_rows = ""
    for scan in recent_scans:
        created = scan.created_at.strftime("%d/%m/%Y %H:%M") if scan.created_at else "-"
        item = scan.merchandise
        client_name = item.client.name if item and item.client else "-"
        route_name = route_display_name(scan.route) if scan.route else "-"
        photo_link = f"<a class='btn btn-light' href='/web/mercadorias/arquivo/{scan.photo_filename}' target='_blank'>Foto</a>" if scan.photo_filename else "-"
        scan_rows += f"""
        <tr>
            <td>{created}<br><span class='muted'>{escape(scan.scanned_by.name if scan.scanned_by else '-')}</span></td>
            <td>{escape(scan.scanned_code)}<br><span class='muted'>{escape(client_name)}</span></td>
            <td>{escape(route_name)}</td>
            <td>{merchandise_status_badge(scan.result_status)}</td>
            <td>{escape(scan.notes or '-')}</td>
            <td>{photo_link}</td>
        </tr>
        """
    if not scan_rows:
        scan_rows = "<tr><td colspan='6'>Nenhuma leitura registrada ainda.</td></tr>"

    body = f"""
    <style>
        .merch-form {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; align-items:end; }}
        .merch-form label {{ font-weight:800; color:#0b1f3a; font-size:13px; }}
        .merch-form input,.merch-form select,.merch-form textarea {{ width:100%; margin-top:6px; border:1px solid #cbd5e1; border-radius:7px; padding:9px; font:inherit; }}
        .scan-box {{ display:grid; grid-template-columns:1fr auto; gap:10px; align-items:end; }}
        @media (max-width:900px) {{ .merch-form,.scan-box {{ grid-template-columns:1fr; }} }}
    </style>
    <div class='grid cards'>
        <div class='card'><div class='muted'>Mercadorias</div><div class='metric'>{total}</div></div>
        <div class='card'><div class='muted'>Pendentes</div><div class='metric'>{pending}</div></div>
        <div class='card'><div class='muted'>Entregues</div><div class='metric'>{delivered}</div></div>
        <div class='card'><div class='muted'>Divergencias</div><div class='metric'>{divergent}</div></div>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Cadastro / importacao de mercadorias</h2>
        <p class='muted'>Controle NF, pedido, volume, peso, cubagem, lote, codigo de barras/QR Code, rota vinculada e status operacional.</p>
        {message}
        <form method='post' class='merch-form'>
            <label>Empresa<select name='company_id' {'disabled' if not user.is_master else ''}>{company_options}</select></label>
            <label>Cliente<select name='client_id' required>{client_options}</select></label>
            <label>Rota vinculada<select name='route_id'>{route_form_options}</select></label>
            <label>Parada vinculada<select name='route_stop_id'>{stop_options}</select></label>
            <label>Codigo NF<input name='invoice_code' placeholder='NF ou chave'></label>
            <label>Pedido<input name='order_code' placeholder='Pedido / OS'></label>
            <label>Volume<input name='volume' placeholder='Ex: 3 caixas'></label>
            <label>Lote<input name='batch_code' placeholder='Lote'></label>
            <label>Peso kg<input name='weight_kg' type='number' step='0.001' value='0'></label>
            <label>Cubagem m3<input name='cubic_meters' type='number' step='0.001' value='0'></label>
            <label>Codigo de barras<input name='barcode' required placeholder='Leitor, EAN, Code128...'></label>
            <label>QR Code<input name='qr_code' placeholder='Opcional, se diferente'></label>
            <label>Status<select name='status'>{status_options}</select></label>
            <label style='grid-column:span 2;'>Observacao<input name='notes' placeholder='Avaria, separacao, restricao...'></label>
            <button class='btn' type='submit'>Salvar mercadoria</button>
        </form>
        <div style='margin-top:14px;display:flex;gap:10px;flex-wrap:wrap;'>
            <a class='btn' href='/web/mercadorias/carregamento?company_id={selected_company_id}'>Abrir carregamento por rota</a>
            <a class='btn btn-light' href='/web/mercadorias/modelo-importacao.xlsx'>Baixar planilha padrao</a>
            <a class='btn btn-light' href='/web/mercadorias/exportar.xlsx?company_id={selected_company_id}'>Exportar Excel</a>
        </div>
        <form method='post' action='/web/mercadorias/importar' enctype='multipart/form-data' class='scan-box' style='margin-top:12px;'>
            <input type='hidden' name='company_id' value='{selected_company_id}'>
            <label>Importar planilha .xlsx<input name='file' type='file' accept='.xlsx' required></label>
            <button class='btn' type='submit'>Importar mercadorias</button>
        </form>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Conferencia de armazem / operador</h2>
        <form method='post' action='/web/mercadorias/conferencia' class='merch-form'>
            <input type='hidden' name='company_id' value='{selected_company_id}'>
            <label>Rota<select name='route_id'>{route_options}</select></label>
            <label>Acao<select name='scan_action'><option value='carregado'>Marcar como carregado</option><option value='coletado'>Marcar como coletado</option><option value='em_transito'>Marcar em transito</option><option value='divergente'>Registrar divergencia</option></select></label>
            <label style='grid-column:span 2;'>Codigos lidos<textarea name='codes' rows='3' placeholder='Use leitor Bluetooth/USB ou cole um codigo por linha'></textarea></label>
            <label style='grid-column:span 3;'>Observacao<input name='notes' placeholder='Conferencia de lote, doca, operador, divergencia...'></label>
            <button class='btn' type='submit'>Conferir lote</button>
        </form>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Mercadorias cadastradas</h2>
        <form method='get' class='merch-form' style='margin-bottom:14px;'>
            <input type='hidden' name='company_id' value='{selected_company_id}'>
            <label>Status<select name='status'>{filter_status_options}</select></label>
            <label>Rota<select name='route_id'>{route_options}</select></label>
            <label>Buscar<input name='q' value='{escape(search)}' placeholder='cliente, NF, lote, codigo'></label>
            <button class='btn' type='submit'>Filtrar</button>
        </form>
        <div class='table-wrap'><table class='table'><thead><tr><th>Cliente / docs</th><th>Volume</th><th>Lote</th><th>Codigo</th><th>Rota</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Rastreabilidade das leituras</h2>
        <p class='muted'>Mostra quem leu, quando leu, codigo, rota, resultado, observacao e foto quando enviada.</p>
        <div class='table-wrap'><table class='table'><thead><tr><th>Data / usuario</th><th>Codigo / cliente</th><th>Rota</th><th>Resultado</th><th>Observacao</th><th>Arquivo</th></tr></thead><tbody>{scan_rows}</tbody></table></div>
    </div>
    """
    return layout("Mercadorias", body, "mercadorias")


@web_bp.route("/web/mercadorias/arquivo/<path:filename>")
@web_login_required
def merchandise_scan_file_page(filename):
    user = request.web_user
    safe_name = secure_filename(filename)
    scan = MerchandiseScan.query.filter_by(photo_filename=safe_name).first_or_404()
    if not can_access_company(user, scan.company_id):
        return layout("Acesso negado", "<div class='card'><h2>Arquivo de outra empresa.</h2></div>", "mercadorias"), 403
    if not scan.route or not can_access_route(user, scan.route):
        return layout("Acesso negado", "<div class='card'><h2>Arquivo fora da sua base ou rota.</h2></div>", "mercadorias"), 403
    try:
        return send_stored_file("proofs", safe_name)
    except FileNotFoundError:
        return layout("Arquivo nao encontrado", "<div class='card'><h2>Arquivo da conferencia nao localizado.</h2></div>", "mercadorias"), 404


@web_bp.route("/web/mercadorias/modelo-importacao.xlsx")
@web_login_required
def merchandises_import_template_page():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Mercadorias"
    ws.append(["cliente", "codigo_nf", "pedido", "volume", "peso_kg", "cubagem_m3", "lote", "codigo_barras", "qr_code", "rota_id", "parada_id", "status", "observacao"])
    ws.append(["Cliente Demo A", "NF123", "PED456", "3 caixas", 12.5, 0.320, "L001", "7891234567890", "QR7891234567890", "", "", "pendente", ""])
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="modelo_importacao_mercadorias_ris.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@web_bp.route("/web/mercadorias/exportar.xlsx")
@web_login_required
def merchandises_export_page():
    user = request.web_user
    company_id = request.args.get("company_id", type=int) if user.is_master else user.company_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa invalida.</h2></div>", "mercadorias"), 403
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Mercadorias"
    headers = ["Cliente", "NF", "Pedido", "Volume", "Peso kg", "Cubagem m3", "Lote", "Codigo barras", "QR Code", "Rota", "Parada", "Status", "Ultima leitura", "Observacao"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F5EFF")
    for item in Merchandise.query.filter_by(company_id=company_id).order_by(Merchandise.created_at.desc()).all():
        ws.append([
            item.client.name if item.client else "",
            item.invoice_code or "",
            item.order_code or "",
            item.volume or "",
            item.weight_kg or 0,
            item.cubic_meters or 0,
            item.batch_code or "",
            item.barcode or "",
            item.qr_code or "",
            route_display_name(item.route) if item.route else "",
            item.route_stop.sequence if item.route_stop else "",
            merchandise_status_label(item.status),
            item.last_scanned_at.strftime("%d/%m/%Y %H:%M") if item.last_scanned_at else "",
            item.notes or "",
        ])
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"mercadorias_ris_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@web_bp.route("/web/mercadorias/importar", methods=["POST"])
@web_login_required
def merchandises_import_page():
    user = request.web_user
    company_id = request.form.get("company_id", type=int) if user.is_master else user.company_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa invalida.</h2></div>", "mercadorias"), 403
    file = request.files.get("file")
    filename, upload_error = validate_upload(file, ALLOWED_SPREADSHEET_EXTENSIONS, MAX_SPREADSHEET_MB)
    if upload_error:
        return layout("Importar mercadorias", f"<div class='card'><h2>{escape(upload_error)}</h2><a class='btn' href='/web/mercadorias'>Voltar</a></div>", "mercadorias"), 400
    from openpyxl import load_workbook
    try:
        wb = load_workbook(file, data_only=True)
        ws = wb.active
    except Exception as exc:
        return layout("Importar mercadorias", f"<div class='card'><h2>Nao foi possivel ler a planilha.</h2><p>{escape(str(exc))}</p><a class='btn' href='/web/mercadorias'>Voltar</a></div>", "mercadorias"), 400

    headers = [str(cell.value or "").strip().lower() for cell in ws[1]]
    index = {name: pos for pos, name in enumerate(headers)}

    def value(row, *names):
        for name in names:
            pos = index.get(name)
            if pos is not None and pos < len(row):
                item = row[pos].value
                if item is not None:
                    return str(item).strip()
        return ""

    imported = 0
    skipped = []
    for line_number, row in enumerate(ws.iter_rows(min_row=2), start=2):
        client_name = value(row, "cliente", "nome")
        barcode = normalize_merchandise_code(value(row, "codigo_barras", "codigo", "barcode"))
        qr_code = normalize_merchandise_code(value(row, "qr_code", "qr"))
        code = barcode or qr_code
        if not client_name or not code:
            skipped.append(f"Linha {line_number}: cliente ou codigo ausente.")
            continue
        if merchandise_match_query(company_id, code).first():
            skipped.append(f"Linha {line_number}: codigo duplicado.")
            continue
        client = Client.query.filter(Client.company_id == company_id, db.func.lower(Client.name) == client_name.lower()).first()
        if not client:
            skipped.append(f"Linha {line_number}: cliente nao encontrado ({client_name}).")
            continue
        route = LogisticRoute.query.get(int(value(row, "rota_id") or 0)) if value(row, "rota_id").isdigit() else None
        stop = RouteStop.query.get(int(value(row, "parada_id") or 0)) if value(row, "parada_id").isdigit() else None
        if route and route.company_id != company_id:
            route = None
        if stop and stop.route.company_id != company_id:
            stop = None
        try:
            weight = float(value(row, "peso_kg", "peso") or 0)
            cubic = float(value(row, "cubagem_m3", "cubagem") or 0)
        except ValueError:
            weight, cubic = 0, 0
        db.session.add(Merchandise(
            company_id=company_id,
            client_id=client.id,
            route_id=route.id if route else (stop.route_id if stop else None),
            route_stop_id=stop.id if stop else None,
            invoice_code=value(row, "codigo_nf", "nf") or None,
            order_code=value(row, "pedido") or None,
            volume=value(row, "volume") or None,
            weight_kg=weight,
            cubic_meters=cubic,
            batch_code=value(row, "lote") or None,
            barcode=code,
            qr_code=qr_code or code,
            status=value(row, "status") if value(row, "status") in MERCHANDISE_STATUSES else "pendente",
            notes=value(row, "observacao", "obs") or None,
        ))
        imported += 1
    db.session.commit()
    details = "<br>".join(escape(item) for item in skipped[:20])
    return layout("Importar mercadorias", f"<div class='card'><h2>{imported} mercadoria(s) importada(s).</h2><p class='muted'>{details or 'Nenhuma divergencia encontrada.'}</p><a class='btn' href='/web/mercadorias'>Voltar para mercadorias</a></div>", "mercadorias")


@web_bp.route("/web/mercadorias/carregamento")
@web_login_required
def merchandise_loading_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))
    company_id = request.args.get("company_id", type=int) if user.is_master else user.company_id
    if not company_id:
        company_id = Company.query.order_by(Company.name).first().id if user.is_master and Company.query.first() else None
    if not company_id or not can_access_company(user, company_id):
        return layout("Carregamento", "<div class='card'><h2>Empresa invalida para carregamento.</h2></div>", "mercadorias"), 403

    selected_date = request.args.get("data") or datetime.now().date().isoformat()
    dates = merchandise_loading_dates(company_id)
    routes = loading_routes_for_day(company_id, selected_date)
    selected_route_id = request.args.get("route_id", type=int)
    route = None
    if selected_route_id:
        route = LogisticRoute.query.get(selected_route_id)
        if not route or route.company_id != company_id:
            return layout("Carregamento", "<div class='card'><h2>Rota invalida para esta empresa.</h2></div>", "mercadorias"), 403
    elif routes:
        route = routes[0]
        selected_route_id = route.id

    items = route_loading_items(route)
    summary = loading_summary(items, route)
    ready_warning = ""
    if route and summary["pending"]:
        ready_warning = f"<div class='panel' style='border-color:#fedf89;background:#fffbeb;color:#92400e;font-weight:800;'>Atencao: esta rota ainda possui {summary['pending']} mercadoria(s) pendente(s). Revise antes de liberar a saida.</div>"
    elif route:
        ready_warning = "<div class='panel' style='border-color:#abefc6;background:#ecfdf3;color:#067647;font-weight:800;'>Rota sem pendencias de mercadorias no checklist de carregamento.</div>"

    date_options = "".join(
        f"<option value='{escape(day)}' {'selected' if day == selected_date else ''}>{display_date(day)}</option>"
        for day in dates
    )
    route_options = route_loading_options(routes, selected_route_id)

    grouped_rows = ""
    if route:
        stops = RouteStop.query.filter_by(route_id=route.id).order_by(RouteStop.sequence, RouteStop.id).all()
        listed_ids = set()
        for stop in stops:
            stop_items = [item for item in items if item.route_stop_id == stop.id]
            if not stop_items:
                continue
            listed_ids.update(item.id for item in stop_items)
            grouped_rows += f"<tr style='background:#f8fafc;'><td colspan='8'><strong>Parada {stop.sequence} - {escape(stop.client.name if stop.client else '-')}</strong><br><span class='muted'>{escape(stop.client.address or '') if stop.client else ''}</span></td></tr>"
            for item in stop_items:
                last = item.last_scanned_at.strftime("%d/%m %H:%M") if item.last_scanned_at else "-"
                grouped_rows += f"""
                <tr>
                    <td>{merchandise_status_badge(item.status)}</td>
                    <td><strong>{escape(item.barcode)}</strong><br><span class='muted'>QR {escape(item.qr_code or '-')}</span></td>
                    <td>{escape(item.invoice_code or '-')}<br><span class='muted'>Pedido {escape(item.order_code or '-')}</span></td>
                    <td>{escape(item.volume or '-')}</td>
                    <td>{item.weight_kg or 0:.2f}</td>
                    <td>{item.cubic_meters or 0:.3f}</td>
                    <td>{escape(item.batch_code or '-')}</td>
                    <td>{last}<br><span class='muted'>{escape(item.last_scanned_by.name if item.last_scanned_by else '-')}</span></td>
                </tr>
                """
        loose_items = [item for item in items if item.id not in listed_ids]
        if loose_items:
            grouped_rows += "<tr style='background:#f8fafc;'><td colspan='8'><strong>Sem parada vinculada</strong></td></tr>"
            for item in loose_items:
                last = item.last_scanned_at.strftime("%d/%m %H:%M") if item.last_scanned_at else "-"
                grouped_rows += f"""
                <tr>
                    <td>{merchandise_status_badge(item.status)}</td>
                    <td><strong>{escape(item.barcode)}</strong><br><span class='muted'>QR {escape(item.qr_code or '-')}</span></td>
                    <td>{escape(item.invoice_code or '-')}<br><span class='muted'>Pedido {escape(item.order_code or '-')}</span></td>
                    <td>{escape(item.volume or '-')}</td>
                    <td>{item.weight_kg or 0:.2f}</td>
                    <td>{item.cubic_meters or 0:.3f}</td>
                    <td>{escape(item.batch_code or '-')}</td>
                    <td>{last}<br><span class='muted'>{escape(item.last_scanned_by.name if item.last_scanned_by else '-')}</span></td>
                </tr>
                """
    if not grouped_rows:
        grouped_rows = "<tr><td colspan='8'>Nenhuma mercadoria vinculada a esta rota.</td></tr>"

    scan_rows = ""
    if route:
        scans = MerchandiseScan.query.filter_by(route_id=route.id).order_by(MerchandiseScan.created_at.desc(), MerchandiseScan.id.desc()).limit(120).all()
        for scan in scans:
            created = scan.created_at.strftime("%d/%m/%Y %H:%M") if scan.created_at else "-"
            client_name = scan.merchandise.client.name if scan.merchandise and scan.merchandise.client else "-"
            scan_rows += f"""
            <tr>
                <td>{created}<br><span class='muted'>{escape(scan.scanned_by.name if scan.scanned_by else '-')}</span></td>
                <td>{escape(scan.scanned_code)}<br><span class='muted'>{escape(client_name)}</span></td>
                <td>{merchandise_status_badge(scan.result_status)}</td>
                <td>{escape(scan.notes or '-')}</td>
            </tr>
            """
    if not scan_rows:
        scan_rows = "<tr><td colspan='4'>Nenhuma leitura registrada para esta rota.</td></tr>"

    route_hidden = f"<input type='hidden' name='route_id' value='{route.id}'>" if route else ""
    body = f"""
    <style>
        .load-filter {{ display:grid; grid-template-columns:180px 1fr auto auto auto; gap:12px; align-items:end; }}
        .load-filter label,.load-scan label {{ font-weight:800; color:#0b1f3a; font-size:13px; }}
        .load-filter input,.load-filter select,.load-scan textarea,.load-scan input,.load-scan select {{ width:100%; margin-top:6px; border:1px solid #cbd5e1; border-radius:7px; padding:9px; font:inherit; }}
        .load-scan {{ display:grid; grid-template-columns:1fr 180px auto; gap:12px; align-items:end; }}
        @media (max-width:900px) {{ .load-filter,.load-scan {{ grid-template-columns:1fr; }} }}
    </style>
    <div class='card'>
        <div style='display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start;'>
            <div>
                <h2>Checklist de carregamento por rota</h2>
                <p class='muted'>Escolha data e rota, confira volumes por parada e leia os codigos antes da saida.</p>
            </div>
            <a class='btn btn-light' href='/web/mercadorias'>Voltar para mercadorias</a>
        </div>
        <form method='get' class='load-filter' style='margin-top:12px;'>
            <input type='hidden' name='company_id' value='{company_id}'>
            <label>Data<select name='data'>{date_options}</select></label>
            <label>Rota<select name='route_id'>{route_options}</select></label>
            <button class='btn' type='submit'>Ver carregamento</button>
            <a class='btn btn-light' href='/web/mercadorias/carregamento/exportar.xlsx?route_id={route.id if route else ""}'>Exportar Excel</a>
            <a class='btn btn-light' href='/web/mercadorias/carregamento/relatorio?route_id={route.id if route else ""}' target='_blank'>PDF / imprimir</a>
        </form>
    </div>
    <div class='grid cards' style='margin-top:16px;'>
        <div class='card'><div class='muted'>Volumes / itens</div><div class='metric'>{summary['total']}</div></div>
        <div class='card'><div class='muted'>Peso total</div><div class='metric'>{summary['weight']:.1f} kg</div></div>
        <div class='card'><div class='muted'>Cubagem</div><div class='metric'>{summary['cubic']:.3f} m3</div></div>
        <div class='card'><div class='muted'>Carregados</div><div class='metric'>{summary['loaded']}</div></div>
    </div>
    <div class='grid cards' style='margin-top:16px;grid-template-columns:repeat(3,minmax(0,1fr));'>
        <div class='card'><div class='muted'>Pendentes</div><div class='metric'>{summary['pending']}</div></div>
        <div class='card'><div class='muted'>Divergencias / fora da rota</div><div class='metric'>{summary['divergences']}</div></div>
        <div class='card'><div class='muted'>Duplicados</div><div class='metric'>{summary['duplicates']}</div></div>
    </div>
    <div class='card' style='margin-top:16px;'>{ready_warning}</div>
    <div class='card' style='margin-top:16px;'>
        <h2>Leitura em lote</h2>
        <p class='muted'>Use leitor USB/Bluetooth ou cole um codigo por linha. Codigo fora da rota vira alerta vermelho. Codigo repetido vira alerta amarelo.</p>
        <form method='post' action='/web/mercadorias/conferencia' class='load-scan'>
            <input type='hidden' name='company_id' value='{company_id}'>
            <input type='hidden' name='return_to' value='carregamento'>
            {route_hidden}
            <label>Codigos lidos<textarea name='codes' rows='4' placeholder='Leia ou cole um codigo por linha'></textarea></label>
            <label>Acao<select name='scan_action'><option value='carregado'>Carregado no veiculo</option><option value='divergente'>Registrar divergencia</option></select></label>
            <button class='btn' type='submit' {'disabled' if not route else ''}>Conferir carregamento</button>
        </form>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Mercadorias da rota</h2>
        <div class='table-wrap'><table class='table'><thead><tr><th>Status</th><th>Codigo</th><th>NF / pedido</th><th>Volume</th><th>Peso kg</th><th>m3</th><th>Lote</th><th>Ultima leitura</th></tr></thead><tbody>{grouped_rows}</tbody></table></div>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Historico de conferencia da rota</h2>
        <div class='table-wrap'><table class='table'><thead><tr><th>Data / usuario</th><th>Codigo / cliente</th><th>Resultado</th><th>Observacao</th></tr></thead><tbody>{scan_rows}</tbody></table></div>
    </div>
    """
    return layout("Carregamento", body, "mercadorias")


@web_bp.route("/web/mercadorias/carregamento/exportar.xlsx")
@web_login_required
def merchandise_loading_export_page():
    user = request.web_user
    route = LogisticRoute.query.get_or_404(request.args.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Rota de outra empresa.</h2></div>", "mercadorias"), 403
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Checklist"
    headers = ["Data", "Rota", "Parada", "Cliente", "Codigo", "NF", "Pedido", "Volume", "Peso kg", "Cubagem m3", "Lote", "Status", "Ultima leitura", "Usuario"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F5EFF")
    for item in route_loading_items(route):
        ws.append([
            display_date(route.planned_date),
            route_display_name(route),
            item.route_stop.sequence if item.route_stop else "",
            item.client.name if item.client else "",
            item.barcode,
            item.invoice_code or "",
            item.order_code or "",
            item.volume or "",
            item.weight_kg or 0,
            item.cubic_meters or 0,
            item.batch_code or "",
            merchandise_status_label(item.status),
            item.last_scanned_at.strftime("%d/%m/%Y %H:%M") if item.last_scanned_at else "",
            item.last_scanned_by.name if item.last_scanned_by else "",
        ])
    scans = wb.create_sheet("Historico")
    scans.append(["Data", "Usuario", "Codigo", "Resultado", "Observacao"])
    for cell in scans[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F5EFF")
    for scan in MerchandiseScan.query.filter_by(route_id=route.id).order_by(MerchandiseScan.created_at.desc()).all():
        scans.append([
            scan.created_at.strftime("%d/%m/%Y %H:%M") if scan.created_at else "",
            scan.scanned_by.name if scan.scanned_by else "",
            scan.scanned_code,
            merchandise_status_label(scan.result_status),
            scan.notes or "",
        ])
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"checklist_carregamento_rota_{route.id}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@web_bp.route("/web/mercadorias/carregamento/relatorio")
@web_login_required
def merchandise_loading_report_page():
    user = request.web_user
    route = LogisticRoute.query.get_or_404(request.args.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Rota de outra empresa.</h2></div>", "mercadorias"), 403
    items = route_loading_items(route)
    summary = loading_summary(items, route)
    rows = "".join(
        f"<tr><td>{escape(item.client.name if item.client else '-')}</td><td>{escape(item.barcode)}</td><td>{escape(item.invoice_code or '-')}</td><td>{escape(item.volume or '-')}</td><td>{item.weight_kg or 0:.2f}</td><td>{item.cubic_meters or 0:.3f}</td><td>{merchandise_status_label(item.status)}</td></tr>"
        for item in items
    ) or "<tr><td colspan='7'>Nenhuma mercadoria vinculada.</td></tr>"
    return render_template_string("""
    <!doctype html><html lang="pt-br"><head><meta charset="utf-8"><title>Checklist de carregamento</title>
    <style>body{font-family:Arial,sans-serif;color:#101828;margin:28px;}h1{margin:0 0 4px;}p{color:#475467;}table{width:100%;border-collapse:collapse;margin-top:18px;}th,td{border:1px solid #d0d5dd;padding:8px;text-align:left;font-size:12px;}th{background:#1f5eff;color:white}.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}.box{border:1px solid #d0d5dd;padding:12px;border-radius:8px}.box strong{display:block;font-size:22px;margin-top:4px}@media print{button{display:none}}</style>
    </head><body><button onclick="window.print()">Imprimir / salvar PDF</button><h1>Checklist de carregamento</h1><p>{{ route_name }} | {{ date }} | {{ company }}</p>
    <div class="summary"><div class="box">Itens<strong>{{ total }}</strong></div><div class="box">Peso<strong>{{ weight }} kg</strong></div><div class="box">Cubagem<strong>{{ cubic }} m3</strong></div><div class="box">Pendentes<strong>{{ pending }}</strong></div></div>
    <table><thead><tr><th>Cliente</th><th>Codigo</th><th>NF</th><th>Volume</th><th>Peso kg</th><th>m3</th><th>Status</th></tr></thead><tbody>{{ rows|safe }}</tbody></table></body></html>
    """, route_name=route_display_name(route), date=display_date(route.planned_date), company=route.company.name if route.company else "", total=summary["total"], weight=f"{summary['weight']:.1f}", cubic=f"{summary['cubic']:.3f}", pending=summary["pending"], rows=rows)


@web_bp.route("/web/mercadorias/conferencia", methods=["POST"])
@web_login_required
def warehouse_scan_page():
    user = request.web_user
    company_id = request.form.get("company_id", type=int) if user.is_master else user.company_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa invalida.</h2></div>", "mercadorias"), 403
    route = LogisticRoute.query.get(request.form.get("route_id", type=int)) if request.form.get("route_id") else None
    if route and route.company_id != company_id:
        return layout("Acesso negado", "<div class='card'><h2>Rota de outra empresa.</h2></div>", "mercadorias"), 403
    action = request.form.get("scan_action") or "coletado"
    if action not in ("carregado", "coletado", "em_transito", "divergente"):
        action = "carregado"
    codes = [normalize_merchandise_code(line) for line in (request.form.get("codes") or "").splitlines() if normalize_merchandise_code(line)]
    notes = request.form.get("notes", "").strip() or None
    ok = 0
    divergences = 0
    duplicates = 0
    out_of_route = 0
    seen_in_batch = set()
    for code in codes:
        merchandise = merchandise_match_query(company_id, code).first()
        previous_scan = MerchandiseScan.query.filter(
            MerchandiseScan.company_id == company_id,
            MerchandiseScan.scanned_code == code,
            MerchandiseScan.route_id == route.id if route else MerchandiseScan.route_id == (merchandise.route_id if merchandise else None),
            MerchandiseScan.result_status.in_(("carregado", "coletado", "em_transito")),
        ).first() if route or merchandise else None
        result = "fora_rota"
        if code in seen_in_batch or previous_scan:
            result = "duplicado"
            duplicates += 1
        elif merchandise and (not route or merchandise.route_id == route.id):
            merchandise.status = action
            merchandise.last_scanned_at = datetime.now()
            merchandise.last_scanned_by_user_id = user.id
            result = action
            ok += 1
        else:
            divergences += 1
            out_of_route += 1
            if merchandise:
                merchandise.status = "divergente"
        seen_in_batch.add(code)
        db.session.add(MerchandiseScan(
            company_id=company_id,
            merchandise_id=merchandise.id if merchandise else None,
            route_id=route.id if route else (merchandise.route_id if merchandise else None),
            route_stop_id=merchandise.route_stop_id if merchandise else None,
            scanned_code=code,
            scan_action=action,
            result_status=result,
            notes=notes,
            scanned_by_user_id=user.id,
        ))
    db.session.commit()
    return_to = request.form.get("return_to")
    if return_to == "carregamento" and route:
        return redirect(url_for("web_bp.merchandise_loading_page", company_id=company_id, data=route.planned_date or "", route_id=route.id))
    return layout("Conferencia de mercadorias", f"<div class='card'><h2>Conferencia registrada.</h2><p>{ok} codigo(s) corretos. {duplicates} duplicado(s). {out_of_route} fora da rota/nao cadastrado(s).</p><a class='btn' href='/web/mercadorias?company_id={company_id}'>Voltar</a></div>", "mercadorias")


@web_bp.route("/web/telemetria", methods=["GET", "POST"])
@web_login_required
def telemetry_page():
    user = request.web_user
    from database.database import db
    if request.method == "POST":
        route = LogisticRoute.query.get(request.form.get("route_id", type=int))
        if not route:
            return redirect(url_for("web_bp.driver_page" if is_driver(user) else "web_bp.telemetry_page"))
        if not can_access_route(user, route):
            return layout("Acesso negado", "<div class='card'><h2>Esta rota nao pertence ao seu login.</h2></div>", "telemetria"), 403
        latitude = round(float(request.form.get("latitude")), 6)
        longitude = round(float(request.form.get("longitude")), 6)
        battery_text = request.form.get("battery_percent")
        speed_text = request.form.get("speed_kmh")
        db.session.add(TelemetryEvent(
            company_id=route.company_id,
            route_id=route.id,
            vehicle_id=route.vehicle_id,
            driver_user_id=route.driver_user_id,
            latitude=latitude,
            longitude=longitude,
            speed_kmh=float(speed_text) if speed_text else None,
            ignition_on=request.form.get("ignition_on", "on") == "on",
            battery_percent=int(float(battery_text)) if battery_text else None,
            signal_quality=request.form.get("signal_quality") or None,
            source=request.form.get("source") or ("motorista_mobile" if is_driver(user) else "manual"),
            notes=request.form.get("notes") or None,
        ))
        db.session.commit()
        if request.form.get("ajax") == "1":
            return ("", 204)
        if is_driver(user):
            return redirect(url_for("web_bp.driver_page"))
        return redirect(url_for("web_bp.telemetry_page"))

    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))

    routes = scoped_query(LogisticRoute, user).order_by(LogisticRoute.name).all()
    selected_route_id = request.args.get("route_id", type=int)
    selected_route = LogisticRoute.query.get(selected_route_id) if selected_route_id else (routes[0] if routes else None)
    if selected_route and not can_access_route(user, selected_route):
        selected_route = routes[0] if routes else None
    route_options = "".join(f"<option value='{route.id}' {'selected' if selected_route and route.id == selected_route.id else ''}>{route.name} | {route.vehicle.name if route.vehicle else '-'}</option>" for route in routes)
    events_query = TelemetryEvent.query.filter_by(route_id=selected_route.id) if selected_route else TelemetryEvent.query.filter(False)
    events = events_query.order_by(TelemetryEvent.captured_at.asc(), TelemetryEvent.id.asc()).limit(500).all()
    latest = events[-1] if events else None
    first = events[0] if events else None
    total_distance = distancia_reta_km([[event.latitude, event.longitude] for event in events]) if len(events) > 1 else 0
    avg_speed_events = [event.speed_kmh for event in events if event.speed_kmh is not None]
    avg_speed = sum(avg_speed_events) / len(avg_speed_events) if avg_speed_events else None
    max_speed = max(avg_speed_events) if avg_speed_events else None
    last_when = latest.captured_at.strftime("%d/%m/%Y %H:%M:%S") if latest and latest.captured_at else "-"
    battery = f"{latest.battery_percent}%" if latest and latest.battery_percent is not None else "-"
    signal = latest.signal_quality if latest and latest.signal_quality else "-"
    ignition = "Ligada" if latest and latest.ignition_on else "Desligada" if latest else "-"
    period = "-"
    if first and latest and first.captured_at and latest.captured_at:
        period = f"{first.captured_at.strftime('%d/%m %H:%M')} ate {latest.captured_at.strftime('%d/%m %H:%M')}"

    rows = ""
    for index, event in enumerate(events, start=1):
        speed = f"{event.speed_kmh:.1f} km/h" if event.speed_kmh is not None else "-"
        rows += f"<tr><td>{index}</td><td>{event.captured_at.strftime('%d/%m/%Y %H:%M:%S') if event.captured_at else '-'}</td><td>{format_coord(event.latitude)}</td><td>{format_coord(event.longitude)}</td><td>{speed}</td></tr>"
    if not rows:
        rows = "<tr><td colspan='5'>Nenhum ponto GPS recebido para esta rota.</td></tr>"

    export_link = f"/web/telemetria/exportar.xlsx?route_id={selected_route.id}" if selected_route else "#"
    map_link = f"/web/telemetria/mapa?route_id={selected_route.id}" if selected_route else "about:blank"
    body = f"""
    <div class='card'>
        <div style='display:flex;justify-content:space-between;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:14px;'>
            <form method='get' action='/web/telemetria' style='display:flex;gap:10px;align-items:end;flex-wrap:wrap;'>
                <div><label>Rota / veiculo</label><select name='route_id'>{route_options}</select></div>
                <button class='btn btn-light' type='submit'>Ver percurso</button>
            </form>
            <a class='btn' href='{export_link}'>Exportar Excel</a>
        </div>
        <div class='grid cards' style='margin-bottom:14px;'>
            <div class='card'><div class='muted'>Ultimo sinal</div><div class='metric' style='font-size:22px;'>{last_when}</div></div>
            <div class='card'><div class='muted'>Pontos GPS</div><div class='metric'>{len(events)}</div></div>
            <div class='card'><div class='muted'>Distancia GPS</div><div class='metric'>{total_distance:.1f} km</div></div>
            <div class='card'><div class='muted'>Velocidade media</div><div class='metric'>{f'{avg_speed:.1f} km/h' if avg_speed is not None else '-'}</div></div>
            <div class='card'><div class='muted'>Velocidade maxima</div><div class='metric'>{f'{max_speed:.1f} km/h' if max_speed is not None else '-'}</div></div>
            <div class='card'><div class='muted'>Bateria / sinal</div><div class='metric' style='font-size:22px;'>{battery} | {signal}</div></div>
        </div>
        <div class='panel' style='margin-bottom:12px;display:flex;gap:18px;flex-wrap:wrap;'>
            <span><strong>Periodo:</strong> {period}</span>
            <span><strong>Ignicao:</strong> {ignition}</span>
            <span><strong>Rota:</strong> {selected_route.name if selected_route else '-'}</span>
        </div>
        <iframe src="{map_link}" style='width:100%;height:620px;border:0;border-radius:8px;background:#f8fafc;'></iframe>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Pontos GPS do percurso</h2>
        <p class='muted'>Clique nos pontos do mapa para ver horario, velocidade, latitude e longitude.</p>
        <div class='table-wrap'><table class='table'><thead><tr><th>#</th><th>Data/hora</th><th>Latitude</th><th>Longitude</th><th>Velocidade</th></tr></thead><tbody>{rows}</tbody></table></div>
    </div>
    """
    return layout("Telemetria", body, "telemetria")


@web_bp.route("/web/telemetria/mapa")
@web_login_required
def telemetry_map_page():
    user = request.web_user
    route = LogisticRoute.query.get_or_404(request.args.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota nao pertence ao seu login.</h2></div>", "telemetria"), 403
    events = TelemetryEvent.query.filter_by(route_id=route.id).order_by(TelemetryEvent.captured_at.asc(), TelemetryEvent.id.asc()).limit(1000).all()
    center = [events[-1].latitude, events[-1].longitude] if events else [BASE_LATITUDE, BASE_LONGITUDE]
    mapa = folium.Map(location=center, zoom_start=13, width="100%", height="100%", control_scale=True)
    color = route_color(route.id)
    if events:
        line = [[event.latitude, event.longitude] for event in events]
        folium.PolyLine(line, color=color, weight=5, tooltip=f"Percurso GPS | {route.name}").add_to(mapa)
        folium.Marker(line[0], popup="Inicio do percurso GPS", tooltip="Inicio", icon=folium.Icon(color="green", icon="play")).add_to(mapa)
        folium.Marker(line[-1], popup="Ultima posicao recebida", tooltip="Ultima posicao", icon=folium.Icon(color="red", icon="flag")).add_to(mapa)
        for index, event in enumerate(events, start=1):
            speed = f"{event.speed_kmh:.1f} km/h" if event.speed_kmh is not None else "-"
            when = event.captured_at.strftime("%d/%m/%Y %H:%M:%S") if event.captured_at else "-"
            battery = f"{event.battery_percent}%" if event.battery_percent is not None else "-"
            popup = (
                f"Ponto {index}<br>Data/hora: {when}<br>Velocidade: {speed}<br>"
                f"Bateria: {battery}<br>Sinal: {event.signal_quality or '-'}<br>"
                f"Latitude: {format_coord(event.latitude)}<br>Longitude: {format_coord(event.longitude)}"
            )
            radius = 7 if index == len(events) else 4
            folium.CircleMarker(location=[event.latitude, event.longitude], radius=radius, color="#ffffff", weight=2, fill=True, fill_color=color, fill_opacity=.9, popup=popup, tooltip=f"{when} | {speed}").add_to(mapa)
    else:
        folium.Marker(center, popup="Sem pontos GPS recebidos para esta rota.", tooltip="Sem telemetria").add_to(mapa)

    legend = f"""
    <div style="position: fixed; bottom: 24px; left: 24px; z-index: 9999; background: white; padding: 10px 12px; border: 1px solid #d7e0ec; border-radius: 8px; font-family: Arial; font-size: 13px; box-shadow: 0 4px 14px rgba(0,0,0,.16);">
        <strong>Telemetria</strong><br>
        <span style="color:{color};font-weight:900;">▬</span> Percurso GPS<br>
        <span style="color:#16a34a;font-weight:900;">●</span> Inicio<br>
        <span style="color:#d92d20;font-weight:900;">●</span> Ultima posicao<br>
        <small>Clique nos pontos para ver horario, velocidade e bateria.</small>
    </div>
    """
    mapa.get_root().html.add_child(folium.Element(legend))
    return render_template_string(mapa.get_root().render())


@web_bp.route("/web/telemetria/exportar.xlsx")
@web_login_required
def export_telemetry_excel():
    user = request.web_user
    route = LogisticRoute.query.get_or_404(request.args.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota nao pertence ao seu login.</h2></div>", "telemetria"), 403

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Telemetria"
    ws.append(["Rota", "Veiculo", "Data", "Hora", "Latitude", "Longitude", "Velocidade km/h", "Bateria %", "Sinal", "Ignicao", "Origem", "Observacao"])
    events = TelemetryEvent.query.filter_by(route_id=route.id).order_by(TelemetryEvent.captured_at.asc(), TelemetryEvent.id.asc()).all()
    for event in events:
        captured = event.captured_at
        ws.append([
            route.name,
            route.vehicle.name if route.vehicle else "",
            captured.strftime("%d/%m/%Y") if captured else "",
            captured.strftime("%H:%M:%S") if captured else "",
            round(event.latitude, 6),
            round(event.longitude, 6),
            event.speed_kmh,
            event.battery_percent,
            event.signal_quality,
            "Ligada" if event.ignition_on else "Desligada",
            event.source,
            event.notes,
        ])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F5EFF")
    for column_cells in ws.columns:
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        ws.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max_length + 3, 34)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"telemetria_rota_{route.id}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@web_bp.route("/web/veiculos")
@web_login_required
def vehicles_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso disponivel para perfis autorizados.</h2></div>", "veiculos"), 403
    vehicles = scoped_query(Vehicle, user).order_by(Vehicle.name).all()
    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    company_options = "".join(f"<option value='{company.id}'>{escape(company.name)}</option>" for company in companies if company)
    type_options = "".join(
        f"<option value='{value}'>{label}</option>"
        for value, label in (("van", "Van"), ("caminhao", "Caminhao"), ("carro", "Carro"), ("moto", "Moto"))
    )
    rows = ""
    for vehicle in vehicles:
        edit_company_options = "".join(
            f"<option value='{company.id}' {'selected' if company.id == vehicle.company_id else ''}>{escape(company.name)}</option>"
            for company in companies
            if company
        )
        edit_type_options = "".join(
            f"<option value='{value}' {'selected' if value == vehicle.vehicle_type else ''}>{label}</option>"
            for value, label in (("van", "Van"), ("caminhao", "Caminhao"), ("carro", "Carro"), ("moto", "Moto"))
        )
        active_checked = "checked" if vehicle.active else ""
        rows += f"""
        <tr>
            <td colspan="6">
                <form method="post" action="/web/veiculos/salvar" class="inline-edit-grid">
                    <input type="hidden" name="vehicle_id" value="{vehicle.id}">
                    <div><label>Nome</label><input name="name" value="{escape(vehicle.name)}" required></div>
                    <div><label>Empresa</label><select name="company_id">{edit_company_options}</select></div>
                    <div><label>Tipo</label><select name="vehicle_type">{edit_type_options}</select></div>
                    <div><label>Velocidade km/h</label><input name="average_speed_kmh" type="number" min="1" step="1" value="{vehicle.average_speed_kmh}" required></div>
                    <label class="checkline"><input type="checkbox" name="active" value="1" {active_checked}> Ativo</label>
                    <button class="btn btn-light" type="submit">Salvar edicao</button>
                </form>
            </td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='6'>Nenhum veiculo cadastrado.</td></tr>"
    body = f"""
    <div class="grid two">
        <div class="card">
            <h2>Cadastrar veiculo</h2>
            <p class="muted">A velocidade media entra no calculo de tempo das rotas e simulacoes.</p>
            <form method="post" action="/web/veiculos/salvar" class="grid">
                <label>Empresa</label><select name="company_id">{company_options}</select>
                <label>Nome do veiculo</label><input name="name" placeholder="Ex.: Van 01" required>
                <label>Tipo</label><select name="vehicle_type">{type_options}</select>
                <label>Velocidade media km/h</label><input name="average_speed_kmh" type="number" min="1" step="1" value="80" required>
                <label class="checkline"><input type="checkbox" name="active" value="1" checked> Ativo</label>
                <button class="btn" type="submit">Salvar veiculo</button>
            </form>
        </div>
        <div class="card">
            <h2>Padrao sugerido</h2>
            <div class="table-wrap">
                <table class="table">
                    <thead><tr><th>Tipo</th><th>Velocidade</th><th>Uso comum</th></tr></thead>
                    <tbody>
                        <tr><td>Caminhao</td><td>60 km/h</td><td>Carga maior e rotas pesadas</td></tr>
                        <tr><td>Van</td><td>80 km/h</td><td>Operacao urbana/regional</td></tr>
                        <tr><td>Carro</td><td>80 km/h</td><td>Atendimento leve</td></tr>
                        <tr><td>Moto</td><td>70 km/h</td><td>Agilidade e pequenos volumes</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    <div class="card" style="margin-top:16px;">
        <h2>Veiculos cadastrados</h2>
        <div class="table-wrap">
            <table class="table"><thead><tr><th>Editar</th></tr></thead><tbody>{rows}</tbody></table>
        </div>
    </div>
    """
    return layout("Veiculos", body, "veiculos")


@web_bp.route("/web/veiculos/salvar", methods=["POST"])
@web_login_required
def save_vehicle_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso disponivel para perfis autorizados.</h2></div>", "veiculos"), 403
    from database.database import db
    vehicle_id = request.form.get("vehicle_id", type=int)
    company_id = request.form.get("company_id", type=int) if user.is_master else user.company_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa invalida para este usuario.</h2></div>", "veiculos"), 403
    vehicle_type = request.form.get("vehicle_type") or "van"
    if vehicle_type not in ("van", "caminhao", "carro", "moto"):
        vehicle_type = "van"
    speed = request.form.get("average_speed_kmh", type=int) or default_vehicle_speed(vehicle_type)
    speed = max(1, speed)
    if vehicle_id:
        vehicle = Vehicle.query.get_or_404(vehicle_id)
        if not can_access_company(user, vehicle.company_id):
            return layout("Acesso negado", "<div class='card'><h2>Veiculo de outra empresa.</h2></div>", "veiculos"), 403
    else:
        vehicle = Vehicle(company_id=company_id, name="", vehicle_type=vehicle_type, average_speed_kmh=speed, active=True)
        db.session.add(vehicle)
    vehicle.company_id = company_id
    vehicle.name = request.form.get("name") or vehicle_type.title()
    vehicle.vehicle_type = vehicle_type
    vehicle.average_speed_kmh = speed
    vehicle.active = bool(request.form.get("active"))
    db.session.commit()
    return redirect(url_for("web_bp.vehicles_page"))


@web_bp.route("/web/financeiro")
@web_login_required
def finance_page():
    user = request.web_user
    if not can_see_finance(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso exclusivo do CEO/proprietario.</h2></div>", "financeiro"), 403
    rows = ""
    total = 0
    for route in scoped_query(LogisticRoute, user).order_by(LogisticRoute.id.desc()).all():
        cost = route_cost(route)
        total += cost
        rows += f"<tr><td>{route.name}</td><td>{route.company.name}</td><td>{route.vehicle.name if route.vehicle else '-'}</td><td>{format_money(cost)}</td></tr>"
    body = f"<div class='grid cards'><div class='card'><div class='muted'>Custo operacional estimado</div><div class='metric'>{format_money(total)}</div></div></div><div class='card' style='margin-top:16px;'><table class='table'><thead><tr><th>Rota</th><th>Empresa</th><th>Veiculo</th><th>Custo estimado</th></tr></thead><tbody>{rows}</tbody></table></div>"
    return layout("Valores", body, "financeiro")


@web_bp.route("/web/mapa/<int:route_id>")
@web_login_required
def route_map_page(route_id):
    user = request.web_user
    route = LogisticRoute.query.get_or_404(route_id)
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota pertence a outra empresa.</h2></div>", "rotas"), 403
    stops = sorted(route.stops, key=lambda item: item.sequence)
    center = [stops[0].client.latitude, stops[0].client.longitude] if stops else [BASE_LATITUDE, BASE_LONGITUDE]
    mapa = folium.Map(location=center, zoom_start=13)
    points = [[BASE_LATITUDE, BASE_LONGITUDE]] + [[stop.client.latitude, stop.client.longitude] for stop in stops] + [[BASE_LATITUDE, BASE_LONGITUDE]]
    route_path = percurso_pelas_ruas(points)
    distance = route_path["distance_km"]
    speed = route.vehicle.average_speed_kmh if route.vehicle else 60
    duration = tempo_formatado((distance / speed) * 60) if speed else "-"
    financial = f" | Custo: {format_money(route_cost(route))}" if can_see_finance(user) else ""
    resumo = f"{route.name} | {route.company.name} | {route.vehicle.name if route.vehicle else '-'} | {speed} km/h | {distance:.1f} km | {duration}{financial}"
    line_color = route_color(route.id)
    folium.PolyLine(route_path["line"], color=line_color, weight=6, tooltip=resumo, popup=resumo, dash_array=None if route_path["real_route"] else "8, 8").add_to(mapa)
    if route_path["line"]:
        mapa.fit_bounds(route_path["line"], padding=(24, 24))
    folium.Marker([BASE_LATITUDE, BASE_LONGITUDE], popup=f"BASE<br>{resumo}", tooltip="BASE", icon=folium.Icon(color="green", icon="home")).add_to(mapa)
    map_fill_css = """
    <style>
        html, body { width:100%; height:100%; margin:0; padding:0; overflow:hidden; }
        .folium-map { width:100% !important; height:100% !important; min-height:100% !important; }
        .leaflet-container { font-family:Arial, sans-serif; }
    </style>
    """
    mapa.get_root().header.add_child(folium.Element(map_fill_css))
    legend = f"""
    <div style="position: fixed; bottom: 24px; left: 24px; z-index: 9999; background: white; padding: 10px 12px; border: 1px solid #d7e0ec; border-radius: 8px; font-family: Arial; font-size: 13px; box-shadow: 0 4px 14px rgba(0,0,0,.16);">
        <strong>{escape(route.name)}</strong><br>
        <span style="color:{line_color};font-weight:900;">▬</span> Trajeto da rota<br>
        <small>{escape(route_path["message"])}</small><br>
        <hr style="border:0;border-top:1px solid #e5e7eb;margin:6px 0;">
        <strong>Status dos pontos</strong><br>
        <span style="color:#d92d20;font-weight:800;">●</span> A fazer<br>
        <span style="color:#fdb022;font-weight:800;">●</span> Em atendimento<br>
        <span style="color:#16a34a;font-weight:800;">●</span> Realizado
    </div>
    """
    mapa.get_root().html.add_child(folium.Element(legend))
    for stop in stops:
        client = stop.client
        numbered_marker(stop, client).add_to(mapa)
    return render_template_string(mapa.get_root().render())


def simulate_sequence(clients, vehicle_type):
    speed_by_type = {
        "moto": 70,
        "carro": 80,
        "van": 80,
        "caminhao": 60,
    }
    cost_by_type = {
        "moto": 1.35,
        "carro": 2.10,
        "van": 3.20,
        "caminhao": 5.40,
    }
    speed = speed_by_type.get(vehicle_type, 60)
    km_cost = cost_by_type.get(vehicle_type, 3.20)
    ordered = sorted(
        clients,
        key=lambda client: (
            0 if (client.priority or "").lower() == "alta" else 1 if (client.priority or "").lower() == "media" else 2,
            client.delivery_window_start or "99:99",
            client.name,
        ),
    )
    points = [[BASE_LATITUDE, BASE_LONGITUDE]] + [[client.latitude, client.longitude] for client in ordered] + [[BASE_LATITUDE, BASE_LONGITUDE]]
    route_path = percurso_pelas_ruas(points)
    distance = route_path["distance_km"]
    minutes = (distance / speed) * 60 if speed else 0
    cost = (distance * km_cost) + ((minutes / 60) * 35)
    return {
        "clients": ordered,
        "distance": distance,
        "minutes": minutes,
        "cost": cost,
        "speed": speed,
        "vehicle_type": vehicle_type,
    }


def simulation_route_metrics(route_clients, speed, km_cost):
    if not route_clients:
        return {"distance": 0, "travel_minutes": 0, "service_minutes": 0, "break_minutes": 0, "minutes": 0, "cost": 0}
    points = [[BASE_LATITUDE, BASE_LONGITUDE]] + [[client.latitude, client.longitude] for client in route_clients] + [[BASE_LATITUDE, BASE_LONGITUDE]]
    distance = distancia_reta_km(points) * 1.35
    travel_minutes = (distance / speed) * 60 if speed else 0
    service_minutes = len(route_clients) * DEFAULT_SERVICE_MINUTES
    base_work_minutes = travel_minutes + service_minutes
    break_minutes = INTRADAY_BREAK_MINUTES if base_work_minutes > INTRADAY_BREAK_REQUIRED_AFTER and len(route_clients) > 1 else 0
    minutes = travel_minutes + service_minutes + break_minutes
    cost = (distance * km_cost) + ((minutes / 60) * 35)
    return {
        "distance": distance,
        "travel_minutes": travel_minutes,
        "service_minutes": service_minutes,
        "break_minutes": break_minutes,
        "minutes": minutes,
        "cost": cost,
    }


def apply_route_continuity(result):
    speed = result.get("speed") or default_vehicle_speed(result.get("vehicle_type"))
    for route in result.get("routes", []):
        clients = route.get("clients", [])
        route["long_route"] = route.get("minutes", 0) > WORKDAY_LIMIT_MINUTES
        route["continuity_days"] = []
        if not route["long_route"] or not clients:
            continue

        days = []
        current_clients = []
        current_minutes = 0
        current_distance = 0
        last_point = [BASE_LATITUDE, BASE_LONGITUDE]

        def close_day(reason):
            nonlocal current_clients, current_minutes, current_distance
            needs_intraday_break = current_minutes > INTRADAY_BREAK_REQUIRED_AFTER and len(current_clients) > 1
            intraday_break = INTRADAY_BREAK_MINUTES if needs_intraday_break else 0
            days.append({
                "day": len(days) + 1,
                "clients": current_clients,
                "minutes": current_minutes + intraday_break,
                "distance": current_distance,
                "reason": reason,
                "needs_intraday_break": needs_intraday_break,
                "intraday_break_minutes": intraday_break,
                "intraday_window": "entre a 4a e a 6a hora",
                "interjourney_rest_minutes": INTERJOURNEY_REST_MINUTES,
            })
            current_clients = []
            current_minutes = 0
            current_distance = 0

        for client in clients:
            point = [client.latitude, client.longitude]
            segment_distance = distancia_reta_km([last_point, point]) * 1.35
            travel_minutes = (segment_distance / speed) * 60 if speed else 0
            service_minutes = DEFAULT_SERVICE_MINUTES
            projected = current_minutes + travel_minutes + service_minutes
            projected_with_required_break = projected + (INTRADAY_BREAK_MINUTES if projected > INTRADAY_BREAK_REQUIRED_AFTER and current_clients else 0)
            if current_clients and projected_with_required_break > WORKDAY_LIMIT_MINUTES:
                close_day("Pernoite ou continuidade antes do proximo atendimento.")
                current_minutes = 0
                current_distance = 0
                segment_distance = distancia_reta_km([last_point, point]) * 1.35
                travel_minutes = (segment_distance / speed) * 60 if speed else 0
            current_clients.append(client)
            current_minutes += travel_minutes + service_minutes
            current_distance += segment_distance
            last_point = point

        return_distance = distancia_reta_km([last_point, [BASE_LATITUDE, BASE_LONGITUDE]]) * 1.35
        return_minutes = (return_distance / speed) * 60 if speed else 0
        if current_clients and current_minutes + return_minutes > WORKDAY_LIMIT_MINUTES:
            close_day("Pernoite ou continuidade antes do retorno para a base.")
            days.append({
                "day": len(days) + 1,
                "clients": [],
                "minutes": return_minutes,
                "distance": return_distance,
                "reason": "Retorno para a base no dia seguinte.",
                "needs_intraday_break": False,
                "intraday_break_minutes": 0,
                "intraday_window": "",
                "interjourney_rest_minutes": 0,
            })
        else:
            current_minutes += return_minutes
            current_distance += return_distance
            close_day("Retorno para a base dentro da jornada.")

        for day in days:
            day["requires_interjourney_rest"] = day["day"] < len(days)
            day["interjourney_rest_text"] = tempo_formatado(INTERJOURNEY_REST_MINUTES) if day["requires_interjourney_rest"] else ""

        route["continuity_days"] = days
        route["continuity_note"] = f"Rota acima de 10h. Sugestao: executar em {len(days)} dia(s), com descanso interjornada minimo de {tempo_formatado(INTERJOURNEY_REST_MINUTES)} entre um dia e outro."

    result["long_route_count"] = sum(1 for route in result.get("routes", []) if route.get("long_route"))
    result["continuity_day_count"] = sum(len(route.get("continuity_days", [])) for route in result.get("routes", []))
    return result


def split_simulation_routes(clients, vehicle_type):
    speed_by_type = {"moto": 70, "carro": 80, "van": 80, "caminhao": 60}
    speed = speed_by_type.get(vehicle_type, 60)
    cost_by_type = {"moto": 1.35, "carro": 2.10, "van": 3.20, "caminhao": 5.40}
    km_cost = cost_by_type.get(vehicle_type, 3.20)
    routes = []
    remaining = list(clients)

    def route_metrics(route_clients):
        return simulation_route_metrics(route_clients, speed, km_cost)

    def client_priority_rank(client):
        return 0 if (client.priority or "").lower() == "alta" else 1 if (client.priority or "").lower() == "media" else 2

    def client_score_key(client):
        return (client_priority_rank(client), client.delivery_window_start or "99:99", client.name)

    while remaining:
        current = []
        current_minutes = 0
        last_point = [BASE_LATITUDE, BASE_LONGITUDE]
        break_inserted = False

        while remaining:
            scored = []
            for client in remaining:
                point = [client.latitude, client.longitude]
                distance = distancia_reta_km([last_point, point]) * 1.35
                travel = (distance / speed) * 60 if speed else 0
                arrival = current_minutes + travel
                departure = arrival + DEFAULT_SERVICE_MINUTES
                simulated_end = departure
                would_insert_break = False
                return_distance = distancia_reta_km([point, [BASE_LATITUDE, BASE_LONGITUDE]]) * 1.35
                return_minutes = (return_distance / speed) * 60 if speed else 0
                route_end_without_break = simulated_end + return_minutes
                if (
                    not break_inserted
                    and departure >= INTRADAY_BREAK_WINDOW_START
                    and route_end_without_break > INTRADAY_BREAK_REQUIRED_AFTER
                    and current
                ):
                    simulated_end += INTRADAY_BREAK_MINUTES
                    would_insert_break = True
                route_end = simulated_end + return_minutes
                window_end = time_text_to_minutes(client.delivery_window_end) or (24 * 60)
                arrival_absolute = (8 * 60) + arrival
                lateness = max(0, arrival_absolute - window_end)
                over_limit = max(0, route_end - WORKDAY_LIMIT_MINUTES)
                candidate_cost = (distance * km_cost) + ((route_end / 60) * 35)
                score = (distance * 7) + (candidate_cost * 0.55) + (client_priority_rank(client) * 35) + (window_end * 0.025) + (lateness * 12) + (over_limit * 45)
                scored.append((score, client, distance, arrival, departure, simulated_end, route_end, would_insert_break))

            scored.sort(key=lambda item: (item[0], client_score_key(item[1])))
            fitting = [item for item in scored if item[6] <= WORKDAY_LIMIT_MINUTES]
            if fitting:
                chosen = fitting[0]
            elif not current:
                chosen = scored[0]
            else:
                break

            _, client, _distance, _arrival, departure, simulated_end, _route_end, would_insert_break = chosen
            current.append(client)
            current_minutes = simulated_end if would_insert_break else departure
            if would_insert_break:
                break_inserted = True
            last_point = [client.latitude, client.longitude]
            remaining.remove(client)

        metrics = route_metrics(current)
        routes.append({"number": len(routes) + 1, "clients": current, **metrics})

    ordered = [client for route in routes for client in route["clients"]]
    total_distance = sum(route["distance"] for route in routes)
    total_minutes = sum(route["minutes"] for route in routes)
    total_cost = sum(route["cost"] for route in routes)
    return apply_route_continuity({
        "clients": ordered,
        "routes": routes,
        "distance": total_distance,
        "minutes": total_minutes,
        "cost": total_cost,
        "speed": speed,
        "vehicle_type": vehicle_type,
        "max_route_minutes": max((route["minutes"] for route in routes), default=0),
    })


def manual_simulation_routes(clients, vehicle_type, assignments):
    speed_by_type = {"moto": 70, "carro": 80, "van": 80, "caminhao": 60}
    speed = speed_by_type.get(vehicle_type, 60)
    cost_by_type = {"moto": 1.35, "carro": 2.10, "van": 3.20, "caminhao": 5.40}
    km_cost = cost_by_type.get(vehicle_type, 3.20)
    grouped = {}
    for client in clients:
        route_number = assignments.get(client.id, 1)
        try:
            route_number = max(1, int(route_number))
        except (TypeError, ValueError):
            route_number = 1
        grouped.setdefault(route_number, []).append(client)

    routes = []
    ordered = []
    for new_number, original_number in enumerate(sorted(grouped), start=1):
        route_clients = grouped[original_number]
        metrics = simulation_route_metrics(route_clients, speed, km_cost)
        routes.append({"number": new_number, "clients": route_clients, **metrics})
        ordered.extend(route_clients)

    return apply_route_continuity({
        "clients": ordered,
        "routes": routes,
        "distance": sum(route["distance"] for route in routes),
        "minutes": sum(route["minutes"] for route in routes),
        "cost": sum(route["cost"] for route in routes),
        "speed": speed,
        "vehicle_type": vehicle_type,
        "max_route_minutes": max((route["minutes"] for route in routes), default=0),
        "manual_adjusted": True,
    })


def simulation_cubage_tool():
    return """
    <div class='card' style='margin-top:16px;'>
        <div style='display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;'>
            <div>
                <h2>Cubagem, carga e veiculo ideal</h2>
                <p class='muted'>Calcule volume, litragem, peso cubado, carroceria, estoque e sugestao de frete antes de gravar a rota.</p>
            </div>
            <button class='btn btn-light' type='button' id='addCargoRow'>Adicionar caixa</button>
        </div>
        <div class='panel' style='margin:12px 0;'>
            <div style='display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px;align-items:end;'>
                <div><label>Fator cubagem kg/m3</label><input id='cubageFactor' type='number' value='300' min='1' step='1'></div>
                <div><label>Altura util estoque (m)</label><input id='stockHeight' type='number' value='2.50' min='0.1' step='0.1'></div>
                <div><label>Uso maximo veiculo (%)</label><input id='vehicleUse' type='number' value='85' min='40' max='100' step='1'></div>
                <div><label>Reserva estoque (%)</label><input id='stockReserve' type='number' value='15' min='0' max='80' step='1'></div>
                <div><label>Perfil</label><select id='cargoProfile'><option value='rodoviario'>Rodoviario</option><option value='armazenagem'>Armazenagem</option><option value='conteiner'>Conteiner/exportacao</option></select></div>
            </div>
        </div>
        <div class='table-wrap'>
            <table class='table' id='cargoTable'>
                <thead><tr><th>Item</th><th>Qtd</th><th>C x L x A cm</th><th>Peso un. kg</th><th>Empilha?</th><th></th></tr></thead>
                <tbody>
                    <tr>
                        <td><input value='Caixa 1' data-field='name'></td>
                        <td><input type='number' value='10' min='1' step='1' data-field='qty'></td>
                        <td style='min-width:260px;'><input type='number' value='40' min='1' step='0.1' data-field='length' style='width:31%;'> <input type='number' value='30' min='1' step='0.1' data-field='width' style='width:31%;'> <input type='number' value='25' min='1' step='0.1' data-field='height' style='width:31%;'></td>
                        <td><input type='number' value='8' min='0' step='0.1' data-field='weight'></td>
                        <td><input type='checkbox' data-field='stackable' checked></td>
                        <td><button class='btn btn-light cargo-remove' type='button'>Remover</button></td>
                    </tr>
                </tbody>
            </table>
        </div>
        <div class='grid cards' style='margin-top:16px;'>
            <div class='card'><div class='muted'>Volume total</div><div class='metric' id='cargoM3'>0,000 m3</div></div>
            <div class='card'><div class='muted'>Litragem</div><div class='metric' id='cargoLiters'>0 L</div></div>
            <div class='card'><div class='muted'>Peso real</div><div class='metric' id='cargoWeight'>0 kg</div></div>
            <div class='card'><div class='muted'>Peso cubado</div><div class='metric' id='cargoCubedWeight'>0 kg</div></div>
        </div>
        <div class='grid cards' style='margin-top:16px;'>
            <div class='card'><div class='muted'>Espaco ocupado no transporte</div><div class='metric' id='cargoTransportM3'>0,000 m3</div></div>
            <div class='card'><div class='muted'>Area de piso estimada</div><div class='metric' id='cargoFloorArea'>0 m2</div></div>
            <div class='card'><div class='muted'>Ocupacao veiculo sugerido</div><div class='metric' id='cargoVehicleOccupancy'>0%</div></div>
            <div class='card'><div class='muted'>Veiculos necessarios</div><div class='metric' id='cargoVehicleCount'>0</div></div>
        </div>
        <div class='grid cards' style='margin-top:16px;'>
            <div class='card'><div class='muted'>Metro linear aprox.</div><div class='metric' id='cargoLinearMeters'>0 m</div></div>
            <div class='card'><div class='muted'>Capacidade por viagem</div><div class='metric' id='cargoTripCapacity'>0 m3</div></div>
            <div class='card'><div class='muted'>Peso por viagem</div><div class='metric' id='cargoTripWeight'>0 kg</div></div>
            <div class='card'><div class='muted'>Gargalo da operacao</div><div class='metric' id='cargoBottleneck'>-</div></div>
        </div>
        <div class='grid two' style='margin-top:16px;'>
            <div class='card'>
                <h2>Recomendacao operacional</h2>
                <div id='cargoRecommendation' class='panel'>Informe os itens da carga para calcular.</div>
            </div>
            <div class='card'>
                <h2>Analise avancada</h2>
                <ul class='ai-list' id='cargoInsights'></ul>
            </div>
        </div>
        <div class='card' style='margin-top:16px;'>
            <h2>Opcoes de veiculo e conteiner</h2>
            <div class='table-wrap'>
                <table class='table'>
                    <thead><tr><th>Opcao</th><th>Capacidade</th><th>Dimensoes uteis</th><th>Ocupacao</th><th>Status</th></tr></thead>
                    <tbody id='vehicleSuggestionRows'></tbody>
                </table>
            </div>
        </div>
        <div class='card' style='margin-top:16px;'>
            <div style='display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;'>
                <div>
                    <h2>Comparador de modais</h2>
                    <p class='muted'>Compare rodoviario direto, hub, ferrovia, cabotagem e aereo considerando distancia, carga, prazo, custo, risco e quantidade de veiculos.</p>
                </div>
                <button class='btn btn-light' type='button' id='exportModalComparison'>Exportar comparativo CSV</button>
            </div>
            <div class='panel' style='margin:12px 0;'>
                <div style='display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px;align-items:end;'>
                    <div><label>Origem</label><input id='modalOrigin' value='Base operacional'></div>
                    <div><label>Destino</label><input id='modalDestination' value='Cliente / regiao'></div>
                    <div><label>Distancia estimada km</label><input id='modalDistance' type='number' value='650' min='1' step='1'></div>
                    <div><label>Prazo maximo horas</label><input id='modalDeadline' type='number' value='24' min='1' step='1'></div>
                    <div><label>Hub / transbordo</label><input id='modalHub' value='Hub regional'></div>
                </div>
                <div style='display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;'>
                    <label style='display:flex;gap:6px;align-items:center;margin:0;font-weight:800;'><input style='width:auto;' class='modal-mode' type='checkbox' value='road' checked> Rodoviario</label>
                    <label style='display:flex;gap:6px;align-items:center;margin:0;font-weight:800;'><input style='width:auto;' class='modal-mode' type='checkbox' value='hub' checked> Hub</label>
                    <label style='display:flex;gap:6px;align-items:center;margin:0;font-weight:800;'><input style='width:auto;' class='modal-mode' type='checkbox' value='rail' checked> Ferrovia</label>
                    <label style='display:flex;gap:6px;align-items:center;margin:0;font-weight:800;'><input style='width:auto;' class='modal-mode' type='checkbox' value='sea' checked> Cabotagem</label>
                    <label style='display:flex;gap:6px;align-items:center;margin:0;font-weight:800;'><input style='width:auto;' class='modal-mode' type='checkbox' value='air' checked> Aereo</label>
                </div>
            </div>
            <div class='grid cards' style='margin-top:16px;'>
                <div class='card'><div class='muted'>Modal recomendado</div><div class='metric' id='modalBest'>-</div></div>
                <div class='card'><div class='muted'>Custo estimado</div><div class='metric' id='modalBestCost'>-</div></div>
                <div class='card'><div class='muted'>Prazo estimado</div><div class='metric' id='modalBestTime'>-</div></div>
                <div class='card'><div class='muted'>Risco operacional</div><div class='metric' id='modalBestRisk'>-</div></div>
            </div>
            <div class='table-wrap' style='margin-top:16px;'>
                <table class='table'>
                    <thead><tr><th>Opcao</th><th>Composicao</th><th>Prazo</th><th>Custo</th><th>Veiculos / unidades</th><th>Risco</th><th>Indicacao</th></tr></thead>
                    <tbody id='modalComparisonRows'></tbody>
                </table>
            </div>
            <div class='grid two' style='margin-top:16px;'>
                <div class='card'>
                    <h2>Sugestao da IA operacional</h2>
                    <div id='modalRecommendation' class='panel'>Informe distancia e carga para comparar modais.</div>
                </div>
                <div class='card'>
                    <h2>Como usar comercialmente</h2>
                    <ul class='ai-list'>
                        <li>Mostre ao cliente que o sistema decide por custo, prazo e capacidade, nao apenas por mapa.</li>
                        <li>Use ferrovia/cabotagem para cargas maiores e prazos mais flexiveis.</li>
                        <li>Use aereo apenas quando prazo for critico ou carga tiver alto valor agregado.</li>
                        <li>Use hub quando houver consolidacao, pernoite, transferencia ou reducao de veiculo dedicado.</li>
                    </ul>
                </div>
            </div>
        </div>
        <div class='card' style='margin-top:16px;'>
            <div style='display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;'>
                <div>
                    <h2>Condicoes operacionais de atendimento</h2>
                    <p class='muted'>Calcule se compensa atender um cliente em determinada distancia. Edite combustivel, pedagio, hospedagem, alimentacao, diaria, custos extras e margem de lucro.</p>
                </div>
                <button class='btn btn-light' type='button' id='exportOperationalCost'>Exportar calculo CSV</button>
            </div>
            <div class='panel' style='margin:12px 0;'>
                <div style='display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:10px;align-items:end;'>
                    <div><label>Valor cobrado/ofertado R$</label><input id='opRevenue' type='number' value='0' min='0' step='10'></div>
                    <div><label>Margem desejada %</label><input id='opMargin' type='number' value='22' min='0' max='80' step='1'></div>
                    <div><label>Combustivel R$/L</label><input id='opFuelPrice' type='number' value='6.20' min='0' step='0.01'></div>
                    <div><label>Pedagio total R$</label><input id='opToll' type='number' value='0' min='0' step='1'></div>
                    <div><label>Manutencao R$/km</label><input id='opMaintenanceKm' type='number' value='0.55' min='0' step='0.01'></div>
                    <div><label>Distancia inclui retorno?</label><select id='opRoundTrip'><option value='yes'>Sim, ida e volta</option><option value='no'>Nao, apenas ida</option></select></div>
                    <div><label>Hospedagem diaria R$</label><input id='opLodging' type='number' value='180' min='0' step='1'></div>
                    <div><label>Alimentacao/dia R$</label><input id='opMeals' type='number' value='75' min='0' step='1'></div>
                    <div><label>Diaria motorista R$</label><input id='opDriverDaily' type='number' value='220' min='0' step='1'></div>
                    <div><label>Ajudante/equipe R$</label><input id='opHelper' type='number' value='0' min='0' step='1'></div>
                    <div><label>Seguro/admin R$</label><input id='opAdmin' type='number' value='90' min='0' step='1'></div>
                    <div><label>Outros custos R$</label><input id='opOther' type='number' value='0' min='0' step='1'></div>
                </div>
            </div>
            <div class='grid cards' style='margin-top:16px;'>
                <div class='card'><div class='muted'>Veiculo recomendado</div><div class='metric' id='opVehicle'>-</div></div>
                <div class='card'><div class='muted'>Custo operacional</div><div class='metric' id='opCost'>-</div></div>
                <div class='card'><div class='muted'>Preco minimo</div><div class='metric' id='opMinimumPrice'>-</div></div>
                <div class='card'><div class='muted'>Resultado</div><div class='metric' id='opResult'>-</div></div>
            </div>
            <div class='grid two' style='margin-top:16px;'>
                <div class='card'>
                    <h2>Diagnostico da operacao</h2>
                    <div id='opRecommendation' class='panel'>Informe os custos para calcular a viabilidade.</div>
                </div>
                <div class='card'>
                    <h2>Comparativo por veiculo</h2>
                    <div class='table-wrap'>
                        <table class='table'>
                            <thead><tr><th>Veiculo</th><th>Consumo</th><th>Custo total</th><th>Preco minimo</th><th>Status</th></tr></thead>
                            <tbody id='opVehicleRows'></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <script>
        (function () {
            const vehicles = [
                {name:'Moto bau', type:'rodoviario', volume:0.08, payload:20, l:0.45, w:0.35, h:0.50},
                {name:'Carro utilitario', type:'rodoviario', volume:1.0, payload:300, l:1.40, w:1.00, h:0.70},
                {name:'Fiorino / pequeno utilitario', type:'rodoviario', volume:3.2, payload:650, l:1.80, w:1.30, h:1.35},
                {name:'Van', type:'rodoviario', volume:10, payload:1400, l:3.20, w:1.60, h:1.90},
                {name:'VUC', type:'rodoviario', volume:18, payload:3000, l:4.20, w:2.10, h:2.10},
                {name:'Caminhao 3/4', type:'rodoviario', volume:35, payload:6000, l:6.00, w:2.30, h:2.40},
                {name:'Toco', type:'rodoviario', volume:45, payload:10000, l:7.50, w:2.45, h:2.50},
                {name:'Truck', type:'rodoviario', volume:60, payload:14000, l:9.00, w:2.45, h:2.70},
                {name:'Carreta bau', type:'rodoviario', volume:90, payload:28000, l:13.50, w:2.45, h:2.70},
                {name:'Conteiner 20 dry', type:'conteiner', volume:33.2, payload:28200, l:5.90, w:2.35, h:2.39},
                {name:'Conteiner 40 dry', type:'conteiner', volume:67.7, payload:26700, l:12.03, w:2.35, h:2.39},
                {name:'Conteiner 40 high cube', type:'conteiner', volume:76.3, payload:26500, l:12.03, w:2.35, h:2.69}
            ];
            const table = document.getElementById('cargoTable');
            const tbody = table ? table.querySelector('tbody') : null;
            const money = new Intl.NumberFormat('pt-BR', {maximumFractionDigits: 2});
            function num(value, fallback) {
                const parsed = Number(String(value || '').replace(',', '.'));
                return Number.isFinite(parsed) ? parsed : fallback;
            }
            function fmt(value, suffix, digits) {
                return money.format(Number(value || 0).toFixed(digits || 0)) + ' ' + suffix;
            }
            function brl(value) {
                return new Intl.NumberFormat('pt-BR', {style:'currency', currency:'BRL'}).format(Number(value || 0));
            }
            function cargoRows() {
                if (!tbody) return [];
                return Array.from(tbody.querySelectorAll('tr')).map(row => {
                    const get = name => row.querySelector('[data-field="' + name + '"]');
                    const qty = Math.max(0, num(get('qty').value, 0));
                    const length = Math.max(0, num(get('length').value, 0));
                    const width = Math.max(0, num(get('width').value, 0));
                    const height = Math.max(0, num(get('height').value, 0));
                    const weight = Math.max(0, num(get('weight').value, 0));
                    return {
                        name: get('name').value || 'Item',
                        qty, length, width, height, weight,
                        stackable: get('stackable').checked,
                        volume: qty * length * width * height / 1000000,
                        realWeight: qty * weight,
                        footprint: qty * length * width / 10000,
                        maxLength: length / 100,
                        maxWidth: width / 100,
                        maxHeight: height / 100
                    };
                });
            }
            function checkedModes() {
                return Array.from(document.querySelectorAll('.modal-mode:checked')).map(item => item.value);
            }
            function modalRisk(option, deadline) {
                let score = option.baseRisk;
                if (option.hours > deadline) score += 28;
                if (option.transfers > 1) score += 8;
                if (option.units > 3) score += 6;
                if (option.mode === 'air' && option.chargeWeight > 1200) score += 10;
                if (option.mode === 'sea' && deadline < 72) score += 14;
                if (score >= 70) return {label:'Alto', score, color:'badge-danger'};
                if (score >= 42) return {label:'Medio', score, color:'badge-warning'};
                return {label:'Baixo', score, color:'badge-success'};
            }
            function updateModalComparator(context) {
                const distance = Math.max(1, num(document.getElementById('modalDistance').value, 650));
                const deadline = Math.max(1, num(document.getElementById('modalDeadline').value, 24));
                const modes = checkedModes();
                const chargeWeight = Math.max(context.chargeWeight || 0, 1);
                const volume = Math.max(context.volume || 0, .01);
                const units = Math.max(context.vehicleCount || 1, 1);
                const bestVehicle = context.best ? context.best.name : 'Veiculo dedicado';
                const containerUnits = Math.max(1, Math.ceil(volume / 67.7), Math.ceil(chargeWeight / 26700));
                const railUnits = Math.max(1, Math.ceil(volume / 60), Math.ceil(chargeWeight / 26000));
                const roadKmCost = context.best && context.best.payload >= 10000 ? 5.2 : context.best && context.best.payload >= 3000 ? 4.2 : 3.4;
                const baseHandling = 180 + (chargeWeight * .04) + (volume * 8);
                const options = [
                    {
                        mode:'road',
                        name:'Rodoviario direto',
                        composition: bestVehicle + ' direto origem-destino',
                        hours: (distance / 62) + 1.5,
                        cost: (distance * roadKmCost * units) + baseHandling,
                        units,
                        baseRisk: 34,
                        transfers: 0,
                        chargeWeight
                    },
                    {
                        mode:'hub',
                        name:'Rodoviario com hub',
                        composition: 'Coleta + consolidacao em ' + (document.getElementById('modalHub').value || 'hub') + ' + distribuicao',
                        hours: (distance / 58) + 4,
                        cost: (distance * roadKmCost * units * .88) + baseHandling + 450,
                        units,
                        baseRisk: 42,
                        transfers: 1,
                        chargeWeight
                    },
                    {
                        mode:'rail',
                        name:'Ferrovia + rodoviario',
                        composition: 'Primeira milha rodoviaria + terminal ferroviario + ultima milha',
                        hours: ((distance * .22) / 55) + ((distance * .78) / 42) + 8,
                        cost: (distance * .22 * roadKmCost) + (distance * .78 * 1.45 * railUnits) + baseHandling + 900,
                        units: railUnits,
                        baseRisk: 48,
                        transfers: 2,
                        chargeWeight
                    },
                    {
                        mode:'sea',
                        name:'Cabotagem + rodoviario',
                        composition: 'Rodoviario ate porto + cabotagem + rodoviario final',
                        hours: ((distance * .25) / 55) + ((distance * .75) / 32) + 24,
                        cost: (distance * .25 * roadKmCost) + (distance * .75 * .95 * containerUnits) + baseHandling + 1600,
                        units: containerUnits,
                        baseRisk: 54,
                        transfers: 2,
                        chargeWeight
                    },
                    {
                        mode:'air',
                        name:'Aereo + rodoviario',
                        composition: 'Coleta rodoviaria + terminal aereo + voo + entrega final',
                        hours: ((distance * .12) / 55) + ((distance * .88) / 620) + 6,
                        cost: (chargeWeight * 5.6) + (distance * .12 * roadKmCost) + baseHandling + 1200,
                        units: Math.max(1, Math.ceil(chargeWeight / 1200)),
                        baseRisk: 38,
                        transfers: 2,
                        chargeWeight
                    }
                ].filter(option => modes.includes(option.mode));
                const tbodyModal = document.getElementById('modalComparisonRows');
                if (!options.length) {
                    tbodyModal.innerHTML = "<tr><td colspan='7'>Selecione pelo menos um modal para comparar.</td></tr>";
                    return;
                }
                const minCost = Math.min(...options.map(item => item.cost));
                const minTime = Math.min(...options.map(item => item.hours));
                const evaluated = options.map(option => {
                    const risk = modalRisk(option, deadline);
                    const deadlinePenalty = option.hours > deadline ? 1.35 : 1;
                    const score = (option.cost * .48) + (option.hours * 85 * deadlinePenalty) + (risk.score * 130);
                    let indication = 'Equilibrado';
                    if (option.hours <= deadline && option.cost === minCost) indication = 'Menor custo';
                    if (option.hours === minTime) indication = 'Menor prazo';
                    if (option.hours > deadline) indication = 'Fora do prazo';
                    return {...option, risk, score, indication};
                }).sort((a, b) => a.score - b.score);
                const bestModal = evaluated[0];
                document.getElementById('modalBest').textContent = bestModal.name;
                document.getElementById('modalBestCost').textContent = brl(bestModal.cost);
                document.getElementById('modalBestTime').textContent = fmt(bestModal.hours, 'h', 1);
                document.getElementById('modalBestRisk').textContent = bestModal.risk.label;
                tbodyModal.innerHTML = evaluated.map(option => {
                    return '<tr><td><strong>' + option.name + '</strong></td><td>' + option.composition + '</td><td>' + fmt(option.hours, 'h', 1) + '</td><td>' + brl(option.cost) + '</td><td>' + option.units + '</td><td><span class="badge ' + option.risk.color + '">' + option.risk.label + '</span></td><td>' + option.indication + '</td></tr>';
                }).join('');
                const overDeadline = bestModal.hours > deadline;
                const road = evaluated.find(item => item.mode === 'road');
                const savingVsRoad = road ? road.cost - bestModal.cost : 0;
                let message = '<strong>Opcao recomendada: ' + bestModal.name + '.</strong><br>';
                message += 'Prazo previsto de ' + fmt(bestModal.hours, 'h', 1) + ' para limite informado de ' + fmt(deadline, 'h', 0) + '. ';
                message += overDeadline ? 'Como passa do prazo, renegocie janela ou escolha modal mais rapido.<br>' : 'Atende o prazo informado.<br>';
                message += 'Custo estimado: ' + brl(bestModal.cost) + '. Unidades/veiculos: ' + bestModal.units + '.<br>';
                if (savingVsRoad > 0 && bestModal.mode !== 'road') {
                    message += 'Economia estimada contra rodoviario direto: ' + brl(savingVsRoad) + '.<br>';
                }
                message += 'Carga considerada: ' + volume.toFixed(3) + ' m3 e peso tarifavel aproximado de ' + chargeWeight.toFixed(1) + ' kg.';
                document.getElementById('modalRecommendation').innerHTML = message;
                window.__risModalComparison = evaluated.map(option => ({
                    opcao: option.name,
                    composicao: option.composition,
                    prazo_horas: option.hours.toFixed(2),
                    custo: option.cost.toFixed(2),
                    unidades: option.units,
                    risco: option.risk.label,
                    indicacao: option.indication
                }));
            }
            function operationalVehicleOptions(context) {
                const bestName = context.best ? context.best.name : '';
                return [
                    {name:'Moto bau', consumption:28, speed:70, capacity:0.08, payload:20, fixed:35},
                    {name:'Carro utilitario', consumption:12, speed:80, capacity:1.0, payload:300, fixed:80},
                    {name:'Fiorino / pequeno utilitario', consumption:10, speed:75, capacity:3.2, payload:650, fixed:120},
                    {name:'Van', consumption:8, speed:80, capacity:10, payload:1400, fixed:180},
                    {name:'VUC', consumption:6, speed:65, capacity:18, payload:3000, fixed:260},
                    {name:'Caminhao 3/4', consumption:5, speed:60, capacity:35, payload:6000, fixed:340},
                    {name:'Toco', consumption:4, speed:58, capacity:45, payload:10000, fixed:460},
                    {name:'Truck', consumption:3.2, speed:55, capacity:60, payload:14000, fixed:620},
                    {name:'Carreta bau', consumption:2.4, speed:55, capacity:90, payload:28000, fixed:850}
                ].map(item => ({...item, preferred: bestName && item.name === bestName}));
            }
            function updateOperationalCalculator(context) {
                const oneWayDistance = Math.max(1, num(document.getElementById('modalDistance').value, 650));
                const charged = Math.max(0, num(document.getElementById('opRevenue').value, 0));
                const margin = Math.max(0, Math.min(80, num(document.getElementById('opMargin').value, 22))) / 100;
                const fuelPrice = Math.max(0, num(document.getElementById('opFuelPrice').value, 6.2));
                const toll = Math.max(0, num(document.getElementById('opToll').value, 0));
                const maintenanceKm = Math.max(0, num(document.getElementById('opMaintenanceKm').value, .55));
                const lodging = Math.max(0, num(document.getElementById('opLodging').value, 180));
                const meals = Math.max(0, num(document.getElementById('opMeals').value, 75));
                const driverDaily = Math.max(0, num(document.getElementById('opDriverDaily').value, 220));
                const helper = Math.max(0, num(document.getElementById('opHelper').value, 0));
                const admin = Math.max(0, num(document.getElementById('opAdmin').value, 90));
                const other = Math.max(0, num(document.getElementById('opOther').value, 0));
                const totalKm = document.getElementById('opRoundTrip').value === 'yes' ? oneWayDistance * 2 : oneWayDistance;
                const volume = Math.max(context.volume || 0, .01);
                const chargeWeight = Math.max(context.chargeWeight || 0, 1);
                const vehiclesOps = operationalVehicleOptions(context).map(vehicle => {
                    const capacityOk = volume <= vehicle.capacity * .85 && chargeWeight <= vehicle.payload;
                    const travelHours = totalKm / vehicle.speed;
                    const days = Math.max(1, Math.ceil(travelHours / 10));
                    const fuel = (totalKm / vehicle.consumption) * fuelPrice;
                    const stay = Math.max(0, days - 1) * lodging;
                    const food = days * meals;
                    const driver = days * driverDaily;
                    const cost = fuel + toll + (totalKm * maintenanceKm) + stay + food + driver + helper + admin + other + vehicle.fixed;
                    const minimumPrice = margin >= .8 ? cost : cost / (1 - margin);
                    const result = charged ? charged - cost : minimumPrice - cost;
                    const status = !capacityOk ? 'Carga nao cabe' : charged && charged < minimumPrice ? 'Abaixo da margem' : travelHours > 10 ? 'Exige jornada longa' : 'Viavel';
                    const score = (!capacityOk ? 999999 : 0) + minimumPrice + (travelHours * 35) + (vehicle.preferred ? -150 : 0);
                    return {...vehicle, capacityOk, travelHours, days, fuel, cost, minimumPrice, result, status, score};
                }).sort((a, b) => a.score - b.score);
                const bestOp = vehiclesOps[0];
                if (!bestOp) return;
                document.getElementById('opVehicle').textContent = bestOp.name;
                document.getElementById('opCost').textContent = brl(bestOp.cost);
                document.getElementById('opMinimumPrice').textContent = brl(bestOp.minimumPrice);
                document.getElementById('opResult').textContent = charged ? brl(charged - bestOp.cost) : brl(bestOp.result);
                const viable = bestOp.capacityOk && (!charged || charged >= bestOp.minimumPrice);
                let message = '<strong>' + (viable ? 'Atendimento viavel.' : 'Atendimento precisa revisao.') + '</strong><br>';
                message += 'Veiculo sugerido: ' + bestOp.name + '. Distancia considerada: ' + totalKm.toFixed(0) + ' km. Tempo estimado: ' + fmt(bestOp.travelHours, 'h', 1) + '.<br>';
                message += 'Custo direto estimado: ' + brl(bestOp.cost) + '. Para margem de ' + Math.round(margin * 100) + '%, cobrar no minimo ' + brl(bestOp.minimumPrice) + '.<br>';
                if (charged) {
                    message += charged >= bestOp.minimumPrice ? 'Valor informado cobre a margem desejada.' : 'Valor informado nao cobre a margem desejada; negociar preco, reduzir custo ou alterar modal.';
                } else {
                    message += 'Informe o valor cobrado/ofertado para comparar lucro real contra a margem desejada.';
                }
                if (bestOp.travelHours > 10) {
                    message += '<br>Como passa de 10h de jornada, programar continuidade, descanso interjornada ou dividir a operacao.';
                }
                document.getElementById('opRecommendation').innerHTML = message;
                document.getElementById('opVehicleRows').innerHTML = vehiclesOps.map(vehicle => {
                    const badge = vehicle.status === 'Viavel' ? 'badge-success' : vehicle.status === 'Abaixo da margem' || vehicle.status === 'Exige jornada longa' ? 'badge-warning' : 'badge-danger';
                    return '<tr><td><strong>' + vehicle.name + '</strong></td><td>' + vehicle.consumption + ' km/L</td><td>' + brl(vehicle.cost) + '</td><td>' + brl(vehicle.minimumPrice) + '</td><td><span class="badge ' + badge + '">' + vehicle.status + '</span></td></tr>';
                }).join('');
                window.__risOperationalCost = vehiclesOps.map(vehicle => ({
                    veiculo: vehicle.name,
                    consumo_km_l: vehicle.consumption,
                    distancia_km: totalKm.toFixed(0),
                    tempo_horas: vehicle.travelHours.toFixed(2),
                    dias: vehicle.days,
                    custo_total: vehicle.cost.toFixed(2),
                    preco_minimo: vehicle.minimumPrice.toFixed(2),
                    status: vehicle.status
                }));
            }
            function calculate() {
                const rows = cargoRows();
                const factor = Math.max(1, num(document.getElementById('cubageFactor').value, 300));
                const stockHeight = Math.max(.1, num(document.getElementById('stockHeight').value, 2.5));
                const vehicleUse = Math.max(40, Math.min(100, num(document.getElementById('vehicleUse').value, 85))) / 100;
                const reserve = Math.max(0, num(document.getElementById('stockReserve').value, 15)) / 100;
                const profile = document.getElementById('cargoProfile').value;
                const volume = rows.reduce((sum, item) => sum + item.volume, 0);
                const realWeight = rows.reduce((sum, item) => sum + item.realWeight, 0);
                const cubedWeight = volume * factor;
                const chargeWeight = Math.max(realWeight, cubedWeight);
                const maxLength = Math.max(0, ...rows.map(item => item.maxLength));
                const maxWidth = Math.max(0, ...rows.map(item => item.maxWidth));
                const maxHeight = Math.max(0, ...rows.map(item => item.maxHeight));
                const nonStackFootprint = rows.filter(item => !item.stackable).reduce((sum, item) => sum + item.footprint, 0);
                const stockVolume = volume * (1 + reserve) / 0.75;
                const stockArea = Math.max(stockVolume / stockHeight, nonStackFootprint * (1 + reserve));
                const stackableVolume = rows.filter(item => item.stackable).reduce((sum, item) => sum + item.volume, 0);
                const stackableFloor = stockHeight > 0 ? stackableVolume / stockHeight : 0;
                const floorArea = Math.max(nonStackFootprint, stackableFloor);
                document.getElementById('cargoM3').textContent = fmt(volume, 'm3', 3);
                document.getElementById('cargoLiters').textContent = fmt(volume * 1000, 'L', 0);
                document.getElementById('cargoWeight').textContent = fmt(realWeight, 'kg', 1);
                document.getElementById('cargoCubedWeight').textContent = fmt(cubedWeight, 'kg', 1);
                document.getElementById('cargoTransportM3').textContent = fmt(volume, 'm3', 3);
                document.getElementById('cargoFloorArea').textContent = fmt(floorArea, 'm2', 2);

                const viable = vehicles.map(vehicle => {
                    const dimensionOk = maxLength <= vehicle.l && maxWidth <= vehicle.w && maxHeight <= vehicle.h;
                    const volumeUse = vehicle.volume ? volume / (vehicle.volume * vehicleUse) : 99;
                    const weightUse = vehicle.payload ? chargeWeight / vehicle.payload : 99;
                    const ok = dimensionOk && volumeUse <= 1 && weightUse <= 1;
                    return {...vehicle, dimensionOk, volumeUse, weightUse, ok};
                });
                const preferredList = viable.filter(item => profile === 'conteiner' ? item.type === 'conteiner' : item.type === 'rodoviario');
                const best = preferredList.find(item => item.ok) || viable.find(item => item.ok);
                const bestOccupancy = best ? Math.max(best.volumeUse, best.weightUse) * 100 : 0;
                const linearMeters = best ? Math.min(best.l, floorArea / Math.max(best.w, .1)) : 0;
                const volumeTrips = best && best.volume ? Math.ceil(volume / Math.max(best.volume * vehicleUse, .01)) : 0;
                const weightTrips = best && best.payload ? Math.ceil(chargeWeight / Math.max(best.payload, .01)) : 0;
                const vehicleCount = best ? Math.max(1, volumeTrips, weightTrips) : 0;
                const bottleneck = !best ? '-' : volumeTrips >= weightTrips ? 'Volume' : 'Peso';
                document.getElementById('cargoVehicleOccupancy').textContent = fmt(bestOccupancy, '%', 1);
                document.getElementById('cargoLinearMeters').textContent = fmt(linearMeters, 'm', 2);
                document.getElementById('cargoVehicleCount').textContent = vehicleCount ? String(vehicleCount) : '0';
                document.getElementById('cargoTripCapacity').textContent = best ? fmt(best.volume * vehicleUse, 'm3', 2) : '0 m3';
                document.getElementById('cargoTripWeight').textContent = best ? fmt(best.payload, 'kg', 0) : '0 kg';
                document.getElementById('cargoBottleneck').textContent = bottleneck;
                const rowsHtml = viable.map(item => {
                    const occupancy = Math.max(item.volumeUse, item.weightUse) * 100;
                    const status = item.ok ? 'Viavel' : (!item.dimensionOk ? 'Dimensao excede' : item.volumeUse > 1 ? 'Volume excede' : 'Peso excede');
                    const badgeClass = item.ok ? 'badge-success' : 'badge-danger';
                    return '<tr><td><strong>' + item.name + '</strong></td><td>' + item.volume + ' m3 | ' + item.payload + ' kg</td><td>' + item.l + ' x ' + item.w + ' x ' + item.h + ' m</td><td>' + fmt(occupancy, '%', 1) + '</td><td><span class="badge ' + badgeClass + '">' + status + '</span></td></tr>';
                }).join('');
                document.getElementById('vehicleSuggestionRows').innerHTML = rowsHtml;
                const density = volume > 0 ? realWeight / volume : 0;
                document.getElementById('cargoRecommendation').innerHTML = best
                    ? '<strong>Veiculo recomendado: ' + best.name + '.</strong><br>Numero estimado para a operacao: <strong>' + vehicleCount + ' veiculo(s)</strong> deste tipo.<br>Espaco que a carga ocupa no transporte: ' + volume.toFixed(3) + ' m3, cerca de ' + floorArea.toFixed(2) + ' m2 de piso e ' + linearMeters.toFixed(2) + ' m lineares na carroceria sugerida.<br>Capacidade considerada com uso maximo de ' + Math.round(vehicleUse * 100) + '%.<br>Gargalo principal: ' + bottleneck + '.<br>Carroceria util minima por dimensao: ' + maxLength.toFixed(2) + ' x ' + maxWidth.toFixed(2) + ' x ' + maxHeight.toFixed(2) + ' m.<br>Espaco estimado para estoque: ' + stockVolume.toFixed(2) + ' m3 ou cerca de ' + stockArea.toFixed(2) + ' m2 com altura util de ' + stockHeight.toFixed(2) + ' m.'
                    : '<strong>Nenhuma opcao padrao atende com folga.</strong><br>Avalie fracionar a carga, usar veiculo dedicado maior ou revisar dimensoes/empilhamento.';
                const insights = [];
                insights.push(density < factor ? 'Carga volumosa: peso cubado maior que peso real, frete tende a ser cobrado por cubagem.' : 'Carga densa: peso real e o principal limitador do transporte.');
                if (nonStackFootprint > 0) insights.push('Existem itens nao empilhaveis: considerar area de piso da carroceria/estoque, nao apenas m3.');
                if (best && Math.max(best.volumeUse, best.weightUse) > .85) insights.push('O veiculo sugerido esta proximo do limite: manter reserva para embalagem, amarracao e avarias.');
                if (volume > 28) insights.push('Volume compativel com estudo de conteiner ou carreta dependendo da distancia e do perfil fiscal/operacional.');
                insights.push('Para empresas de ponta: proximo passo e salvar esta simulacao por frete e comparar custo por ocupacao, prazo e risco.');
                document.getElementById('cargoInsights').innerHTML = insights.map(text => '<li>' + text + '</li>').join('');
                updateModalComparator({volume, realWeight, cubedWeight, chargeWeight, best, vehicleCount});
                updateOperationalCalculator({volume, realWeight, cubedWeight, chargeWeight, best, vehicleCount});
            }
            function addRow() {
                const index = tbody.querySelectorAll('tr').length + 1;
                const tr = document.createElement('tr');
                tr.innerHTML = "<td><input value='Caixa " + index + "' data-field='name'></td><td><input type='number' value='1' min='1' step='1' data-field='qty'></td><td style='min-width:260px;'><input type='number' value='40' min='1' step='0.1' data-field='length' style='width:31%;'> <input type='number' value='30' min='1' step='0.1' data-field='width' style='width:31%;'> <input type='number' value='25' min='1' step='0.1' data-field='height' style='width:31%;'></td><td><input type='number' value='5' min='0' step='0.1' data-field='weight'></td><td><input type='checkbox' data-field='stackable' checked></td><td><button class='btn btn-light cargo-remove' type='button'>Remover</button></td>";
                tbody.appendChild(tr);
                calculate();
            }
            if (tbody) {
                document.getElementById('addCargoRow').addEventListener('click', addRow);
                table.addEventListener('input', calculate);
                table.addEventListener('change', calculate);
                table.addEventListener('click', function (event) {
                    if (event.target.classList.contains('cargo-remove') && tbody.querySelectorAll('tr').length > 1) {
                        event.target.closest('tr').remove();
                        calculate();
                    }
                });
                ['cubageFactor', 'stockHeight', 'vehicleUse', 'stockReserve', 'cargoProfile'].forEach(id => {
                    document.getElementById(id).addEventListener('input', calculate);
                    document.getElementById(id).addEventListener('change', calculate);
                });
                ['modalDistance', 'modalDeadline', 'modalOrigin', 'modalDestination', 'modalHub'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) {
                        el.addEventListener('input', calculate);
                        el.addEventListener('change', calculate);
                    }
                });
                ['opRevenue', 'opMargin', 'opFuelPrice', 'opToll', 'opMaintenanceKm', 'opRoundTrip', 'opLodging', 'opMeals', 'opDriverDaily', 'opHelper', 'opAdmin', 'opOther'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) {
                        el.addEventListener('input', calculate);
                        el.addEventListener('change', calculate);
                    }
                });
                document.querySelectorAll('.modal-mode').forEach(el => el.addEventListener('change', calculate));
                document.getElementById('exportModalComparison').addEventListener('click', function () {
                    const rows = window.__risModalComparison || [];
                    if (!rows.length) return;
                    const headers = Object.keys(rows[0]);
                    const csv = [headers.join(';')].concat(rows.map(row => headers.map(key => '"' + String(row[key]).replace(/"/g, '""') + '"').join(';'))).join('\\n');
                    const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
                    const link = document.createElement('a');
                    link.href = URL.createObjectURL(blob);
                    link.download = 'comparador_modais_ris.csv';
                    link.click();
                    setTimeout(() => URL.revokeObjectURL(link.href), 500);
                });
                document.getElementById('exportOperationalCost').addEventListener('click', function () {
                    const rows = window.__risOperationalCost || [];
                    if (!rows.length) return;
                    const headers = Object.keys(rows[0]);
                    const csv = [headers.join(';')].concat(rows.map(row => headers.map(key => '"' + String(row[key]).replace(/"/g, '""') + '"').join(';'))).join('\\n');
                    const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
                    const link = document.createElement('a');
                    link.href = URL.createObjectURL(blob);
                    link.download = 'viabilidade_operacional_ris.csv';
                    link.click();
                    setTimeout(() => URL.revokeObjectURL(link.href), 500);
                });
                calculate();
            }
        })();
    </script>
    """


def normalize_header(value):
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    for char in (" ", "-", ".", "/", "\\"):
        text = text.replace(char, "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def first_value(row, *names):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return ""


def parse_float_value(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except ValueError:
        return None


def parse_time_value(value):
    if value is None or str(value).strip() == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    text = str(value).strip()
    if ":" in text:
        parts = text.split(":")
        try:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        except (ValueError, IndexError):
            return ""
    return text[:5] if len(text) >= 4 else text


def priority_rank(value):
    priority = str(value or "media").strip().lower()
    return 0 if priority == "alta" else 1 if priority == "media" else 2


def imported_client_score(client):
    return (
        priority_rank(client["priority"]),
        client["window_start"] or "99:99",
        client["name"],
    )


def plan_imported_routes(clients, vehicle_type):
    speed_by_type = {"moto": 70, "carro": 80, "van": 80, "caminhao": 60}
    speed = speed_by_type.get(vehicle_type, 60)
    remaining = sorted(clients, key=imported_client_score)
    routes = []
    route_number = 1

    while remaining:
        current_minutes = 0
        last_point = [BASE_LATITUDE, BASE_LONGITUDE]
        break_inserted = False
        route_items = []

        while remaining:
            scored = []
            for client in remaining:
                point = [client["latitude"], client["longitude"]]
                distance = distancia_reta_km([last_point, point])
                travel = (distance / speed) * 60 if speed else 0
                arrival = current_minutes + travel
                departure = arrival + DEFAULT_SERVICE_MINUTES
                simulated_end = departure
                would_insert_break = False
                if (
                    not break_inserted
                    and departure >= INTRADAY_BREAK_WINDOW_START
                    and simulated_end > INTRADAY_BREAK_REQUIRED_AFTER
                    and route_items
                ):
                    simulated_end += INTRADAY_BREAK_MINUTES
                    would_insert_break = True
                window_end = time_text_to_minutes(client["window_end"]) or (24 * 60)
                lateness = max(0, arrival - window_end)
                over_limit = max(0, simulated_end - WORKDAY_LIMIT_MINUTES)
                score = (distance * 8) + (priority_rank(client["priority"]) * 35) + (window_end * 0.025) + (lateness * 12) + (over_limit * 30)
                scored.append((score, client, distance, travel, arrival, departure, simulated_end, would_insert_break))
            scored.sort(key=lambda item: (item[0], imported_client_score(item[1])))
            fitting = [item for item in scored if item[6] <= WORKDAY_LIMIT_MINUTES]
            if fitting:
                chosen = fitting[0]
            elif not route_items:
                chosen = scored[0]
            else:
                break

            _, client, distance, travel, arrival, departure, simulated_end, would_insert_break = chosen
            break_after = None
            current_minutes = departure
            if would_insert_break:
                break_after = {
                    "start": minutes_to_hhmm(current_minutes),
                    "end": minutes_to_hhmm(current_minutes + INTRADAY_BREAK_MINUTES),
                    "duration": INTRADAY_BREAK_MINUTES,
                }
                current_minutes += INTRADAY_BREAK_MINUTES
                break_inserted = True
            route_items.append({
                "client": client,
                "sequence": len(route_items) + 1,
                "arrival": minutes_to_hhmm(arrival),
                "departure": minutes_to_hhmm(departure),
                "distance": distance,
                "break_after": break_after,
            })
            last_point = [client["latitude"], client["longitude"]]
            remaining.remove(client)

        if route_items:
            routes.append({
                "number": route_number,
                "items": route_items,
                "work_minutes": current_minutes,
                "status": "ACIMA DE 10H" if current_minutes > WORKDAY_LIMIT_MINUTES else "PROXIMO DE 10H" if current_minutes >= WORKDAY_LIMIT_MINUTES - 45 else "DENTRO DE 10H",
                "has_break": any(item["break_after"] for item in route_items),
            })
            route_number += 1
        else:
            break
    return routes


def build_imported_routes_workbook(routes, vehicle_type, source_name, official_route_ids=None, execution_date=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    official_route_ids = official_route_ids or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "Rotas planejadas"
    headers = [
        "arquivo_origem", "tipo", "data_execucao", "rota_sugerida", "rota_oficial_id", "sequencia", "cliente", "endereco", "prioridade",
        "janela_inicio", "janela_fim", "latitude", "longitude", "veiculo", "chegada_prevista",
        "saida_prevista", "distancia_trecho_km", "intervalo_apos", "jornada_rota", "status_rota", "observacao",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F5EFF")

    for route in routes:
        for item in route["items"]:
            client = item["client"]
            interval = ""
            if item["break_after"]:
                interval = f"{item['break_after']['start']} - {item['break_after']['end']} ({item['break_after']['duration']} min)"
            observation = "Revisar/dividir rota" if route["work_minutes"] > WORKDAY_LIMIT_MINUTES else ""
            ws.append([
                source_name,
                "OFICIAL" if route["number"] in official_route_ids else "SIMULADO",
                display_date(execution_date) if execution_date else "",
                f"ROTA {route['number']}",
                official_route_ids.get(route["number"], ""),
                item["sequence"],
                client["name"],
                client["address"],
                client["priority"],
                client["window_start"],
                client["window_end"],
                round(client["latitude"], 6),
                round(client["longitude"], 6),
                vehicle_type,
                item["arrival"],
                item["departure"],
                round(item["distance"], 2),
                interval,
                tempo_formatado(route["work_minutes"]),
                route["status"],
                observation,
            ])

    summary = wb.create_sheet("Resumo")
    summary.append(["Indicador", "Valor"])
    summary.append(["Arquivo origem", source_name])
    summary.append(["Veiculo", vehicle_type])
    summary.append(["Tipo", "OFICIAL" if official_route_ids else "SIMULADO"])
    summary.append(["Data de execucao", display_date(execution_date) if execution_date else "-"])
    summary.append(["Rotas necessarias", len(routes)])
    summary.append(["Clientes planejados", sum(len(route["items"]) for route in routes)])
    summary.append(["Limite operacional", "10h por rota"])
    summary.append(["Intervalo intrajornada", "1h quando a jornada exceder 6h, entre a 4a e a 6a hora"])
    summary.append([])
    summary.append(["Rota", "Pontos", "Jornada", "Status", "Intervalo"])
    for route in routes:
        summary.append([
            f"ROTA {route['number']}",
            len(route["items"]),
            tempo_formatado(route["work_minutes"]),
            route["status"],
            "Sim" if route["has_break"] else "Nao",
        ])
    for sheet in wb.worksheets:
        for column_cells in sheet.columns:
            max_length = max(len(str(cell.value or "")) for cell in column_cells)
            sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max_length + 3, 42)
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def build_route_import_template_workbook():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Modelo importacao"
    headers = ["cliente", "endereco", "latitude", "longitude", "prioridade", "janela_inicio", "janela_fim", "observacao"]
    ws.append(headers)
    sample_rows = [
        ["Cliente Exemplo A", "Rua Exemplo, 100", -29.684200, -53.806900, "alta", "08:00", "10:00", "Trocar pelos dados reais do cliente"],
        ["Cliente Exemplo B", "Av. Exemplo, 500", -29.690000, -53.810000, "media", "10:00", "12:00", ""],
        ["Cliente Exemplo C", "Estrada Exemplo, km 3", -29.700000, -53.815000, "baixa", "13:00", "15:00", ""],
    ]
    for row in sample_rows:
        ws.append(row)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F5EFF")
    ws.freeze_panes = "A2"
    notes = wb.create_sheet("Como usar")
    notes.append(["Campo", "Orientacao"])
    notes.append(["cliente", "Nome do cliente ou ponto de atendimento."])
    notes.append(["endereco", "Endereco completo ou referencia operacional."])
    notes.append(["latitude/longitude", "Usar 6 casas decimais. Exemplo: -29.684200 e -53.806900."])
    notes.append(["prioridade", "alta, media ou baixa."])
    notes.append(["janela_inicio/janela_fim", "Horario no formato HH:MM."])
    notes.append(["Fluxo", "Importe primeiro como SIMULADO; quando aprovado, marque para gravar como OFICIAL na data de execucao."])
    for sheet in wb.worksheets:
        for column_cells in sheet.columns:
            max_length = max(len(str(cell.value or "")) for cell in column_cells)
            sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max_length + 3, 48)
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def build_simulation_export_workbook(result, company_name, vehicle_type):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Simulacao de rotas"
    headers = [
        "empresa", "tipo", "rota", "cor_rota", "sequencia", "cliente", "endereco", "prioridade",
        "janela_inicio", "janela_fim", "latitude", "longitude", "veiculo", "distancia_rota_km",
        "tempo_rota", "custo_rota", "status_rota", "continuidade", "dia_sugerido",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F5EFF")

    for route in result["routes"]:
        status = "ACIMA DE 10H" if route["minutes"] > WORKDAY_LIMIT_MINUTES else "DENTRO DE 10H"
        color = route_color(route["number"])
        client_day = {}
        for day in route.get("continuity_days", []):
            for day_client in day.get("clients", []):
                client_day[day_client.id] = day["day"]
        for index, client in enumerate(route["clients"], start=1):
            ws.append([
                company_name,
                "AJUSTE MANUAL" if result.get("manual_adjusted") else "SIMULADO",
                f"ROTA {route['number']}",
                color,
                index,
                client.name,
                client.address or "",
                client.priority,
                client.delivery_window_start or "",
                client.delivery_window_end or "",
                round(float(client.latitude), 6),
                round(float(client.longitude), 6),
                vehicle_type,
                round(route["distance"], 2),
                tempo_formatado(route["minutes"]),
                round(route["cost"], 2),
                status,
                "Sim" if route.get("long_route") else "Nao",
                f"Dia {client_day.get(client.id)}" if client_day.get(client.id) else "",
            ])

    summary = wb.create_sheet("Resumo")
    summary.append(["Indicador", "Valor"])
    summary.append(["Empresa", company_name])
    summary.append(["Veiculo", vehicle_type])
    summary.append(["Rotas necessarias", len(result["routes"])])
    summary.append(["Distancia total km", round(result["distance"], 2)])
    summary.append(["Tempo total", tempo_formatado(result["minutes"])])
    summary.append(["Maior jornada por rota", tempo_formatado(result["max_route_minutes"])])
    summary.append(["Rotas longas", result.get("long_route_count", 0)])
    summary.append(["Dias de continuidade", result.get("continuity_day_count", 0)])
    summary.append(["Custo total", round(result["cost"], 2)])
    summary.append(["Tipo", "AJUSTE MANUAL" if result.get("manual_adjusted") else "SIMULADO"])
    summary.append([])
    summary.append(["Rota", "Pontos", "Distancia km", "Tempo", "Custo", "Status", "Continuidade"])
    for route in result["routes"]:
        summary.append([
            f"ROTA {route['number']}",
            len(route["clients"]),
            round(route["distance"], 2),
            tempo_formatado(route["minutes"]),
            round(route["cost"], 2),
            "ACIMA DE 10H" if route["minutes"] > WORKDAY_LIMIT_MINUTES else "DENTRO DE 10H",
            route.get("continuity_note", ""),
        ])

    continuity = wb.create_sheet("Continuidade")
    continuity.append(["Rota", "Dia", "Atendimentos", "Tempo previsto", "Distancia km", "Intrajornada", "Interjornada minima", "Orientacao"])
    for cell in continuity[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="F97316")
    for route in result["routes"]:
        for day in route.get("continuity_days", []):
            continuity.append([
                f"ROTA {route['number']}",
                f"Dia {day['day']}",
                ", ".join(client.name for client in day.get("clients", [])) or "Deslocamento / retorno sem atendimento",
                tempo_formatado(day["minutes"]),
                round(day["distance"], 2),
                f"Sim, {day['intraday_break_minutes']} min {day['intraday_window']}" if day.get("needs_intraday_break") else "Nao",
                day.get("interjourney_rest_text") or "",
                day["reason"],
            ])

    for sheet in wb.worksheets:
        for column_cells in sheet.columns:
            max_length = max(len(str(cell.value or "")) for cell in column_cells)
            sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max_length + 3, 46)
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def default_vehicle_speed(vehicle_type):
    return {"caminhao": 60, "van": 80, "carro": 80, "moto": 70}.get(vehicle_type, 80)


def find_or_create_import_vehicle(company_id, vehicle_type):
    from database.database import db

    vehicle = Vehicle.query.filter_by(company_id=company_id, vehicle_type=vehicle_type, active=True).order_by(Vehicle.id).first()
    if vehicle:
        return vehicle
    vehicle = Vehicle(
        company_id=company_id,
        name=f"{vehicle_type.title()} importacao",
        vehicle_type=vehicle_type,
        average_speed_kmh=default_vehicle_speed(vehicle_type),
        active=True,
    )
    db.session.add(vehicle)
    db.session.flush()
    return vehicle


def find_or_create_import_client(company_id, client_data, base_id=None):
    from database.database import db

    client = Client.query.filter_by(company_id=company_id, name=client_data["name"], base_id=base_id).first()
    if not client:
        client = Client.query.filter_by(company_id=company_id, name=client_data["name"]).first()
    if not client:
        client = Client(
            company_id=company_id,
            base_id=base_id,
            name=client_data["name"],
            latitude=round(float(client_data["latitude"]), 6),
            longitude=round(float(client_data["longitude"]), 6),
            active=True,
        )
        db.session.add(client)
    elif base_id and not client.base_id:
        client.base_id = base_id
    client.address = client_data.get("address") or client.address
    client.latitude = round(float(client_data["latitude"]), 6)
    client.longitude = round(float(client_data["longitude"]), 6)
    client.priority = client_data.get("priority") or "media"
    client.delivery_window_start = client_data.get("window_start")
    client.delivery_window_end = client_data.get("window_end")
    client.active = True
    db.session.flush()
    return client


def create_official_routes_from_plan(company_id, planned_routes, vehicle_type, execution_date, source_name, user, base_id=None):
    from database.database import db

    vehicle = find_or_create_import_vehicle(company_id, vehicle_type)
    official_ids = {}
    for planned in planned_routes:
        route = LogisticRoute(
            company_id=company_id,
            base_id=base_id,
            vehicle_id=vehicle.id,
            name=f"ROTA {planned['number']} - {display_date(execution_date)}",
            driver_name="Equipe a definir",
            status="planejada",
            planned_date=execution_date,
            estimated_duration_minutes=int(planned["work_minutes"]),
        )
        db.session.add(route)
        db.session.flush()
        official_ids[planned["number"]] = route.id

        stops_by_sequence = {}
        for item in planned["items"]:
            client = find_or_create_import_client(company_id, item["client"], base_id)
            stop = RouteStop(
                route_id=route.id,
                client_id=client.id,
                sequence=item["sequence"],
                planned_arrival=item["arrival"],
                status="pendente",
                service_minutes=DEFAULT_SERVICE_MINUTES,
            )
            db.session.add(stop)
            db.session.flush()
            stops_by_sequence[item["sequence"]] = stop
            db.session.add(StopObservation(
                company_id=company_id,
                route_id=route.id,
                route_stop_id=stop.id,
                observation_type="rota_importada_oficial",
                description=f"Parada oficial gerada pela importacao {source_name} para execucao em {display_date(execution_date)}.",
                latitude=client.latitude,
                longitude=client.longitude,
                created_by_user_id=user.id,
            ))

        for item in planned["items"]:
            if item.get("break_after"):
                after_stop = stops_by_sequence.get(item["sequence"])
                if after_stop:
                    db.session.add(RouteBreak(
                        company_id=company_id,
                        route_id=route.id,
                        after_route_stop_id=after_stop.id,
                        title="Intervalo intrajornada",
                        break_type="intrajornada",
                        duration_minutes=item["break_after"]["duration"],
                        source="ia",
                        notes=f"Sugerido na importacao: {item['break_after']['start']} - {item['break_after']['end']}",
                        active=True,
                        created_by_user_id=user.id,
                    ))

        db.session.add(StopObservation(
            company_id=company_id,
            route_id=route.id,
            observation_type="rota_oficial_criada",
            description=f"Rota oficial criada a partir da simulacao/importacao {source_name}. Status: {planned['status']}.",
            created_by_user_id=user.id,
        ))
    db.session.commit()
    return official_ids


def create_official_routes_from_simulation(company_id, result, vehicle_type, execution_date, user, base_id=None):
    from database.database import db

    vehicle = find_or_create_import_vehicle(company_id, vehicle_type)
    created_ids = []
    speed = vehicle.average_speed_kmh or default_vehicle_speed(vehicle_type)
    for planned in result["routes"]:
        if planned.get("long_route") and planned.get("continuity_days"):
            base_date = datetime.strptime(execution_date, "%Y-%m-%d").date()
            for day in planned["continuity_days"]:
                day_clients = day.get("clients", [])
                if not day_clients:
                    continue
                day_date = (base_date + timedelta(days=int(day["day"]) - 1)).isoformat()
                route = LogisticRoute(
                    company_id=company_id,
                    base_id=base_id or infer_base_id_from_clients(day_clients, user),
                    vehicle_id=vehicle.id,
                    name=f"ROTA {planned['number']} DIA {day['day']} - {display_date(day_date)}",
                    driver_name=f"Equipe a definir | continuidade dia {day['day']}",
                    status="planejada",
                    planned_date=day_date,
                    estimated_distance_km=round(float(day["distance"]), 2),
                    estimated_duration_minutes=int(day["minutes"]),
                )
                db.session.add(route)
                db.session.flush()
                created_ids.append(route.id)

                current_minutes = 0
                last_point = [BASE_LATITUDE, BASE_LONGITUDE]
                route_stops = []
                route_stop_elapsed = []
                for sequence, client in enumerate(day_clients, start=1):
                    point = [client.latitude, client.longitude]
                    distance = distancia_reta_km([last_point, point]) * 1.35
                    travel_minutes = (distance / speed) * 60 if speed else 0
                    current_minutes += travel_minutes
                    stop = RouteStop(
                        route_id=route.id,
                        client_id=client.id,
                        sequence=sequence,
                        planned_arrival=minutes_to_hhmm(current_minutes),
                        status="pendente",
                        service_minutes=DEFAULT_SERVICE_MINUTES,
                    )
                    db.session.add(stop)
                    db.session.flush()
                    route_stops.append(stop)
                    current_minutes += DEFAULT_SERVICE_MINUTES
                    route_stop_elapsed.append((stop, current_minutes))
                    last_point = point
                    db.session.add(StopObservation(
                        company_id=company_id,
                        route_id=route.id,
                        route_stop_id=stop.id,
                        observation_type="simulacao_longa_aprovada",
                        description=f"Parada gerada como continuidade Dia {day['day']} da Rota {planned['number']} para execucao em {display_date(day_date)}.",
                        latitude=client.latitude,
                        longitude=client.longitude,
                        created_by_user_id=user.id,
                    ))

                if len(route_stops) > 1 and day.get("needs_intraday_break"):
                    break_candidates = [item for item in route_stop_elapsed if INTRADAY_BREAK_WINDOW_START <= item[1] <= INTRADAY_BREAK_WINDOW_END]
                    if not break_candidates:
                        break_candidates = [item for item in route_stop_elapsed if item[1] >= INTRADAY_BREAK_WINDOW_START]
                    break_after = (break_candidates[0][0] if break_candidates else route_stops[min(len(route_stops) - 1, max(0, len(route_stops) // 2 - 1))])
                    db.session.add(RouteBreak(
                        company_id=company_id,
                        route_id=route.id,
                        after_route_stop_id=break_after.id,
                        title="Intervalo intrajornada",
                        break_type="intrajornada",
                        duration_minutes=INTRADAY_BREAK_MINUTES,
                        source="ia",
                        notes=f"Obrigatorio em jornada acima de 6h, programado entre a 4a e a 6a hora. {day['reason']}",
                        active=True,
                        created_by_user_id=user.id,
                    ))

                db.session.add(StopObservation(
                    company_id=company_id,
                    route_id=route.id,
                    observation_type="rota_longa_continuidade",
                    description=(
                        f"Rota longa oficializada como Dia {day['day']} da Rota {planned['number']}. "
                        f"Tempo previsto {tempo_formatado(day['minutes'])}, distancia {day['distance']:.1f} km. "
                        f"Descanso interjornada minimo: {day.get('interjourney_rest_text') or '-'}. {day['reason']}"
                    ),
                    created_by_user_id=user.id,
                ))
            continue

        route = LogisticRoute(
            company_id=company_id,
            base_id=base_id or infer_base_id_from_clients(planned["clients"], user),
            vehicle_id=vehicle.id,
            name=f"ROTA {planned['number']} - {display_date(execution_date)}",
            driver_name="Equipe a definir",
            status="planejada",
            planned_date=execution_date,
            estimated_distance_km=round(float(planned["distance"]), 2),
            estimated_duration_minutes=int(planned["minutes"]),
        )
        db.session.add(route)
        db.session.flush()
        created_ids.append(route.id)

        current_minutes = 0
        last_point = [BASE_LATITUDE, BASE_LONGITUDE]
        route_stops = []
        route_stop_elapsed = []
        for sequence, client in enumerate(planned["clients"], start=1):
            point = [client.latitude, client.longitude]
            distance = distancia_reta_km([last_point, point]) * 1.35
            travel_minutes = (distance / speed) * 60 if speed else 0
            current_minutes += travel_minutes
            planned_arrival = minutes_to_hhmm(current_minutes)
            stop = RouteStop(
                route_id=route.id,
                client_id=client.id,
                sequence=sequence,
                planned_arrival=planned_arrival,
                status="pendente",
                service_minutes=DEFAULT_SERVICE_MINUTES,
            )
            db.session.add(stop)
            db.session.flush()
            route_stops.append(stop)
            current_minutes += DEFAULT_SERVICE_MINUTES
            route_stop_elapsed.append((stop, current_minutes))
            last_point = point
            db.session.add(StopObservation(
                company_id=company_id,
                route_id=route.id,
                route_stop_id=stop.id,
                observation_type="simulacao_aprovada",
                description=f"Parada oficial gerada a partir da simulacao aprovada para execucao em {display_date(execution_date)}.",
                latitude=client.latitude,
                longitude=client.longitude,
                created_by_user_id=user.id,
            ))

        if planned.get("break_minutes", 0) and len(route_stops) > 1:
            break_candidates = [item for item in route_stop_elapsed if INTRADAY_BREAK_WINDOW_START <= item[1] <= INTRADAY_BREAK_WINDOW_END]
            if not break_candidates:
                break_candidates = [item for item in route_stop_elapsed if item[1] >= INTRADAY_BREAK_WINDOW_START]
            break_after = (break_candidates[0][0] if break_candidates else route_stops[min(len(route_stops) - 1, max(0, len(route_stops) // 2 - 1))])
            db.session.add(RouteBreak(
                company_id=company_id,
                route_id=route.id,
                after_route_stop_id=break_after.id,
                title="Intervalo intrajornada",
                break_type="intrajornada",
                duration_minutes=int(planned["break_minutes"]),
                source="ia",
                notes="Obrigatorio em jornada acima de 6h, programado entre a 4a e a 6a hora.",
                active=True,
                created_by_user_id=user.id,
            ))

        db.session.add(StopObservation(
            company_id=company_id,
            route_id=route.id,
            observation_type="rota_oficial_criada",
            description=(
                f"Rota oficial criada a partir da simulacao {'com ajuste manual' if result.get('manual_adjusted') else 'otimizada'}. "
                f"Distancia {planned['distance']:.1f} km, tempo {tempo_formatado(planned['minutes'])}, custo {format_money(planned['cost'])}."
            ),
            created_by_user_id=user.id,
        ))
    db.session.commit()
    return created_ids


def demo_selected_clients(company_id, posted_ids=None, limit=10):
    if not company_id:
        return []
    query = Client.query.filter_by(company_id=company_id, active=True).order_by(Client.priority, Client.name)
    user = current_web_user()
    if user and getattr(user, "base_id", None) and role_level(user) < 3:
        query = query.filter_by(base_id=user.base_id)
    clients = [client for client in query.all() if client.latitude is not None and client.longitude is not None]
    if posted_ids:
        selected = [client for client_id in posted_ids for client in clients if client.id == client_id]
        return selected or clients[:limit]
    return clients[:limit]


@web_bp.route("/web/demo-comercial", methods=["GET", "POST"])
@web_login_required
def commercial_demo_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))

    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    selected_company_id = request.form.get("company_id", type=int) or request.args.get("company_id", type=int)
    if not selected_company_id and companies:
        selected_company_id = companies[0].id
    if not user.is_master:
        selected_company_id = user.company_id
    if not selected_company_id or not can_access_company(user, selected_company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "demo_comercial"), 403

    vehicle_type = request.form.get("vehicle_type") or request.args.get("vehicle_type") or "van"
    execution_date = request.form.get("execution_date") or request.args.get("execution_date")
    if not execution_date:
        execution_date = (datetime.now().date() + timedelta(days=1)).isoformat()
    selected_ids = request.form.getlist("client_ids", type=int)
    selected_clients = demo_selected_clients(selected_company_id, selected_ids)
    result = split_simulation_routes(selected_clients, vehicle_type) if selected_clients else None

    company_options = "".join(
        f"<option value='{company.id}' {'selected' if company.id == selected_company_id else ''}>{escape(company.name)}</option>"
        for company in companies if company
    )
    vehicle_options = "".join(
        f"<option value='{item}' {'selected' if item == vehicle_type else ''}>{item.title()}</option>"
        for item in ["van", "caminhao", "carro", "moto"]
    )
    client_checks = "".join(
        f"<label class='demo-check'><input type='checkbox' name='client_ids' value='{client.id}' checked><span><strong>{escape(client.name)}</strong><small>{client.delivery_window_start or '-'} - {client.delivery_window_end or '-'} | {format_coord(client.latitude)}, {format_coord(client.longitude)}</small></span></label>"
        for client in selected_clients
    )
    hidden_clients = "".join(f"<input type='hidden' name='client_ids' value='{client.id}'>" for client in selected_clients)
    config_fields = f"""
        <input type="hidden" name="company_id" value="{selected_company_id}">
        <input type="hidden" name="vehicle_type" value="{vehicle_type}">
        <input type="hidden" name="execution_date" value="{execution_date}">
        {hidden_clients}
    """

    route_rows = ""
    summary_cards = """
        <div class="demo-metric"><span>Clientes</span><strong>0</strong></div>
        <div class="demo-metric"><span>Rotas</span><strong>0</strong></div>
        <div class="demo-metric"><span>Maior jornada</span><strong>-</strong></div>
        <div class="demo-metric"><span>Custo estimado</span><strong>-</strong></div>
    """
    if result:
        summary_cards = f"""
            <div class="demo-metric"><span>Clientes</span><strong>{len(selected_clients)}</strong></div>
            <div class="demo-metric"><span>Rotas</span><strong>{len(result['routes'])}</strong></div>
            <div class="demo-metric"><span>Maior jornada</span><strong>{tempo_formatado(result['max_route_minutes'])}</strong></div>
            <div class="demo-metric"><span>Custo estimado</span><strong>{format_money(result['cost'])}</strong></div>
        """
        for planned in result["routes"]:
            names = ", ".join(escape(client.name) for client in planned["clients"])
            warning = "Rota longa" if planned.get("long_route") else "Dentro da jornada"
            route_rows += f"""
            <tr>
                <td><span class="route-dot" style="background:{route_color(planned['number'])};"></span>Rota {planned['number']}</td>
                <td>{names}</td>
                <td>{tempo_formatado(planned['minutes'])}</td>
                <td>{planned['distance']:.1f} km</td>
                <td>{format_money(planned['cost'])}</td>
                <td>{warning}</td>
            </tr>
            """
    if not route_rows:
        route_rows = "<tr><td colspan='6'>Cadastre clientes com latitude e longitude para rodar a demonstracao.</td></tr>"

    body = f"""
    <style>
        .demo-shell {{ display:grid; gap:14px; }}
        .demo-hero {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:18px; display:grid; grid-template-columns:1.2fr .8fr; gap:16px; align-items:start; }}
        .demo-hero h2 {{ margin:0 0 6px; color:#071b36; }}
        .demo-hero p {{ margin:0; color:#53637a; line-height:1.45; }}
        .demo-config {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
        .demo-config label {{ margin:0; }}
        .demo-metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
        .demo-metric {{ background:#f8fbff; border:1px solid #d9e4f5; border-radius:8px; padding:12px; }}
        .demo-metric span {{ display:block; color:#53637a; font-size:12px; }}
        .demo-metric strong {{ display:block; margin-top:4px; font-size:24px; color:#071b36; }}
        .demo-flow {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; }}
        .demo-step {{ background:#fff; border:1px solid #d7e0ec; border-radius:8px; padding:14px; min-height:190px; display:flex; flex-direction:column; gap:10px; }}
        .demo-step b {{ display:grid; place-items:center; width:30px; height:30px; border-radius:999px; background:#1f5eff; color:#fff; }}
        .demo-step h3 {{ margin:0; font-size:16px; color:#071b36; }}
        .demo-step p {{ margin:0; color:#53637a; font-size:13px; line-height:1.4; flex:1; }}
        .demo-step form {{ margin:0; }}
        .demo-checks {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }}
        .demo-check {{ display:flex; gap:8px; align-items:flex-start; border:1px solid #e4e7ec; border-radius:8px; padding:9px; font-weight:400; margin:0; background:#fbfcfe; }}
        .demo-check input {{ width:auto; margin-top:3px; }}
        .demo-check small {{ display:block; color:#667085; margin-top:3px; }}
        .route-dot {{ display:inline-block; width:10px; height:10px; border-radius:999px; margin-right:7px; }}
        @media (max-width:1100px) {{ .demo-hero, .demo-flow {{ grid-template-columns:1fr; }} .demo-config, .demo-metrics, .demo-checks {{ grid-template-columns:1fr; }} }}
    </style>
    <div class="demo-shell">
        <div class="demo-hero">
            <div>
                <h2>Roteiro rapido para vender a ferramenta</h2>
                <p>Use esta tela para apresentar o ciclo completo: simula a operacao, grava rotas oficiais, abre a torre de controle, mostra o motorista no celular e entrega relatorio para o cliente.</p>
            </div>
            <form method="post" class="demo-config">
                <label>Empresa
                    <select name="company_id">{company_options}</select>
                </label>
                <label>Veiculo
                    <select name="vehicle_type">{vehicle_options}</select>
                </label>
                <label>Data oficial
                    <input type="date" name="execution_date" value="{execution_date}">
                </label>
                <button class="btn" type="submit">Atualizar demonstracao</button>
            </form>
        </div>

        <div class="demo-metrics">{summary_cards}</div>

        <div class="card">
            <h2>Clientes usados na demonstracao</h2>
            <p class="muted">A demonstracao usa clientes ativos com latitude e longitude. Voce pode ajustar a selecao abaixo e clicar em atualizar.</p>
            <form method="post">
                <input type="hidden" name="company_id" value="{selected_company_id}">
                <input type="hidden" name="vehicle_type" value="{vehicle_type}">
                <input type="hidden" name="execution_date" value="{execution_date}">
                <div class="demo-checks">{client_checks or 'Nenhum cliente com latitude e longitude cadastrado.'}</div>
                <button class="btn btn-light" style="margin-top:10px;" type="submit">Atualizar clientes da demo</button>
            </form>
        </div>

        <div class="demo-flow">
            <div class="demo-step">
                <b>1</b>
                <h3>Simular operacao</h3>
                <p>Abre a simulacao com clientes, veiculo, custo, distancia, mapa e sugestao de divisao por rotas.</p>
                <form method="post" action="/web/simulacao">{config_fields}<button class="btn" type="submit">Simular agora</button></form>
            </div>
            <div class="demo-step">
                <b>2</b>
                <h3>Gerar rotas oficiais</h3>
                <p>Grava a simulacao como rota oficial na data escolhida. Se ja existir rota nessa data, o sistema abre a operacao existente para evitar duplicidade.</p>
                <form method="post" action="/web/demo-comercial/oficializar">{config_fields}<button class="btn" type="submit">Gerar oficial</button></form>
            </div>
            <div class="demo-step">
                <b>3</b>
                <h3>Abrir gestao operacional</h3>
                <p>Mostra a torre do dia com linha do tempo, mapa, pontos, alertas, intervalos e status de execucao.</p>
                <a class="btn" href="/web/operacao?data={execution_date}">Abrir gestao</a>
            </div>
            <div class="demo-step">
                <b>4</b>
                <h3>Visao do motorista</h3>
                <p>Para demonstrar no celular, entre com o usuario do motorista. Exemplo de teste: login rota1 e senha rota1.</p>
                <a class="btn" href="/login">Abrir login</a>
            </div>
            <div class="demo-step">
                <b>5</b>
                <h3>Exportar relatorio</h3>
                <p>Entrega ao cliente os indicadores operacionais em Excel, com dados filtrados pela empresa do login.</p>
                <a class="btn" href="/web/exportar-dashboard.xlsx">Exportar Excel</a>
            </div>
        </div>

        <div class="card">
            <h2>Previa da operacao simulada</h2>
            <table class="table">
                <thead><tr><th>Rota</th><th>Clientes</th><th>Tempo</th><th>Distancia</th><th>Custo</th><th>Status</th></tr></thead>
                <tbody>{route_rows}</tbody>
            </table>
        </div>
    </div>
    """
    return layout("Demonstracao Comercial", body, "demo_comercial")


@web_bp.route("/web/demo-comercial/oficializar", methods=["POST"])
@web_login_required
def commercial_demo_officialize_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso disponivel para perfis autorizados.</h2></div>", "demo_comercial"), 403
    company_id = request.form.get("company_id", type=int) or user.company_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "demo_comercial"), 403
    execution_date = request.form.get("execution_date") or (datetime.now().date() + timedelta(days=1)).isoformat()
    try:
        datetime.strptime(execution_date, "%Y-%m-%d")
    except ValueError:
        return layout("Demonstracao Comercial", "<div class='card'><h2>Informe uma data valida para oficializar.</h2><a class='btn' href='/web/demo-comercial'>Voltar</a></div>", "demo_comercial"), 400

    existing = LogisticRoute.query.filter_by(company_id=company_id, planned_date=execution_date).order_by(LogisticRoute.id).first()
    if existing:
        return redirect(url_for("web_bp.operational_page", data=execution_date, route_id=existing.id))

    vehicle_type = request.form.get("vehicle_type") or "van"
    selected_ids = request.form.getlist("client_ids", type=int)
    selected_clients = demo_selected_clients(company_id, selected_ids)
    if not selected_clients:
        return layout("Demonstracao Comercial", "<div class='card'><h2>Cadastre clientes com latitude e longitude antes de gerar rotas oficiais.</h2><a class='btn' href='/web/clientes'>Abrir clientes</a></div>", "demo_comercial"), 400

    result = split_simulation_routes(selected_clients, vehicle_type)
    route_ids = create_official_routes_from_simulation(company_id, result, vehicle_type, execution_date, user, infer_base_id_from_clients(selected_clients, user))
    first_route_id = route_ids[0] if route_ids else None
    if first_route_id:
        return redirect(url_for("web_bp.operational_page", data=execution_date, route_id=first_route_id))
    return redirect(url_for("web_bp.operational_page", data=execution_date))


@web_bp.route("/web/simulacao", methods=["GET", "POST"])
@web_login_required
def simulation_page():
    user = request.web_user
    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    selected_company_id = request.form.get("company_id", type=int) or request.args.get("company_id", type=int)
    if not selected_company_id and companies:
        selected_company_id = companies[0].id
    if not user.is_master:
        selected_company_id = user.company_id

    vehicle_type = request.form.get("vehicle_type") or "van"
    company_clients_query = Client.query.filter_by(company_id=selected_company_id, active=True).order_by(Client.name) if selected_company_id else Client.query.filter(False)
    if getattr(user, "base_id", None) and role_level(user) < 3:
        company_clients_query = company_clients_query.filter_by(base_id=user.base_id)
    company_clients = company_clients_query.all()
    selected_ids = request.form.getlist("client_ids", type=int)
    selected_clients = [client for client in company_clients if client.id in selected_ids]
    result = None
    if request.method == "POST" and selected_clients:
        if request.form.get("simulation_action") == "manual_adjust":
            assignments = {client.id: request.form.get(f"manual_route_{client.id}", type=int) or 1 for client in selected_clients}
            result = manual_simulation_routes(selected_clients, vehicle_type, assignments)
        else:
            result = split_simulation_routes(selected_clients, vehicle_type)

    company_options = "".join(
        f"<option value='{company.id}' {'selected' if company.id == selected_company_id else ''}>{company.name}</option>"
        for company in companies if company
    )
    vehicle_options = "".join(
        f"<option value='{item}' {'selected' if item == vehicle_type else ''}>{item.title()}</option>"
        for item in ["van", "caminhao", "carro", "moto"]
    )
    client_checks = "".join(
        f"<label style='display:flex;gap:8px;align-items:flex-start;margin:8px 0;font-weight:400;'><input style='width:auto;margin-top:3px;' type='checkbox' name='client_ids' value='{client.id}' {'checked' if client.id in selected_ids else ''}> <span><strong>{client.name}</strong><br><span class='muted'>Prioridade {client.priority} | Janela {client.delivery_window_start or '-'} - {client.delivery_window_end or '-'} | {format_coord(client.latitude)}, {format_coord(client.longitude)}</span></span></label>"
        for client in company_clients
    )

    summary = "<div class='panel'><strong>Selecione clientes e clique em Simular rota.</strong></div>"
    map_link = ""
    comparison_rows = ""
    if selected_clients:
        for item in ["van", "caminhao", "carro", "moto"]:
            sim = split_simulation_routes(selected_clients, item)
            comparison_rows += f"<tr><td>{item.title()}</td><td>{sim['speed']} km/h</td><td>{len(sim['routes'])}</td><td>{sim['distance']:.1f} km</td><td>{tempo_formatado(sim['max_route_minutes'])}</td><td>{tempo_formatado(sim['minutes'])}</td><td>{format_money(sim['cost'])}</td></tr>"
    if result:
        route_cards = ""
        route_count = len(result["routes"])
        def manual_route_options(selected_number):
            return "".join(
                f"<option value='{number}' {'selected' if number == selected_number else ''}>Rota {number}</option>"
                for number in range(1, route_count + 2)
            )
        hidden_clients = "".join(f"<input type='hidden' name='client_ids' value='{client.id}'>" for client in result["clients"])
        manual_assignments = {}
        for planned_route in result["routes"]:
            status_class = "badge-success" if planned_route["minutes"] <= WORKDAY_LIMIT_MINUTES else "badge-danger"
            color = route_color(planned_route["number"])
            continuity_html = ""
            if planned_route.get("long_route"):
                day_rows = ""
                for day in planned_route.get("continuity_days", []):
                    day_clients = ", ".join(escape(client.name) for client in day.get("clients", [])) or "Deslocamento / retorno sem atendimento"
                    intraday = f"Sim, {day['intraday_break_minutes']} min {day['intraday_window']}" if day.get("needs_intraday_break") else "Nao"
                    interjourney = day.get("interjourney_rest_text") or "-"
                    day_rows += (
                        f"<tr><td>Dia {day['day']}</td><td>{day_clients}</td>"
                        f"<td>{tempo_formatado(day['minutes'])}</td><td>{day['distance']:.1f} km</td>"
                        f"<td>{intraday}</td><td>{interjourney}</td><td>{escape(day['reason'])}</td></tr>"
                    )
                continuity_html = f"""
                <div class='panel' style='margin-top:12px;border-color:#f97316;background:#fff7ed;'>
                    <strong>Rota longa com continuidade:</strong> {escape(planned_route.get('continuity_note', 'Rota acima de 10h.'))}
                    <div class='table-wrap' style='margin-top:8px;'>
                        <table class='table'>
                            <thead><tr><th>Etapa</th><th>Atendimentos</th><th>Tempo previsto</th><th>Distancia</th><th>Intrajornada</th><th>Interjornada minima</th><th>Orientacao</th></tr></thead>
                            <tbody>{day_rows}</tbody>
                        </table>
                    </div>
                </div>
                """
            ordered_rows = "".join(
                f"<tr><td>{index}</td><td>{client.name}</td><td>{client.priority}</td><td>{client.delivery_window_start or '-'} - {client.delivery_window_end or '-'}</td><td>{format_coord(client.latitude)}, {format_coord(client.longitude)}</td><td><select name='manual_route_{client.id}'>{manual_route_options(planned_route['number'])}</select></td></tr>"
                for index, client in enumerate(planned_route["clients"], start=1)
            )
            for client in planned_route["clients"]:
                manual_assignments[client.id] = planned_route["number"]
            route_cards += f"""
            <div class='card' style='margin-top:14px;border-left:8px solid {color};'>
                <div style='display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;'>
                    <h2><span style='display:inline-block;width:12px;height:12px;border-radius:50%;background:{color};margin-right:8px;'></span>Rota {planned_route['number']} sugerida</h2>
                    <span class='badge {status_class}'>{tempo_formatado(planned_route['minutes'])} | {planned_route['distance']:.1f} km | {format_money(planned_route['cost'])} | {len(planned_route['clients'])} pontos</span>
                </div>
                {continuity_html}
                <table class='table'><thead><tr><th>#</th><th>Cliente</th><th>Prioridade</th><th>Janela</th><th>Latitude, Longitude</th><th>Mover para</th></tr></thead><tbody>{ordered_rows}</tbody></table>
            </div>
            """
        manual_param = ",".join(f"{client_id}-{route_number}" for client_id, route_number in manual_assignments.items())
        manual_notice = "Ajuste manual aplicado." if result.get("manual_adjusted") else "Motor otimizado por proximidade, janela, limite de 10h e custo. Se desejar, mova clientes entre rotas e recalcule."
        route_cards = f"""
        <form method='post' style='margin-top:14px;'>
            <input type='hidden' name='simulation_action' value='manual_adjust'>
            <input type='hidden' name='company_id' value='{selected_company_id}'>
            <input type='hidden' name='vehicle_type' value='{vehicle_type}'>
            {hidden_clients}
            <div class='panel' style='display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;'>
                <span><strong>Ajuste manual entre rotas:</strong> {manual_notice}</span>
                <span style='display:flex;gap:8px;flex-wrap:wrap;'>
                    <button class='btn' type='submit'>Recalcular com ajuste manual</button>
                    <button class='btn btn-light' type='submit' formaction='/web/simulacao/exportar.xlsx' formmethod='post'>Exportar simulacao Excel</button>
                </span>
                <span style='display:flex;gap:8px;align-items:end;flex-wrap:wrap;'>
                    <label style='margin:0;font-weight:800;'>Data oficial<br><input name='execution_date' type='date' required style='padding:9px;border:1px solid #cfd8e3;border-radius:7px;'></label>
                    <button class='btn' type='submit' formaction='/web/simulacao/oficializar' formmethod='post'>Aprovar e gerar rota oficial</button>
                </span>
            </div>
            {route_cards}
        </form>
        """
        summary = f"""
        <div class='grid cards'>
            <div class='card'><div class='muted'>Veiculo</div><div class='metric'>{vehicle_type.title()}</div></div>
            <div class='card'><div class='muted'>Rotas necessarias</div><div class='metric'>{len(result['routes'])}</div></div>
            <div class='card'><div class='muted'>Maior jornada por rota</div><div class='metric'>{tempo_formatado(result['max_route_minutes'])}</div></div>
            <div class='card'><div class='muted'>Distancia total</div><div class='metric'>{result['distance']:.1f} km</div></div>
            <div class='card'><div class='muted'>Tempo total da operacao</div><div class='metric'>{tempo_formatado(result['minutes'])}</div></div>
            <div class='card'><div class='muted'>Custo estimado</div><div class='metric'>{format_money(result['cost'])}</div></div>
            <div class='card'><div class='muted'>Rotas longas</div><div class='metric'>{result.get('long_route_count', 0)}</div></div>
            <div class='card'><div class='muted'>Dias de continuidade</div><div class='metric'>{result.get('continuity_day_count', 0) or '-'}</div></div>
        </div>
        <div class='panel' style='margin-top:14px;'><strong>Regra aplicada:</strong> rotas acima de 6h recebem 1h de intervalo intrajornada entre a 4a e a 6a hora. Quando a jornada prevista passa de 10h, a simulacao sinaliza rota longa, sugere continuidade por Dia 1, Dia 2 e demais dias, e considera descanso interjornada minimo de 11h entre os dias.</div>
        {route_cards}
        """
        ids = ",".join(str(client.id) for client in result["clients"])
        map_link = f"<a class='btn' href='/web/simulacao/mapa?vehicle_type={vehicle_type}&client_ids={ids}&manual={manual_param}' target='_blank'>Abrir mapa da simulacao</a>"

    comparison = ""
    if comparison_rows:
        comparison = f"<div class='card' style='margin-top:16px;'><h2>Comparativo por veiculo</h2><table class='table'><thead><tr><th>Veiculo</th><th>Velocidade</th><th>Rotas</th><th>Distancia</th><th>Maior jornada</th><th>Tempo total</th><th>Custo</th></tr></thead><tbody>{comparison_rows}</tbody></table></div>"

    cubagem_tool = simulation_cubage_tool()
    body = f"""
    <div class='card' style='margin-bottom:16px;'>
        <h2>Importar planilha do cliente e gerar rotas</h2>
        <p class='muted'>Use quando a planilha ja vier com latitude e longitude. Primeiro voce pode simular; quando aprovar, marque como oficial para gravar as rotas na data de execucao.</p>
        <form method='post' action='/web/simulacao/importar-planilha' enctype='multipart/form-data' style='display:grid;grid-template-columns:minmax(210px,1fr) 170px 160px 170px 190px;gap:12px;align-items:end;'>
            <div>
                <label>Arquivo .xlsx</label>
                <input name='file' type='file' accept='.xlsx' required>
            </div>
            <div>
                <label>Empresa</label>
                <select name='company_id'>{company_options}</select>
            </div>
            <div>
                <label>Tipo de veiculo</label>
                <select name='vehicle_type'>{vehicle_options}</select>
            </div>
            <div>
                <label>Data de execucao</label>
                <input name='execution_date' type='date'>
            </div>
            <label style='display:flex;gap:8px;align-items:center;margin:0 0 10px;font-weight:800;'>
                <input style='width:auto;' name='make_official' value='1' type='checkbox'> Gravar como oficial
            </label>
            <button class='btn' type='submit'>Gerar simulado/oficial</button>
        </form>
        <p class='muted'>Colunas aceitas: cliente/nome, endereco, latitude, longitude, prioridade, janela_inicio e janela_fim. <a href='/web/simulacao/modelo-importacao.xlsx'>Baixar planilha padrao</a>.</p>
    </div>
    <form method='post' class='grid two'>
        <div class='card'>
            <h2>Parametros da simulacao</h2>
            <label>Empresa</label>
            <select name='company_id'>{company_options}</select>
            <label>Tipo de veiculo</label>
            <select name='vehicle_type'>{vehicle_options}</select>
            <label>Pontos de atendimento</label>
            <div class='panel'>{client_checks or 'Nenhum cliente cadastrado para esta empresa.'}</div>
            <button class='btn' style='margin-top:14px;' type='submit'>Simular rota</button>
            {map_link}
        </div>
        <div class='card'>
            <h2>Regras da IA operacional</h2>
            <ul class='ai-list'>
                <li>Ordena prioridade alta antes de media e baixa.</li>
                <li>Considera janela de entrega como criterio de desempate.</li>
                <li>Compara tempo e custo por tipo de veiculo.</li>
                <li>Aplica intrajornada de 1h para jornadas acima de 6h e interjornada minima de 11h nas rotas longas.</li>
                <li>A importacao pode ficar apenas como SIMULADO ou virar OFICIAL na data escolhida.</li>
            </ul>
        </div>
    </form>
    <div style='margin-top:16px;'>{summary}</div>
    {comparison}
    {cubagem_tool}
    """
    return layout("Simulacao de rotas", body, "simulacao")


@web_bp.route("/web/simulacao/modelo-importacao.xlsx")
@web_login_required
def route_import_template_download():
    output = build_route_import_template_workbook()
    return send_file(output, as_attachment=True, download_name="modelo_importacao_rotas_ris.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@web_bp.route("/web/simulacao/exportar.xlsx", methods=["POST"])
@web_login_required
def export_simulation_excel_page():
    user = request.web_user
    company_id = request.form.get("company_id", type=int) or user.company_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "simulacao"), 403
    vehicle_type = request.form.get("vehicle_type") or "van"
    client_ids = request.form.getlist("client_ids", type=int)
    clients_query = Client.query.filter(Client.id.in_(client_ids), Client.company_id == company_id, Client.active == True) if client_ids else Client.query.filter(False)
    if getattr(user, "base_id", None) and role_level(user) < 3:
        clients_query = clients_query.filter_by(base_id=user.base_id)
    clients = clients_query.all()
    ordered_clients = [client for client_id in client_ids for client in clients if client.id == client_id]
    if not ordered_clients:
        return layout("Exportar simulacao", "<div class='card'><h2>Selecione clientes e simule antes de exportar.</h2><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 400

    assignments = {}
    has_manual = False
    for client in ordered_clients:
        value = request.form.get(f"manual_route_{client.id}", type=int)
        if value:
            assignments[client.id] = value
            has_manual = True
    result = manual_simulation_routes(ordered_clients, vehicle_type, assignments) if has_manual else split_simulation_routes(ordered_clients, vehicle_type)
    company = Company.query.get(company_id)
    output = build_simulation_export_workbook(result, company.name if company else "Empresa", vehicle_type)
    filename = f"simulacao_rotas_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@web_bp.route("/web/simulacao/oficializar", methods=["POST"])
@web_login_required
def officialize_simulation_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso disponivel para perfis autorizados.</h2></div>", "simulacao"), 403
    company_id = request.form.get("company_id", type=int) or user.company_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Acesso negado", "<div class='card'><h2>Empresa fora da sua permissao.</h2></div>", "simulacao"), 403
    execution_date = request.form.get("execution_date") or ""
    try:
        datetime.strptime(execution_date, "%Y-%m-%d")
    except ValueError:
        return layout("Oficializar simulacao", "<div class='card'><h2>Informe a data oficial de execucao.</h2><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 400

    vehicle_type = request.form.get("vehicle_type") or "van"
    client_ids = request.form.getlist("client_ids", type=int)
    clients = Client.query.filter(Client.id.in_(client_ids), Client.company_id == company_id, Client.active == True).all() if client_ids else []
    ordered_clients = [client for client_id in client_ids for client in clients if client.id == client_id]
    if not ordered_clients:
        return layout("Oficializar simulacao", "<div class='card'><h2>Selecione clientes e simule antes de oficializar.</h2><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 400

    assignments = {}
    has_manual = False
    for client in ordered_clients:
        value = request.form.get(f"manual_route_{client.id}", type=int)
        if value:
            assignments[client.id] = value
            has_manual = True
    result = manual_simulation_routes(ordered_clients, vehicle_type, assignments) if has_manual else split_simulation_routes(ordered_clients, vehicle_type)
    route_ids = create_official_routes_from_simulation(company_id, result, vehicle_type, execution_date, user, infer_base_id_from_clients(ordered_clients, user))
    first_route_id = route_ids[0] if route_ids else None
    if first_route_id:
        return redirect(url_for("web_bp.operational_page", route_id=first_route_id))
    return redirect(url_for("web_bp.operational_page"))


@web_bp.route("/web/simulacao/importar-planilha", methods=["POST"])
@web_login_required
def import_route_spreadsheet_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso disponivel para perfis autorizados.</h2></div>", "simulacao"), 403
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return layout("Importar planilha", "<div class='card'><h2>Selecione uma planilha .xlsx.</h2><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 400
    filename, upload_error = validate_upload(upload, ALLOWED_SPREADSHEET_EXTENSIONS, MAX_SPREADSHEET_MB)
    if upload_error:
        return layout("Importar planilha", f"<div class='card'><h2>{escape(upload_error)}</h2><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 400

    from openpyxl import load_workbook
    try:
        workbook = load_workbook(upload, data_only=True)
        sheet = workbook.active
    except Exception as exc:
        return layout("Importar planilha", f"<div class='card'><h2>Nao foi possivel ler a planilha.</h2><p>{escape(str(exc))}</p><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 400

    raw_headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    headers = [normalize_header(value) for value in raw_headers]
    clients = []
    errors = []
    for row_index, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        row = {headers[index]: value for index, value in enumerate(values) if index < len(headers)}
        name = str(first_value(row, "cliente", "nome", "nome_cliente", "id_do_cliente", "descricao_do_cliente") or "").strip()
        latitude = parse_float_value(first_value(row, "latitude", "lat"))
        longitude = parse_float_value(first_value(row, "longitude", "long", "lng", "lon"))
        if not name and latitude is None and longitude is None:
            continue
        if not name or latitude is None or longitude is None:
            errors.append(f"Linha {row_index}: informe cliente/nome, latitude e longitude.")
            continue
        priority = str(first_value(row, "prioridade", "priority") or "media").strip().lower()
        if priority not in ("alta", "media", "baixa"):
            priority = "media"
        clients.append({
            "name": name,
            "address": str(first_value(row, "endereco", "address", "local", "logradouro") or "").strip(),
            "priority": priority,
            "window_start": parse_time_value(first_value(row, "janela_inicio", "inicio_janela", "delivery_window_start", "janela_de", "horario_inicio")),
            "window_end": parse_time_value(first_value(row, "janela_fim", "fim_janela", "delivery_window_end", "janela_ate", "horario_fim")),
            "latitude": latitude,
            "longitude": longitude,
            "source_row": row_index,
        })
    if not clients:
        details = "<br>".join(escape(item) for item in errors[:8]) or "Nenhuma linha valida encontrada."
        return layout("Importar planilha", f"<div class='card'><h2>Nenhum cliente valido na planilha.</h2><p>{details}</p><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 400

    vehicle_type = request.form.get("vehicle_type") or "van"
    if vehicle_type not in ("van", "caminhao", "carro", "moto"):
        vehicle_type = "van"
    company_id = request.form.get("company_id", type=int) or user.company_id
    base_id = request.form.get("base_id", type=int) or None
    if getattr(user, "base_id", None) and role_level(user) < 3:
        base_id = user.base_id
    if not company_id or not can_access_company(user, company_id):
        return layout("Importar planilha", "<div class='card'><h2>Empresa invalida para este usuario.</h2><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 403
    if base_id and not can_access_base(user, base_id):
        return layout("Importar planilha", "<div class='card'><h2>Base invalida para este usuario.</h2><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 403
    execution_date = request.form.get("execution_date") or ""
    make_official = request.form.get("make_official") == "1"
    if make_official:
        try:
            datetime.strptime(execution_date, "%Y-%m-%d")
        except ValueError:
            return layout("Importar planilha", "<div class='card'><h2>Informe a data de execucao para gravar como oficial.</h2><p>Para apenas simular, deixe a opcao oficial desmarcada.</p><a class='btn' href='/web/simulacao'>Voltar</a></div>", "simulacao"), 400
    planned_routes = plan_imported_routes(clients, vehicle_type)
    official_route_ids = {}
    if make_official:
        official_route_ids = create_official_routes_from_plan(company_id, planned_routes, vehicle_type, execution_date, filename, user, base_id)
    output = build_imported_routes_workbook(planned_routes, vehicle_type, filename, official_route_ids=official_route_ids, execution_date=execution_date)
    prefix = "rotas_oficiais" if make_official else "rotas_simuladas"
    download_name = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(output, as_attachment=True, download_name=download_name, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@web_bp.route("/web/simulacao/mapa")
@web_login_required
def simulation_map_page():
    user = request.web_user
    ids_text = request.args.get("client_ids", "")
    client_ids = [int(item) for item in ids_text.split(",") if item.strip().isdigit()]
    clients_query = Client.query.filter(Client.id.in_(client_ids)) if client_ids else Client.query.filter(False)
    if not user.is_master:
        clients_query = clients_query.filter_by(company_id=user.company_id)
    clients = clients_query.all()
    ordered_clients = [client for client_id in client_ids for client in clients if client.id == client_id]
    vehicle_type = request.args.get("vehicle_type", "van")
    manual_text = request.args.get("manual", "")
    manual_assignments = {}
    for pair in manual_text.split(","):
        if "-" not in pair:
            continue
        client_text, route_text = pair.split("-", 1)
        if client_text.strip().isdigit() and route_text.strip().isdigit():
            manual_assignments[int(client_text)] = int(route_text)
    result = manual_simulation_routes(ordered_clients, vehicle_type, manual_assignments) if manual_assignments else split_simulation_routes(ordered_clients, vehicle_type)
    center = [ordered_clients[0].latitude, ordered_clients[0].longitude] if ordered_clients else [BASE_LATITUDE, BASE_LONGITUDE]
    mapa = folium.Map(location=center, zoom_start=13)
    folium.Marker([BASE_LATITUDE, BASE_LONGITUDE], popup="BASE<br>Simulacao otimizada", tooltip="BASE", icon=folium.Icon(color="green", icon="home")).add_to(mapa)

    legend_rows = ""
    for planned_route in result["routes"]:
        color = route_color(planned_route["number"])
        points = [[BASE_LATITUDE, BASE_LONGITUDE]] + [[client.latitude, client.longitude] for client in planned_route["clients"]] + [[BASE_LATITUDE, BASE_LONGITUDE]]
        route_path = percurso_pelas_ruas(points)
        distance = route_path["distance_km"]
        minutes = (distance / result["speed"]) * 60 if result["speed"] else 0
        resumo = f"Rota {planned_route['number']} | {vehicle_type.title()} | {result['speed']} km/h | {distance:.1f} km | {tempo_formatado(minutes)} | {len(planned_route['clients'])} pontos"
        folium.PolyLine(route_path["line"], color=color, weight=6, tooltip=resumo, popup=resumo, dash_array=None if route_path["real_route"] else "8, 8").add_to(mapa)
        legend_rows += f"<span style='color:{color};font-weight:900;'>▬</span> Rota {planned_route['number']} - {len(planned_route['clients'])} pontos<br>"
        for index, client in enumerate(planned_route["clients"], start=1):
            popup = f"Simulacao otimizada<br>Rota {planned_route['number']}<br>Parada {index}<br>{client.name}<br>Prioridade: {client.priority}<br>Janela: {client.delivery_window_start or '-'} - {client.delivery_window_end or '-'}<br>{format_coord(client.latitude)}, {format_coord(client.longitude)}"
            folium.Marker(
                location=[client.latitude, client.longitude],
                popup=popup,
                tooltip=f"Rota {planned_route['number']} | {index}. {client.name}",
                icon=folium.DivIcon(html=f"""
                <div style='width:28px;height:28px;border-radius:50%;background:{color};color:white;border:3px solid white;box-shadow:0 2px 8px rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;'>
                    {index}
                </div>
                """),
            ).add_to(mapa)

    legend = f"""
    <div style="position: fixed; bottom: 24px; left: 24px; z-index: 9999; background: white; padding: 10px 12px; border: 1px solid #d7e0ec; border-radius: 8px; font-family: Arial; font-size: 13px; box-shadow: 0 4px 14px rgba(0,0,0,.16);">
        <strong>Rotas simuladas</strong><br>
        {legend_rows}
        <small>Trajeto real quando o roteador estiver disponivel; pontilhado indica fallback.</small>
    </div>
    """
    mapa.get_root().html.add_child(folium.Element(legend))
    return render_template_string(mapa._repr_html_())


def parse_hhmm(value, default="08:00"):
    try:
        hour, minute = [int(part) for part in str(value or default).split(":")[:2]]
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    hour, minute = [int(part) for part in default.split(":")]
    return hour, minute


def hhmm_to_day_minutes(value, default="08:00"):
    hour, minute = parse_hhmm(value, default)
    return (hour * 60) + minute


def route_start_time(route):
    hour, minute = parse_hhmm(getattr(route, "start_time", None) or "08:00")
    return f"{hour:02d}:{minute:02d}"


def route_start_minutes(route):
    return hhmm_to_day_minutes(route_start_time(route))


def route_actual_start_time(route):
    value = getattr(route, "actual_start_time", None)
    if value:
        hour, minute = parse_hhmm(value, route_start_time(route))
        return f"{hour:02d}:{minute:02d}"
    return None


def route_actual_end_time(route):
    value = getattr(route, "actual_end_time", None)
    if value:
        hour, minute = parse_hhmm(value, route_completion_time(route))
        return f"{hour:02d}:{minute:02d}"
    return None


def minutes_to_hhmm(minutes, start_time="08:00"):
    hour, minute = parse_hhmm(start_time)
    base = datetime(2026, 1, 1, hour, minute)
    value = base + timedelta(minutes=int(round(minutes)))
    return value.strftime("%H:%M")


def stop_status_label(status):
    labels = {
        "pendente": "A fazer",
        "em_atendimento": "Em atendimento",
        "realizado": "Realizado",
    }
    return labels.get(status or "pendente", status or "A fazer")


def stop_status_color(status):
    colors = {
        "pendente": "#d92d20",
        "em_atendimento": "#fdb022",
        "realizado": "#16a34a",
    }
    return colors.get(status or "pendente", "#d92d20")


def route_breaks_by_stop(route):
    breaks = RouteBreak.query.filter_by(route_id=route.id, active=True).order_by(RouteBreak.id).all()
    grouped = {}
    requires_intraday_break = route_work_minutes_without_break(route) > INTRADAY_BREAK_REQUIRED_AFTER
    for item in breaks:
        if (item.break_type or "intrajornada") == "intrajornada" and not requires_intraday_break:
            continue
        grouped.setdefault(item.after_route_stop_id, []).append(item)
    return grouped


def stop_schedule(route, include_breaks=True):
    stops = sorted(route.stops, key=lambda item: item.sequence)
    speed = route.vehicle.average_speed_kmh if route.vehicle else 60
    start_time = route_start_time(route)
    start_day_minutes = route_start_minutes(route)
    current_minutes = 0
    last_point = [BASE_LATITUDE, BASE_LONGITUDE]
    rows = []
    breaks_by_stop = route_breaks_by_stop(route) if include_breaks else {}
    for stop in stops:
        point = [stop.client.latitude, stop.client.longitude]
        segment_km = distancia_reta_km([last_point, point])
        travel_minutes = (segment_km / speed) * 60 if speed else 0
        current_minutes += travel_minutes
        arrival_elapsed = current_minutes
        arrival_minutes = start_day_minutes + arrival_elapsed
        arrival = minutes_to_hhmm(arrival_elapsed, start_time)
        service_minutes = stop_service_minutes(stop)
        departure_elapsed = current_minutes + service_minutes
        departure_minutes = start_day_minutes + departure_elapsed
        departure = minutes_to_hhmm(departure_elapsed, start_time)
        breaks_after = []
        rows.append({
            "stop": stop,
            "arrival": arrival,
            "arrival_minutes": arrival_minutes,
            "arrival_elapsed": arrival_elapsed,
            "departure": departure,
            "departure_minutes": departure_minutes,
            "departure_elapsed": departure_elapsed,
            "segment_km": segment_km,
            "service_minutes": service_minutes,
            "breaks_after": breaks_after,
        })
        current_minutes += service_minutes
        for break_item in breaks_by_stop.get(stop.id, []):
            start_elapsed = current_minutes
            start_minutes = start_day_minutes + start_elapsed
            duration = max(1, int(break_item.duration_minutes or 60))
            end_elapsed = current_minutes + duration
            end_minutes = start_day_minutes + end_elapsed
            breaks_after.append({
                "break": break_item,
                "start": minutes_to_hhmm(start_elapsed, start_time),
                "start_minutes": start_minutes,
                "start_elapsed": start_elapsed,
                "end": minutes_to_hhmm(end_elapsed, start_time),
                "end_minutes": end_minutes,
                "end_elapsed": end_elapsed,
                "duration_minutes": duration,
            })
            current_minutes = end_elapsed
        last_point = point
    return rows


def resequence_route(route_id):
    stops = RouteStop.query.filter_by(route_id=route_id).order_by(RouteStop.sequence, RouteStop.id).all()
    for index, stop in enumerate(stops, start=1):
        stop.sequence = index


def route_options_for_company(company_id, selected_id=None):
    routes = sorted(LogisticRoute.query.filter_by(company_id=company_id).all(), key=route_sort_key)
    return "".join(
        f"<option value='{route.id}' {'selected' if route.id == selected_id else ''}>{route_display_name(route)} | {display_date(route.planned_date)} | {escape(route_free_label(route))}</option>"
        for route in routes
    )


def operational_route_table(routes, selected_route_id, selected_day=None):
    rows = ""
    for route in routes:
        active = "active" if route.id == selected_route_id else ""
        vehicle = route.vehicle.name if route.vehicle else "-"
        official_date = display_date(route.planned_date)
        color = route_color(route.id)
        day_arg = f"&data={selected_day}" if selected_day else ""
        rows += f"""
        <a class="route-pill {active}" href="/web/operacao?route_id={route.id}{day_arg}" style="border-left:6px solid {color};">
            <strong>{route_display_name(route)}</strong>
            <span>{vehicle} | {len(route.stops)} pontos | {route.status}</span>
            <small>Execucao: {official_date}</small>
            <small>Info: {escape(route_free_label(route))}</small>
        </a>
        """
    return rows


def route_display_name(route):
    name = (route.name or f"Rota {route.id}").strip()
    lowered = name.lower()
    if lowered.startswith("rota demo "):
        suffix = name.split()[-1]
        return f"ROTA {suffix}"
    return name.upper()


def timeline_window_for_routes(routes):
    points = []
    for route in routes:
        schedule = stop_schedule(route)
        points.append(route_start_minutes(route))
        points.append(route_completion_info(route, schedule)["day_minutes"])
        for item in schedule:
            points.append(item["arrival_minutes"])
            points.append(item["departure_minutes"])
            for break_info in item.get("breaks_after", []):
                points.append(break_info["start_minutes"])
                points.append(break_info["end_minutes"])
    if not points:
        return 7 * 60 + 30, 18 * 60, (18 * 60) - (7 * 60 + 30)

    start_minutes = max(0, (int(min(points)) // 30) * 30 - 30)
    end_minutes = min((24 * 60) - 1, ((int(max(points)) + 29) // 30) * 30 + 30)
    if end_minutes - start_minutes < 180:
        end_minutes = min((24 * 60) - 1, start_minutes + 180)
    return start_minutes, end_minutes, max(1, end_minutes - start_minutes)


def timeline_label_step(total_minutes):
    if total_minutes <= 6 * 60:
        return 30
    if total_minutes <= 12 * 60:
        return 60
    return 120


def timeline_grid_style(total_minutes, step_minutes):
    step_pct = max(2, (step_minutes / max(1, total_minutes)) * 100)
    return f"background:repeating-linear-gradient(90deg, #e5e7eb 0 1px, transparent 1px {step_pct:.4f}%);"


def all_routes_timeline(routes):
    if not routes:
        return "<div class='card'><h2>Visao ampla das rotas</h2><p class='muted'>Nenhuma rota disponivel para monitoramento.</p></div>"

    start_minutes, end_minutes, total_minutes = timeline_window_for_routes(routes)
    label_step = timeline_label_step(total_minutes)
    grid_style = timeline_grid_style(total_minutes, label_step)

    labels = ""
    current = start_minutes
    while current <= end_minutes:
        pos = ((current - start_minutes) / total_minutes) * 100
        labels += f"<span style='left:{pos:.2f}%;'>{current // 60:02d}:{current % 60:02d}</span>"
        current += label_step

    routes = sorted(routes, key=route_sort_key)
    all_alerts = predictive_alerts_for_routes(routes)
    alerts_by_route = {}
    for alert in all_alerts:
        alerts_by_route.setdefault(alert["route"].id, []).append(alert)
    timeline_counts = {
        "critico": sum(1 for item in all_alerts if item["level"] == "critico"),
        "atencao": sum(1 for item in all_alerts if item["level"] == "atencao"),
    }
    rows = ""
    for index, route in enumerate(routes, start=1):
        schedule = stop_schedule(route)
        route_alerts = alerts_by_route.get(route.id, [])
        critical_alerts = [item for item in route_alerts if item["level"] == "critico"]
        attention_alerts = [item for item in route_alerts if item["level"] == "atencao"]
        risky_stop_ids = {item.get("route_stop_id") for item in route_alerts if item.get("route_stop_id")}
        route_needs_help = bool(critical_alerts or attention_alerts)
        route_help_count = len(critical_alerts) + len(attention_alerts)
        route_help_title = " | ".join(
            f"{item['title']}: {item['detail']}" for item in (critical_alerts + attention_alerts)[:4]
        ) or "Sem alerta preditivo"
        route_help_level = "critico" if critical_alerts else "atencao"
        help_bell = ""
        if route_needs_help:
            help_label = "precisa de ajuda" if critical_alerts else "atencao"
            help_bell = f"""
            <a class="route-help-bell {route_help_level}" href="/web/operacao?route_id={route.id}" title="{escape(route_help_title)}">
                &#128276; <span>{route_help_count}</span>
            </a>
            <em>{help_label}</em>
            """
        route_label = f"{index}-{route_display_name(route)[:18]}"
        route_code = route_display_name(route)
        route_date = display_date(route.planned_date)
        route_line_color = route_color(route.id)
        manual_info = escape(route_free_label(route))
        work_status, work_text = route_work_status(route)
        route_done = (route.status or "").lower() in ("concluida", "concluída", "finalizada", "realizada")
        if not route_done and schedule:
            route_done = all((item["stop"].status or "").lower() == "realizado" for item in schedule)
        end_marker = "✓" if route_done else "■"
        end_marker_class = "done-icon" if route_done else "truck-icon"
        route_start_left = max(0, min(100, ((route_start_minutes(route) - start_minutes) / total_minutes) * 100))
        points = ""
        blocks = ""
        last_left = 4
        for item in schedule:
            stop = item["stop"]
            client = stop.client
            arrival_minutes = item["arrival_minutes"]
            departure_minutes = item["departure_minutes"]
            left = max(0, min(100, ((arrival_minutes - start_minutes) / total_minutes) * 100))
            width = max(1.2, ((departure_minutes - arrival_minutes) / total_minutes) * 100)
            status_color = stop_status_color(stop.status)
            service_color = "#d4a017" if (stop.status or "pendente") == "pendente" else status_color
            risk_class = " risk-stop" if stop.id in risky_stop_ids else ""
            risk_text = " | ALERTA: cliente pode ficar fora da janela" if stop.id in risky_stop_ids else ""
            points += f"""
            <a class="route-stop-pin{risk_class}" href="/web/operacao?route_id={route.id}&reorder_stop={stop.id}#reordenar-{stop.id}" style="left:{left:.2f}%;background:{status_color};" title="{stop.sequence}. {client.name} | Chegada {item['arrival']} | Saida {item['departure']}{risk_text} | Clique para alterar sequencia">{stop.sequence}<small>↕</small></a>
            """
            blocks += f"<span class='service-block' style='left:{left:.2f}%;width:{width:.2f}%;background:{service_color};' title='Atendimento {item['service_minutes']} min'></span>"
            for break_info in item.get("breaks_after", []):
                break_item = break_info["break"]
                break_left = max(0, min(100, ((break_info["start_minutes"] - start_minutes) / total_minutes) * 100))
                break_width = max(1.4, ((break_info["duration_minutes"]) / total_minutes) * 100)
                blocks += f"<span class='break-block' style='left:{break_left:.2f}%;width:{break_width:.2f}%;' title='{escape(break_item.title)} | {break_info['start']} - {break_info['end']} | {break_info['duration_minutes']} min'></span>"
            last_left = left
        completion_info = route_completion_info(route, schedule)
        completion_left = max(0, min(98, ((completion_info["day_minutes"] - start_minutes) / total_minutes) * 100)) if schedule else last_left
        route_line_left = min(route_start_left, completion_left)
        route_line_width = max(1.2, abs(completion_left - route_start_left))
        rows += f"""
        <div class="tower-row">
            <div class="tower-label"><strong>{route_label}</strong><span>{manual_info}</span><small>Execucao: {route_date}</small><small class="workday {work_status}">{work_text}</small>{help_bell}</div>
            <div class="tower-lane" style="{grid_style}">
                <span class="home-icon" style="left:{route_start_left:.2f}%;">⌂</span>
                <span class="route-green-line" style="left:{route_line_left:.2f}%;width:{route_line_width:.2f}%;background:{route_line_color};"></span>
                {blocks}
                {points}
                <span class="{end_marker_class}" style="left:{completion_left:.2f}%;" title="Retorno na BASE: {completion_info['time']}">{end_marker}</span>
                <span class="home-icon end-home" style="left:{completion_left:.2f}%;">⌂</span>
            </div>
        </div>
        """

    return f"""
    <div class="card route-monitor-card">
        <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px;">
            <div>
                <h2>Linha do tempo preditiva das rotas</h2>
                <p class="muted">Sininho vermelho indica rota que precisa de ajuda. Em preparacao, o alerta antecipa clientes com risco de ficar fora da janela do dia.</p>
            </div>
            <div class="timeline-risk-counters">
                <span class="critico"><b>{timeline_counts['critico']}</b> critico</span>
                <span class="atencao"><b>{timeline_counts['atencao']}</b> atencao</span>
            </div>
        </div>
        <div class="tower-scroll">
            <div class="tower-time"><div></div><div class="tower-axis" style="{grid_style}">{labels}</div></div>
            {rows}
        </div>
        <div class="tower-legend">
            <span><i style="background:#d92d20;"></i>A fazer</span>
            <span><i style="background:#fdb022;"></i>Em atendimento</span>
            <span><i style="background:#16a34a;"></i>Realizado</span>
            <span><i style="background:#d4a017;"></i>Tempo de atendimento</span>
            <span><i style="background:#7c3aed;"></i>Intervalo intrajornada</span>
            <span><i style="background:#d92d20;border-radius:50%;"></i>Sininho vermelho: rota precisa de ajuda</span>
        </div>
    </div>
    """


def time_text_to_minutes(value):
    if not value or ":" not in value:
        return None
    try:
        hour, minute = [int(part) for part in value.split(":")]
        return (hour * 60) + minute
    except ValueError:
        return None


def intraday_break_suggestion(route):
    schedule = stop_schedule(route, include_breaks=False)
    if not schedule:
        return None
    if route_work_minutes_without_break(route) <= INTRADAY_BREAK_REQUIRED_AFTER:
        return None
    window_start = INTRADAY_BREAK_WINDOW_START
    window_end = INTRADAY_BREAK_WINDOW_END
    start_time = route_start_time(route)
    candidates = [item for item in schedule if window_start <= item["departure_elapsed"] <= window_end]
    if not candidates:
        candidates = [item for item in schedule if item["departure_elapsed"] >= window_start]
    chosen = candidates[0] if candidates else schedule[-1]
    completion_minutes = schedule[-1]["departure_elapsed"]
    return {
        "stop": chosen["stop"],
        "recommended_start": minutes_to_hhmm(max(window_start, min(chosen["departure_elapsed"], window_end)), start_time),
        "window_start": minutes_to_hhmm(window_start, start_time),
        "window_end": minutes_to_hhmm(window_end, start_time),
        "journey_end_limit": minutes_to_hhmm(WORKDAY_LIMIT_MINUTES, start_time),
        "completion_minutes": completion_minutes,
        "over_limit": completion_minutes > WORKDAY_LIMIT_MINUTES,
    }


def predictive_alerts_for_routes(routes):
    now = datetime.now()
    now_minutes = (now.hour * 60) + now.minute
    alerts = []

    def add(level, route, title, detail, route_stop_id=None):
        alerts.append({
            "level": level,
            "route": route,
            "title": title,
            "detail": detail,
            "route_stop_id": route_stop_id,
        })

    for route in routes:
        status = (route.status or "").lower()
        active = status in ("assumida", "em_rota", "em rota", "iniciada")
        latest_telemetry = TelemetryEvent.query.filter_by(route_id=route.id).order_by(TelemetryEvent.captured_at.desc(), TelemetryEvent.id.desc()).first()

        if not route.driver_user_id:
            add("atencao", route, "Rota sem login de motorista", "Vincule um responsavel para permitir telemetria, comprovantes e execucao no celular.")

        if status in ("planejada", "planned", "preparacao", "preparação", "rascunho"):
            schedule_count = len(route.stops)
            if not route.vehicle:
                add("atencao", route, "Preparacao incompleta", "Rota ainda sem veiculo definido para estimar capacidade e tempo.")
            if schedule_count == 0:
                add("atencao", route, "Preparacao incompleta", "Rota ainda sem clientes/pontos para validar atendimento do dia.")

        work_minutes = route_work_minutes(route)
        work_minutes_without_break = route_work_minutes_without_break(route)
        has_intraday_break = RouteBreak.query.filter_by(route_id=route.id, break_type="intrajornada", active=True).first() is not None
        if work_minutes > WORKDAY_LIMIT_MINUTES:
            add("critico", route, "Jornada acima de 10h", f"Previsao {tempo_formatado(work_minutes)}. Dividir rota, retirar clientes ou reorganizar antes de liberar.")
        elif work_minutes >= WORKDAY_LIMIT_MINUTES - 45:
            add("atencao", route, "Jornada perto de 10h", f"Previsao {tempo_formatado(work_minutes)}. Rota bem carregada; validar risco de atraso.")
        if work_minutes_without_break > INTRADAY_BREAK_REQUIRED_AFTER and not has_intraday_break:
            add("atencao", route, "Intervalo intrajornada pendente", "Jornada acima de 6h exige 1h de intervalo entre a 4a e a 6a hora do inicio da jornada.")

        if active and not latest_telemetry:
            add("critico", route, "Rota ativa sem telemetria", "Motorista assumiu ou iniciou, mas nenhum ponto GPS chegou ao sistema.")
        elif active and latest_telemetry and latest_telemetry.captured_at:
            age_minutes = (now - latest_telemetry.captured_at).total_seconds() / 60
            if age_minutes > 30:
                add("critico", route, "Telemetria sem atualizacao", f"Ultimo sinal ha {int(age_minutes)} min. Verificar internet/celular do motorista.")
            elif age_minutes > 15:
                add("atencao", route, "Telemetria atrasada", f"Ultimo sinal ha {int(age_minutes)} min.")
            if latest_telemetry.battery_percent is not None and latest_telemetry.battery_percent <= 20:
                add("critico", route, "Bateria baixa do dispositivo", f"Bateria em {latest_telemetry.battery_percent}%. Risco de perder rastreio e comprovantes.")

        schedule = stop_schedule(route)
        for item in schedule:
            stop = item["stop"]
            client = stop.client
            stop_status = (stop.status or "pendente").lower()
            arrival_minutes = time_text_to_minutes(item["arrival"])
            window_start = time_text_to_minutes(client.delivery_window_start)
            window_end = time_text_to_minutes(client.delivery_window_end)

            if stop_status == "pendente" and window_end is not None and arrival_minutes is not None and arrival_minutes > window_end:
                add("critico", route, "Cliente pode ficar sem atendimento no dia", f"{client.name}: chegada prevista {item['arrival']} apos janela {client.delivery_window_end}. Replanejar um dia antes.", stop.id)
            elif stop_status == "pendente" and window_start is not None and now_minutes > window_start and status not in ("concluida", "finalizada"):
                add("atencao", route, "Janela ja iniciou", f"{client.name}: janela iniciou {client.delivery_window_start} e parada ainda esta pendente.", stop.id)

            if stop_status == "em_atendimento":
                arrival_proof = StopProof.query.filter_by(route_stop_id=stop.id, proof_type="chegada").order_by(StopProof.captured_at.desc(), StopProof.id.desc()).first()
                if arrival_proof and arrival_proof.captured_at:
                    service_minutes = (now - arrival_proof.captured_at).total_seconds() / 60
                    if service_minutes > 60:
                        add("critico", route, "Atendimento parado tempo demais", f"{client.name}: em atendimento ha {int(service_minutes)} min sem saida registrada.", stop.id)
                    elif service_minutes > 35:
                        add("atencao", route, "Atendimento acima do previsto", f"{client.name}: em atendimento ha {int(service_minutes)} min.", stop.id)

    return alerts


def predictive_alerts_panel(routes):
    alerts = predictive_alerts_for_routes(routes)

    order = {"critico": 0, "atencao": 1, "info": 2}
    alerts.sort(key=lambda item: (order.get(item["level"], 3), item["route"].name, item["title"]))
    counts = {
        "critico": sum(1 for item in alerts if item["level"] == "critico"),
        "atencao": sum(1 for item in alerts if item["level"] == "atencao"),
        "info": sum(1 for item in alerts if item["level"] == "info"),
    }
    color_by_level = {
        "critico": "#d92d20",
        "atencao": "#f79009",
        "info": "#1f5eff",
    }
    rows = ""
    for item in alerts[:12]:
        color = color_by_level.get(item["level"], "#1f5eff")
        rows += f"""
        <tr>
            <td><span class='risk-dot' style='background:{color};'></span><strong>{item['level'].upper()}</strong></td>
            <td><a href='/web/operacao?route_id={item['route'].id}'>{route_display_name(item['route'])}</a><br><span>{display_date(item['route'].planned_date)} | {route_free_label(item['route'])}</span></td>
            <td><strong>{item['title']}</strong><br><span>{item['detail']}</span></td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='3'>Sem alertas preditivos neste momento.</td></tr>"

    return f"""
    <div class="card predictive-card">
        <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;margin-bottom:12px;">
            <div>
                <h2>Torre de controle preditiva</h2>
                <p class="muted">Alertas automaticos para atraso, janela, telemetria, bateria e atendimento parado.</p>
            </div>
            <div class="risk-counters">
                <span><b>{counts['critico']}</b> critico</span>
                <span><b>{counts['atencao']}</b> atencao</span>
                <span><b>{counts['info']}</b> info</span>
            </div>
        </div>
        <div class="ops-table-wrap small">
            <table class="ops-table">
                <thead><tr><th>Risco</th><th>Rota</th><th>Alerta</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    """


@web_bp.route("/web/operacao")
@web_login_required
def operational_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))
    all_routes = sorted(scoped_query(LogisticRoute, user).all(), key=route_sort_key)
    selected_route_id = request.args.get("route_id", type=int)
    route_filter_id = request.args.get("filtro_rota", type=int)
    selected_route = None
    if selected_route_id:
        selected_route = LogisticRoute.query.get(selected_route_id)
        if selected_route and not can_access_route(user, selected_route):
            selected_route = None
    selected_day = operational_day_from_routes(all_routes, request.args.get("data"), selected_route)
    day_routes = [route for route in all_routes if route.planned_date == selected_day]
    routes = day_routes
    if route_filter_id:
        routes = [route for route in day_routes if route.id == route_filter_id]
        if not selected_route_id:
            selected_route_id = route_filter_id
    if selected_route and selected_route.planned_date != selected_day:
        selected_route = None
        selected_route_id = None
    if route_filter_id and selected_route and selected_route.id != route_filter_id:
        selected_route = None
    if not selected_route and routes:
        selected_route = routes[0]
        selected_route_id = selected_route.id

    route_rows = operational_route_table(routes, selected_route_id, selected_day)
    available_days = sorted({route.planned_date for route in all_routes if route.planned_date})
    day_options = "".join(
        f"<option value='{day}' {'selected' if day == selected_day else ''}>{display_date(day)}</option>"
        for day in available_days
    )
    if not day_options:
        day_options = f"<option value='{selected_day}'>{display_date(selected_day)}</option>"
    route_filter_options = "<option value=''>Todas as rotas do dia</option>" + "".join(
        f"<option value='{route.id}' {'selected' if route_filter_id == route.id else ''}>{escape(route_display_name(route))} | {escape(route_free_label(route))}</option>"
        for route in day_routes
    )
    filter_text = (
        f"Mostrando {route_display_name(selected_route)} em {display_date(selected_day)}."
        if route_filter_id and selected_route
        else f"Mostrando somente rotas com execucao em {display_date(selected_day)}."
    )
    day_selector = f"""
    <form method="get" action="/web/operacao" class="day-selector-card">
        <label>Dia operacional
            <input type="date" name="data" value="{selected_day}">
        </label>
        <label>Datas com rotas oficiais
            <select name="data_select" onchange="this.form.data.value=this.value">{day_options}</select>
        </label>
        <label>Filtrar rota
            <select name="filtro_rota">{route_filter_options}</select>
        </label>
        <button class="btn" type="submit">Filtrar</button>
        <span>{filter_text}</span>
    </form>
    """
    history_route_options = "".join(
        f"<option value='{route.id}' {'selected' if selected_route and route.id == selected_route.id else ''}>{escape(route_display_name(route))} | {display_date(route.planned_date)} | {escape(route_free_label(route))}</option>"
        for route in routes
    )
    if not history_route_options:
        history_route_options = "<option value=''>Nenhuma rota cadastrada</option>"
    history_lookup = f"""
    <details class="history-quick-card">
        <summary>
            <span>Consultar rota passada</span>
            <small>Data + rota para abrir o relatorio dos ultimos 90 dias</small>
        </summary>
        <div class="history-quick-content">
            <div>
                <h2>Consultar rota passada</h2>
                <p>Disponivel para perfis autorizados. Selecione a data da rota, escolha a rota e clique em <strong>Buscar historico</strong>.</p>
            </div>
            <form method="get" action="/web/relatorios" class="history-quick-form">
                <label>Data da rota
                    <input type="date" name="data">
                </label>
                <label>Rota
                    <select name="route_id">{history_route_options}</select>
                </label>
                <button class="btn" type="submit">Buscar historico</button>
                <a class="btn btn-light" href="/web/relatorios">Ver ultimos 90 dias</a>
            </form>
        </div>
    </details>
    """
    detail = "<div class='ops-empty'><h2>Nenhuma rota cadastrada.</h2><p>Crie uma rota para visualizar o painel operacional.</p></div>"
    if selected_route:
        schedule = stop_schedule(selected_route)
        selected_reorder_stop_id = request.args.get("reorder_stop", type=int)
        reorder_total = len(schedule)
        reason_options = reorder_reason_options()
        break_stop_options = route_stop_options(selected_route)
        route_requires_intraday_break = route_work_minutes_without_break(selected_route) > INTRADAY_BREAK_REQUIRED_AFTER
        break_suggestion = intraday_break_suggestion(selected_route)
        break_suggestion_text = "Esta rota nao ultrapassa 6h de trabalho; intervalo intrajornada nao e necessario."
        if route_requires_intraday_break:
            break_suggestion_text = "Sem pontos suficientes para sugerir intervalo entre a 4a e a 6a hora."
        break_suggestion_button = ""
        if break_suggestion:
            suggested_stop = break_suggestion["stop"]
            route_status_key, route_status_text = route_work_status(selected_route)
            limit_note = " Atencao: previsao ultrapassa 10h de jornada; ajuste a rota para ficar em ate 10h ou proximo disso." if break_suggestion["over_limit"] else f" Jornada prevista: {route_status_text}."
            break_suggestion_text = (
                f"Sugestao IA: inserir intervalo apos parada {suggested_stop.sequence} - {suggested_stop.client.name}, "
                f"preferencialmente entre {break_suggestion['window_start']} e {break_suggestion['window_end']}. "
                f"Fim limite operacional: {break_suggestion['journey_end_limit']}.{limit_note}"
            )
            break_suggestion_button = f"""
                <form method="post" action="/web/intervalos-rota">
                    <input type="hidden" name="action" value="ia">
                    <input type="hidden" name="route_id" value="{selected_route.id}">
                    <button class="mini-btn" type="submit">Inserir sugestao IA</button>
                </form>
            """
        existing_breaks = RouteBreak.query.filter_by(route_id=selected_route.id, active=True).order_by(RouteBreak.id).all()
        if not route_requires_intraday_break:
            existing_breaks = [item for item in existing_breaks if (item.break_type or "intrajornada") != "intrajornada"]
        break_rows = ""
        for break_item in existing_breaks:
            break_rows += f"""
            <form method="post" action="/web/intervalos-rota" class="break-edit-row">
                <input type="hidden" name="route_id" value="{selected_route.id}">
                <input type="hidden" name="break_id" value="{break_item.id}">
                <label>Apos parada</label><select name="after_route_stop_id">{route_stop_options(selected_route, break_item.after_route_stop_id)}</select>
                <label>Duracao</label><input name="duration_minutes" type="number" min="1" max="180" value="{break_item.duration_minutes}">
                <label>Titulo</label><input name="title" value="{escape(break_item.title or 'Intervalo intrajornada')}">
                <label>Observacao</label><input name="notes" value="{escape(break_item.notes or '')}">
                <button class="mini-btn" name="action" value="save" type="submit">Salvar</button>
                <button class="mini-btn danger" name="action" value="delete" type="submit">Remover</button>
            </form>
            """
        if not break_rows:
            break_rows = "<p class='muted'>Nenhum intervalo cadastrado para esta rota.</p>"
        break_panel = f"""
        <div class="ops-break-panel">
            <details>
                <summary>Intervalos e descanso da rota</summary>
                <p class="muted">{break_suggestion_text}</p>
                <div class="break-actions">{break_suggestion_button}</div>
                <form method="post" action="/web/intervalos-rota" class="break-create-row">
                    <input type="hidden" name="action" value="create">
                    <input type="hidden" name="route_id" value="{selected_route.id}">
                    <label>Apos parada</label><select name="after_route_stop_id">{break_stop_options}</select>
                    <label>Duracao</label><input name="duration_minutes" type="number" min="1" max="180" value="60">
                    <label>Titulo</label><input name="title" value="Intervalo intrajornada">
                    <label>Observacao</label><input name="notes" placeholder="Ex.: descanso, refeicao, pausa operacional">
                    <button class="mini-btn" type="submit">Adicionar intervalo</button>
                </form>
                <div class="break-list">{break_rows}</div>
            </details>
        </div>
        """
        completion_info = route_completion_info(selected_route, schedule)
        completion = completion_info["time"]
        transfer_options = route_options_for_company(selected_route.company_id)
        total_distance = sum(item["segment_km"] for item in schedule) + completion_info["return_km"]
        vehicle_name = selected_route.vehicle.name if selected_route.vehicle else "-"
        vehicle_speed = selected_route.vehicle.average_speed_kmh if selected_route.vehicle else 60
        latest_telemetry = TelemetryEvent.query.filter_by(route_id=selected_route.id).order_by(TelemetryEvent.captured_at.desc(), TelemetryEvent.id.desc()).first()
        mobile_battery = f"{latest_telemetry.battery_percent}%" if latest_telemetry and latest_telemetry.battery_percent is not None else "-"
        telemetry_time = latest_telemetry.captured_at.strftime("%d/%m %H:%M") if latest_telemetry and latest_telemetry.captured_at else "Sem telemetria"
        pending_count = sum(1 for item in schedule if (item["stop"].status or "pendente") == "pendente")
        service_count = sum(1 for item in schedule if (item["stop"].status or "") == "em_atendimento")
        done_count = sum(1 for item in schedule if (item["stop"].status or "") == "realizado")
        observations_count = StopObservation.query.filter_by(route_id=selected_route.id).count()
        available_clients = Client.query.filter_by(company_id=selected_route.company_id, active=True).order_by(Client.name).all()
        add_stop_client_options = "".join(
            f"<option value='{client.id}'>{escape(client.name)} | {format_coord(client.latitude)}, {format_coord(client.longitude)}</option>"
            for client in available_clients
        )
        add_stop_position_options = "".join(
            f"<option value='{index}' {'selected' if index == len(schedule) + 1 else ''}>{'Final da rota' if index == len(schedule) + 1 else f'Antes da parada {index}'}</option>"
            for index in range(1, len(schedule) + 2)
        )
        add_stop_panel = f"""
        <details class="add-service-stop-panel" id="novo-servico">
            <summary><span>Novo servico</span><small>Adicionar cliente/local nesta rota</small></summary>
            <form method="post" action="/web/parada/adicionar-servico">
                <input type="hidden" name="route_id" value="{selected_route.id}">
                <label>Cliente / local
                    <select name="client_id" required>{add_stop_client_options or "<option value=''>Cadastre um cliente primeiro</option>"}</select>
                </label>
                <label>Posicao na rota
                    <select name="target_sequence">{add_stop_position_options}</select>
                </label>
                <label>Tempo atendimento
                    <input name="service_minutes" type="number" min="1" max="240" value="{DEFAULT_SERVICE_MINUTES}">
                </label>
                <label class="full">Servico previsto
                    <textarea name="service_description" rows="2" placeholder="Ex.: coleta, entrega, manutencao, troca de equipamento, vistoria, retirada de assinatura"></textarea>
                </label>
                <button class="mini-btn" type="submit">Inserir parada na rota</button>
                <a class="mini-btn" href="/web/clientes">Cadastrar novo cliente/local</a>
            </form>
        </details>
        """
        session_date = datetime.now().strftime("%d/%m/%Y")
        official_execution_date = display_date(selected_route.planned_date)
        route_code = route_display_name(selected_route)
        selected_start_time = route_start_time(selected_route)
        selected_actual_start = route_actual_start_time(selected_route)
        selected_actual_end = route_actual_end_time(selected_route)
        track_start_minutes, track_end_minutes, track_total_minutes = timeline_window_for_routes(routes or [selected_route])

        def track_left(day_minutes):
            return max(0, min(100, ((day_minutes - track_start_minutes) / track_total_minutes) * 100))

        timeline_points = ""
        planned_start_left = track_left(route_start_minutes(selected_route))
        base_return_left = track_left(completion_info["day_minutes"]) if schedule else planned_start_left
        track_route_left = min(planned_start_left, base_return_left)
        track_route_width = max(1.2, abs(base_return_left - planned_start_left))
        timeline_points += f"""
        <div class="track-base-start" style="left:{planned_start_left:.2f}%;" title="Saida da BASE {selected_start_time}">
            <span>⌂</span>
            <small>{selected_start_time}</small>
        </div>
        """
        timeline_points += f"""
        <div class="track-route-event planned-start" style="left:{planned_start_left:.2f}%;" title="Inicio planejado {selected_start_time}">
            <span>INI</span>
            <small>{selected_start_time}</small>
        </div>
        """
        if selected_actual_start:
            actual_start_left = track_left(hhmm_to_day_minutes(selected_actual_start))
            timeline_points += f"""
            <div class="track-route-event actual-start" style="left:{actual_start_left:.2f}%;" title="Rota iniciada {selected_actual_start}">
                <span>GO</span>
                <small>{selected_actual_start}</small>
            </div>
            """
        for item in schedule:
            stop = item["stop"]
            point_left = track_left(item["arrival_minutes"])
            timeline_points += f"""
            <div class="track-point" style="left:{point_left:.2f}%;">
                <span style="background:{stop_status_color(stop.status)};">{stop.sequence}</span>
                <small>{item['arrival']}</small>
            </div>
            """
            for break_info in item.get("breaks_after", []):
                break_left = track_left(break_info["start_minutes"])
                break_width = max(3, (break_info["duration_minutes"] / track_total_minutes) * 100)
                timeline_points += f"""
                <div class="track-break" style="left:{break_left:.2f}%;width:{break_width:.2f}%;" title="Intervalo {break_info['start']} - {break_info['end']} | {break_info['duration_minutes']} min">
                    <span>INT</span>
                    <small>{break_info['start']}</small>
                </div>
                """
        if schedule:
            timeline_points += f"""
            <div class="track-base-return" style="left:{base_return_left:.2f}%;" title="Retorno na BASE {completion}">
                <span>⌂</span>
                <small>{completion}</small>
            </div>
            """
        if selected_actual_end:
            actual_end_left = track_left(hhmm_to_day_minutes(selected_actual_end))
            timeline_points += f"""
            <div class="track-route-event actual-end" style="left:{actual_end_left:.2f}%;" title="Rota finalizada {selected_actual_end}">
                <span>FIM</span>
                <small>{selected_actual_end}</small>
            </div>
            """
        stop_rows = ""
        stop_rows += f"""
        <tr>
            <td><span class="row-arrow">⌂</span></td>
            <td><strong>BASE</strong><br><span>Inicio da rota</span></td>
            <td>Base operacional</td>
            <td><span class="status-dot done"></span>Partida</td>
            <td>{selected_actual_start or selected_start_time}<br><span>{'Inicio real da rota' if selected_actual_start else 'Inicio planejado da rota'}</span></td>
            <td>-</td>
            <td>-</td>
        </tr>
        """
        for item in schedule:
            stop = item["stop"]
            client = stop.client
            status_color = stop_status_color(stop.status)
            service_description = stop_service_description(stop)
            service_display = escape(service_description) if service_description else "Servico nao informado"
            service_class = "service-filled" if service_description else "service-empty"
            proof_count = StopProof.query.filter_by(route_stop_id=stop.id).count()
            proof_badge = f"<br><a href='/web/fiscal?route_id={selected_route.id}&stop_id={stop.id}'>{proof_count} comprovante(s)</a>" if proof_count else ""
            break_hint = ""
            for break_info in item.get("breaks_after", []):
                break_item = break_info["break"]
                break_hint += f"<br><span class='break-inline'>Intervalo {break_info['start']} - {break_info['end']} ({break_info['duration_minutes']} min)</span>"
            reorder_open = "open" if selected_reorder_stop_id == stop.id else ""
            selected_reorder_class = " selected-reorder" if selected_reorder_stop_id == stop.id else ""
            reorder_cell = f"""
                <details class="transfer-box reorder-box" id="reordenar-{stop.id}" {reorder_open}>
                    <summary>Alterar sequencia</summary>
                    <form method="post" action="/web/reordenar-parada">
                        <input type="hidden" name="route_stop_id" value="{stop.id}">
                        <input type="hidden" name="return_to" value="operacao">
                        <label>Nova posicao</label>
                        <select name="target_sequence">{sequence_options(reorder_total, stop.sequence)}</select>
                        <label>Justificativa</label>
                        <select name="reason" required>{reason_options}</select>
                        <input name="details" placeholder="Ex.: cliente fechado, bloqueio da via, prioridade">
                        <button class="mini-btn" type="submit" name="direction" value="target">Salvar ordem</button>
                    </form>
                </details>
            """
            service_time_cell = f"""
                <details class="transfer-box service-time-box">
                    <summary>Tempo atendimento</summary>
                    <form method="post" action="/web/parada/tempo-atendimento">
                        <input type="hidden" name="route_stop_id" value="{stop.id}">
                        <input type="hidden" name="return_to" value="operacao">
                        <label>Minutos</label>
                        <input name="service_minutes" type="number" min="1" max="240" value="{item['service_minutes']}">
                        <input name="reason" placeholder="Motivo do ajuste">
                        <button class="mini-btn" type="submit">Salvar tempo</button>
                    </form>
                </details>
            """
            service_task_cell = f"""
                <details class="transfer-box service-task-box">
                    <summary>Servico da parada</summary>
                    <form method="post" action="/web/parada/servico">
                        <input type="hidden" name="route_stop_id" value="{stop.id}">
                        <label>Servico previsto</label>
                        <textarea name="service_description" rows="3" placeholder="Ex.: coletar equipamento, entregar volume, realizar manutencao, retirar assinatura">{escape(service_description)}</textarea>
                        <button class="mini-btn" type="submit">Salvar servico</button>
                    </form>
                </details>
            """
            transfer_cell = reorder_cell + service_time_cell + service_task_cell
            if can_see_admin(user):
                transfer_cell += f"""
                <details class="transfer-box">
                    <summary>Transferir</summary>
                    <form method="post" action="/web/transferir-parada">
                        <input type="hidden" name="route_stop_id" value="{stop.id}">
                        <input type="hidden" name="from_route_id" value="{selected_route.id}">
                        <select name="to_route_id">{transfer_options}</select>
                        <input name="reason" placeholder="Motivo">
                        <button class="mini-btn" type="submit">Salvar</button>
                    </form>
                </details>
                """
            stop_rows += f"""
            <tr class="{selected_reorder_class}">
                <td><span class="stop-number" style="background:{status_color};">{stop.sequence}</span></td>
                <td><strong>{client.name}</strong><br><span>{format_coord(client.latitude)}, {format_coord(client.longitude)}</span><br><em class="service-summary {service_class}">{service_display}</em></td>
                <td>{client.delivery_window_start or '-'} - {client.delivery_window_end or '-'}</td>
                <td><span class="status-dot" style="background:{status_color};"></span>{stop_status_label(stop.status)}<br><span>{client.priority}</span>{proof_badge}</td>
                <td>Chegada {item['arrival']}<br><span>Saida {item['departure']} | Atendimento {item['service_minutes']} min</span>{break_hint}</td>
                <td>{item['segment_km']:.1f} km</td>
                <td>{transfer_cell}</td>
            </tr>
            """
        stop_rows += f"""
        <tr>
            <td><span class="row-arrow">⌂</span></td>
            <td><strong>BASE</strong><br><span>Fim da rota</span></td>
            <td>Base operacional</td>
            <td><span class="status-dot pending"></span>Projetado</td>
            <td>{selected_actual_end or completion}<br><span>{'Rota finalizada' if selected_actual_end else 'Conclusao prevista'}</span></td>
            <td>{completion_info['return_km']:.1f} km<br><span>Retorno a base</span></td>
            <td>-</td>
        </tr>
        """
        observations = StopObservation.query.filter_by(route_id=selected_route.id).order_by(StopObservation.created_at.desc()).limit(8).all()
        obs_rows = "".join(
            f"<tr><td>{obs.created_at.strftime('%d/%m %H:%M') if obs.created_at else '-'}</td><td>{obs.observation_type}</td><td>{obs.description}</td><td>{format_coord(obs.latitude) if obs.latitude is not None else '-'}, {format_coord(obs.longitude) if obs.longitude is not None else '-'}</td></tr>"
            for obs in observations
        )
        detail = f"""
        <div class="ops-console">
            <div class="ops-toolbar">
                <div class="route-list">{route_rows}</div>
                <div class="ops-actions">
                    <form method="post" action="/web/rota/status" class="route-status-form">
                        <input type="hidden" name="route_id" value="{selected_route.id}">
                        <input type="hidden" name="action" value="start">
                        <input name="event_time" type="time" value="{selected_actual_start or selected_start_time}" required>
                        <button type="submit">Iniciar rota</button>
                    </form>
                    <form method="post" action="/web/rota/status" class="route-status-form finish">
                        <input type="hidden" name="route_id" value="{selected_route.id}">
                        <input type="hidden" name="action" value="finish">
                        <input name="event_time" type="time" value="{selected_actual_end or completion}" required>
                        <button type="submit">Finalizar rota</button>
                    </form>
                    <a class="primary-action" href="#novo-servico" data-open-details="novo-servico">+ Novo servico</a>
                    <a href="/web/operacao?route_id={selected_route.id}">Atualizar</a>
                    <a href="/web/mapa/{selected_route.id}" target="_blank">Abrir mapa</a>
                    <details class="ops-toolbox">
                        <summary>Ferramentas</summary>
                        <div class="ops-toolbox-panel">
                            <button type="button" data-view="balanced">Equilibrado</button>
                            <button type="button" data-view="map">Mapa maior</button>
                            <button type="button" data-view="list">Lista maior</button>
                            <button type="button" data-density="compact">Compacto</button>
                            <button type="button" data-swap="toggle">Inverter</button>
                            <form method="post" action="/web/reorganizar-rota-ia">
                                <input type="hidden" name="route_id" value="{selected_route.id}">
                                <input type="hidden" name="return_to" value="operacao">
                                <button type="submit">Reorganizar com IA</button>
                            </form>
                            <form method="post" action="/web/rota/inicio" class="start-time-form">
                                <input type="hidden" name="route_id" value="{selected_route.id}">
                                <label>Inicio planejado</label>
                                <input name="start_time" type="time" value="{selected_start_time}" required>
                                <button type="submit">Salvar inicio</button>
                            </form>
                        </div>
                    </details>
                </div>
            </div>
            <div class="ops-header">
                <div>
                    <strong>{selected_route.id}</strong>
                    <span>Data da sessao {session_date}</span>
                    <span>Execucao oficial {official_execution_date}</span>
                    <b>{selected_route.name} - {selected_route.company.name}</b>
                </div>
                <div class="route-state"><span class="status-dot pending"></span>{selected_route.status}</div>
            </div>
            <div class="ops-metrics">
                <div><strong>{datetime.now().strftime('%d/%m/%Y %H:%M')}</strong><span>Partida da rota</span></div>
                <div><strong>{pending_count}</strong><span>Pendentes</span></div>
                <div><strong>{service_count}</strong><span>Em atendimento</span></div>
                <div><strong>{done_count}</strong><span>Realizados</span></div>
                <div><strong>{observations_count}</strong><span>Eventos</span></div>
                <div><strong>{vehicle_name}</strong><span>{vehicle_speed} km/h</span></div>
                <div><strong>{completion}</strong><span>Conclusao projetada</span></div>
                <div><strong>{mobile_battery}</strong><span>Bateria dispositivo | {telemetry_time}</span></div>
            </div>
            <div class="ops-split">
                <section class="ops-left">
                    <div class="route-id"><strong>{route_code}</strong><span>{escape(route_free_label(selected_route))}</span></div>
                    {add_stop_panel}
                    <div class="ops-table-wrap">
                        <table class="ops-table">
                            <thead><tr><th></th><th>ID do cliente</th><th>Descricao do cliente</th><th>Status</th><th>Previsao</th><th>Trecho</th><th>Acao</th></tr></thead>
                            <tbody>{stop_rows}</tbody>
                        </table>
                    </div>
                    {break_panel}
                </section>
                <section class="ops-map">
                    <iframe src="/web/mapa/{selected_route.id}"></iframe>
                    <div class="route-track">
                        <div class="track-line">
                            <span class="track-route-line" style="left:{track_route_left:.2f}%;width:{track_route_width:.2f}%;background:{route_color(selected_route.id)};"></span>
                            {timeline_points}
                        </div>
                    </div>
                </section>
            </div>
        </div>
        """
    wide_timeline = all_routes_timeline(routes)
    monitor_switch = """
    <div class="monitor-mode-card">
        <div>
            <strong>Modo de acompanhamento</strong>
            <span>Escolha como acompanhar a operacao neste computador.</span>
        </div>
        <div class="monitor-mode-actions">
            <button type="button" data-monitor-mode="timeline">Linha do tempo</button>
            <button type="button" data-monitor-mode="map">Mapa e pontos</button>
            <button type="button" data-monitor-mode="complete">Completo</button>
        </div>
    </div>
    """
    body = f"""
    <style>
        .predictive-card {{ margin-bottom:16px; }}
        .history-quick-card {{ margin-bottom:16px; border:1px solid #cfd8e3; border-radius:8px; background:#fff; box-shadow:0 8px 24px rgba(16,24,40,.06); overflow:hidden; }}
        .history-quick-card summary {{ display:flex; justify-content:space-between; gap:12px; align-items:center; padding:14px 16px; cursor:pointer; color:#0b1f3a; font-weight:900; list-style:none; }}
        .history-quick-card summary::-webkit-details-marker {{ display:none; }}
        .history-quick-card summary:before {{ content:"▶"; color:#1849a9; margin-right:4px; }}
        .history-quick-card[open] summary:before {{ content:"▼"; }}
        .history-quick-card summary small {{ color:#667085; font-size:12px; font-weight:700; text-align:right; }}
        .history-quick-content {{ display:grid; grid-template-columns:minmax(250px, 1fr) minmax(360px, 1.8fr); gap:18px; align-items:end; padding:0 16px 16px; }}
        .history-quick-card h2 {{ margin:0 0 6px; }}
        .history-quick-card p {{ margin:0; color:#475467; }}
        .history-quick-form {{ display:grid; grid-template-columns:minmax(150px, .8fr) minmax(220px, 1fr) auto auto; gap:10px; align-items:end; }}
        .history-quick-form label {{ color:#0b1f3a; font-weight:800; }}
        .history-quick-form input,.history-quick-form select {{ width:100%; margin-top:6px; border:1px solid #cbd5e1; border-radius:7px; padding:10px; font:inherit; background:#fff; }}
        .day-selector-card {{ display:grid; grid-template-columns:minmax(145px,.6fr) minmax(190px,.8fr) minmax(240px,1fr) auto minmax(230px,1fr); gap:10px; align-items:end; margin-bottom:12px; padding:12px 14px; border:1px solid #cfd8e3; border-radius:8px; background:#fff; box-shadow:0 8px 24px rgba(16,24,40,.05); }}
        .day-selector-card label {{ color:#0b1f3a; font-weight:900; }}
        .day-selector-card input,.day-selector-card select {{ width:100%; box-sizing:border-box; margin-top:6px; border:1px solid #cbd5e1; border-radius:7px; padding:10px; font:inherit; background:#fff; }}
        .day-selector-card span {{ color:#475467; font-weight:800; line-height:1.35; }}
        .monitor-mode-card {{ display:flex; justify-content:space-between; gap:12px; align-items:center; margin-bottom:12px; padding:10px 14px; border:1px solid #cfd8e3; border-radius:8px; background:#fff; box-shadow:0 8px 24px rgba(16,24,40,.05); }}
        .monitor-mode-card strong {{ display:block; color:#0b1f3a; font-size:15px; }}
        .monitor-mode-card span {{ display:block; color:#667085; font-size:12px; margin-top:3px; }}
        .monitor-mode-actions {{ display:flex; gap:8px; flex-wrap:wrap; }}
        .monitor-mode-actions button {{ border:1px solid #cfd8e3; background:#fff; color:#1849a9; border-radius:7px; padding:9px 12px; font-weight:900; cursor:pointer; }}
        .monitor-mode-actions button.active {{ border-color:#1f5eff; background:#eef4ff; color:#0b3b91; box-shadow:inset 0 -3px 0 #1f5eff; }}
        body.monitor-timeline [data-monitor-panel="map"] {{ display:none; }}
        body.monitor-map [data-monitor-panel="timeline"] {{ display:none; }}
        .risk-counters,.timeline-risk-counters {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
        .risk-counters span {{ display:inline-flex; gap:6px; align-items:center; padding:7px 10px; border:1px solid #d7e0ec; border-radius:8px; background:#f8fafc; color:#475467; font-size:12px; font-weight:800; }}
        .risk-counters b,.timeline-risk-counters b {{ color:#172033; font-size:17px; }}
        .timeline-risk-counters span {{ display:inline-flex; gap:6px; align-items:center; padding:8px 11px; border:1px solid #d7e0ec; border-radius:8px; background:#f8fafc; color:#475467; font-size:12px; font-weight:900; }}
        .timeline-risk-counters .critico {{ color:#b42318; background:#fff1f3; border-color:#fda29b; }}
        .timeline-risk-counters .atencao {{ color:#b54708; background:#fffaeb; border-color:#fedf89; }}
        .risk-dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:7px; vertical-align:middle; }}
        .route-monitor-card {{ overflow:hidden; margin-bottom:16px; }}
        .tower-scroll {{ overflow:auto; border:1px solid #d7e0ec; background:#fff; min-height:260px; }}
        .tower-time,.tower-row {{ display:grid; grid-template-columns:150px minmax(1200px, 1fr); }}
        .tower-time {{ position:sticky; top:0; z-index:3; background:#fff; border-bottom:1px solid #98a2b3; height:38px; }}
        .tower-axis {{ position:relative; background:repeating-linear-gradient(90deg, #e5e7eb 0 1px, transparent 1px 6.17%); }}
        .tower-axis span {{ position:absolute; top:9px; transform:translateX(-50%); font-size:11px; font-weight:800; color:#172033; }}
        .tower-row {{ min-height:40px; border-bottom:1px solid #98a2b3; }}
        .tower-label {{ padding:7px 10px; border-right:1px solid #98a2b3; background:#f8fafc; font-size:12px; }}
        .tower-label strong {{ display:block; color:#172033; }}
        .tower-label span {{ display:block; color:#1849a9; font-size:10px; margin-top:4px; font-weight:800; }}
        .tower-lane {{ position:relative; background:repeating-linear-gradient(90deg, #e5e7eb 0 1px, transparent 1px 6.17%); }}
        .route-green-line {{ position:absolute; top:21px; height:3px; background:#0b8f20; border-radius:999px; }}
        .route-stop-pin {{ position:absolute; top:12px; transform:translateX(-50%); display:grid; place-items:center; min-width:20px; height:17px; border-radius:4px; color:#fff; border:2px solid #0811ad; background:#0811ad; font-size:10px; font-weight:800; text-decoration:none; z-index:2; }}
        .service-block {{ position:absolute; top:7px; height:8px; border-radius:2px; border:1px solid rgba(0,0,0,.35); z-index:1; }}
        .break-block {{ position:absolute; top:26px; height:8px; border-radius:3px; background:#7c3aed; border:1px solid #4c1d95; box-shadow:0 0 0 2px rgba(124,58,237,.12); z-index:2; }}
        .home-icon {{ position:absolute; top:14px; transform:translateX(-50%); color:#344054; font-weight:900; z-index:3; }}
        .home-icon.end-home {{ top:25px; }}
        .truck-icon {{ position:absolute; top:14px; transform:translateX(-50%); width:14px; height:14px; border-radius:3px; background:#d92d20; border:1px solid #7a271a; color:#d92d20; z-index:2; }}
        .done-icon {{ position:absolute; top:9px; transform:translateX(-50%); display:grid; place-items:center; width:20px; height:20px; border-radius:50%; background:#16a34a; color:white; border:2px solid white; font-size:13px; font-weight:900; box-shadow:0 2px 8px rgba(16,24,40,.22); z-index:3; }}
        .tower-legend {{ display:flex; gap:16px; flex-wrap:wrap; margin-top:10px; color:#667085; font-size:12px; }}
        .tower-legend span {{ display:inline-flex; align-items:center; gap:6px; }}
        .tower-legend i {{ display:inline-block; width:12px; height:12px; border-radius:3px; }}
        .ops-console {{ margin:0; background:#f5f7fb; border:1px solid #cfd8e3; min-height:calc(100vh - 170px); color:#172033; border-radius:8px; overflow:hidden; box-shadow:0 10px 30px rgba(16,24,40,.08); }}
        .ops-toolbar {{ min-height:58px; display:grid; grid-template-columns:minmax(320px,1fr) auto; align-items:center; gap:12px; padding:10px 12px; background:#fff; border-bottom:1px solid #cfd8e3; position:sticky; top:0; z-index:8; }}
        .route-list {{ display:flex; gap:8px; overflow-x:auto; padding:2px 2px 4px; scrollbar-width:thin; }}
        .route-pill {{ display:block; min-width:154px; max-width:190px; border:1px solid #d7e0ec; background:#fff; color:#1849a9; text-decoration:none; border-radius:7px; padding:8px 10px; box-shadow:0 1px 2px rgba(16,24,40,.04); }}
        .route-pill.active {{ border-color:#1f5eff; background:#eef4ff; box-shadow:inset 0 -3px 0 #1f5eff, 0 6px 18px rgba(31,94,255,.10); }}
        .route-pill strong {{ display:block; font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
        .route-pill span {{ display:block; color:#667085; font-size:10px; margin-top:3px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
        .route-pill small {{ display:block; color:#1849a9; font-size:10px; margin-top:3px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
        .ops-actions {{ display:flex; gap:7px; white-space:nowrap; align-items:center; flex-wrap:nowrap; justify-content:flex-end; }}
        .ops-actions form {{ margin:0; }}
        .ops-actions a,.ops-actions button,.mini-btn {{ border:1px solid #cfd8e3; background:#fff; color:#1849a9; text-decoration:none; border-radius:6px; padding:7px 10px; font-weight:800; font-size:12px; cursor:pointer; }}
        .ops-actions a:hover,.ops-actions button:hover,.ops-actions button.active {{ border-color:#1f5eff; background:#eef4ff; }}
        .ops-actions a.primary-action {{ background:#16a34a; border-color:#16a34a; color:#fff; box-shadow:0 8px 18px rgba(22,163,74,.18); }}
        .ops-actions a.primary-action:hover {{ background:#15803d; border-color:#15803d; color:#fff; }}
        .ops-toolbox {{ position:relative; }}
        .ops-toolbox > summary {{ list-style:none; border:1px solid #cfd8e3; background:#fff; color:#1849a9; border-radius:6px; padding:7px 10px; font-weight:900; font-size:12px; cursor:pointer; }}
        .ops-toolbox > summary::-webkit-details-marker {{ display:none; }}
        .ops-toolbox[open] > summary {{ border-color:#1f5eff; background:#eef4ff; }}
        .ops-toolbox-panel {{ position:absolute; right:0; top:calc(100% + 8px); width:300px; display:grid; gap:8px; padding:10px; border:1px solid #cfd8e3; border-radius:8px; background:#fff; box-shadow:0 18px 44px rgba(16,24,40,.18); z-index:20; }}
        .ops-toolbox-panel button,.ops-toolbox-panel a {{ width:100%; text-align:left; }}
        .ops-toolbox-panel .start-time-form {{ justify-content:space-between; }}
        .start-time-form {{ display:flex; gap:6px; align-items:center; padding:4px 6px; border:1px solid #cfd8e3; border-radius:7px; background:#fbfdff; }}
        .start-time-form label {{ font-size:11px; font-weight:900; color:#475467; }}
        .start-time-form input {{ width:86px; border:1px solid #cfd8e3; border-radius:5px; padding:6px; font-size:12px; }}
        .route-status-form {{ display:flex; gap:6px; align-items:center; padding:4px 6px; border:1px solid #bbf7d0; border-radius:7px; background:#f0fdf4; }}
        .route-status-form.finish {{ border-color:#fecaca; background:#fff1f2; }}
        .route-status-form input {{ width:86px; border:1px solid #cfd8e3; border-radius:5px; padding:6px; font-size:12px; }}
        .route-status-form button {{ background:#16a34a; color:#fff; border-color:#16a34a; }}
        .route-status-form.finish button {{ background:#d92d20; border-color:#d92d20; color:#fff; }}
        .ops-header {{ display:flex; justify-content:space-between; gap:12px; align-items:center; padding:9px 14px; background:#fbfdff; border-bottom:1px solid #d9e2ec; font-size:12px; }}
        .ops-header strong {{ margin-right:10px; }} .ops-header span {{ margin-right:12px; color:#475467; }}
        .route-state {{ font-weight:800; color:#172033; }}
        .ops-metrics {{ display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:1px; background:#d9e2ec; border-bottom:1px solid #cfd8e3; }}
        .ops-metrics div {{ background:#fff; padding:8px 10px; min-height:48px; }}
        .ops-metrics strong {{ display:block; font-size:13px; color:#172033; }}
        .ops-metrics span {{ display:block; color:#667085; font-size:11px; margin-top:5px; }}
        .ops-split {{ display:grid; grid-template-columns:minmax(430px, 43%) minmax(560px, 57%); min-height:calc(100vh - 305px); gap:0; }}
        .ops-console.view-map .ops-split {{ grid-template-columns:minmax(360px, 34%) minmax(650px, 66%); }}
        .ops-console.view-list .ops-split {{ grid-template-columns:minmax(680px, 60%) minmax(420px, 40%); }}
        .ops-console.swapped .ops-left {{ order:2; border-right:0; border-left:2px solid #cfd8e3; }}
        .ops-console.swapped .ops-map {{ order:1; }}
        .ops-left {{ background:#fff; border-right:2px solid #cfd8e3; overflow:hidden; display:flex; flex-direction:column; min-width:0; }}
        .route-id {{ padding:10px 14px; font-size:12px; border-bottom:1px solid #d9e2ec; background:#fbfdff; }}
        .route-id strong {{ display:block; color:#172033; }} .route-id span {{ color:#667085; }}
        .ops-table-wrap {{ overflow:auto; flex:1; }}
        .ops-table {{ width:100%; border-collapse:separate; border-spacing:0; min-width:700px; font-size:12px; }}
        .ops-table th {{ position:sticky; top:0; z-index:1; background:#344054; color:#fff; text-align:left; padding:7px 9px; font-size:10px; letter-spacing:0; }}
        .ops-table th:first-child {{ border-top-left-radius:0; }}
        .ops-table th:last-child {{ border-top-right-radius:0; }}
        .ops-table td {{ border-bottom:1px solid #e5e7eb; padding:8px 9px; vertical-align:top; background:#fff; }}
        .ops-table tr:nth-child(even) td {{ background:#f8fafc; }}
        .ops-table tr.selected-reorder td {{ background:#fff7ed; box-shadow:inset 0 0 0 999px rgba(247,144,9,.08); }}
        .ops-table td span {{ color:#667085; font-size:11px; }}
        .ops-table td .break-inline {{ color:#5b21b6; font-weight:900; }}
        .stop-number {{ display:inline-grid; place-items:center; width:24px; height:24px; border-radius:6px; color:#fff; font-weight:800; box-shadow:0 2px 8px rgba(16,24,40,.14); }}
        .row-arrow {{ color:#667085; font-weight:800; }}
        .status-dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; vertical-align:middle; }}
        .status-dot.pending {{ background:#d92d20; }} .status-dot.done {{ background:#16a34a; }}
        .transfer-box summary,.ops-forms summary {{ cursor:pointer; color:#1849a9; font-weight:800; }}
        .transfer-box form,.ops-forms form {{ display:grid; gap:6px; margin-top:8px; }}
        .transfer-box label {{ color:#0b1f3a; font-size:11px; font-weight:800; margin:0; }}
        .transfer-box select,.transfer-box input,.ops-forms select,.ops-forms input {{ border:1px solid #cfd8e3; border-radius:4px; padding:7px; font-size:12px; }}
        .transfer-box textarea {{ width:100%; min-height:68px; resize:vertical; border:1px solid #cfd8e3; border-radius:4px; padding:7px; font-size:12px; font-family:Arial,sans-serif; }}
        .service-summary {{ display:inline-block; margin-top:5px; padding:4px 6px; border-radius:5px; font-size:11px; font-style:normal; font-weight:800; line-height:1.25; }}
        .service-summary.service-filled {{ color:#1849a9; background:#eef4ff; border:1px solid #c7d7fe; }}
        .service-summary.service-empty {{ color:#667085; background:#f8fafc; border:1px dashed #cfd8e3; }}
        .reorder-box[open] {{ padding:6px; border:1px solid #fedf89; border-radius:6px; background:#fffaeb; }}
        .ops-forms {{ display:grid; gap:8px; padding:12px; border-top:1px solid #d9e2ec; background:#fbfdff; }}
        .ops-forms details {{ border:1px solid #d9e2ec; border-radius:6px; padding:10px; background:#fff; }}
        .ops-break-panel {{ padding:12px; border-top:1px solid #d9e2ec; background:#fbfdff; }}
        .ops-break-panel details {{ border:1px solid #d9e2ec; border-radius:6px; padding:10px; background:#fff; }}
        .ops-break-panel summary {{ cursor:pointer; color:#1849a9; font-weight:900; }}
        .add-service-stop-panel {{ border-bottom:1px solid #d9e2ec; background:#f8fbff; padding:8px 12px; }}
        .add-service-stop-panel summary {{ cursor:pointer; color:#1849a9; font-weight:900; list-style:none; display:flex; align-items:center; gap:8px; }}
        .add-service-stop-panel summary span {{ font-size:14px; }}
        .add-service-stop-panel summary small {{ color:#667085; font-size:11px; font-weight:800; }}
        .add-service-stop-panel summary:before {{ content:"+"; display:inline-grid; place-items:center; width:18px; height:18px; border-radius:50%; background:#eef4ff; color:#1849a9; border:1px solid #c7d7fe; }}
        .add-service-stop-panel[open] summary:before {{ content:"-"; }}
        .add-service-stop-panel form {{ display:grid; grid-template-columns:minmax(180px,1.4fr) minmax(120px,.75fr) minmax(110px,.6fr) auto auto; gap:8px; align-items:end; margin-top:10px; }}
        .add-service-stop-panel label {{ color:#0b1f3a; font-size:11px; font-weight:900; }}
        .add-service-stop-panel .full {{ grid-column:1 / span 3; }}
        .add-service-stop-panel select,.add-service-stop-panel input,.add-service-stop-panel textarea {{ width:100%; box-sizing:border-box; margin-top:4px; border:1px solid #cfd8e3; border-radius:5px; padding:7px; font-size:12px; font-family:Arial,sans-serif; background:#fff; }}
        .add-service-stop-panel textarea {{ resize:vertical; min-height:48px; }}
        .break-create-row,.break-edit-row {{ display:grid; grid-template-columns:1.2fr .55fr 1fr 1.2fr auto; gap:7px; align-items:end; margin-top:10px; }}
        .break-edit-row {{ grid-template-columns:1.2fr .55fr 1fr 1.2fr auto auto; border-top:1px solid #eef2f6; padding-top:10px; }}
        .break-create-row label,.break-edit-row label {{ color:#0b1f3a; font-size:11px; font-weight:800; }}
        .break-create-row input,.break-create-row select,.break-edit-row input,.break-edit-row select {{ width:100%; border:1px solid #cfd8e3; border-radius:4px; padding:7px; font-size:12px; }}
        .mini-btn.danger {{ color:#b42318; border-color:#fda29b; background:#fff1f3; }}
        .break-actions {{ margin:8px 0; }}
        .ops-table-wrap.small {{ max-height:210px; margin-top:8px; }}
        .ops-map {{ position:relative; background:#eef3f8; min-height:calc(100vh - 305px); min-width:0; overflow:hidden; }}
        .ops-map iframe {{ position:absolute; inset:0; width:100%; height:100%; min-height:100%; border:0; display:block; background:#eef3f8; }}
        .route-track {{ position:absolute; left:18px; right:18px; bottom:14px; height:42px; display:grid; grid-template-columns:1fr; align-items:center; gap:8px; background:rgba(255,255,255,.94); border:1px solid #cfd8e3; border-radius:8px; padding:5px 14px; box-shadow:0 8px 26px rgba(16,24,40,.16); backdrop-filter:blur(6px); z-index:5; }}
        .home-mark {{ display:grid; place-items:center; color:#172033; font-weight:800; }}
        .track-line {{ height:34px; background:transparent; position:relative; }}
        .track-route-line {{ position:absolute; top:50%; transform:translateY(-50%); height:4px; border-radius:999px; }}
        .track-point {{ position:absolute; top:50%; display:grid; justify-items:center; gap:2px; transform:translate(-50%,-18px); z-index:3; }}
        .track-point span {{ display:grid; place-items:center; width:17px; height:17px; border-radius:4px; color:#fff; font-size:10px; font-weight:800; }}
        .track-point small {{ font-size:10px; color:#172033; }}
        .track-break {{ position:absolute; top:50%; display:grid; justify-items:center; gap:2px; transform:translateY(8px); z-index:4; }}
        .track-break span {{ display:grid; place-items:center; width:100%; min-width:28px; height:14px; border-radius:4px; color:#fff; background:#7c3aed; border:1px solid #4c1d95; font-size:9px; font-weight:900; box-shadow:0 2px 8px rgba(76,29,149,.25); }}
        .track-break small {{ font-size:10px; color:#5b21b6; font-weight:900; }}
        .track-base-start,.track-base-return {{ position:absolute; top:50%; display:grid; justify-items:center; gap:2px; transform:translate(-50%,4px); z-index:4; }}
        .track-base-start span,.track-base-return span {{ display:grid; place-items:center; width:18px; height:18px; border-radius:50%; color:#344054; background:#fff; border:1px solid #cfd8e3; font-size:12px; font-weight:900; box-shadow:0 2px 8px rgba(16,24,40,.12); }}
        .track-base-start small,.track-base-return small {{ font-size:10px; color:#172033; font-weight:900; }}
        .track-route-event {{ position:absolute; top:50%; display:grid; justify-items:center; gap:2px; transform:translate(-50%,-38px); z-index:5; }}
        .track-route-event span {{ display:grid; place-items:center; min-width:28px; height:17px; border-radius:999px; color:#fff; background:#1849a9; border:2px solid #fff; font-size:9px; font-weight:900; box-shadow:0 2px 8px rgba(16,24,40,.22); }}
        .track-route-event small {{ font-size:10px; color:#172033; font-weight:900; }}
        .track-route-event.actual-start span {{ background:#16a34a; }}
        .track-route-event.actual-end {{ transform:translate(-50%,19px); }}
        .track-route-event.actual-end span {{ background:#d92d20; }}
        .ops-empty {{ background:#fff; border:1px solid #cfd8e3; padding:28px; }}
        .ops-console.compact .ops-metrics div {{ min-height:46px; padding:7px 10px; }}
        .ops-console.compact .ops-table td {{ padding:6px 8px; }}
        .ops-console.compact .ops-table th {{ padding:6px 8px; }}
        .ops-console.compact .route-id {{ padding:7px 10px; }}
        .ops-console.compact .ops-forms {{ padding:8px; }}
        .ops-console.compact .ops-break-panel {{ padding:8px; }}
        .ops-console.compact .route-track {{ height:44px; bottom:8px; }}
        @media (max-width:1100px) {{
            .history-quick-content,.history-quick-form {{ grid-template-columns:1fr; }}
            .day-selector-card {{ grid-template-columns:1fr; }}
            .monitor-mode-card {{ align-items:flex-start; flex-direction:column; }}
            .history-quick-card summary {{ align-items:flex-start; flex-direction:column; }}
            .history-quick-card summary small {{ text-align:left; }}
            .tower-time,.tower-row {{ grid-template-columns:120px minmax(950px, 1fr); }}
            .ops-metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
            .ops-split {{ grid-template-columns:1fr; }}
            .ops-left {{ border-right:0; border-bottom:2px solid #9aa7b5; }}
            .ops-map iframe,.ops-map {{ min-height:520px; }}
            .break-create-row,.break-edit-row {{ grid-template-columns:1fr; }}
        }}
    </style>
    {history_lookup}
    {day_selector}
    {monitor_switch}
    <section data-monitor-panel="timeline">{wide_timeline}</section>
    <section data-monitor-panel="map">{detail}</section>
    <script>
        (function () {{
            const monitorKey = 'ris.ops.monitor.v2';
            const savedMonitor = localStorage.getItem(monitorKey) || 'map';

            function applyMonitor() {{
                document.body.classList.remove('monitor-timeline', 'monitor-map', 'monitor-complete');
                const mode = localStorage.getItem(monitorKey) || 'complete';
                document.body.classList.add('monitor-' + mode);
                document.querySelectorAll('[data-monitor-mode]').forEach(btn => btn.classList.toggle('active', btn.dataset.monitorMode === mode));
            }}

            localStorage.setItem(monitorKey, savedMonitor);
            applyMonitor();
            document.querySelectorAll('[data-monitor-mode]').forEach(btn => btn.addEventListener('click', function () {{
                localStorage.setItem(monitorKey, this.dataset.monitorMode);
                applyMonitor();
            }}));

            const root = document.querySelector('.ops-console');
            if (!root) return;
            const savedView = localStorage.getItem('ris.ops.view') || 'balanced';
            const savedDensity = localStorage.getItem('ris.ops.density') || 'comfort';
            const savedSwap = localStorage.getItem('ris.ops.swap') === 'true';

            function apply() {{
                root.classList.remove('view-map', 'view-list', 'compact', 'swapped');
                if (localStorage.getItem('ris.ops.view') === 'map') root.classList.add('view-map');
                if (localStorage.getItem('ris.ops.view') === 'list') root.classList.add('view-list');
                if (localStorage.getItem('ris.ops.density') === 'compact') root.classList.add('compact');
                if (localStorage.getItem('ris.ops.swap') === 'true') root.classList.add('swapped');
                document.querySelectorAll('[data-view]').forEach(btn => btn.classList.toggle('active', btn.dataset.view === (localStorage.getItem('ris.ops.view') || 'balanced')));
                document.querySelectorAll('[data-density]').forEach(btn => btn.classList.toggle('active', localStorage.getItem('ris.ops.density') === 'compact'));
                document.querySelectorAll('[data-swap]').forEach(btn => btn.classList.toggle('active', localStorage.getItem('ris.ops.swap') === 'true'));
            }}

            localStorage.setItem('ris.ops.view', savedView);
            localStorage.setItem('ris.ops.density', savedDensity);
            localStorage.setItem('ris.ops.swap', savedSwap ? 'true' : 'false');
            apply();

            document.querySelectorAll('[data-view]').forEach(btn => btn.addEventListener('click', function () {{
                localStorage.setItem('ris.ops.view', this.dataset.view);
                apply();
            }}));
            document.querySelectorAll('[data-density]').forEach(btn => btn.addEventListener('click', function () {{
                const compact = localStorage.getItem('ris.ops.density') === 'compact';
                localStorage.setItem('ris.ops.density', compact ? 'comfort' : 'compact');
                apply();
            }}));
            document.querySelectorAll('[data-swap]').forEach(btn => btn.addEventListener('click', function () {{
                const swapped = localStorage.getItem('ris.ops.swap') === 'true';
                localStorage.setItem('ris.ops.swap', swapped ? 'false' : 'true');
                apply();
            }}));

            document.querySelectorAll('[data-open-details]').forEach(link => link.addEventListener('click', function (event) {{
                const panel = document.getElementById(this.dataset.openDetails);
                if (!panel) return;
                event.preventDefault();
                panel.open = true;
                panel.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                const firstField = panel.querySelector('select, input, textarea');
                if (firstField) setTimeout(() => firstField.focus(), 250);
            }}));
        }})();
    </script>
    """
    return layout("Gestao operacional", body, "operacao")


@web_bp.route("/web/transferir-parada", methods=["POST"])
@web_login_required
def transfer_stop_page():
    user = request.web_user
    if not can_see_admin(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso disponivel a partir da gerencia.</h2></div>", "operacao"), 403
    stop = RouteStop.query.get_or_404(request.form.get("route_stop_id", type=int))
    from_route = LogisticRoute.query.get_or_404(request.form.get("from_route_id", type=int))
    to_route = LogisticRoute.query.get_or_404(request.form.get("to_route_id", type=int))
    if not can_access_route(user, from_route) or not can_access_route(user, to_route) or from_route.company_id != to_route.company_id:
        return layout("Acesso negado", "<div class='card'><h2>Transferencia invalida para outra empresa.</h2></div>", "operacao"), 403
    if stop.route_id != from_route.id or from_route.id == to_route.id:
        return redirect(url_for("web_bp.operational_page", route_id=from_route.id))
    old_route_id = from_route.id
    stop.route_id = to_route.id
    stop.sequence = len(to_route.stops) + 1
    from database.database import db
    db.session.add(RouteTransfer(company_id=from_route.company_id, route_stop_id=stop.id, from_route_id=from_route.id, to_route_id=to_route.id, reason=request.form.get("reason"), created_by_user_id=user.id))
    resequence_route(old_route_id)
    resequence_route(to_route.id)
    db.session.commit()
    return redirect(url_for("web_bp.operational_page", route_id=to_route.id))


@web_bp.route("/web/observacoes", methods=["POST"])
@web_login_required
def create_observation_page():
    user = request.web_user
    route = LogisticRoute.query.get_or_404(request.form.get("route_id", type=int))
    if is_driver(user) and route.driver_user_id != user.id:
        return layout("Acesso negado", "<div class='card'><h2>Esta rota nao pertence ao seu login.</h2></div>", "motorista"), 403
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Rota de outra empresa.</h2></div>", "operacao"), 403
    latitude = request.form.get("latitude")
    longitude = request.form.get("longitude")
    from database.database import db
    db.session.add(StopObservation(company_id=route.company_id, route_id=route.id, observation_type=request.form.get("observation_type"), description=request.form.get("description"), latitude=round(float(latitude), 6) if latitude else None, longitude=round(float(longitude), 6) if longitude else None, created_by_user_id=user.id))
    db.session.commit()
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))
    return redirect(url_for("web_bp.operational_page", route_id=route.id))


@web_bp.route("/web/locais", methods=["GET", "POST"])
@web_login_required
def manual_locations_page():
    user = request.web_user
    if not can_see_admin(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso disponivel a partir da gerencia.</h2></div>", "locais"), 403
    from database.database import db
    if request.method == "POST":
        company_id = request.form.get("company_id", type=int) if user.is_master else user.company_id
        db.session.add(ManualLocation(company_id=company_id, name=request.form.get("name"), location_type=request.form.get("location_type"), latitude=round(float(request.form.get("latitude")), 6), longitude=round(float(request.form.get("longitude")), 6), notes=request.form.get("notes"), active=True))
        db.session.commit()
        return redirect(url_for("web_bp.manual_locations_page"))
    locations = scoped_query(ManualLocation, user).order_by(ManualLocation.created_at.desc()).all()
    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    company_options = "".join(f"<option value='{company.id}'>{company.name}</option>" for company in companies if company)
    rows = "".join(f"<tr><td>{location.name}</td><td>{location.company.name}</td><td>{location.location_type}</td><td>{format_coord(location.latitude)}, {format_coord(location.longitude)}</td><td>{location.notes or '-'}</td></tr>" for location in locations)
    body = f"""
    <div class='grid two'>
        <div class='card'><h2>Cadastrar oficina ou apoio</h2><p class='muted'>Descanso e refeicao sao controlados em Intervalos e descanso da rota, dentro da gestao operacional.</p><form method='post' class='grid'><label>Empresa</label><select name='company_id'>{company_options}</select><label>Nome</label><input name='name' required><label>Tipo</label><select name='location_type'><option value='oficina'>Oficina</option><option value='apoio'>Ponto de apoio</option><option value='posto'>Posto / abastecimento</option><option value='base_auxiliar'>Base auxiliar</option></select><label>Latitude</label><input name='latitude' required placeholder='-29.000000'><label>Longitude</label><input name='longitude' required placeholder='-53.000000'><label>Observacoes</label><input name='notes'><button class='btn' type='submit'>Salvar local</button></form></div>
        <div class='card'><h2>Locais cadastrados</h2><table class='table'><thead><tr><th>Nome</th><th>Empresa</th><th>Tipo</th><th>Coordenadas</th><th>Observacoes</th></tr></thead><tbody>{rows}</tbody></table></div>
    </div>
    """
    return layout("Oficinas e apoio", body, "locais")


@web_bp.route("/web/relatorios", methods=["GET", "POST"])
@web_login_required
def reports_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso disponivel para perfis autorizados.</h2></div>", "relatorios"), 403
    from database.database import db
    requested_route_id = request.args.get("route_id", type=int)
    company_id = request.form.get("company_id", type=int) if request.method == "POST" and user.is_master else user.company_id
    if request.method == "GET" and user.is_master:
        company_id = request.args.get("company_id", type=int)
        if requested_route_id and not company_id:
            requested_route = LogisticRoute.query.get(requested_route_id)
            company_id = requested_route.company_id if requested_route else None
    if user.is_master and not company_id:
        first_company = Company.query.order_by(Company.id).first()
        company_id = first_company.id if first_company else None
    if request.method == "POST" and company_id and can_see_admin(user):
        routes = scoped_query(LogisticRoute, user).filter_by(company_id=company_id).all()
        route_ids = [route.id for route in routes]
        observations = StopObservation.query.filter(StopObservation.route_id.in_(route_ids)).all() if route_ids else []
        transfers = RouteTransfer.query.filter(
            (RouteTransfer.from_route_id.in_(route_ids)) | (RouteTransfer.to_route_id.in_(route_ids))
        ).all() if route_ids else []
        summary = f"Rotas: {len(routes)} | Observacoes: {len(observations)} | Transferencias: {len(transfers)} | Gerado automaticamente."
        db.session.add(ReportSnapshot(company_id=company_id, title="Relatorio operacional", report_type="operacional_90_dias", summary=summary, created_by_user_id=user.id))
        db.session.commit()
        return redirect(url_for("web_bp.reports_page"))
    since = datetime.now() - timedelta(days=90)
    route_query = scoped_query(LogisticRoute, user)
    if company_id:
        route_query = route_query.filter_by(company_id=company_id)
    routes = route_query.order_by(LogisticRoute.created_at.desc(), LogisticRoute.id.desc()).all()
    selected_route_id = requested_route_id
    selected_route = next((route for route in routes if route.id == selected_route_id), None) if selected_route_id else (routes[0] if routes else None)
    selected_date = request.args.get("data", "").strip()
    day_start = None
    day_end = None
    date_error = ""
    if selected_date:
        try:
            day_start = datetime.strptime(selected_date, "%Y-%m-%d")
            day_end = day_start + timedelta(days=1)
        except ValueError:
            selected_date = ""
            date_error = "Data invalida. Use o campo de calendario."

    query = ReportSnapshot.query.filter(ReportSnapshot.created_at >= since)
    if not user.is_master:
        query = query.filter_by(company_id=user.company_id)
    elif company_id:
        query = query.filter_by(company_id=company_id)
    reports = query.order_by(ReportSnapshot.created_at.desc()).all()
    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    company_options = "".join(f"<option value='{company.id}' {'selected' if company and company.id == company_id else ''}>{company.name}</option>" for company in companies if company)
    route_options = "".join(
        f"<option value='{route.id}' {'selected' if selected_route and route.id == selected_route.id else ''}>{escape(route_display_name(route))} | {display_date(route.planned_date)} | {escape(route_free_label(route))}</option>"
        for route in routes
    )
    if not route_options:
        route_options = "<option value=''>Nenhuma rota cadastrada</option>"

    history_rows = "<tr><td colspan='5'>Selecione uma rota e, se desejar, uma data para pesquisar.</td></tr>"
    stop_rows = "<tr><td colspan='6'>Nenhuma rota selecionada.</td></tr>"
    telemetry_rows = "<tr><td colspan='5'>Nenhum ponto GPS encontrado para a pesquisa.</td></tr>"
    selected_route_summary = "Nenhuma rota selecionada."
    if selected_route:
        selected_route_summary = f"{route_display_name(selected_route)} | {display_date(selected_route.planned_date)} | {route_free_label(selected_route)} | {selected_route.company.name} | {selected_route.status}"
        observations_query = StopObservation.query.filter_by(route_id=selected_route.id).filter(StopObservation.created_at >= since)
        proofs_query = StopProof.query.filter_by(route_id=selected_route.id).filter(StopProof.captured_at >= since)
        transfers_query = RouteTransfer.query.filter(RouteTransfer.company_id == selected_route.company_id).filter(
            (RouteTransfer.from_route_id == selected_route.id) | (RouteTransfer.to_route_id == selected_route.id)
        ).filter(RouteTransfer.created_at >= since)
        telemetry_query = TelemetryEvent.query.filter_by(route_id=selected_route.id).filter(TelemetryEvent.captured_at >= since)
        if day_start and day_end:
            observations_query = observations_query.filter(StopObservation.created_at >= day_start, StopObservation.created_at < day_end)
            proofs_query = proofs_query.filter(StopProof.captured_at >= day_start, StopProof.captured_at < day_end)
            transfers_query = transfers_query.filter(RouteTransfer.created_at >= day_start, RouteTransfer.created_at < day_end)
            telemetry_query = telemetry_query.filter(TelemetryEvent.captured_at >= day_start, TelemetryEvent.captured_at < day_end)

        events = []
        for obs in observations_query.order_by(StopObservation.created_at.desc()).limit(80).all():
            events.append({
                "when": obs.created_at,
                "type": obs.observation_type or "Observacao",
                "description": obs.description or "-",
                "stop": obs.route_stop.client.name if obs.route_stop and obs.route_stop.client else "-",
                "place": f"{format_coord(obs.latitude)}, {format_coord(obs.longitude)}" if obs.latitude is not None and obs.longitude is not None else "-",
            })
        for proof in proofs_query.order_by(StopProof.captured_at.desc()).limit(80).all():
            proof_stop = proof.route_stop.client.name if proof.route_stop and proof.route_stop.client else "-"
            receiver = f" | Recebedor: {proof.receiver_name}" if proof.receiver_name else ""
            events.append({
                "when": proof.captured_at,
                "type": proof_type_label(proof.proof_type),
                "description": f"{proof.notes or 'Registro do motorista'}{receiver}",
                "stop": proof_stop,
                "place": f"{format_coord(proof.latitude)}, {format_coord(proof.longitude)}" if proof.latitude is not None and proof.longitude is not None else "-",
            })
        for transfer in transfers_query.order_by(RouteTransfer.created_at.desc()).limit(80).all():
            stop_name = transfer.route_stop.client.name if transfer.route_stop and transfer.route_stop.client else "-"
            events.append({
                "when": transfer.created_at,
                "type": "Transferencia de servico",
                "description": f"{transfer.from_route.name if transfer.from_route else '-'} -> {transfer.to_route.name if transfer.to_route else '-'} | {transfer.reason or '-'}",
                "stop": stop_name,
                "place": "-",
            })
        events.sort(key=lambda item: item["when"] or datetime.min, reverse=True)
        if events:
            history_rows = "".join(
                f"<tr><td>{event['when'].strftime('%d/%m/%Y %H:%M') if event['when'] else '-'}</td><td>{escape(event['type'])}</td><td>{escape(event['stop'])}</td><td>{escape(event['description'])}</td><td>{escape(event['place'])}</td></tr>"
                for event in events[:120]
            )
        else:
            history_rows = "<tr><td colspan='5'>Nenhum evento encontrado para esta rota/data.</td></tr>"

        stops = RouteStop.query.filter_by(route_id=selected_route.id).order_by(RouteStop.sequence, RouteStop.id).all()
        if stops:
            stop_rows = "".join(
                f"<tr><td>{stop.sequence}</td><td>{escape(stop.client.name if stop.client else '-')}</td><td>{escape(stop.client.address if stop.client and stop.client.address else '-')}</td><td>{escape(stop.planned_arrival or '-')}</td><td>{escape(stop_status_label(stop.status))}</td><td>{format_coord(stop.client.latitude) if stop.client else '-'}, {format_coord(stop.client.longitude) if stop.client else '-'}</td></tr>"
                for stop in stops
            )
        else:
            stop_rows = "<tr><td colspan='6'>Esta rota ainda nao possui pontos.</td></tr>"

        telemetry = telemetry_query.order_by(TelemetryEvent.captured_at.desc()).limit(40).all()
        if telemetry:
            telemetry_rows = "".join(
                f"<tr><td>{item.captured_at.strftime('%d/%m/%Y %H:%M') if item.captured_at else '-'}</td><td>{format_coord(item.latitude)}, {format_coord(item.longitude)}</td><td>{item.speed_kmh if item.speed_kmh is not None else '-'} km/h</td><td>{item.battery_percent if item.battery_percent is not None else '-'}%</td><td>{escape(item.signal_quality or '-')}</td></tr>"
                for item in telemetry
            )

    rows = "".join(f"<tr><td>{report.created_at.strftime('%d/%m/%Y %H:%M') if report.created_at else '-'}</td><td>{report.company.name}</td><td>{report.title}</td><td>{report.summary}</td></tr>" for report in reports)
    if not rows:
        rows = "<tr><td colspan='4'>Nenhum snapshot armazenado nos ultimos 90 dias.</td></tr>"
    admin_snapshot = ""
    if can_see_admin(user):
        admin_snapshot = f"""
        <div class='card'><h2>Gerar relatorio de 90 dias</h2><form method='post' style='display:flex;gap:10px;align-items:end;flex-wrap:wrap;'><div><label>Empresa</label><select name='company_id'>{company_options}</select></div><button class='btn' type='submit'>Gerar snapshot</button></form></div>
        """
    body = f"""
    <style>
        .history-filter {{ display:grid; grid-template-columns:repeat(4, minmax(180px, 1fr)); gap:12px; align-items:end; }}
        .history-filter label {{ font-weight:800; color:#0b1f3a; }}
        .history-filter input, .history-filter select {{ width:100%; margin-top:6px; border:1px solid #cbd5e1; border-radius:7px; padding:10px; font:inherit; }}
        .history-summary {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }}
        .history-summary span {{ padding:8px 10px; border:1px solid #d7e0ec; border-radius:8px; background:#f8fafc; color:#475467; font-size:12px; font-weight:800; }}
        @media (max-width: 900px) {{ .history-filter {{ grid-template-columns:1fr; }} }}
    </style>
    <div class='card'>
        <h2>Historico de rota</h2>
        <p class='muted'>Consulta disponivel para perfis autorizados, respeitando a empresa e a permissao de cada login. Escolha a data e a rota para pesquisar eventos, comprovantes, transferencias e pontos GPS dos ultimos 90 dias.</p>
        <form method='get' class='history-filter'>
            <div>
                <label>Empresa</label>
                <select name='company_id' {'disabled' if not user.is_master else ''}>{company_options}</select>
            </div>
            <div>
                <label>Data da pesquisa</label>
                <input type='date' name='data' value='{escape(selected_date)}'>
            </div>
            <div>
                <label>Rota</label>
                <select name='route_id'>{route_options}</select>
            </div>
            <button class='btn' type='submit'>Pesquisar historico</button>
        </form>
        <p class='muted'>{date_error}</p>
        <div class='history-summary'>
            <span>Rota selecionada: {escape(selected_route_summary)}</span>
            <span>Periodo: {'somente ' + datetime.strptime(selected_date, '%Y-%m-%d').strftime('%d/%m/%Y') if selected_date else 'ultimos 90 dias'}</span>
        </div>
    </div>
    <div class='card' style='margin-top:16px;'><h2>Eventos da rota</h2><div class='table-wrap'><table class='table'><thead><tr><th>Data</th><th>Tipo</th><th>Cliente/parada</th><th>Descricao</th><th>Local</th></tr></thead><tbody>{history_rows}</tbody></table></div></div>
    <div class='card' style='margin-top:16px;'><h2>Pontos da rota</h2><div class='table-wrap'><table class='table'><thead><tr><th>Ordem</th><th>Cliente</th><th>Endereco</th><th>Previsto</th><th>Status</th><th>Latitude/Longitude</th></tr></thead><tbody>{stop_rows}</tbody></table></div></div>
    <div class='card' style='margin-top:16px;'><h2>Telemetria da rota</h2><div class='table-wrap'><table class='table'><thead><tr><th>Data</th><th>Latitude/Longitude</th><th>Velocidade</th><th>Bateria</th><th>Sinal</th></tr></thead><tbody>{telemetry_rows}</tbody></table></div></div>
    {admin_snapshot}
    <div class='card' style='margin-top:16px;'><h2>Relatorios armazenados nos ultimos 90 dias</h2><table class='table'><thead><tr><th>Data</th><th>Empresa</th><th>Titulo</th><th>Resumo</th></tr></thead><tbody>{rows}</tbody></table></div>
    """
    return layout("Historico de rotas", body, "relatorios")









DRIVER_REORDER_REASONS = [
    "Janela de entrega mais urgente",
    "Cliente prioritario",
    "Cliente fechado",
    "Bloqueio da via",
    "Via bloqueada ou congestionamento",
    "Orientacao da gerencia",
    "Seguranca do motorista",
    "Necessidade operacional no trajeto",
    "Outro motivo justificado",
]


def reorder_reason_options():
    return "".join(f"<option value='{escape(reason)}'>{escape(reason)}</option>" for reason in DRIVER_REORDER_REASONS)


def sequence_options(total, current_sequence):
    return "".join(
        f"<option value='{index}' {'selected' if index == current_sequence else ''}>{index}</option>"
        for index in range(1, total + 1)
    )


def route_stop_options(route, selected_id=None):
    stops = RouteStop.query.filter_by(route_id=route.id).order_by(RouteStop.sequence, RouteStop.id).all()
    if not stops:
        return "<option value=''>Nenhuma parada disponivel</option>"
    return "".join(
        f"<option value='{stop.id}' {'selected' if stop.id == selected_id else ''}>Apos parada {stop.sequence} - {escape(stop.client.name if stop.client else '-')}</option>"
        for stop in stops
    )


def proof_upload_dir():
    folder = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "uploads", "proofs"))
    os.makedirs(folder, exist_ok=True)
    return folder


def onboarding_upload_dir():
    folder = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "uploads", "onboarding"))
    os.makedirs(folder, exist_ok=True)
    return folder


def proof_type_label(proof_type):
    labels = {
        "chegada": "Chegada no cliente",
        "saida": "Saida / comprovante",
    }
    return labels.get(proof_type or "", proof_type or "-")


def proof_photo_url(proof):
    if not proof.photo_filename:
        return "-"
    return f"<a class='btn btn-light' href='/web/comprovantes/arquivo/{proof.photo_filename}' target='_blank'>Foto</a>"


def fiscal_status_badge(proof):
    has_receiver = bool(proof.receiver_name or proof.receiver_document)
    has_fiscal = bool(getattr(proof, "invoice_number", None) or getattr(proof, "invoice_link", None) or getattr(proof, "invoice_file_filename", None))
    has_signature = bool(proof.signature_data)
    has_photo = bool(proof.photo_filename)
    if has_receiver and (has_fiscal or has_signature or has_photo):
        return "<span class='badge badge-success'>Comprovado</span>"
    if has_receiver or has_fiscal or has_signature or has_photo:
        return "<span class='badge badge-warning'>Parcial</span>"
    return "<span class='badge'>Pendente</span>"


def proof_invoice_url(proof):
    links = []
    if getattr(proof, "invoice_link", None):
        links.append(f"<a class='btn btn-light' href='{escape(proof.invoice_link)}' target='_blank'>Link NF</a>")
    if getattr(proof, "invoice_file_filename", None):
        links.append(f"<a class='btn btn-light' href='/web/comprovantes/arquivo/{proof.invoice_file_filename}' target='_blank'>Arquivo NF</a>")
    return " ".join(links) if links else "-"


def proof_detail_url(proof):
    return f"<a class='btn btn-light' href='/web/fiscal/comprovante/{proof.id}'>Detalhes</a>"


def driver_route_for(user):
    return LogisticRoute.query.filter_by(driver_user_id=user.id).order_by(LogisticRoute.id.desc()).first()


def driver_navigation_target(route):
    stops = sorted(route.stops, key=lambda item: item.sequence)
    for stop in stops:
        if (stop.status or "pendente").lower() != "realizado":
            return {
                "kind": "stop",
                "stop": stop,
                "label": f"{stop.sequence}. {stop.client.name}",
                "subtitle": f"Janela {stop.client.delivery_window_start or '-'} - {stop.client.delivery_window_end or '-'} | {format_coord(stop.client.latitude)}, {format_coord(stop.client.longitude)}",
                "lat": stop.client.latitude,
                "lon": stop.client.longitude,
            }
    return {
        "kind": "base",
        "stop": None,
        "label": "BASE - retorno e finalizacao da rota",
        "subtitle": f"{format_coord(BASE_LATITUDE)}, {format_coord(BASE_LONGITUDE)}",
        "lat": BASE_LATITUDE,
        "lon": BASE_LONGITUDE,
    }


def navigation_links(lat, lon, label):
    destination = f"{float(lat):.6f},{float(lon):.6f}"
    google = f"https://www.google.com/maps/dir/?api=1&destination={destination}&travelmode=driving"
    waze = f"https://waze.com/ul?ll={destination}&navigate=yes"
    return google, waze


def driver_navigation_panel(route, assumed):
    target = driver_navigation_target(route)
    google_url, waze_url = navigation_links(target["lat"], target["lon"], target["label"])
    disabled_note = "" if assumed else "<p class='muted'>Assuma a rota para liberar a navegacao ponto a ponto.</p>"
    button_attr = "" if assumed else "aria-disabled='true' onclick='return false;'"
    return f"""
    <div class='card driver-nav-card' style='margin-bottom:16px;'>
        <div>
            <span class='badge'>Proxima navegacao</span>
            <h2>{escape(target["label"])}</h2>
            <p class='muted'>{escape(target["subtitle"])}</p>
            {disabled_note}
        </div>
        <div class='driver-nav-actions'>
            <a class='btn nav-launch' data-nav='google' href='{google_url}' target='_blank' rel='noopener' {button_attr}>Abrir Google Maps</a>
            <a class='btn btn-light nav-launch' data-nav='waze' href='{waze_url}' target='_blank' rel='noopener' {button_attr}>Abrir Waze</a>
        </div>
    </div>
    """


def merchandise_summary_for_stop(stop):
    items = Merchandise.query.filter_by(route_stop_id=stop.id).all()
    if not items:
        return "<span class='muted'>Nenhuma mercadoria vinculada.</span>"
    total = len(items)
    delivered = len([item for item in items if item.status == "entregue"])
    divergent = len([item for item in items if item.status == "divergente"])
    pending = total - delivered
    badge = "badge-success" if pending == 0 and divergent == 0 else ("badge-danger" if divergent else "badge-warning")
    return f"<span class='badge {badge}'>{delivered}/{total} conferidas</span><br><span class='muted'>{pending} pendente(s) | {divergent} divergencia(s)</span>"


def driver_stop_rows(route):
    schedule = stop_schedule(route)
    reason_options = reorder_reason_options()
    rows = ""
    total = len(schedule)
    for item in schedule:
        stop = item["stop"]
        client = stop.client
        up_disabled = "disabled" if stop.sequence <= 1 else ""
        down_disabled = "disabled" if stop.sequence >= total else ""
        status_color = stop_status_color(stop.status)
        status_key = (stop.status or "pendente").lower()
        service_description = stop_service_description(stop)
        service_hint = f"<br><span class='muted'><strong>Servico:</strong> {escape(service_description)}</span>" if service_description else "<br><span class='muted'>Servico nao informado pela operacao.</span>"
        proofs_count = StopProof.query.filter_by(route_stop_id=stop.id).count()
        latest_proof = StopProof.query.filter_by(route_stop_id=stop.id).order_by(StopProof.captured_at.desc(), StopProof.id.desc()).first()
        proof_hint = f"<br><span class='muted'>{proofs_count} comprovante(s)</span>" if proofs_count else ""
        if latest_proof and latest_proof.captured_at:
            proof_hint += f"<br><span class='muted'>Ultimo: {proof_type_label(latest_proof.proof_type)} {latest_proof.captured_at.strftime('%H:%M')}</span>"
        merchandise_panel = f"""
            <details class='panel' style='min-width:260px;margin-top:8px;'>
                <summary><strong>Conferir mercadoria</strong><br>{merchandise_summary_for_stop(stop)}</summary>
                <form class='grid geo-form merch-scan-form' method='post' action='/motorista/mercadoria/conferir' enctype='multipart/form-data' style='margin-top:10px;'>
                    <input type='hidden' name='route_stop_id' value='{stop.id}'>
                    <input type='hidden' name='latitude' class='proof-latitude'>
                    <input type='hidden' name='longitude' class='proof-longitude'>
                    <label>Acao</label>
                    <select name='scan_action'>
                        <option value='coletado'>Coletar / carregar</option>
                        <option value='entregue'>Entregar</option>
                        <option value='divergente'>Registrar divergencia</option>
                    </select>
                    <label>Codigo de barras ou QR Code</label>
                    <input name='code' class='merch-code' required placeholder='Aponte a camera ou use leitor'>
                    <button class='btn btn-light scan-camera' type='button'>Ler com camera</button>
                    <video class='scanner-video' style='display:none;width:100%;border-radius:8px;border:1px solid #d7e0ec;' muted playsinline></video>
                    <label>Foto opcional</label><input name='photo' type='file' accept='image/*' capture='environment'>
                    <label>Observacao</label><input name='notes' placeholder='Avaria, falta, volume trocado...'>
                    <button class='btn' type='submit'>Salvar conferencia</button>
                </form>
            </details>
        """
        if status_key == "realizado":
            attendance_action = f"<span class='badge badge-success'>Concluido</span>{proof_hint}{merchandise_panel}"
        elif status_key == "em_atendimento":
            attendance_action = f"""
                <details class='panel' style='min-width:270px;'>
                    <summary><strong>Registrar saida / comprovante</strong></summary>
                    <form class='grid geo-form proof-form' method='post' action='/motorista/comprovante' enctype='multipart/form-data' style='margin-top:10px;'>
                        <input type='hidden' name='route_stop_id' value='{stop.id}'>
                        <input type='hidden' name='proof_type' value='saida'>
                        <input type='hidden' name='latitude' class='proof-latitude'>
                        <input type='hidden' name='longitude' class='proof-longitude'>
                        <label>Nome do recebedor</label><input name='receiver_name' required placeholder='Nome de quem recebeu'>
                        <label>Documento / identificacao</label><input name='receiver_document' placeholder='RG, CPF, matricula ou setor'>
                        <label>Numero da nota / documento fiscal</label><input name='invoice_number' placeholder='NF, pedido, OS ou chave'>
                        <label>Link da nota fiscal</label><input name='invoice_link' type='url' placeholder='https://...'>
                        <label>PDF/XML/DANFE da nota</label><input name='invoice_file' type='file' accept='.pdf,.xml,image/*,application/pdf,text/xml,application/xml'>
                        <label>Foto do comprovante / local</label><input name='photo' type='file' accept='image/*' capture='environment'>
                        <label>Assinatura</label>
                        <canvas class='signature-pad' width='260' height='88' style='width:100%;height:88px;border:1px solid #d7e0ec;border-radius:8px;background:white;touch-action:none;'></canvas>
                        <input type='hidden' name='signature_data' class='signature-data'>
                        <button class='btn btn-light clear-signature' type='button'>Limpar assinatura</button>
                        <label>Observacao</label><input name='notes' placeholder='Volume entregue, avaria, divergencia...'>
                        <button class='btn' type='submit'>Registrar saida</button>
                    </form>
                </details>
                {proof_hint}
                {merchandise_panel}
            """
        else:
            attendance_action = f"""
                <form class='geo-form' method='post' action='/motorista/comprovante' style='display:grid;gap:6px;min-width:210px;'>
                    <input type='hidden' name='route_stop_id' value='{stop.id}'>
                    <input type='hidden' name='proof_type' value='chegada'>
                    <input type='hidden' name='latitude' class='proof-latitude'>
                    <input type='hidden' name='longitude' class='proof-longitude'>
                    <button class='btn' type='submit'>Registrar chegada</button>
                </form>
                {proof_hint}
                {merchandise_panel}
            """
        rows += f"""
        <tr>
            <td><span style='display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;background:{status_color};color:white;font-weight:800;'>{stop.sequence}</span></td>
            <td><strong>{client.name}</strong><br><span class='muted'>{format_coord(client.latitude)}, {format_coord(client.longitude)}</span>{service_hint}</td>
            <td>{client.delivery_window_start or '-'} - {client.delivery_window_end or '-'}</td>
            <td><span class='badge'>{client.priority}</span><br><span class='muted'>{stop_status_label(stop.status)}</span></td>
            <td>Chegada {item['arrival']}<br><span class='muted'>Saida {item['departure']} | Atendimento {item['service_minutes']} min</span></td>
            <td>{attendance_action}</td>
            <td>
                <form method='post' action='/web/reordenar-parada' style='display:grid;gap:6px;min-width:230px;'>
                    <input type='hidden' name='route_stop_id' value='{stop.id}'>
                    <input type='hidden' name='return_to' value='driver'>
                    <label>Nova posicao</label>
                    <select name='target_sequence'>{sequence_options(total, stop.sequence)}</select>
                    <label>Justificativa</label>
                    <select name='reason' required>{reason_options}</select>
                    <input name='details' placeholder='Complemento da justificativa'>
                    <button class='btn' name='direction' value='target' type='submit'>Salvar sequencia</button>
                    <div style='display:grid;grid-template-columns:1fr 1fr;gap:6px;'>
                        <button class='btn btn-light' name='direction' value='up' {up_disabled}>Subir</button>
                        <button class='btn btn-light' name='direction' value='down' {down_disabled}>Descer</button>
                    </div>
                </form>
            </td>
        </tr>
        """
    return rows


def route_completion_time(route):
    return route_completion_info(route)["time"]


def route_completion_info(route, schedule=None):
    schedule = schedule if schedule is not None else stop_schedule(route)
    start_time = route_start_time(route)
    if not schedule:
        return {
            "time": start_time,
            "elapsed": 0,
            "day_minutes": route_start_minutes(route),
            "return_km": 0,
            "return_minutes": 0,
        }
    last = schedule[-1]
    if last.get("breaks_after"):
        departure_elapsed = last["breaks_after"][-1]["end_elapsed"]
    else:
        departure_elapsed = last["departure_elapsed"]
    last_point = [last["stop"].client.latitude, last["stop"].client.longitude]
    return_km = distancia_reta_km([last_point, [BASE_LATITUDE, BASE_LONGITUDE]])
    speed = route.vehicle.average_speed_kmh if route.vehicle else 60
    return_minutes = (return_km / speed) * 60 if speed else 0
    elapsed = departure_elapsed + return_minutes
    return {
        "time": minutes_to_hhmm(elapsed, start_time),
        "elapsed": elapsed,
        "day_minutes": route_start_minutes(route) + elapsed,
        "return_km": return_km,
        "return_minutes": return_minutes,
    }


def route_work_minutes(route):
    return int(route_completion_info(route)["elapsed"])


def route_work_minutes_without_break(route):
    return int(route_completion_info(route, stop_schedule(route, include_breaks=False))["elapsed"])


def route_work_status(route):
    minutes = route_work_minutes(route)
    if minutes > WORKDAY_LIMIT_MINUTES:
        return "acima_limite", f"{tempo_formatado(minutes)} | acima de 10h"
    if minutes >= WORKDAY_LIMIT_MINUTES - 45:
        return "proximo_limite", f"{tempo_formatado(minutes)} | proximo de 10h"
    return "dentro_limite", f"{tempo_formatado(minutes)} | dentro de 10h"


def timeline_html(route):
    schedule = stop_schedule(route)
    if not schedule:
        return "<p class='muted'>Sem pontos para montar linha do tempo.</p>"
    items = ""
    for item in schedule:
        stop = item["stop"]
        client = stop.client
        color = stop_status_color(stop.status)
        items += f"""
        <div style='display:grid;grid-template-columns:34px 1fr;gap:10px;margin-bottom:14px;'>
            <div style='width:28px;height:28px;border-radius:50%;background:{color};color:white;display:grid;place-items:center;font-weight:800;'>{stop.sequence}</div>
            <div style='border-left:3px solid {color};padding-left:12px;'>
                <strong>{client.name}</strong><br>
                <span class='muted'>Chegada {item['arrival']} | Saida {item['departure']} | Atendimento {item['service_minutes']} min | {stop_status_label(stop.status)}</span>
            </div>
        </div>
        """
    return items


def numbered_marker(stop, client):
    color = stop_status_color(stop.status)
    return folium.Marker(
        location=[client.latitude, client.longitude],
        popup=(
            f"{client.name}<br>Parada {stop.sequence}<br>"
            f"Status: {stop_status_label(stop.status)}<br>"
            f"Janela: {client.delivery_window_start or '-'} - {client.delivery_window_end or '-'}<br>"
            f"Prioridade: {client.priority}<br>"
            f"Coordenadas: {format_coord(client.latitude)}, {format_coord(client.longitude)}<br>"
            f"Chegada planejada: {stop.planned_arrival or '-'}<br>"
            f"Tempo atendimento previsto: {stop_service_minutes(stop)} min"
        ),
        tooltip=f"{stop.sequence}. {client.name} - {stop_status_label(stop.status)}",
        icon=folium.DivIcon(
            html=f"""
            <div style='width:28px;height:28px;border-radius:50%;background:{color};color:white;border:3px solid white;box-shadow:0 2px 8px rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;'>
                {stop.sequence}
            </div>
            """
        ),
    )




@web_bp.route("/motorista")
@web_login_required
def driver_page():
    user = request.web_user
    if not is_driver(user):
        return redirect(url_for("web_bp.operational_page"))
    route = driver_route_for(user)
    if not route:
        return layout("Minha rota", "<div class='card'><h2>Nenhuma rota atribuida ao seu usuario.</h2><p class='muted'>Fale com a gerencia para vincular uma rota ao seu login.</p></div>", "motorista")

    assumed = (route.status or "").lower() in ("assumida", "em_rota", "em rota", "iniciada")
    action_panel = f"""
    <div class='card' style='margin-bottom:16px;'>
        <h2>Sequencia da rota</h2>
        <div class='grid two'>
            <div class='panel'>
                <strong>1. Visualizar rota</strong>
                <p class='muted'>Confira mapa, pontos, janelas de atendimento e ordem prevista.</p>
                <a class='btn btn-light' href='/web/mapa/{route.id}' target='_blank'>Visualizar rota</a>
            </div>
            <div class='panel'>
                <strong>2. Assumir rota</strong>
                <p class='muted'>Confirme que voce e o responsavel pela execucao desta rota.</p>
                <form method='post' action='/motorista/assumir'>
                    <button class='btn' type='submit' {'disabled' if assumed else ''}>{'Rota assumida' if assumed else 'Assumir rota'}</button>
                </form>
            </div>
            <div class='panel'>
                <strong>3. Reorganizar com IA</strong>
                <p class='muted'>Sugere a melhor sequencia pelo horario, prioridade e distancia dos pontos pendentes.</p>
                <form method='post' action='/web/reorganizar-rota-ia'>
                    <input type='hidden' name='route_id' value='{route.id}'>
                    <input type='hidden' name='return_to' value='driver'>
                    <button class='btn btn-light' type='submit' {'disabled' if not assumed else ''}>Reorganizar com IA</button>
                </form>
            </div>
        </div>
    </div>
    """

    rows = driver_stop_rows(route) if assumed else ""
    observations = StopObservation.query.filter_by(route_id=route.id).order_by(StopObservation.created_at.desc()).limit(6).all()
    obs_rows = "".join(
        f"<tr><td>{obs.created_at.strftime('%d/%m %H:%M') if obs.created_at else '-'}</td><td>{obs.observation_type}</td><td>{obs.description}</td></tr>"
        for obs in observations
    )
    proofs = StopProof.query.filter_by(route_id=route.id).order_by(StopProof.captured_at.desc(), StopProof.id.desc()).limit(10).all()
    proof_rows = "".join(
        f"<tr><td>{proof.captured_at.strftime('%d/%m %H:%M') if proof.captured_at else '-'}</td><td>{proof_type_label(proof.proof_type)}</td><td>{proof.route_stop.client.name if proof.route_stop and proof.route_stop.client else '-'}</td><td>{escape(proof.receiver_name or '-')}</td><td>{escape(getattr(proof, 'invoice_number', None) or '-')}</td><td>{proof_photo_url(proof)} {proof_invoice_url(proof)} {proof_detail_url(proof)}</td></tr>"
        for proof in proofs
    )
    if not proof_rows:
        proof_rows = "<tr><td colspan='6'>Nenhum comprovante registrado ainda.</td></tr>"
    body = f"""
    <style>
        .driver-nav-card {{
            display:grid;
            grid-template-columns:minmax(260px,1fr) auto;
            gap:16px;
            align-items:center;
            border-left:5px solid #1f5eff;
        }}
        .driver-nav-card h2 {{ margin:8px 0 4px; }}
        .driver-nav-actions {{ display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; }}
        .driver-nav-actions a[aria-disabled='true'] {{ opacity:.55; pointer-events:none; }}
        @media(max-width:720px) {{
            .driver-nav-card {{ grid-template-columns:1fr; }}
            .driver-nav-actions {{ justify-content:stretch; }}
            .driver-nav-actions .btn {{ width:100%; text-align:center; }}
        }}
    </style>
    {driver_navigation_panel(route, assumed)}
    {action_panel}
    <div class='grid two'>
        <div class='card'>
            <h2>{route.name}</h2>
            <p class='muted'>{route.company.name} | {route.vehicle.name if route.vehicle else '-'} | Responsavel/regiao/info: {route_free_label(route)}</p>
            <div class='table-wrap'><table class='table'><thead><tr><th>#</th><th>Atendimento</th><th>Janela</th><th>Prioridade</th><th>Estimativa</th><th>Chegada / saida</th><th>Alterar ordem</th></tr></thead><tbody>{rows if assumed else "<tr><td colspan='7'>Assuma a rota para liberar a execucao e alteracao de ordem.</td></tr>"}</tbody></table></div>
        </div>
        <div class='card'>
            <h2>Mapa da minha rota</h2>
            <iframe src='/web/mapa/{route.id}' style='width:100%;height:520px;border:0;border-radius:8px;'></iframe>
        </div>
    </div>
    <div class='grid two' style='margin-top:16px;'>
        <div class='card'>
            <h2>Enviar telemetria do celular</h2>
            <p class='muted'>Registra posicao, velocidade se disponivel e bateria do dispositivo movel.</p>
            <form method='post' action='/web/telemetria' class='grid' id='mobileTelemetryForm'>
                <input type='hidden' name='route_id' value='{route.id}'>
                <input type='hidden' name='source' value='motorista_mobile'>
                <label>Latitude</label><input name='latitude' required placeholder='-29.000000'>
                <label>Longitude</label><input name='longitude' required placeholder='-53.000000'>
                <label>Velocidade km/h</label><input name='speed_kmh' type='number' step='0.1'>
                <label>Bateria do dispositivo movel (%)</label><input name='battery_percent' type='number' min='0' max='100'>
                <label>Sinal</label><select name='signal_quality'><option value='bom'>Bom</option><option value='medio'>Medio</option><option value='fraco'>Fraco</option><option value='sem_sinal'>Sem sinal</option></select>
                <label>Observacao</label><input name='notes' placeholder='Opcional'>
                <button class='btn btn-light' type='button' id='fillTelemetry'>Usar dados do celular</button>
                <button class='btn' type='submit'>Enviar telemetria</button>
            </form>
        </div>
        <div class='card'>
            <h2>Registrar parada fora do ponto</h2>
            <form method='post' action='/web/observacoes' class='grid'>
                <input type='hidden' name='route_id' value='{route.id}'>
                <label>Motivo</label>
                <select name='observation_type'><option value='necessidade_motorista'>Necessidade do motorista</option><option value='via_bloqueada'>Via bloqueada</option><option value='oficina'>Oficina</option><option value='descanso'>Local de descanso</option><option value='outro'>Outro</option></select>
                <label>Descricao</label><input name='description' required placeholder='Descreva o ocorrido'>
                <label>Latitude</label><input name='latitude' placeholder='-29.000000'>
                <label>Longitude</label><input name='longitude' placeholder='-53.000000'>
                <button class='btn' type='submit'>Registrar</button>
            </form>
        </div>
        <div class='card'>
            <h2>Historico da rota</h2>
            <div class='table-wrap'><table class='table'><thead><tr><th>Data</th><th>Tipo</th><th>Descricao</th></tr></thead><tbody>{obs_rows}</tbody></table></div>
            <h2 style='margin-top:18px;'>Comprovantes digitais</h2>
            <div class='table-wrap'><table class='table'><thead><tr><th>Data</th><th>Tipo</th><th>Cliente</th><th>Recebedor</th><th>Doc fiscal</th><th>Arquivos</th></tr></thead><tbody>{proof_rows}</tbody></table></div>
        </div>
    </div>
    <script>
        (function () {{
            const routeId = "{route.id}";
            let lastAutoTelemetry = 0;
            function collectBattery() {{
                if (!navigator.getBattery) return Promise.resolve('');
                return navigator.getBattery().then(function (battery) {{
                    return String(Math.round(battery.level * 100));
                }}).catch(function () {{ return ''; }});
            }}
            function sendTelemetryPosition(pos, note) {{
                const now = Date.now();
                if (!note && now - lastAutoTelemetry < 60000) return;
                lastAutoTelemetry = now;
                collectBattery().then(function (battery) {{
                    const payload = new URLSearchParams();
                    payload.set('route_id', routeId);
                    payload.set('ajax', '1');
                    payload.set('source', 'motorista_mobile_auto');
                    payload.set('latitude', pos.coords.latitude.toFixed(6));
                    payload.set('longitude', pos.coords.longitude.toFixed(6));
                    if (pos.coords.speed !== null) payload.set('speed_kmh', (pos.coords.speed * 3.6).toFixed(1));
                    if (battery) payload.set('battery_percent', battery);
                    payload.set('signal_quality', 'bom');
                    payload.set('notes', note || 'Telemetria automatica do celular');
                    fetch('/web/telemetria', {{
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
                        body: payload.toString(),
                        keepalive: true
                    }}).catch(function () {{}});
                }});
            }}
            if ({'true' if assumed else 'false'} && navigator.geolocation) {{
                navigator.geolocation.watchPosition(function (pos) {{
                    sendTelemetryPosition(pos);
                }}, function () {{}}, {{ enableHighAccuracy:true, timeout:10000, maximumAge:30000 }});
            }}
            document.querySelectorAll('.nav-launch').forEach(function (link) {{
                link.addEventListener('click', function () {{
                    if (!navigator.geolocation) return;
                    navigator.geolocation.getCurrentPosition(function (pos) {{
                        sendTelemetryPosition(pos, 'Posicao enviada antes de abrir navegacao externa: ' + (link.dataset.nav || 'gps'));
                    }}, function () {{}}, {{ enableHighAccuracy:true, timeout:3500, maximumAge:15000 }});
                }});
            }});
            const btn = document.getElementById('fillTelemetry');
            const form = document.getElementById('mobileTelemetryForm');
            if (!btn || !form) return;
            btn.addEventListener('click', function () {{
                if (navigator.geolocation) {{
                    navigator.geolocation.getCurrentPosition(function (pos) {{
                        form.latitude.value = pos.coords.latitude.toFixed(6);
                        form.longitude.value = pos.coords.longitude.toFixed(6);
                        if (pos.coords.speed !== null) form.speed_kmh.value = (pos.coords.speed * 3.6).toFixed(1);
                    }});
                }}
                if (navigator.getBattery) {{
                    navigator.getBattery().then(function (battery) {{
                        form.battery_percent.value = Math.round(battery.level * 100);
                    }});
                }}
            }});
        }})();
        (function () {{
            document.querySelectorAll('form.geo-form').forEach(function (form) {{
                form.addEventListener('submit', function (event) {{
                    if (form.dataset.geoDone === '1') return;
                    if (!navigator.geolocation) return;
                    event.preventDefault();
                    navigator.geolocation.getCurrentPosition(function (pos) {{
                        const lat = form.querySelector('.proof-latitude');
                        const lon = form.querySelector('.proof-longitude');
                        if (lat) lat.value = pos.coords.latitude.toFixed(6);
                        if (lon) lon.value = pos.coords.longitude.toFixed(6);
                        form.dataset.geoDone = '1';
                        form.submit();
                    }}, function () {{
                        form.dataset.geoDone = '1';
                        form.submit();
                    }}, {{ enableHighAccuracy:true, timeout:4000, maximumAge:60000 }});
                }});
            }});
            document.querySelectorAll('.signature-pad').forEach(function (canvas) {{
                const ctx = canvas.getContext('2d');
                let drawing = false;
                ctx.lineWidth = 2;
                ctx.lineCap = 'round';
                ctx.strokeStyle = '#111827';
                function point(evt) {{
                    const rect = canvas.getBoundingClientRect();
                    const source = evt.touches ? evt.touches[0] : evt;
                    return {{
                        x: (source.clientX - rect.left) * (canvas.width / rect.width),
                        y: (source.clientY - rect.top) * (canvas.height / rect.height)
                    }};
                }}
                function start(evt) {{ drawing = true; const p = point(evt); ctx.beginPath(); ctx.moveTo(p.x, p.y); evt.preventDefault(); }}
                function move(evt) {{ if (!drawing) return; const p = point(evt); ctx.lineTo(p.x, p.y); ctx.stroke(); evt.preventDefault(); }}
                function end() {{ drawing = false; const input = canvas.closest('form').querySelector('.signature-data'); if (input) input.value = canvas.toDataURL('image/png'); }}
                canvas.addEventListener('mousedown', start);
                canvas.addEventListener('mousemove', move);
                window.addEventListener('mouseup', end);
                canvas.addEventListener('touchstart', start, {{ passive:false }});
                canvas.addEventListener('touchmove', move, {{ passive:false }});
                canvas.addEventListener('touchend', end);
            }});
            document.querySelectorAll('.clear-signature').forEach(function (button) {{
                button.addEventListener('click', function () {{
                    const form = button.closest('form');
                    const canvas = form.querySelector('.signature-pad');
                    const input = form.querySelector('.signature-data');
                    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
                    if (input) input.value = '';
                }});
            }});
            document.querySelectorAll('.scan-camera').forEach(function (button) {{
                button.addEventListener('click', async function () {{
                    const form = button.closest('form');
                    const input = form.querySelector('.merch-code');
                    const video = form.querySelector('.scanner-video');
                    if (!('BarcodeDetector' in window) || !navigator.mediaDevices) {{
                        alert('Leitura por camera nao disponivel neste navegador. Use o leitor Bluetooth/USB ou digite o codigo.');
                        input.focus();
                        return;
                    }}
                    const detector = new BarcodeDetector({{ formats:['qr_code','ean_13','ean_8','code_128','code_39','itf'] }});
                    const stream = await navigator.mediaDevices.getUserMedia({{ video: {{ facingMode: 'environment' }} }});
                    video.srcObject = stream;
                    video.style.display = 'block';
                    await video.play();
                    let active = true;
                    const stop = function () {{
                        active = false;
                        stream.getTracks().forEach(track => track.stop());
                        video.style.display = 'none';
                    }};
                    const tick = async function () {{
                        if (!active) return;
                        try {{
                            const codes = await detector.detect(video);
                            if (codes.length) {{
                                input.value = codes[0].rawValue || '';
                                stop();
                                input.focus();
                                return;
                            }}
                        }} catch (error) {{}}
                        requestAnimationFrame(tick);
                    }};
                    tick();
                    setTimeout(stop, 15000);
                }});
            }});
        }})();
    </script>
    """
    return layout("Minha rota", body, "motorista")


@web_bp.route("/web/comprovantes/arquivo/<path:filename>")
@web_login_required
def proof_file_page(filename):
    user = request.web_user
    safe_name = secure_filename(filename)
    proof = StopProof.query.filter_by(photo_filename=safe_name).first()
    if not proof:
        proof = StopProof.query.filter_by(invoice_file_filename=safe_name).first_or_404()
    if is_driver(user) and proof.driver_user_id != user.id:
        return layout("Acesso negado", "<div class='card'><h2>Comprovante de outro motorista.</h2></div>", "motorista"), 403
    if not can_access_company(user, proof.company_id):
        return layout("Acesso negado", "<div class='card'><h2>Comprovante de outra empresa.</h2></div>", "operacao"), 403
    if not proof.route or not can_access_route(user, proof.route):
        return layout("Acesso negado", "<div class='card'><h2>Comprovante fora da sua base ou rota.</h2></div>", "operacao"), 403
    try:
        return send_stored_file("proofs", safe_name)
    except FileNotFoundError:
        return layout("Arquivo nao encontrado", "<div class='card'><h2>Arquivo do comprovante nao localizado.</h2></div>", "operacao"), 404


@web_bp.route("/web/fiscal")
@web_login_required
def fiscal_page():
    user = request.web_user
    if is_driver(user):
        return redirect(url_for("web_bp.driver_page"))

    selected_company_id = request.args.get("company_id", type=int) if user.is_master else user.company_id
    if not selected_company_id and user.is_master:
        first_company = Company.query.order_by(Company.name).first()
        selected_company_id = first_company.id if first_company else None
    if not selected_company_id or not can_access_company(user, selected_company_id):
        return layout("Fiscal / Comprovantes", "<div class='card'><h2>Empresa nao encontrada para consulta.</h2></div>", "fiscal"), 403

    selected_date = request.args.get("data", "")
    selected_route_id = request.args.get("route_id", type=int)
    selected_stop_id = request.args.get("stop_id", type=int)
    search = request.args.get("q", "").strip().lower()

    companies = Company.query.order_by(Company.name).all() if user.is_master else [user.company]
    company_options = "".join(
        f"<option value='{company.id}' {'selected' if company.id == selected_company_id else ''}>{escape(company.name)}</option>"
        for company in companies if company
    )
    route_list = scoped_query(LogisticRoute, user).filter_by(company_id=selected_company_id).order_by(LogisticRoute.planned_date.desc(), LogisticRoute.id.desc()).limit(300).all()
    route_options = "<option value=''>Todas as rotas</option>" + "".join(
        f"<option value='{route.id}' {'selected' if selected_route_id == route.id else ''}>{escape(route_display_name(route))} | {display_date(route.planned_date)} | {escape(route_free_label(route))}</option>"
        for route in route_list
    )

    query = scoped_route_record_query(StopProof, user).filter_by(company_id=selected_company_id)
    if selected_route_id:
        query = query.filter_by(route_id=selected_route_id)
    if selected_stop_id:
        query = query.filter_by(route_stop_id=selected_stop_id)
    if selected_date:
        try:
            start = datetime.strptime(selected_date, "%Y-%m-%d")
            end = start + timedelta(days=1)
            query = query.filter(StopProof.captured_at >= start, StopProof.captured_at < end)
        except ValueError:
            selected_date = ""
    proofs = query.order_by(StopProof.captured_at.desc(), StopProof.id.desc()).limit(300).all()
    if search:
        proofs = [
            proof for proof in proofs
            if search in " ".join([
                proof.route_stop.client.name if proof.route_stop and proof.route_stop.client else "",
                proof.route.name if proof.route else "",
                proof.receiver_name or "",
                proof.receiver_document or "",
                getattr(proof, "invoice_number", None) or "",
                proof.notes or "",
            ]).lower()
        ]

    rows = ""
    for proof in proofs:
        route_name = route_display_name(proof.route) if proof.route else "-"
        stop_name = proof.route_stop.client.name if proof.route_stop and proof.route_stop.client else "-"
        captured = proof.captured_at.strftime("%d/%m/%Y %H:%M") if proof.captured_at else "-"
        doc = escape(getattr(proof, "invoice_number", None) or proof.receiver_document or "-")
        receiver = escape(proof.receiver_name or "-")
        files = f"{proof_photo_url(proof)} {proof_invoice_url(proof)} {proof_detail_url(proof)}"
        rows += f"""
        <tr>
            <td>{captured}</td>
            <td>{escape(route_name)}<br><span class='muted'>{proof_type_label(proof.proof_type)}</span></td>
            <td>{escape(stop_name)}</td>
            <td>{receiver}<br><span class='muted'>{doc}</span></td>
            <td>{fiscal_status_badge(proof)}</td>
            <td>{files}</td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='6'>Nenhum comprovante encontrado para os filtros selecionados.</td></tr>"

    body = f"""
    <style>
        .fiscal-filter {{ display:grid; grid-template-columns:1fr 1fr 160px 1fr auto; gap:12px; align-items:end; }}
        .fiscal-filter label {{ font-weight:800; color:#0b1f3a; }}
        .fiscal-filter input,.fiscal-filter select {{ width:100%; margin-top:6px; border:1px solid #cbd5e1; border-radius:7px; padding:10px; font:inherit; }}
        @media (max-width:900px) {{ .fiscal-filter {{ grid-template-columns:1fr; }} }}
    </style>
    <div class='card'>
        <h2>Fiscal / Comprovantes</h2>
        <p class='muted'>Consulta operacional de prova de atendimento: assinatura, foto, documento do recebedor, nota fiscal/link fiscal e observacoes do motorista. Nao exibe valores cobrados.</p>
        <form method='get' class='fiscal-filter'>
            <label>Empresa
                <select name='company_id' {'disabled' if not user.is_master else ''}>{company_options}</select>
            </label>
            <label>Rota
                <select name='route_id'>{route_options}</select>
            </label>
            <label>Data
                <input type='date' name='data' value='{escape(selected_date)}'>
            </label>
            <label>Buscar
                <input name='q' value='{escape(search)}' placeholder='cliente, NF, recebedor, documento'>
            </label>
            <button class='btn' type='submit'>Filtrar</button>
        </form>
    </div>
    <div class='card' style='margin-top:16px;'>
        <h2>Comprovantes encontrados</h2>
        <div class='table-wrap'>
            <table class='table'>
                <thead><tr><th>Data</th><th>Rota</th><th>Cliente</th><th>Recebedor / documento</th><th>Status</th><th>Arquivos</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    """
    return layout("Fiscal / Comprovantes", body, "fiscal")


@web_bp.route("/web/fiscal/comprovante/<int:proof_id>")
@web_login_required
def fiscal_proof_detail_page(proof_id):
    user = request.web_user
    proof = StopProof.query.get_or_404(proof_id)
    if is_driver(user) and proof.driver_user_id != user.id:
        return layout("Acesso negado", "<div class='card'><h2>Comprovante de outro motorista.</h2></div>", "motorista"), 403
    if not can_access_company(user, proof.company_id):
        return layout("Acesso negado", "<div class='card'><h2>Comprovante de outra empresa.</h2></div>", "fiscal"), 403
    route = proof.route
    stop = proof.route_stop
    client = stop.client if stop and stop.client else None
    signature = f"<img src='{proof.signature_data}' style='max-width:100%;border:1px solid #d7e0ec;border-radius:8px;background:white;padding:8px;'>" if proof.signature_data else "<span class='muted'>Assinatura nao registrada.</span>"
    photo = proof_photo_url(proof)
    invoice = proof_invoice_url(proof)
    captured = proof.captured_at.strftime("%d/%m/%Y %H:%M:%S") if proof.captured_at else "-"
    body = f"""
    <div class='card'>
        <div style='display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;'>
            <div>
                <h2>Comprovante fiscal / operacional</h2>
                <p class='muted'>{escape(route_display_name(route) if route else '-')} | {escape(client.name if client else '-')} | {captured}</p>
            </div>
            <button class='btn btn-light' onclick='window.print()'>Imprimir comprovante</button>
        </div>
        <div class='grid two' style='margin-top:16px;'>
            <div class='panel'>
                <h2>Dados do atendimento</h2>
                <p><strong>Tipo:</strong> {proof_type_label(proof.proof_type)}</p>
                <p><strong>Recebedor:</strong> {escape(proof.receiver_name or '-')}</p>
                <p><strong>Documento/identificacao:</strong> {escape(proof.receiver_document or '-')}</p>
                <p><strong>Nota/documento fiscal:</strong> {escape(getattr(proof, 'invoice_number', None) or '-')}</p>
                <p><strong>GPS:</strong> {format_coord(proof.latitude) if proof.latitude is not None else '-'}, {format_coord(proof.longitude) if proof.longitude is not None else '-'}</p>
                <p><strong>Observacao:</strong> {escape(proof.notes or '-')}</p>
                <p><strong>Arquivos:</strong> {photo} {invoice}</p>
            </div>
            <div class='panel'>
                <h2>Assinatura do cliente</h2>
                {signature}
            </div>
        </div>
    </div>
    """
    return layout("Detalhe do comprovante", body, "fiscal")


@web_bp.route("/motorista/comprovante", methods=["POST"])
@web_login_required
def driver_proof_page():
    user = request.web_user
    if not is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Apenas motoristas podem registrar comprovantes.</h2></div>", "motorista"), 403
    stop = RouteStop.query.get_or_404(request.form.get("route_stop_id", type=int))
    route = LogisticRoute.query.get_or_404(stop.route_id)
    if route.driver_user_id != user.id:
        return layout("Acesso negado", "<div class='card'><h2>Esta parada nao pertence a sua rota.</h2></div>", "motorista"), 403

    proof_type = request.form.get("proof_type", "chegada")
    if proof_type not in ("chegada", "saida"):
        proof_type = "chegada"

    def parse_coord(name):
        value = request.form.get(name)
        if not value:
            return None
        try:
            return round(float(value), 6)
        except ValueError:
            return None

    photo_filename = None
    photo = request.files.get("photo")
    if photo and photo.filename:
        original, upload_error = validate_upload(photo, ALLOWED_PHOTO_EXTENSIONS, MAX_PROOF_FILE_MB)
        if upload_error:
            return layout("Comprovante", f"<div class='card'><h2>{escape(upload_error)}</h2><a class='btn' href='/motorista'>Voltar</a></div>", "motorista"), 400
        photo_filename = f"{uuid4().hex}_{original}"
        save_upload(photo, "proofs", photo_filename)

    invoice_file_filename = None
    invoice_file = request.files.get("invoice_file")
    if invoice_file and invoice_file.filename:
        original, upload_error = validate_upload(invoice_file, ALLOWED_INVOICE_EXTENSIONS, MAX_PROOF_FILE_MB)
        if upload_error:
            return layout("Comprovante", f"<div class='card'><h2>{escape(upload_error)}</h2><a class='btn' href='/motorista'>Voltar</a></div>", "motorista"), 400
        invoice_file_filename = f"{uuid4().hex}_{original}"
        save_upload(invoice_file, "proofs", invoice_file_filename)

    latitude = parse_coord("latitude")
    longitude = parse_coord("longitude")
    receiver_name = request.form.get("receiver_name", "").strip() or None
    receiver_document = request.form.get("receiver_document", "").strip() or None
    invoice_number = request.form.get("invoice_number", "").strip() or None
    invoice_link_raw = request.form.get("invoice_link", "").strip()
    invoice_link = sanitize_external_url(invoice_link_raw)
    if invoice_link_raw and not invoice_link:
        return layout("Comprovante", "<div class='card'><h2>Link fiscal invalido. Use endereco iniciado por http:// ou https://.</h2><a class='btn' href='/motorista'>Voltar</a></div>", "motorista"), 400
    notes = request.form.get("notes", "").strip() or None

    if proof_type == "chegada":
        stop.status = "em_atendimento"
        route.status = "em_rota"
        if not getattr(route, "actual_start_time", None):
            route.actual_start_time = datetime.now().strftime("%H:%M")
        description = f"Chegada registrada no cliente {stop.client.name} por {user.name}."
    else:
        stop.status = "realizado"
        description = f"Saida/conclusao registrada no cliente {stop.client.name} por {user.name}."
        if receiver_name:
            description += f" Recebedor: {receiver_name}."
        if receiver_document:
            description += f" Identificacao: {receiver_document}."
        if invoice_number:
            description += f" Documento fiscal: {invoice_number}."
        if invoice_link:
            description += " Link fiscal informado."
        if notes:
            description += f" Observacao: {notes}."

    from database.database import db
    db.session.add(StopProof(
        company_id=route.company_id,
        route_id=route.id,
        route_stop_id=stop.id,
        driver_user_id=user.id,
        proof_type=proof_type,
        receiver_name=receiver_name,
        receiver_document=receiver_document,
        invoice_number=invoice_number,
        invoice_link=invoice_link,
        invoice_file_filename=invoice_file_filename,
        fiscal_status="completo" if invoice_number or invoice_link or invoice_file_filename else "pendente",
        notes=notes,
        latitude=latitude,
        longitude=longitude,
        photo_filename=photo_filename,
        signature_data=request.form.get("signature_data") or None,
    ))
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        route_stop_id=stop.id,
        observation_type=f"comprovante_{proof_type}",
        description=description,
        latitude=latitude,
        longitude=longitude,
        created_by_user_id=user.id,
    ))
    if proof_type == "saida":
        remaining = [item for item in route.stops if item.id != stop.id and (item.status or "").lower() != "realizado"]
        if not remaining:
            route.status = "concluida"
            route.actual_end_time = datetime.now().strftime("%H:%M")
    db.session.commit()
    return redirect(url_for("web_bp.driver_page"))


@web_bp.route("/motorista/mercadoria/conferir", methods=["POST"])
@web_login_required
def driver_merchandise_scan_page():
    user = request.web_user
    if not is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Apenas motoristas podem conferir mercadorias nesta tela.</h2></div>", "motorista"), 403
    stop = RouteStop.query.get_or_404(request.form.get("route_stop_id", type=int))
    route = LogisticRoute.query.get_or_404(stop.route_id)
    if route.driver_user_id != user.id:
        return layout("Acesso negado", "<div class='card'><h2>Esta parada nao pertence a sua rota.</h2></div>", "motorista"), 403

    code = normalize_merchandise_code(request.form.get("code"))
    action = request.form.get("scan_action") or "entregue"
    if action not in ("coletado", "entregue", "divergente"):
        action = "entregue"
    if not code:
        return layout("Conferencia de mercadoria", "<div class='card'><h2>Informe ou leia o codigo da mercadoria.</h2><a class='btn' href='/motorista'>Voltar</a></div>", "motorista"), 400

    def parse_coord(name):
        value = request.form.get(name)
        if not value:
            return None
        try:
            return round(float(value), 6)
        except ValueError:
            return None

    photo_filename = None
    photo = request.files.get("photo")
    if photo and photo.filename:
        original, upload_error = validate_upload(photo, ALLOWED_PHOTO_EXTENSIONS, MAX_PROOF_FILE_MB)
        if upload_error:
            return layout("Conferencia de mercadoria", f"<div class='card'><h2>{escape(upload_error)}</h2><a class='btn' href='/motorista'>Voltar</a></div>", "motorista"), 400
        photo_filename = f"{uuid4().hex}_{original}"
        save_upload(photo, "proofs", photo_filename)

    merchandise = merchandise_match_query(route.company_id, code).first()
    latitude = parse_coord("latitude")
    longitude = parse_coord("longitude")
    notes = request.form.get("notes", "").strip() or None
    result = "divergente"
    description = f"Codigo {code} lido por {user.name}."
    if merchandise and merchandise.route_stop_id == stop.id:
        merchandise.status = action
        merchandise.last_scanned_at = datetime.now()
        merchandise.last_scanned_by_user_id = user.id
        result = action
        description = f"Mercadoria {code} conferida como {merchandise_status_label(action)} por {user.name}."
    else:
        if merchandise:
            merchandise.status = "divergente"
            merchandise.last_scanned_at = datetime.now()
            merchandise.last_scanned_by_user_id = user.id
            description = f"Divergencia: mercadoria {code} pertence a outra rota/parada."
        else:
            description = f"Divergencia: codigo {code} nao cadastrado para esta empresa."
    if notes:
        description += f" Observacao: {notes}."

    db.session.add(MerchandiseScan(
        company_id=route.company_id,
        merchandise_id=merchandise.id if merchandise else None,
        route_id=route.id,
        route_stop_id=stop.id,
        scanned_code=code,
        scan_action=action,
        result_status=result,
        latitude=latitude,
        longitude=longitude,
        photo_filename=photo_filename,
        notes=notes,
        scanned_by_user_id=user.id,
    ))
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        route_stop_id=stop.id,
        observation_type=f"mercadoria_{result}",
        description=description,
        latitude=latitude,
        longitude=longitude,
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return redirect(url_for("web_bp.driver_page"))


def reorder_stop_with_justification(user, stop, route, direction, target_sequence, reason, details):
    if reason not in DRIVER_REORDER_REASONS:
        reason = "Outro motivo justificado"
    details = (details or "").strip()

    ordered = RouteStop.query.filter_by(route_id=route.id).order_by(RouteStop.sequence, RouteStop.id).all()
    current_index = next((index for index, item in enumerate(ordered) if item.id == stop.id), None)
    if current_index is None:
        return False
    old_sequence = stop.sequence

    if direction == "up":
        target_index = current_index - 1
    elif direction == "down":
        target_index = current_index + 1
    else:
        target_index = max(0, min(len(ordered) - 1, (target_sequence or stop.sequence) - 1))

    if target_index < 0 or target_index >= len(ordered) or target_index == current_index:
        return False

    moved = ordered.pop(current_index)
    ordered.insert(target_index, moved)
    for index, item in enumerate(ordered, start=1):
        item.sequence = index

    actor = "Motorista" if is_driver(user) else "Operacao"
    description = f"{actor} alterou sequencia: {stop.client.name}, posicao {old_sequence} para {stop.sequence}. Motivo: {reason}."
    if details:
        description += f" Detalhes: {details}"
    from database.database import db
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        route_stop_id=stop.id,
        observation_type="reordenacao_manual_motorista" if is_driver(user) else "reordenacao_manual_operacao",
        description=description,
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return True


def ai_resequence_route(route, user):
    stops = RouteStop.query.filter_by(route_id=route.id).order_by(RouteStop.sequence, RouteStop.id).all()
    if len(stops) < 2:
        return False

    locked = [stop for stop in stops if (stop.status or "").lower() == "realizado"]
    candidates = [stop for stop in stops if (stop.status or "").lower() != "realizado"]
    if len(candidates) < 2:
        return False

    speed = route.vehicle.average_speed_kmh if route.vehicle and route.vehicle.average_speed_kmh else 60
    current_point = [BASE_LATITUDE, BASE_LONGITUDE]
    current_minutes = 0
    if locked:
        current_point = [locked[-1].client.latitude, locked[-1].client.longitude]
        current_minutes += sum(stop_service_minutes(stop) for stop in locked)

    priority_weight = {"alta": -45, "media": 0, "baixa": 25}
    ordered_open = []
    remaining = list(candidates)
    while remaining:
        scored = []
        for stop in remaining:
            client = stop.client
            point = [client.latitude, client.longitude]
            distance = distancia_reta_km([current_point, point])
            travel = (distance / speed) * 60 if speed else 0
            projected_arrival = current_minutes + travel
            window_start = time_text_to_minutes(client.delivery_window_start) or 0
            window_end = time_text_to_minutes(client.delivery_window_end) or (24 * 60)
            lateness = max(0, projected_arrival - window_end)
            early_wait = max(0, window_start - projected_arrival)
            priority = priority_weight.get((client.priority or "media").lower(), 0)
            service_minutes = stop_service_minutes(stop)
            projected_departure = projected_arrival + service_minutes
            over_limit = max(0, projected_departure - WORKDAY_LIMIT_MINUTES)
            score = (distance * 7) + priority + (window_end * 0.035) + (lateness * 12) + (early_wait * 0.4) + (over_limit * 18)
            scored.append((score, stop, distance, travel))
        scored.sort(key=lambda item: (item[0], item[1].sequence, item[1].id))
        _, chosen, chosen_distance, chosen_travel = scored[0]
        ordered_open.append(chosen)
        remaining.remove(chosen)
        current_point = [chosen.client.latitude, chosen.client.longitude]
        current_minutes += chosen_travel + stop_service_minutes(chosen)

    final_order = locked + ordered_open
    old_order = [stop.id for stop in stops]
    new_order = [stop.id for stop in final_order]
    if old_order == new_order:
        return False

    for index, stop in enumerate(final_order, start=1):
        stop.sequence = index

    from database.database import db
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        observation_type="reordenacao_ia",
        description=(
            "IA reorganizou a sequencia considerando janelas de atendimento, prioridade, distancia aproximada "
            "e preservando pontos ja realizados."
        ),
        created_by_user_id=user.id,
    ))
    db.session.commit()
    if route_work_minutes(route) > WORKDAY_LIMIT_MINUTES:
        db.session.add(StopObservation(
            company_id=route.company_id,
            route_id=route.id,
            observation_type="jornada_acima_10h",
            description=f"Apos reorganizacao por IA, rota prevista em {tempo_formatado(route_work_minutes(route))}. Recomenda-se dividir ou retirar pontos para ficar em ate 10h.",
            created_by_user_id=user.id,
        ))
        db.session.commit()
    return True


@web_bp.route("/web/rota/inicio", methods=["POST"])
@web_login_required
def update_route_start_time_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Inicio da rota e editado pela operacao.</h2></div>", "motorista"), 403
    route = LogisticRoute.query.get_or_404(request.form.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota pertence a outra empresa.</h2></div>", "operacao"), 403
    raw_time = request.form.get("start_time", "").strip()
    hour, minute = parse_hhmm(raw_time, route_start_time(route))
    new_time = f"{hour:02d}:{minute:02d}"
    old_time = route_start_time(route)
    route.start_time = new_time
    from database.database import db
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        observation_type="inicio_rota_ajustado",
        description=f"Inicio da rota ajustado de {old_time} para {new_time}.",
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return redirect(url_for("web_bp.operational_page", route_id=route.id, data=route.planned_date or None))


@web_bp.route("/web/rota/status", methods=["POST"])
@web_login_required
def update_route_status_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Inicio e finalizacao da rota sao controlados pela operacao nesta tela.</h2></div>", "motorista"), 403
    route = LogisticRoute.query.get_or_404(request.form.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota pertence a outra empresa.</h2></div>", "operacao"), 403

    action = request.form.get("action")
    fallback = route_start_time(route) if action == "start" else route_completion_time(route)
    hour, minute = parse_hhmm(request.form.get("event_time", ""), fallback)
    event_time = f"{hour:02d}:{minute:02d}"
    from database.database import db

    if action == "start":
        if (route.status or "").lower() in ("em_rota", "concluida", "finalizada", "realizada") and route.actual_start_time:
            return redirect(url_for("web_bp.operational_page", route_id=route.id, data=route.planned_date or None))
        route.actual_start_time = event_time
        route.status = "em_rota"
        obs_type = "rota_iniciada"
        description = f"Rota iniciada pela operacao as {event_time}."
    elif action == "finish":
        if (route.status or "").lower() in ("concluida", "finalizada", "realizada") and route.actual_end_time:
            return redirect(url_for("web_bp.operational_page", route_id=route.id, data=route.planned_date or None))
        route.actual_end_time = event_time
        route.status = "concluida"
        for stop in route.stops:
            if (stop.status or "").lower() != "realizado":
                stop.status = "realizado"
        obs_type = "rota_finalizada"
        description = f"Rota finalizada pela operacao as {event_time}, com retorno a BASE."
    else:
        return layout("Acao invalida", "<div class='card'><h2>Acao de rota invalida.</h2></div>", "operacao"), 400

    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        observation_type=obs_type,
        description=description,
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return redirect(url_for("web_bp.operational_page", route_id=route.id, data=route.planned_date or None))


@web_bp.route("/web/parada/tempo-atendimento", methods=["POST"])
@web_login_required
def update_stop_service_time_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Tempo previsto de atendimento e editado pela operacao.</h2></div>", "motorista"), 403
    stop = RouteStop.query.get_or_404(request.form.get("route_stop_id", type=int))
    route = LogisticRoute.query.get_or_404(stop.route_id)
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota pertence a outra empresa.</h2></div>", "operacao"), 403

    minutes = request.form.get("service_minutes", type=int) or DEFAULT_SERVICE_MINUTES
    minutes = max(1, min(240, minutes))
    old_minutes = stop_service_minutes(stop)
    reason = request.form.get("reason", "").strip()
    stop.service_minutes = minutes

    from database.database import db
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        route_stop_id=stop.id,
        observation_type="tempo_atendimento_ajustado",
        description=(
            f"Tempo de atendimento ajustado de {old_minutes} min para {minutes} min em {stop.client.name}."
            + (f" Motivo: {reason}" if reason else "")
        ),
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return redirect(url_for("web_bp.operational_page", route_id=route.id))


@web_bp.route("/web/parada/servico", methods=["POST"])
@web_login_required
def update_stop_service_description_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Servico previsto da parada e editado pela operacao.</h2></div>", "motorista"), 403
    stop = RouteStop.query.get_or_404(request.form.get("route_stop_id", type=int))
    route = LogisticRoute.query.get_or_404(stop.route_id)
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota pertence a outra empresa.</h2></div>", "operacao"), 403

    old_description = stop_service_description(stop)
    new_description = request.form.get("service_description", "").strip()
    if len(new_description) > 1200:
        new_description = new_description[:1200]
    stop.service_description = new_description or None

    from database.database import db
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        route_stop_id=stop.id,
        observation_type="servico_parada_ajustado",
        description=(
            f"Servico previsto da parada {stop.sequence} - {stop.client.name} atualizado."
            + (f" Antes: {old_description}." if old_description else "")
            + (f" Agora: {new_description}." if new_description else " Servico removido.")
        ),
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return redirect(url_for("web_bp.operational_page", route_id=route.id, data=route.planned_date or None))


@web_bp.route("/web/parada/adicionar-servico", methods=["POST"])
@web_login_required
def add_service_stop_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Insercao de parada e feita pela operacao.</h2></div>", "motorista"), 403
    route = LogisticRoute.query.get_or_404(request.form.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota pertence a outra empresa.</h2></div>", "operacao"), 403
    client = Client.query.get_or_404(request.form.get("client_id", type=int))
    if client.company_id != route.company_id or not client.active:
        return layout("Cliente invalido", "<div class='card'><h2>Cliente fora da empresa ou inativo.</h2></div>", "operacao"), 400

    current_total = RouteStop.query.filter_by(route_id=route.id).count()
    target_sequence = request.form.get("target_sequence", type=int) or (current_total + 1)
    target_sequence = max(1, min(current_total + 1, target_sequence))
    service_minutes = request.form.get("service_minutes", type=int) or DEFAULT_SERVICE_MINUTES
    service_minutes = max(1, min(240, service_minutes))
    service_description = request.form.get("service_description", "").strip()
    if len(service_description) > 1200:
        service_description = service_description[:1200]

    from database.database import db
    for item in RouteStop.query.filter_by(route_id=route.id).filter(RouteStop.sequence >= target_sequence).order_by(RouteStop.sequence.desc()).all():
        item.sequence += 1
    stop = RouteStop(
        route_id=route.id,
        client_id=client.id,
        sequence=target_sequence,
        service_minutes=service_minutes,
        service_description=service_description or None,
        status="pendente",
    )
    db.session.add(stop)
    db.session.flush()
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        route_stop_id=stop.id,
        observation_type="parada_servico_inserida",
        description=(
            f"Parada de servico inserida manualmente na posicao {target_sequence}: {client.name}. "
            f"Tempo previsto {service_minutes} min."
            + (f" Servico: {service_description}" if service_description else "")
        ),
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return redirect(url_for("web_bp.operational_page", route_id=route.id, data=route.planned_date or None, reorder_stop=stop.id) + f"#reordenar-{stop.id}")


@web_bp.route("/web/reordenar-parada", methods=["POST"])
@web_login_required
def reorder_stop_page():
    user = request.web_user
    stop = RouteStop.query.get_or_404(request.form.get("route_stop_id", type=int))
    route = LogisticRoute.query.get_or_404(stop.route_id)
    if is_driver(user):
        if route.driver_user_id != user.id:
            return layout("Acesso negado", "<div class='card'><h2>Esta parada nao pertence a sua rota.</h2></div>", "motorista"), 403
    elif not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota pertence a outra empresa.</h2></div>", "operacao"), 403

    direction = request.form.get("direction")
    reason = request.form.get("reason") or "Outro motivo justificado"
    details = request.form.get("details", "").strip()
    target_sequence = request.form.get("target_sequence", type=int)
    reorder_stop_with_justification(user, stop, route, direction, target_sequence, reason, details)
    if request.form.get("return_to") == "driver" or is_driver(user):
        return redirect(url_for("web_bp.driver_page"))
    return redirect(url_for("web_bp.operational_page", route_id=route.id, reorder_stop=stop.id) + f"#reordenar-{stop.id}")


@web_bp.route("/motorista/reordenar", methods=["POST"])
@web_login_required
def driver_reorder_page():
    return reorder_stop_page()


@web_bp.route("/web/reorganizar-rota-ia", methods=["POST"])
@web_login_required
def ai_resequence_route_page():
    user = request.web_user
    route = LogisticRoute.query.get_or_404(request.form.get("route_id", type=int))
    if is_driver(user):
        if route.driver_user_id != user.id:
            return layout("Acesso negado", "<div class='card'><h2>Esta rota nao pertence ao seu login.</h2></div>", "motorista"), 403
    elif not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota pertence a outra empresa.</h2></div>", "operacao"), 403
    ai_resequence_route(route, user)
    if request.form.get("return_to") == "driver" or is_driver(user):
        return redirect(url_for("web_bp.driver_page"))
    return redirect(url_for("web_bp.operational_page", route_id=route.id))


@web_bp.route("/web/intervalos-rota", methods=["POST"])
@web_login_required
def route_breaks_page():
    user = request.web_user
    if is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Intervalos sao editados pela equipe operacional.</h2></div>", "motorista"), 403
    route = LogisticRoute.query.get_or_404(request.form.get("route_id", type=int))
    if not can_access_route(user, route):
        return layout("Acesso negado", "<div class='card'><h2>Esta rota pertence a outra empresa.</h2></div>", "operacao"), 403

    from database.database import db
    action = request.form.get("action", "create")

    if action == "delete":
        break_item = RouteBreak.query.get_or_404(request.form.get("break_id", type=int))
        if break_item.route_id == route.id:
            break_item.active = False
            db.session.add(StopObservation(
                company_id=route.company_id,
                route_id=route.id,
                observation_type="intervalo_removido",
                description=f"Intervalo removido pela operacao: {break_item.title}.",
                created_by_user_id=user.id,
            ))
            db.session.commit()
        return redirect(url_for("web_bp.operational_page", route_id=route.id))

    if action == "ia":
        suggestion = intraday_break_suggestion(route)
        if suggestion:
            existing = RouteBreak.query.filter_by(route_id=route.id, break_type="intrajornada", active=True).first()
            break_item = existing or RouteBreak(company_id=route.company_id, route_id=route.id, break_type="intrajornada", source="ia", created_by_user_id=user.id)
            break_item.after_route_stop_id = suggestion["stop"].id
            break_item.title = "Intervalo intrajornada"
            break_item.duration_minutes = 60
            break_item.source = "ia"
            break_item.notes = f"Sugerido pela IA entre {suggestion['window_start']} e {suggestion['window_end']}."
            db.session.add(break_item)
            db.session.add(StopObservation(
                company_id=route.company_id,
                route_id=route.id,
                route_stop_id=suggestion["stop"].id,
                observation_type="intervalo_ia",
                description=f"IA inseriu intervalo intrajornada apos parada {suggestion['stop'].sequence}, duracao 60 min.",
                created_by_user_id=user.id,
            ))
            db.session.commit()
        return redirect(url_for("web_bp.operational_page", route_id=route.id))

    break_id = request.form.get("break_id", type=int)
    break_item = RouteBreak.query.get(break_id) if break_id else None
    obs_type = "intervalo_criado"
    if break_item:
        if break_item.route_id != route.id:
            return layout("Acesso negado", "<div class='card'><h2>Intervalo pertence a outra rota.</h2></div>", "operacao"), 403
        obs_type = "intervalo_editado"

    after_stop_id = request.form.get("after_route_stop_id", type=int)
    if not after_stop_id:
        return layout(
            "Intervalo da rota",
            f"<div class='card'><h2>Selecione uma parada para inserir o intervalo.</h2><p class='muted'>A rota precisa ter pelo menos uma parada e o campo 'Apos parada' deve estar preenchido.</p><a class='btn' href='/web/operacao?route_id={route.id}'>Voltar para a operacao</a></div>",
            "operacao",
        ), 400
    after_stop = RouteStop.query.get_or_404(after_stop_id)
    if after_stop.route_id != route.id:
        return layout("Acesso negado", "<div class='card'><h2>Parada pertence a outra rota.</h2></div>", "operacao"), 403
    if not break_item:
        break_item = RouteBreak(company_id=route.company_id, route_id=route.id, created_by_user_id=user.id)
    duration = request.form.get("duration_minutes", type=int) or 60
    duration = max(1, min(180, duration))
    break_item.after_route_stop_id = after_stop.id
    break_item.title = request.form.get("title", "").strip() or "Intervalo intrajornada"
    break_item.break_type = "intrajornada"
    break_item.duration_minutes = duration
    break_item.source = "manual"
    break_item.notes = request.form.get("notes", "").strip() or None
    break_item.active = True
    db.session.add(break_item)
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        route_stop_id=after_stop.id,
        observation_type=obs_type,
        description=f"{break_item.title} apos parada {after_stop.sequence}, duracao {duration} min. {break_item.notes or ''}".strip(),
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return redirect(url_for("web_bp.operational_page", route_id=route.id))


@web_bp.route("/esqueci-senha", methods=["GET", "POST"])
def forgot_password_page():
    message = ""
    email_value = ""
    if request.method == "POST":
        email_value = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email_value).first()
        if user:
            message = "Solicitacao localizada. Procure o administrador da sua empresa para redefinir sua senha."
        else:
            message = "Se este e-mail estiver cadastrado, o administrador da empresa podera redefinir a senha."
    return f"""
    <!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Esqueceu a senha - RIS</title>
    <style>body{{margin:0;background:#eef3f8;font-family:Arial,sans-serif;color:#182235}}.wrap{{min-height:100vh;display:grid;place-items:center;padding:24px}}.card{{width:100%;max-width:430px;background:white;border:1px solid #d7e0ec;border-radius:8px;padding:28px;box-shadow:0 12px 32px rgba(16,24,40,.12)}}h1{{margin:0;font-size:28px}}p{{color:#667085;line-height:1.5}}label{{display:block;font-size:13px;font-weight:800;margin:13px 0 6px}}input{{width:100%;box-sizing:border-box;padding:12px;border:1px solid #d7e0ec;border-radius:8px;font-size:15px}}button{{width:100%;margin-top:16px;border:0;border-radius:8px;background:#1f5eff;color:white;padding:12px;font-weight:800;cursor:pointer}}.msg{{color:#14804a;font-weight:800;margin:10px 0}}a{{display:block;margin-top:14px;text-align:center;color:#1849a9;font-weight:800;text-decoration:none}}</style></head>
    <body><div class="wrap"><form class="card" method="post"><h1>Esqueceu a senha?</h1><p>Informe seu e-mail. Por seguranca, a redefinicao sera feita pelo administrador da empresa.</p><div class="msg">{message}</div><label>Email</label><input name="email" type="email" value="{email_value}" required><button type="submit">Solicitar redefinicao</button><a href="/login">Voltar para o login</a></form></div></body></html>
    """


@web_bp.route("/primeiro-acesso", methods=["GET", "POST"])
def first_access_page():
    message = ""
    error = ""
    values = {
        "company_name": "",
        "company_document": "",
        "financial_responsible": "",
        "email": "",
        "phone": "",
        "address": "",
        "notes": "",
    }
    if request.method == "POST":
        for key in values:
            values[key] = request.form.get(key, "").strip()
        required = ["company_name", "company_document", "financial_responsible", "email", "phone", "address"]
        missing = [field for field in required if not values[field]]
        address_file = request.files.get("address_proof")
        document_file = request.files.get("document_proof")
        if missing:
            error = "Preencha todos os dados obrigatorios para solicitar o primeiro acesso."
        elif not address_file or not address_file.filename or not document_file or not document_file.filename:
            error = "Anexe comprovante de endereco e documento CNPJ/CPF."
        else:
            address_original, address_error = validate_upload(address_file, ALLOWED_ONBOARDING_EXTENSIONS, MAX_PROOF_FILE_MB)
            document_original, document_error = validate_upload(document_file, ALLOWED_ONBOARDING_EXTENSIONS, MAX_PROOF_FILE_MB)
            if address_error or document_error:
                error = address_error or document_error
            else:
                address_filename = f"{uuid4().hex}_{address_original}"
                document_filename = f"{uuid4().hex}_{document_original}"
                save_upload(address_file, "onboarding", address_filename)
                save_upload(document_file, "onboarding", document_filename)
                from database.database import db
                db.session.add(PublicContactRequest(
                    request_type="primeiro_acesso",
                    name=values["company_name"][:255],
                    contact=f"{values['email']} | {values['phone']}"[:255],
                    subject="Primeiro acesso / ativacao de conta",
                    company_document=values["company_document"][:120],
                    financial_responsible=values["financial_responsible"][:255],
                    address_proof_filename=address_filename,
                    document_proof_filename=document_filename,
                    message=(
                        f"Empresa: {values['company_name']}\n"
                        f"CNPJ/CPF: {values['company_document']}\n"
                        f"Responsavel financeiro: {values['financial_responsible']}\n"
                        f"E-mail para envio do link: {values['email']}\n"
                        f"Telefone/WhatsApp: {values['phone']}\n"
                        f"Endereco: {values['address']}\n"
                        f"Observacoes: {values['notes'] or '-'}\n"
                        "Solicitacao aguardando conferencia cadastral antes de liberar acesso."
                    ),
                    status="novo",
                ))
                db.session.commit()
                message = "Solicitacao enviada. A equipe vai conferir os dados e liberar o link de primeiro acesso."
                values = {key: "" for key in values}

    def field_value(key):
        return escape(values.get(key, ""))

    return f"""
    <!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Primeiro acesso - RIS</title>
    <style>
        *{{box-sizing:border-box}}body{{margin:0;background:#eef3f8;font-family:Arial,sans-serif;color:#182235}}.wrap{{min-height:100vh;display:grid;place-items:center;padding:24px}}.card{{width:100%;max-width:760px;background:white;border:1px solid #d7e0ec;border-radius:8px;padding:28px;box-shadow:0 12px 32px rgba(16,24,40,.12)}}h1{{margin:0;font-size:30px}}p{{color:#667085;line-height:1.5}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}label{{display:block;font-size:13px;font-weight:800;margin:0 0 6px}}input,textarea{{width:100%;padding:12px;border:1px solid #d7e0ec;border-radius:8px;font-size:15px}}textarea{{min-height:82px;resize:vertical}}.full{{grid-column:1/-1}}button{{width:100%;margin-top:16px;border:0;border-radius:8px;background:#1f5eff;color:white;padding:13px;font-weight:900;cursor:pointer}}.msg{{color:#14804a;font-weight:800;margin:12px 0}}.error{{color:#d92d20;font-weight:800;margin:12px 0}}a{{display:block;margin-top:14px;text-align:center;color:#1849a9;font-weight:800;text-decoration:none}}small{{display:block;color:#667085;margin-top:5px}}@media(max-width:720px){{.grid{{grid-template-columns:1fr}}.card{{padding:22px}}}}
    </style></head>
    <body><div class="wrap"><form class="card" method="post" enctype="multipart/form-data"><h1>Primeiro acesso</h1><p>Solicite a ativacao da empresa. A liberacao do usuario e senha acontece apos conferencia dos dados cadastrais e documentos.</p><div class="msg">{message}</div><div class="error">{error}</div><div class="grid">
        <div><label>Empresa / Razao social *</label><input name="company_name" value="{field_value('company_name')}" required></div>
        <div><label>CNPJ ou CPF *</label><input name="company_document" value="{field_value('company_document')}" required></div>
        <div><label>Responsavel financeiro *</label><input name="financial_responsible" value="{field_value('financial_responsible')}" required></div>
        <div><label>E-mail para receber o link *</label><input name="email" type="email" value="{field_value('email')}" required></div>
        <div><label>Telefone / WhatsApp *</label><input name="phone" value="{field_value('phone')}" required></div>
        <div><label>Comprovante de endereco *</label><input name="address_proof" type="file" accept=".pdf,.jpg,.jpeg,.png,.webp" required><small>PDF ou imagem ate {MAX_PROOF_FILE_MB} MB.</small></div>
        <div class="full"><label>Endereco completo *</label><input name="address" value="{field_value('address')}" required></div>
        <div><label>Documento CNPJ/CPF do responsavel *</label><input name="document_proof" type="file" accept=".pdf,.jpg,.jpeg,.png,.webp" required><small>PDF ou imagem ate {MAX_PROOF_FILE_MB} MB.</small></div>
        <div><label>Observacoes</label><textarea name="notes">{field_value('notes')}</textarea></div>
    </div><button type="submit">ENVIAR SOLICITACAO DE PRIMEIRO ACESSO</button><a href="/login">Voltar para o login</a></form></div></body></html>
    """


@web_bp.route("/web/redefinir-senha", methods=["GET", "POST"])
@web_login_required
def reset_password_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso administrativo restrito.</h2></div>", "empresas"), 403
    from database.database import db
    from werkzeug.security import generate_password_hash
    message = ""
    if request.method == "POST":
        target = User.query.get(request.form.get("user_id", type=int))
        new_password = request.form.get("new_password", "")
        if not target:
            message = "Usuario nao encontrado."
        elif len(new_password) < 6:
            message = "A nova senha deve ter pelo menos 6 caracteres."
        else:
            target.password = generate_password_hash(new_password)
            db.session.commit()
            message = f"Senha redefinida para {target.email}."
    users = User.query.order_by(User.email).all()
    options = "".join(f"<option value='{item.id}'>{item.email} - {item.name} ({item.role})</option>" for item in users)
    body = f"""
    <div class='card'>
        <h2>Redefinir senha de usuario</h2>
        <p class='muted'>Use quando um motorista, operador, gerente ou CEO esquecer a senha.</p>
        <p><strong>{message}</strong></p>
        <form method='post' class='grid'>
            <label>Usuario</label>
            <select name='user_id' required>{options}</select>
            <label>Nova senha</label>
            <input name='new_password' type='text' required placeholder='Digite a nova senha'>
            <button class='btn' type='submit'>Redefinir senha</button>
        </form>
    </div>
    """
    return layout("Redefinir senha", body, "empresas")


@web_bp.route("/web/contatos", methods=["GET", "POST"])
@web_login_required
def public_contacts_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso administrativo restrito.</h2></div>", "contatos"), 403
    from database.database import db
    message = ""
    if request.method == "POST":
        contact_request = PublicContactRequest.query.get(request.form.get("contact_id", type=int))
        if contact_request:
            contact_request.status = request.form.get("status", "em_analise")
            db.session.commit()
            message = "Solicitacao atualizada."
    requests_list = PublicContactRequest.query.order_by(PublicContactRequest.created_at.desc(), PublicContactRequest.id.desc()).limit(80).all()
    rows = ""
    for item in requests_list:
        created = item.created_at.strftime("%d/%m/%Y %H:%M") if item.created_at else "-"
        status_label = "Novo" if item.status == "novo" else "Em analise" if item.status == "em_analise" else "Concluido"
        request_type = "Primeiro acesso" if (item.request_type or "") == "primeiro_acesso" else "Contato"
        attachments = []
        if item.address_proof_filename:
            attachments.append(f"<a class='btn btn-light' href='/web/contatos/arquivo/{escape(item.address_proof_filename)}' target='_blank'>Endereco</a>")
        if item.document_proof_filename:
            attachments.append(f"<a class='btn btn-light' href='/web/contatos/arquivo/{escape(item.document_proof_filename)}' target='_blank'>Documento</a>")
        attachment_links = " ".join(attachments) if attachments else "-"
        message_text = "<br>".join(escape(item.message or "").splitlines())
        rows += f"""
        <tr>
            <td>{created}</td>
            <td><span class='badge'>{request_type}</span></td>
            <td><strong>{escape(item.name)}</strong></td>
            <td>{escape(item.contact)}</td>
            <td>{escape(item.subject)}</td>
            <td>{message_text}</td>
            <td>{attachment_links}</td>
            <td><span class='badge'>{status_label}</span></td>
            <td>
                <form method='post' style='display:flex;gap:8px;align-items:center;'>
                    <input type='hidden' name='contact_id' value='{item.id}'>
                    <select name='status'>
                        <option value='novo' {'selected' if item.status == 'novo' else ''}>Novo</option>
                        <option value='em_analise' {'selected' if item.status == 'em_analise' else ''}>Em analise</option>
                        <option value='concluido' {'selected' if item.status == 'concluido' else ''}>Concluido</option>
                    </select>
                    <button class='btn btn-light' type='submit'>Salvar</button>
                </form>
            </td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='9'>Nenhuma solicitacao publica recebida ainda.</td></tr>"
    body = f"""
    <div class='card'>
        <h2>Contato publico geral</h2>
        <p class='muted'>Solicitacoes enviadas pela tela publica. Esta lista fica visivel somente para a administracao da plataforma.</p>
        <p><strong>{message}</strong></p>
        <div class='table-wrap'>
            <table class='table'>
                <thead><tr><th>Data</th><th>Tipo</th><th>Nome</th><th>Contato</th><th>Assunto</th><th>Mensagem</th><th>Anexos</th><th>Status</th><th>Acao</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    """
    return layout("Contato publico geral", body, "contatos")


@web_bp.route("/web/contatos/arquivo/<path:filename>")
@web_login_required
def contact_request_file_page(filename):
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso administrativo restrito.</h2></div>", "contatos"), 403
    safe_name = secure_filename(filename)
    exists = PublicContactRequest.query.filter(
        (PublicContactRequest.address_proof_filename == safe_name) |
        (PublicContactRequest.document_proof_filename == safe_name)
    ).first()
    if not exists:
        return layout("Arquivo nao encontrado", "<div class='card'><h2>Anexo nao localizado na solicitacao.</h2></div>", "contatos"), 404
    try:
        return send_stored_file("onboarding", safe_name)
    except FileNotFoundError:
        return layout("Arquivo nao encontrado", "<div class='card'><h2>Arquivo fisico nao localizado.</h2></div>", "contatos"), 404


@web_bp.route("/web/insights-publicos", methods=["GET", "POST"])
@web_login_required
def public_insights_page():
    user = request.web_user
    if not can_see_master(user):
        return layout("Acesso negado", "<div class='card'><h2>Acesso administrativo restrito.</h2></div>", "insights_publicos"), 403
    from database.database import db

    message = ""
    if request.method == "POST":
        insight_key = request.form.get("insight_key", "").strip().lower()
        if insight_key in INSIGHT_ORDER:
            insight = PublicInsight.query.filter_by(insight_key=insight_key).first()
            if not insight:
                insight = PublicInsight(insight_key=insight_key)
                db.session.add(insight)
            insight.title = request.form.get("title", "").strip() or insight_key.title()
            insight.intro = request.form.get("intro", "").strip() or "Atualize este insight mensalmente."
            insight.items = request.form.get("items", "").strip() or "Inclua uma novidade ou aplicacao pratica."
            insight.source_title = request.form.get("source_title", "").strip()
            insight.source_url = request.form.get("source_url", "").strip()
            insight.source_date = request.form.get("source_date", "").strip() or "Atualizar mensalmente"
            db.session.commit()
            message = "Insight publico atualizado."

    insights = {item.insight_key: item for item in PublicInsight.query.all()}
    forms = ""
    labels = {
        "resultado": "Resultado",
        "estrategia": "Estrategia",
        "dados": "Dados",
        "logistica": "Logistica",
        "economia": "Economia",
        "pessoas": "Pessoas",
    }
    fallback = public_insights_payload()
    for key in INSIGHT_ORDER:
        item = insights.get(key)
        values = item.to_public_dict() if item else fallback.get(key, {})
        forms += f"""
        <form method='post' class='insight-editor-card'>
            <input type='hidden' name='insight_key' value='{key}'>
            <div class='insight-editor-head'>
                <div>
                    <strong>{labels.get(key, key.title())}</strong>
                    <p class='muted'>Conteudo exibido na tela publica ao clicar neste ponto.</p>
                </div>
                <button class='btn' type='submit'>Atualizar</button>
            </div>
            <div class='form-grid'>
                <label>Titulo
                    <input name='title' value='{escape(values.get("title", ""))}'>
                </label>
                <label>Data / competencia da fonte
                    <input name='source_date' value='{escape(values.get("source_date", ""))}' placeholder='Ex.: Maio/2026'>
                </label>
            </div>
            <label>Resumo
                <textarea name='intro' rows='2'>{escape(values.get("intro", ""))}</textarea>
            </label>
            <label>Novidades / aplicacoes praticas, uma por linha
                <textarea name='items' rows='4'>{escape(chr(10).join(values.get("items", [])))}</textarea>
            </label>
            <div class='form-grid'>
                <label>Nome da fonte
                    <input name='source_title' value='{escape(values.get("source_title", ""))}' placeholder='Ex.: DHL Logistics Trend Radar'>
                </label>
                <label>Link da fonte
                    <input name='source_url' value='{escape(values.get("source_url", ""))}' placeholder='https://...'>
                </label>
            </div>
        </form>
        """

    body = f"""
    <style>
        .insight-editor-card {{
            border:1px solid #d6e0ec; border-radius:8px; padding:16px; margin:12px 0;
            background:#fff; box-shadow:0 8px 22px rgba(15,23,42,.05);
        }}
        .insight-editor-head {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:12px; }}
        .form-grid {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; }}
        .insight-editor-card label {{ display:block; font-weight:700; color:#0b1f3a; margin-top:10px; }}
        .insight-editor-card input, .insight-editor-card textarea {{
            width:100%; margin-top:6px; border:1px solid #cbd5e1; border-radius:7px; padding:10px 11px;
            font:inherit; color:#0b1f3a; background:#fff;
        }}
        .insight-editor-card textarea {{ resize:vertical; min-height:76px; }}
        @media (max-width: 760px) {{ .form-grid {{ grid-template-columns:1fr; }} }}
    </style>
    <div class='card'>
        <h2>Insights publicos</h2>
        <p class='muted'>Atualize mensalmente as novidades e fontes exibidas na tela publica. Apenas a administracao da plataforma visualiza esta manutencao.</p>
        <p><strong>{message}</strong></p>
        {forms}
    </div>
    """
    return layout("Insights publicos", body, "insights_publicos")


@web_bp.route("/motorista/assumir", methods=["POST"])
@web_login_required
def driver_assume_route_page():
    user = request.web_user
    if not is_driver(user):
        return layout("Acesso negado", "<div class='card'><h2>Apenas responsaveis pela rota podem assumir rota.</h2></div>", "motorista"), 403
    route = driver_route_for(user)
    if not route:
        return redirect(url_for("web_bp.driver_page"))
    from database.database import db
    route.status = "assumida"
    db.session.add(StopObservation(
        company_id=route.company_id,
        route_id=route.id,
        observation_type="rota_assumida",
        description=f"Rota assumida por {user.name} ({user.email}).",
        created_by_user_id=user.id,
    ))
    db.session.commit()
    return redirect(url_for("web_bp.driver_page"))



