import os

from flask_migrate import upgrade

from main import app


with app.app_context():
    upgrade(directory=os.path.join(os.path.dirname(__file__), "migrations"))

