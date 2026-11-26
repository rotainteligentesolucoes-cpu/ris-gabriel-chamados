import os
import threading
from datetime import datetime, timedelta

from database.database import db
from models.saas import AuditLog, PlatformEvent


BACKUP_LOCK_ID = 7_420_260_611
SCHEDULER_LEADER_LOCK_ID = 7_420_260_612
SUCCESS_TITLE = "Backup automatico diario concluido"
_scheduler_thread = None
_scheduler_stop_event = None
_scheduler_leader_lock = None


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


def _acquire_scheduler_leader():
    if str(db.engine.url).startswith("postgresql"):
        connection = db.engine.connect()
        acquired = bool(
            connection.execute(
                db.text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": SCHEDULER_LEADER_LOCK_ID},
            ).scalar()
        )
        if not acquired:
            connection.close()
            return None
        return connection

    lock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".backup_scheduler.lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        return descriptor, lock_path
    except FileExistsError:
        return None


def _release_scheduler_leader(lock):
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
        lock.execute(db.text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": SCHEDULER_LEADER_LOCK_ID})
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


def start_backup_scheduler(app, singleton=False):
    global _scheduler_thread, _scheduler_stop_event, _scheduler_leader_lock
    if not _enabled():
        app.logger.info("Backup automatico desativado (BACKUP_ENABLED=false).")
        return None
    if _scheduler_thread and _scheduler_thread.is_alive():
        return _scheduler_thread

    if singleton:
        with app.app_context():
            _scheduler_leader_lock = _acquire_scheduler_leader()
        if _scheduler_leader_lock is None:
            app.logger.info("Outro worker RIS lidera o agendador de backup.")
            return None

    start_delay = max(5, int(os.getenv("BACKUP_START_DELAY_SECONDS", "30")))
    check_seconds = max(60, int(os.getenv("BACKUP_CHECK_SECONDS", "900")))
    _scheduler_stop_event = threading.Event()

    def worker():
        if _scheduler_stop_event.wait(start_delay):
            return
        while not _scheduler_stop_event.is_set():
            run_daily_backup(app)
            _scheduler_stop_event.wait(check_seconds)

    _scheduler_thread = threading.Thread(target=worker, name="ris-daily-backup", daemon=True)
    _scheduler_thread.start()
    app.logger.info("Rotina de backup diario ativa; verificacao a cada %ss.", check_seconds)
    return _scheduler_thread


def stop_backup_scheduler():
    global _scheduler_thread, _scheduler_stop_event, _scheduler_leader_lock
    if _scheduler_stop_event is not None:
        _scheduler_stop_event.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=2)
    _scheduler_thread = None
    _scheduler_stop_event = None
    _release_scheduler_leader(_scheduler_leader_lock)
    _scheduler_leader_lock = None
