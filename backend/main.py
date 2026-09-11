import os
from datetime import timedelta

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_migrate import Migrate, upgrade
from dotenv import load_dotenv
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.security import generate_password_hash

from database.database import db
from models import AuditLog, Client, Company, CompanyBase, CompanySubscription, LogisticRoute, ManualLocation, Merchandise, MerchandiseScan, PlatformEvent, PublicContactRequest, PublicInsight, ReportSnapshot, RouteBreak, RouteStop, RouteTransfer, StopObservation, StopProof, SubscriptionPlan, TelemetryEvent, User, Vehicle
from routes.client_routes import client_bp
from routes.company_routes import company_bp
from routes.route_routes import route_bp
from routes.user_routes import user_bp
from routes.vehicle_routes import vehicle_bp
from routes.web_routes import web_bp
from security.csrf import init_csrf

load_dotenv()

ENV = os.getenv("ENV", "development")
IS_PRODUCTION = ENV == "production"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///ris.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)
DEFAULT_SECRET_KEY = "ris_enterprise_2026"
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", DEFAULT_SECRET_KEY)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=int(os.getenv("SESSION_HOURS", "8")))
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "12")) * 1024 * 1024

if IS_PRODUCTION and app.config["SECRET_KEY"] == DEFAULT_SECRET_KEY:
    raise RuntimeError("Configure SECRET_KEY forte antes de rodar em producao.")
if IS_PRODUCTION and os.getenv("JWT_SECRET", "") in ("", "ris_jwt_enterprise_2026_multiempresa_chave_segura_local"):
    raise RuntimeError("Configure JWT_SECRET forte antes de rodar em producao.")
if IS_PRODUCTION and os.getenv("MASTER_PASSWORD", "RIS@12345") == "RIS@12345":
    raise RuntimeError("Configure MASTER_PASSWORD forte antes de rodar em producao.")


def allowed_cors_origins():
    configured = os.getenv("CORS_ORIGINS", "").strip()
    if configured:
        return [item.strip() for item in configured.split(",") if item.strip()]
    if IS_PRODUCTION:
        return ["https://app.rotainteligentesolucoes.com.br"]
    return [
        "http://127.0.0.1:5000",
        "http://localhost:5000",
        "https://app.rotainteligentesolucoes.com.br",
    ]

db.init_app(app)
migrate = Migrate(app, db, directory=os.path.join(os.path.dirname(__file__), "migrations"))
CORS(app, resources={r"/*": {"origins": allowed_cors_origins()}}, supports_credentials=True)
init_csrf(app)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(self), microphone=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
        "img-src 'self' data: blob: https://*.tile.openstreetmap.org https://tile.openstreetmap.org; "
        "font-src 'self' data: https://cdn.jsdelivr.net https://unpkg.com; "
        "connect-src 'self' https://api.openrouteservice.org; "
        "frame-src 'self' data: blob:; "
        "frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    )
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path in ("/", "/login") or request.path.startswith(("/web", "/motorista")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    return jsonify({"error": "Arquivo maior que o limite permitido."}), 413


@app.errorhandler(Exception)
def handle_application_error(error):
    if isinstance(error, HTTPException):
        return error
    try:
        user_id = None
        company_id = None
        user = None
        if "user_id" in request.cookies:
            user = None
        try:
            from flask import session
            user_id = session.get("user_id")
            if user_id:
                user = User.query.get(user_id)
                company_id = user.company_id if user else None
        except Exception:
            user = None
        db.session.add(PlatformEvent(
            severity="critical",
            category="system_error",
            title=error.__class__.__name__,
            message=str(error)[:4000],
            path=request.path,
            method=request.method,
            status_code=500,
            company_id=company_id,
            user_id=user.id if user else user_id,
            ip_address=request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", request.remote_addr),
            user_agent=(request.headers.get("User-Agent") or "")[:255],
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
    if request.path.startswith("/api"):
        return jsonify({"error": "Erro interno do sistema."}), 500
    return "<h1>Erro interno do sistema</h1><p>O evento foi registrado para suporte master.</p>", 500


@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "service": "ris",
        "environment": ENV,
    })


@app.route("/readyz")
def readyz():
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({
            "status": "ready",
            "database": "ok",
            "environment": ENV,
        })
    except Exception as exc:
        return jsonify({
            "status": "not_ready",
            "database": "error",
            "error": str(exc),
            "environment": ENV,
        }), 503

app.register_blueprint(user_bp)
app.register_blueprint(company_bp)
app.register_blueprint(vehicle_bp)
app.register_blueprint(client_bp)
app.register_blueprint(route_bp)
app.register_blueprint(web_bp)






def ensure_sqlite_schema():
    if not str(db.engine.url).startswith("sqlite"):
        return

    def columns(table_name):
        rows = db.session.execute(db.text(f"PRAGMA table_info({table_name})")).fetchall()
        return {row[1] for row in rows}

    migrations = {
        "users": {
            "base_id": "ALTER TABLE users ADD COLUMN base_id INTEGER",
            "can_view_finance": "ALTER TABLE users ADD COLUMN can_view_finance BOOLEAN DEFAULT 0",
            "active_session_id": "ALTER TABLE users ADD COLUMN active_session_id VARCHAR(64)",
            "active_session_started_at": "ALTER TABLE users ADD COLUMN active_session_started_at DATETIME",
            "active_session_ip": "ALTER TABLE users ADD COLUMN active_session_ip VARCHAR(64)",
            "active_session_user_agent": "ALTER TABLE users ADD COLUMN active_session_user_agent VARCHAR(255)",
            "terms_accepted_at": "ALTER TABLE users ADD COLUMN terms_accepted_at DATETIME",
            "terms_accepted_version": "ALTER TABLE users ADD COLUMN terms_accepted_version VARCHAR(40)",
            "terms_accepted_ip": "ALTER TABLE users ADD COLUMN terms_accepted_ip VARCHAR(64)",
            "created_at": "ALTER TABLE users ADD COLUMN created_at DATETIME",
        },
        "companies": {
            "created_at": "ALTER TABLE companies ADD COLUMN created_at DATETIME",
        },
        "clients": {
            "base_id": "ALTER TABLE clients ADD COLUMN base_id INTEGER",
            "email": "ALTER TABLE clients ADD COLUMN email VARCHAR(255)",
            "address": "ALTER TABLE clients ADD COLUMN address VARCHAR(500)",
            "delivery_window_start": "ALTER TABLE clients ADD COLUMN delivery_window_start VARCHAR(5)",
            "delivery_window_end": "ALTER TABLE clients ADD COLUMN delivery_window_end VARCHAR(5)",
            "priority": "ALTER TABLE clients ADD COLUMN priority VARCHAR(20) DEFAULT 'media'",
            "active": "ALTER TABLE clients ADD COLUMN active BOOLEAN DEFAULT 1",
            "created_at": "ALTER TABLE clients ADD COLUMN created_at DATETIME",
        },
        "vehicles": {
            "base_id": "ALTER TABLE vehicles ADD COLUMN base_id INTEGER",
            "active": "ALTER TABLE vehicles ADD COLUMN active BOOLEAN DEFAULT 1",
            "created_at": "ALTER TABLE vehicles ADD COLUMN created_at DATETIME",
        },
        "logistic_routes": {
            "base_id": "ALTER TABLE logistic_routes ADD COLUMN base_id INTEGER",
            "driver_user_id": "ALTER TABLE logistic_routes ADD COLUMN driver_user_id INTEGER",
            "planned_date": "ALTER TABLE logistic_routes ADD COLUMN planned_date VARCHAR(10)",
            "start_time": "ALTER TABLE logistic_routes ADD COLUMN start_time VARCHAR(5) DEFAULT '08:00'",
            "actual_start_time": "ALTER TABLE logistic_routes ADD COLUMN actual_start_time VARCHAR(5)",
            "actual_end_time": "ALTER TABLE logistic_routes ADD COLUMN actual_end_time VARCHAR(5)",
            "estimated_distance_km": "ALTER TABLE logistic_routes ADD COLUMN estimated_distance_km FLOAT DEFAULT 0",
            "estimated_duration_minutes": "ALTER TABLE logistic_routes ADD COLUMN estimated_duration_minutes INTEGER DEFAULT 0",
            "created_at": "ALTER TABLE logistic_routes ADD COLUMN created_at DATETIME",
        },
        "route_stops": {
            "planned_arrival": "ALTER TABLE route_stops ADD COLUMN planned_arrival VARCHAR(5)",
            "service_minutes": "ALTER TABLE route_stops ADD COLUMN service_minutes INTEGER DEFAULT 10",
            "service_description": "ALTER TABLE route_stops ADD COLUMN service_description TEXT",
            "status": "ALTER TABLE route_stops ADD COLUMN status VARCHAR(30) DEFAULT 'pendente'",
        },
        "stop_proofs": {
            "invoice_number": "ALTER TABLE stop_proofs ADD COLUMN invoice_number VARCHAR(120)",
            "invoice_link": "ALTER TABLE stop_proofs ADD COLUMN invoice_link VARCHAR(1000)",
            "invoice_file_filename": "ALTER TABLE stop_proofs ADD COLUMN invoice_file_filename VARCHAR(255)",
            "fiscal_status": "ALTER TABLE stop_proofs ADD COLUMN fiscal_status VARCHAR(40) DEFAULT 'pendente'",
        },
        "public_contact_requests": {
            "request_type": "ALTER TABLE public_contact_requests ADD COLUMN request_type VARCHAR(40) DEFAULT 'contato'",
            "company_document": "ALTER TABLE public_contact_requests ADD COLUMN company_document VARCHAR(120)",
            "financial_responsible": "ALTER TABLE public_contact_requests ADD COLUMN financial_responsible VARCHAR(255)",
            "address_proof_filename": "ALTER TABLE public_contact_requests ADD COLUMN address_proof_filename VARCHAR(255)",
            "document_proof_filename": "ALTER TABLE public_contact_requests ADD COLUMN document_proof_filename VARCHAR(255)",
        },
        "manual_locations": {
            "base_id": "ALTER TABLE manual_locations ADD COLUMN base_id INTEGER",
        },
        "merchandises": {
            "base_id": "ALTER TABLE merchandises ADD COLUMN base_id INTEGER",
        },
        "company_subscriptions": {
            "payment_method": "ALTER TABLE company_subscriptions ADD COLUMN payment_method VARCHAR(20) DEFAULT 'pix' NOT NULL",
            "payment_status": "ALTER TABLE company_subscriptions ADD COLUMN payment_status VARCHAR(30) DEFAULT 'pending' NOT NULL",
            "payment_reference": "ALTER TABLE company_subscriptions ADD COLUMN payment_reference VARCHAR(120)",
            "last_payment_at": "ALTER TABLE company_subscriptions ADD COLUMN last_payment_at DATETIME",
        },
    }

    for table_name, table_migrations in migrations.items():
        existing = columns(table_name)
        if not existing:
            continue
        for column_name, sql in table_migrations.items():
            if column_name not in existing:
                db.session.execute(db.text(sql))
    db.session.commit()


def ensure_postgres_schema():
    if not str(db.engine.url).startswith("postgresql"):
        return

    migrations = {
        "users": {
            "can_view_finance": "ALTER TABLE users ADD COLUMN can_view_finance BOOLEAN DEFAULT FALSE",
        },
        "company_subscriptions": {
            "payment_method": "ALTER TABLE company_subscriptions ADD COLUMN payment_method VARCHAR(20) DEFAULT 'pix' NOT NULL",
            "payment_status": "ALTER TABLE company_subscriptions ADD COLUMN payment_status VARCHAR(30) DEFAULT 'pending' NOT NULL",
            "payment_reference": "ALTER TABLE company_subscriptions ADD COLUMN payment_reference VARCHAR(120)",
            "last_payment_at": "ALTER TABLE company_subscriptions ADD COLUMN last_payment_at TIMESTAMP",
        },
    }

    for table_name, table_migrations in migrations.items():
        existing = db.session.execute(db.text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table_name
        """), {"table_name": table_name}).fetchall()
        existing_columns = {row[0] for row in existing}
        if not existing_columns:
            continue
        for column_name, sql in table_migrations.items():
            if column_name not in existing_columns:
                db.session.execute(db.text(sql))
    db.session.commit()


DEFAULT_SUBSCRIPTION_PLANS = (
    ("Entrada", "Para operacao inicial com uma empresa, rotas e relatorios basicos.", 299.0, 1, 2, 10, 250, "rotas,clientes,motorista,relatorio"),
    ("Profissional", "Para empresas com varias bases, mercadorias, comprovantes e torre de controle.", 699.0, 1, 8, 40, 1500, "rotas,mercadorias,comprovantes,telemetria,relatorio"),
    ("Enterprise", "Para operacao personalizada, suporte avancado e expansao SaaS.", 0.0, None, None, None, None, "multiempresa,api,monitoramento,avancado"),
)


def seed_subscription_plans():
    """Cria o catalogo comercial mesmo quando os dados de demonstracao estao desativados."""
    for name, description, price, companies, bases, users, routes_month, features in DEFAULT_SUBSCRIPTION_PLANS:
        if not SubscriptionPlan.query.filter_by(name=name).first():
            db.session.add(SubscriptionPlan(
                name=name,
                description=description,
                monthly_price=price,
                max_companies=companies,
                max_bases=bases,
                max_users=users,
                max_routes_month=routes_month,
                features_json=features,
                active=True,
            ))
    db.session.commit()


def seed_demo_data():

    company = Company.query.filter_by(email="demo@ris.local").first()
    if not company:
        company = Company(name="Empresa Demonstracao", email="demo@ris.local", active=True)
        db.session.add(company)
        db.session.flush()

    demo_base = CompanyBase.query.filter_by(company_id=company.id, name="Base Demonstracao").first()
    if not demo_base:
        demo_base = CompanyBase(
            company_id=company.id,
            name="Base Demonstracao",
            base_type="matriz",
            city="Santa Maria",
            active=True,
        )
        db.session.add(demo_base)
        db.session.flush()

    if not CompanySubscription.query.filter_by(company_id=company.id).first():
        starter_plan = SubscriptionPlan.query.filter_by(name="Profissional").first()
        db.session.add(CompanySubscription(
            company_id=company.id,
            plan_id=starter_plan.id if starter_plan else None,
            status="trial",
            billing_email=company.email,
            notes="Assinatura demonstrativa criada automaticamente.",
        ))

    demo_users = [
        ("Operador Demo", "operador@ris.local", "operador"),
        ("Gerente Demo", "gerente@ris.local", "gerente"),
        ("CEO Demo", "ceo@ris.local", "ceo"),
        ("Motorista Rota 1", "motorista1@ris.local", "motorista"),
        ("Motorista Rota 2", "motorista2@ris.local", "motorista"),
        ("Responsavel Rota 1", "rota1", "motorista"),
        ("Responsavel Rota 2", "rota2", "motorista"),
    ]
    for name, email, role in demo_users:
        existing_user = User.query.filter_by(email=email).first()
        if not existing_user:
            password = email if email.startswith("rota") else "RIS@12345"
            db.session.add(User(
                company_id=company.id,
                base_id=demo_base.id,
                name=name,
                email=email,
                password=generate_password_hash(password),
                role=role,
                active=True,
            ))
        elif existing_user.company_id == company.id and not existing_user.base_id:
            existing_user.base_id = demo_base.id

    vehicle_specs = [
        ("Van Demo", "van", 80),
        ("Caminhao Demo", "caminhao", 60),
        ("Moto Demo", "moto", 70),
        ("Carro Demo", "carro", 80),
    ]
    vehicle_map = {}
    for name, vehicle_type, speed in vehicle_specs:
        vehicle = Vehicle.query.filter_by(company_id=company.id, name=name).first()
        if not vehicle:
            vehicle = Vehicle(company_id=company.id, base_id=demo_base.id, name=name, vehicle_type=vehicle_type, average_speed_kmh=speed, active=True)
            db.session.add(vehicle)
            db.session.flush()
        elif not vehicle.base_id:
            vehicle.base_id = demo_base.id
        vehicle_map[name] = vehicle

    client_specs = [
        ("Cliente Demo A", -29.690000, -53.800000, "08:00", "10:00", "alta"),
        ("Cliente Demo B", -29.700000, -53.790000, "10:00", "12:00", "media"),
        ("Cliente Demo C", -29.710000, -53.780000, "13:00", "15:00", "baixa"),
        ("Cliente Demo D", -29.690000, -53.810000, "09:00", "11:00", "alta"),
        ("Cliente Demo E", -29.700000, -53.815000, "11:00", "13:00", "media"),
        ("Cliente Demo F", -29.710000, -53.820000, "14:00", "16:00", "baixa"),
    ]
    client_map = {}
    for name, lat, lon, start, end, priority in client_specs:
        client = Client.query.filter_by(company_id=company.id, name=name).first()
        if not client:
            client = Client(
                company_id=company.id,
                base_id=demo_base.id,
                name=name,
                latitude=round(lat, 6),
                longitude=round(lon, 6),
                delivery_window_start=start,
                delivery_window_end=end,
                priority=priority,
                active=True,
            )
            db.session.add(client)
            db.session.flush()
        elif not client.base_id:
            client.base_id = demo_base.id
        client_map[name] = client

    driver1 = User.query.filter_by(email="motorista1@ris.local").first()
    driver2 = User.query.filter_by(email="motorista2@ris.local").first()
    rota1_user = User.query.filter_by(email="rota1").first()
    rota2_user = User.query.filter_by(email="rota2").first()

    route_specs = [
        ("Rota Demo 1", "Carlos Silva", rota1_user or driver1, "planejada", vehicle_map["Van Demo"], ["Cliente Demo A", "Cliente Demo B", "Cliente Demo C"]),
        ("Rota Demo 2", "Marcos Lima", rota2_user or driver2, "planejada", vehicle_map["Caminhao Demo"], ["Cliente Demo D", "Cliente Demo E", "Cliente Demo F"]),
    ]
    for route_name, driver, driver_user, status, vehicle, stop_names in route_specs:
        route = LogisticRoute.query.filter_by(company_id=company.id, name=route_name).first()
        if not route:
            route = LogisticRoute(company_id=company.id, base_id=demo_base.id, vehicle_id=vehicle.id, driver_user_id=driver_user.id if driver_user else None, name=route_name, driver_name=driver, status=status)
            db.session.add(route)
            db.session.flush()
            for sequence, client_name in enumerate(stop_names, start=1):
                db.session.add(RouteStop(route_id=route.id, client_id=client_map[client_name].id, sequence=sequence))
        elif driver_user:
            route.driver_user_id = driver_user.id
            if not route.base_id:
                route.base_id = demo_base.id

    db.session.commit()

def seed_master_user():
    master_email = os.getenv("MASTER_EMAIL", "rotainteligentesolucoes@gmail.com").lower()
    master_password = os.getenv("MASTER_PASSWORD", "RIS@12345")
    master_name = os.getenv("MASTER_NAME", "Rota Inteligente Solucoes")

    user = User.query.filter_by(email=master_email).first()
    if not user:
        db.session.add(User(
            company_id=None,
            name=master_name,
            email=master_email,
            password=generate_password_hash(master_password),
            role="master",
            active=True,
        ))
    else:
        user.role = "master"
        user.company_id = None
        user.active = True

    backup_master_email = "master@ris.local"
    backup_user = User.query.filter_by(email=backup_master_email).first()
    if not backup_user:
        db.session.add(User(
            company_id=None,
            name="Master RIS Local",
            email=backup_master_email,
            password=generate_password_hash(master_password),
            role="master",
            active=True,
        ))
    else:
        backup_user.role = "master"
        backup_user.company_id = None
        backup_user.active = True

    db.session.commit()


PUBLIC_INSIGHT_DEFAULTS = {
    "resultado": {
        "title": "Resultado",
        "intro": "Em 2026, resultado logistico significa acompanhar prazo, custo, excecoes e comprovacao enquanto a rota ainda esta em execucao.",
        "items": "\n".join([
            "Medir SLA por rota, cliente e motorista durante o dia, nao apenas no fechamento.",
            "Usar comprovante digital com GPS, horario, foto e assinatura para reduzir contestacao.",
            "Alertas antecipados ajudam a agir antes que atraso, bateria baixa ou falta de sinal virem custo.",
        ]),
        "source_title": "OneRail - 7 Last Mile Delivery Trends 2026",
        "source_url": "https://www.onerail.com/7-last-mile-delivery-trends-what-to-expect-in-2026/",
        "source_date": "Maio/2026",
    },
    "estrategia": {
        "title": "Estrategia",
        "intro": "A estrategia logistica de 2026 combina resiliencia, visibilidade ponta a ponta e simulacao antes de colocar a operacao na rua.",
        "items": "\n".join([
            "Planejar por faixas de atendimento, risco de atraso, capacidade e alternativas de transporte.",
            "Comparar cenarios com mais veiculos, menos km, novas janelas e prioridade por cliente.",
            "Usar torre de controle para enxergar gargalos antes de afetarem o SLA.",
        ]),
        "source_title": "UPS Supply Chain Outlook 2026",
        "source_url": "https://developer.ups.com/us/en/supplychain/resources/news-and-market-updates/2026-supply-chain-outlook",
        "source_date": "Maio/2026",
    },
    "dados": {
        "title": "Dados",
        "intro": "Dados deixam de ser apenas historico: passam a orientar decisao, auditoria, previsao e atendimento em tempo real.",
        "items": "\n".join([
            "Integrar transporte, armazem, financeiro e operacao cria uma visao unica do servico.",
            "Telemetria mostra caminho real, velocidade, bateria, perda de sinal e horario de passagem.",
            "Historico de 90 dias ajuda a identificar atrasos recorrentes, produtividade e excecoes.",
        ]),
        "source_title": "Aptean - Logistics Trends Shaping 2026 and Beyond",
        "source_url": "https://www.aptean.com/en-US/insights/blog/logistics-trends",
        "source_date": "Maio/2026",
    },
    "logistica": {
        "title": "Logistica",
        "intro": "A logistica moderna junta roteirizacao, execucao, estoque, cubagem e atendimento em uma operacao coordenada.",
        "items": "\n".join([
            "IA como copiloto ajuda no planejamento de armazem, separacao, recursos e nivel de servico.",
            "Carga nao empilhavel, area de piso, m3 e peso mudam a escolha do veiculo.",
            "O proximo passo e conectar WMS leve, rotas, comprovantes e torre de controle.",
        ]),
        "source_title": "KNAPP AI Trends for Warehouse Logistics 2026",
        "source_url": "https://www.knapp.com/en/insights/blog/ai-trends-logistics-2026/",
        "source_date": "Maio/2026",
    },
    "economia": {
        "title": "Economia",
        "intro": "Economia em 2026 vem de rota dinamica, ocupacao correta do veiculo, menos retrabalho e decisao baseada em dados.",
        "items": "\n".join([
            "Roteirizacao com IA reotimiza por trafego, clima, janela, motorista, veiculo e prioridade.",
            "Cubagem, litros, peso e area ocupada reduzem erro de veiculo e custo por viagem.",
            "Numero ideal de veiculos evita frota sobrando, capacidade faltando e retorno improdutivo.",
        ]),
        "source_title": "OneRail - AI Route Optimization 2026",
        "source_url": "https://www.onerail.com/7-last-mile-delivery-trends-what-to-expect-in-2026/",
        "source_date": "Maio/2026",
    },
    "pessoas": {
        "title": "Pessoas",
        "intro": "Tecnologia logistica tambem precisa melhorar a vida de quem empreende, coordena e executa o trabalho todos os dias.",
        "items": "\n".join([
            "Rotas previsiveis, informacao clara e menos retrabalho reduzem pressao sobre motorista, operador e gestor.",
            "Bem-estar e engajamento devem entrar no painel junto com prazo, custo, seguranca e qualidade.",
            "Quando a operacao e organizada, o empresario ganha tempo para estrategia e o colaborador trabalha com mais dignidade.",
        ]),
        "source_title": "Gallup - State of the Global Workplace 2026",
        "source_url": "https://www.gallup.com/workplace/697904/state-of-the-global-workplace-global-data.aspx",
        "source_date": "Maio/2026",
    },
}


def seed_public_insights():
    for key, data in PUBLIC_INSIGHT_DEFAULTS.items():
        if not PublicInsight.query.filter_by(insight_key=key).first():
            db.session.add(PublicInsight(insight_key=key, **data))
    db.session.commit()


with app.app_context():
    db.create_all()
    ensure_sqlite_schema()
    ensure_postgres_schema()
    if os.getenv("RUN_MIGRATIONS", "true").strip().lower() in ("1", "true", "yes", "sim"):
        upgrade(directory=os.path.join(os.path.dirname(__file__), "migrations"))
    if os.getenv("RESTORE_MODE", "false").strip().lower() not in ("1", "true", "yes", "sim"):
        seed_master_user()
        seed_public_insights()
        seed_subscription_plans()
        if os.getenv("SEED_DEMO_DATA", "true").lower() == "true":
            seed_demo_data()

from services.backup_scheduler import start_backup_scheduler

start_backup_scheduler(app)


@app.route("/api/status")
def home():
    return jsonify({
        "status": "RIS ONLINE",
        "database": "CONNECTED",
        "multiempresa": True,
        "security": "master ve tudo; empresas veem somente seus company_id",
    })


if __name__ == "__main__":
    app.run(debug=not IS_PRODUCTION, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))









