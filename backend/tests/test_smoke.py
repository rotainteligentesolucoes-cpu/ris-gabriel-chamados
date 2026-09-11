import os
import re
import tempfile
import unittest
from unittest.mock import patch

import jwt


_database_file = tempfile.NamedTemporaryFile(prefix="ris-test-", suffix=".db", delete=False)
_database_file.close()

os.environ["DATABASE_URL"] = f"sqlite:///{_database_file.name.replace(os.sep, '/')}"
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["RUN_MIGRATIONS"] = "true"
os.environ["BACKUP_ENABLED"] = "false"
os.environ["APP_ENV"] = "testing"

from main import app, db  # noqa: E402
from models.company import Company  # noqa: E402
from models.route import LogisticRoute  # noqa: E402
from models.saas import AuditLog, CompanySubscription, SubscriptionPlan  # noqa: E402
from models.user import User  # noqa: E402
from models.vehicle import Vehicle  # noqa: E402


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
