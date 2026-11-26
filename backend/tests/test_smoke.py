import os
import re
import tempfile
import unittest
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

import jwt


_database_file = tempfile.NamedTemporaryFile(prefix="ris-test-", suffix=".db", delete=False)
_database_file.close()

os.environ["DATABASE_URL"] = f"sqlite:///{_database_file.name.replace(os.sep, '/')}"
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["RUN_MIGRATIONS"] = "true"
os.environ["BACKUP_ENABLED"] = "false"
os.environ["APP_ENV"] = "testing"
os.environ["AUTO_INITIALIZE_DATABASE"] = "true"

from main import app, db, initialize_database  # noqa: E402
from models.client import Client  # noqa: E402
from models.company import Company, CompanyBase  # noqa: E402
from models.route import LogisticRoute  # noqa: E402
from models.saas import AuditLog, CompanySubscription, SubscriptionPayment, SubscriptionPlan  # noqa: E402
from models.user import User  # noqa: E402
from models.vehicle import Vehicle  # noqa: E402
from routes.web_routes import validate_upload  # noqa: E402
from auth.security import password_validation_error  # noqa: E402
from storage.files import latest_stored_file, stored_file_info  # noqa: E402
from werkzeug.datastructures import FileStorage  # noqa: E402


class ApplicationSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True)
        cls.client = app.test_client()
        with app.app_context():
            company = Company(name="Empresa Teste de Seguranca", email="seguranca@ris.test")
            db.session.add(company)
            db.session.flush()
            driver = User(
                company_id=company.id,
                name="Motorista Teste",
                email="motorista-api@ris.test",
                password="nao-utilizada",
                role="motorista",
                active=True,
                active_session_id="sessao-teste-motorista",
            )
            other_driver = User(
                company_id=company.id,
                name="Outro Motorista",
                email="outro-motorista@ris.test",
                password="nao-utilizada",
                role="motorista",
                active=True,
            )
            vehicle = Vehicle(
                company_id=company.id,
                name="Veiculo Teste",
                vehicle_type="van",
                average_speed_kmh=60,
                active=True,
            )
            db.session.add_all([driver, other_driver, vehicle])
            first_base = CompanyBase(company_id=company.id, name="Base Permitida", active=True)
            second_base = CompanyBase(company_id=company.id, name="Base Restrita", active=True)
            db.session.add_all([first_base, second_base])
            db.session.flush()
            manager = User(
                company_id=company.id,
                base_id=first_base.id,
                name="Gerente de Base",
                email="gerente-base@ris.test",
                password="nao-utilizada",
                role="gerente",
                active=True,
                active_session_id="sessao-gerente-base",
            )
            restricted_client = Client(
                company_id=company.id,
                base_id=second_base.id,
                name="Cliente de Outra Base",
                latitude=-23.5,
                longitude=-46.6,
                active=True,
            )
            db.session.add_all([manager, restricted_client])
            db.session.commit()
            other_route = LogisticRoute(
                company_id=company.id,
                vehicle_id=vehicle.id,
                driver_user_id=other_driver.id,
                name="Rota de Outro Motorista",
                status="planned",
            )
            db.session.add(other_route)
            db.session.commit()
            cls.driver_id = driver.id
            cls.driver_session_id = driver.active_session_id
            cls.other_route_id = other_route.id
            cls.driver_token = jwt.encode(
                {"sub": str(driver.id), "sid": driver.active_session_id},
                os.getenv("JWT_SECRET", "ris_jwt_enterprise_2026_multiempresa_chave_segura_local"),
                algorithm="HS256",
            )
            cls.manager_id = manager.id
            cls.manager_session_id = manager.active_session_id
            cls.restricted_client_id = restricted_client.id

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        try:
            os.remove(_database_file.name)
        except FileNotFoundError:
            pass

    def test_health_and_readiness(self):
        health = self.client.get("/healthz")
        readiness = self.client.get("/readyz")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json()["status"], "ok")
        self.assertEqual(readiness.status_code, 200)
        self.assertEqual(readiness.get_json()["database"], "ok")

    def test_database_initialization_is_idempotent(self):
        initialize_database()
        initialize_database()
        with app.app_context():
            self.assertEqual(SubscriptionPlan.query.filter_by(name="Entrada").count(), 1)

    def test_login_page_receives_csrf_token(self):
        response = self.client.get("/login")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertRegex(html, r'name="csrf_token" value="[^"]+"')
        self.assertIn('name="csrf-token"', html)

    def test_login_post_rejects_missing_csrf_token(self):
        response = self.client.post("/login", data={})

        self.assertEqual(response.status_code, 400)

    def test_login_post_accepts_valid_csrf_token(self):
        page = self.client.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = self.client.post(
            "/login",
            data={"email": "inexistente@example.com", "password": "invalida", "csrf_token": token},
        )

        self.assertNotEqual(response.status_code, 400)

    def test_public_status_endpoint(self):
        response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "RIS ONLINE")

    def test_master_can_open_saas_production_page(self):
        with app.app_context():
            master = User.query.filter_by(role="master").first()
            master_id = master.id
        with self.client.session_transaction() as web_session:
            web_session["user_id"] = master_id

        response = self.client.get("/web/saas-producao")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Backup e restauracao", response.get_data(as_text=True))
        self.assertNotIn('href="/web/minha-assinatura"', response.get_data(as_text=True))

    def test_master_subscription_link_returns_to_saas_administration(self):
        with app.app_context():
            master = User.query.filter_by(role="master").first()
            master_id = master.id
        with self.client.session_transaction() as web_session:
            web_session["user_id"] = master_id

        response = self.client.get("/web/minha-assinatura", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/web/saas-producao"))

    def test_master_can_register_monthly_pix_charge(self):
        with app.app_context():
            master = User.query.filter_by(role="master").first()
            company = Company.query.filter_by(email="seguranca@ris.test").first()
            plan = SubscriptionPlan.query.filter_by(name="Profissional").first()
            subscription = CompanySubscription.query.filter_by(company_id=company.id).first()
            if not subscription:
                subscription = CompanySubscription(company_id=company.id, plan_id=plan.id)
                db.session.add(subscription)
                db.session.commit()
            master_id = master.id
            company_id = company.id
        csrf_token = "csrf-pix-history"
        with self.client.session_transaction() as web_session:
            web_session["user_id"] = master_id
            web_session["_csrf_token"] = csrf_token

        response = self.client.post(
            "/web/saas-producao",
            data={
                "csrf_token": csrf_token,
                "form_action": "payment",
                "company_id": company_id,
                "billing_period": "2026-10",
                "due_date": "2026-10-10",
                "amount": "699.50",
                "payment_status": "paid",
                "payment_reference": "PIX-TESTE-2026-10",
            },
        )

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            payment = SubscriptionPayment.query.filter_by(company_id=company_id, billing_period="2026-10").one()
            self.assertEqual(payment.amount, Decimal("699.50"))
            self.assertEqual(payment.status, "paid")
            self.assertIsNotNone(payment.paid_at)

    def test_commercial_plans_exist_without_demo_data(self):
        with app.app_context():
            plans = {
                plan.name: plan
                for plan in SubscriptionPlan.query.filter_by(active=True).all()
            }

        self.assertEqual(set(plans), {"Entrada", "Profissional", "Enterprise"})
        self.assertEqual(plans["Entrada"].monthly_price, 299.0)
        self.assertEqual(plans["Profissional"].monthly_price, 699.0)
        self.assertEqual(plans["Enterprise"].monthly_price, 0.0)

    def test_subscription_uses_pix_only_by_default(self):
        with app.app_context():
            company = Company.query.filter_by(email="seguranca@ris.test").first()
            plan = SubscriptionPlan.query.filter_by(name="Entrada").first()
            subscription = CompanySubscription(company_id=company.id, plan_id=plan.id)
            db.session.add(subscription)
            db.session.flush()

            self.assertEqual(subscription.payment_method, "pix")
            self.assertEqual(subscription.payment_status, "pending")
            db.session.rollback()

    def test_subscription_payment_history_uses_decimal_and_one_row_per_period(self):
        with app.app_context():
            company = Company.query.filter_by(email="seguranca@ris.test").first()
            plan = SubscriptionPlan.query.filter_by(name="Entrada").first()
            subscription = CompanySubscription.query.filter_by(company_id=company.id).first()
            if not subscription:
                subscription = CompanySubscription(company_id=company.id, plan_id=plan.id)
                db.session.add(subscription)
                db.session.flush()
            payment = SubscriptionPayment(
                subscription_id=subscription.id,
                company_id=company.id,
                billing_period="2026-09",
                due_date="2026-09-10",
                amount=Decimal("299.90"),
            )
            db.session.add(payment)
            db.session.commit()

            stored = SubscriptionPayment.query.filter_by(subscription_id=subscription.id, billing_period="2026-09").one()
            self.assertEqual(stored.amount, Decimal("299.90"))
            self.assertEqual(stored.payment_method, "pix")

    def test_manager_cannot_delete_client_from_another_base(self):
        csrf_token = "csrf-gerente-base"
        with self.client.session_transaction() as web_session:
            web_session["user_id"] = self.manager_id
            web_session["session_id"] = self.manager_session_id
            web_session["_csrf_token"] = csrf_token

        response = self.client.post(
            "/web/clientes/excluir",
            data={"client_id": self.restricted_client_id, "csrf_token": csrf_token},
        )

        self.assertEqual(response.status_code, 403)
        with app.app_context():
            client = db.session.get(Client, self.restricted_client_id)
            self.assertTrue(client.active)

    def test_upload_validation_rejects_fake_pdf_and_accepts_pdf_signature(self):
        fake = FileStorage(stream=BytesIO(b"arquivo malicioso"), filename="documento.pdf")
        valid = FileStorage(stream=BytesIO(b"%PDF-1.7\nconteudo"), filename="documento.pdf")

        _filename, fake_error = validate_upload(fake, {".pdf"}, 1)
        valid_filename, valid_error = validate_upload(valid, {".pdf"}, 1)

        self.assertIsNotNone(fake_error)
        self.assertEqual(valid_filename, "documento.pdf")
        self.assertIsNone(valid_error)

    def test_new_password_policy_requires_ten_characters(self):
        self.assertIsNotNone(password_validation_error("curta123"))
        self.assertIsNone(password_validation_error("senha-segura-2026"))

    def test_s3_backup_metadata_survives_without_local_file(self):
        client = MagicMock()
        client.head_object.return_value = {"ContentLength": 54321}
        client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "ris/backups/ris_backup_20260920_024302.json", "Size": 54321},
                {"Key": "ris/backups/ris_backup_20260921_024302.json", "Size": 65432},
            ]
        }
        with patch.dict(os.environ, {"STORAGE_BACKEND": "s3", "S3_BUCKET": "ris-test"}), \
                patch("storage.files._s3_client", return_value=client):
            info = stored_file_info("backups", "ris_backup_20260921_024302.json")
            latest = latest_stored_file("backups", prefix="ris_backup_", suffix=".json")

        self.assertEqual(info["size"], 54321)
        self.assertEqual(latest["name"], "ris_backup_20260921_024302.json")
        self.assertEqual(latest["size"], 65432)

    def test_driver_cannot_create_operational_records(self):
        headers = {"Authorization": f"Bearer {self.driver_token}"}
        requests = (
            ("/clients", {"name": "Cliente", "latitude": -23.5, "longitude": -46.6}),
            ("/vehicles", {"vehicle_type": "van"}),
            ("/routes", {"name": "Rota", "vehicle_id": 1}),
        )

        for endpoint, payload in requests:
            with self.subTest(endpoint=endpoint):
                response = self.client.post(endpoint, json=payload, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertIn("Motoristas nao podem criar", response.get_json()["error"])

    def test_driver_cannot_open_another_route_and_attempt_is_audited(self):
        with self.client.session_transaction() as web_session:
            web_session["user_id"] = self.driver_id
            web_session["session_id"] = self.driver_session_id

        with patch("routes.web_routes.send_route_access_alert_async") as send_alert:
            response = self.client.get(f"/web/mapa/{self.other_route_id}")

        self.assertEqual(response.status_code, 403)
        with app.app_context():
            event = AuditLog.query.filter_by(
                action="driver_unauthorized_route_access",
                user_id=self.driver_id,
                resource_id=str(self.other_route_id),
            ).first()
            self.assertIsNotNone(event)
            self.assertIn("Motorista Teste", event.details)
        send_alert.assert_called_once()

if __name__ == "__main__":
    unittest.main()
