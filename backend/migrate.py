import os

os.environ["AUTO_INITIALIZE_DATABASE"] = "false"
os.environ["BACKUP_SCHEDULER_MODE"] = "external"

from main import initialize_database


initialize_database()

