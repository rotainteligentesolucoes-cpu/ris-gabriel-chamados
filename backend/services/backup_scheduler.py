import os
import threading
from datetime import datetime, timedelta

from database.database import db
from models.saas import AuditLog, PlatformEvent


BACKUP_LOCK_ID = 7_420_260_611
SUCCESS_TITLE = "Backup automatico diario concluido"


def _enabled():
    return os.getenv("BACKUP_ENABLED", "").strip().lower() in ("1", "true", "sim", "yes")


def _last_success():
    return (
        PlatformEvent.query
        .filter_by(category="backup", title=SUCCESS_TITLE, resolved=True)
        .order_by(PlatformEvent.created_at.desc())
        .first()
    )


def _backup_due():
    last_event = _last_success()
    if not last_event or not last_event.created_at:
        return True
    interval_hours = max(1, int(os.getenv("BACKUP_INTERVAL_HOURS", "24")))
    return last_event.created_at <= datetime.utcnow() - timedelta(hours=interval_hours)


def _acquire_lock():
    if str(db.engine.url).startswith("postgresql"):
        connection = db.engine.connect()
        acquired = bool(
            connection.execute(
                db.text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": BACKUP_LOCK_ID},
            ).scalar()
        )
        if not acquired:
            connection.close()
            return None
        return connection

    lock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".backup_daily.lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        return descriptor, lock_path
    except FileExistsError:
        try:
            if datetime.fromtimestamp(os.path.getmtime(lock_path)) < datetime.now() - timedelta(hours=2):
                os.remove(lock_path)
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, str(os.getpid()).encode("ascii"))
                return descriptor, lock_path
        except (FileNotFoundError, FileExistsError, OSError):
            pass
        return None


def _release_lock(lock):
    if lock is None:
        return
    if isinstance(lock, tuple):
        descriptor, lock_path = lock
        os.close(descriptor)
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass
        return
    try:
        lock.execute(db.text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": BACKUP_LOCK_ID})
    finally:
        lock.close()


def run_daily_backup(app):
    if not _enabled():
        return False

    with app.app_context():
        if not _backup_due():
            return False
        lock = _acquire_lock()
        if lock is None:
            return False
        try:
            if not _backup_due():
                return False

            # Imported here to avoid coupling application startup to the web blueprint.
            from routes.web_routes import generate_logical_backup

            filename, _path, payload = generate_logical_backup()
            total_rows = sum(len(rows) for rows in payload["tables"].values())
            db.session.add(PlatformEvent(
                severity="info",
                category="backup",
                title=SUCCESS_TITLE,
                message=f"Rotina diaria gerou {filename} com {total_rows} registros exportados.",
                path=filename,
                resolved=True,
            ))
            db.session.add(AuditLog(
                action="backup_auto_generate",
                resource_type="backup",
                resource_id=filename,
                details=f"Backup automatico diario com {total_rows} registros.",
            ))
            db.session.commit()
            app.logger.info("Backup automatico diario concluido: %s", filename)
            return True
        except Exception as exc:
            db.session.rollback()
            try:
                db.session.add(PlatformEvent(
                    severity="critical",
                    category="backup",
                    title="Falha no backup automatico diario",
                    message=str(exc)[:2000],
                    resolved=False,
                ))
                db.session.commit()
            except Exception:
                db.session.rollback()
            app.logger.exception("Falha no backup automatico diario")
            return False
        finally:
            _release_lock(lock)


def start_backup_scheduler(app):
    if not _enabled():
        app.logger.info("Backup automatico desativado (BACKUP_ENABLED=false).")
        return None

    start_delay = max(5, int(os.getenv("BACKUP_START_DELAY_SECONDS", "30")))
    check_seconds = max(60, int(os.getenv("BACKUP_CHECK_SECONDS", "900")))

    def worker():
        if threading.Event().wait(start_delay):
            return
        while True:
            run_daily_backup(app)
            threading.Event().wait(check_seconds)

    thread = threading.Thread(target=worker, name="ris-daily-backup", daemon=True)
    thread.start()
    app.logger.info("Rotina de backup diario ativa; verificacao a cada %ss.", check_seconds)
    return thread
