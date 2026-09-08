import argparse
import json
import os


def main():
    parser = argparse.ArgumentParser(description="Restaura backup logico da RIS em banco isolado e vazio.")
    parser.add_argument("backup", help="Caminho do arquivo ris_backup_*.json")
    parser.add_argument("--database-url", required=True, help="Banco vazio de destino; nunca use producao.")
    args = parser.parse_args()

    if os.getenv("ENV", "development") == "production":
        raise SystemExit("Restauracao recusada com ENV=production.")
    if args.database_url == os.getenv("DATABASE_URL"):
        raise SystemExit("O banco de destino deve ser diferente do banco configurado na aplicacao.")

    os.environ["DATABASE_URL"] = args.database_url
    os.environ["SEED_DEMO_DATA"] = "false"
    os.environ["BACKUP_ENABLED"] = "false"
    os.environ["RESTORE_MODE"] = "true"

    from main import app, db
    from routes.web_routes import BACKUP_MODELS
    from services.backup_restore import restore_logical_backup

    with app.app_context():
        result = restore_logical_backup(db, args.backup, BACKUP_MODELS)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
