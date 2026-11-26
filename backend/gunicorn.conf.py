import os


bind = f"0.0.0.0:{os.getenv('PORT', '5000')}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))


def post_worker_init(_worker):
    """Elect one worker to run the backup scheduler after forking."""
    from main import app
    from services.backup_scheduler import start_backup_scheduler

    start_backup_scheduler(app, singleton=True)


def worker_exit(_server, _worker):
    from services.backup_scheduler import stop_backup_scheduler

    stop_backup_scheduler()
