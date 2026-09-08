import json
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, select


PROTECTED_VALUE = "[protegido]"


def _coerce_value(column, value):
    if value is None:
        return None
    try:
        python_type = column.type.python_type
    except (AttributeError, NotImplementedError):
        return value
    if python_type is datetime and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if python_type is date and isinstance(value, str):
        return date.fromisoformat(value)
    if python_type is time and isinstance(value, str):
        return time.fromisoformat(value)
    if python_type is Decimal:
        return Decimal(str(value))
    if python_type is bytes and isinstance(value, str):
        return bytes.fromhex(value)
    return value


def load_backup(path):
    with open(path, "r", encoding="utf-8") as backup_file:
        payload = json.load(backup_file)
    if payload.get("app") != "RIS SaaS" or payload.get("backup_type") != "logical_json":
        raise ValueError("Arquivo nao e um backup logico valido da RIS.")
    if not isinstance(payload.get("tables"), dict):
        raise ValueError("Backup sem estrutura de tabelas.")
    return payload


def restore_logical_backup(db, path, backup_models):
    payload = load_backup(path)
    counts_before = {
        name: db.session.scalar(select(func.count()).select_from(model))
        for name, model in backup_models
    }
    occupied = {name: count for name, count in counts_before.items() if count}
    if occupied:
        raise RuntimeError(
            "Restauracao bloqueada: o banco de destino nao esta vazio. "
            f"Tabelas ocupadas: {', '.join(sorted(occupied))}."
        )

    restored = {}
    try:
        for table_name, model in backup_models:
            rows = payload["tables"].get(table_name, [])
            if not isinstance(rows, list):
                raise ValueError(f"Tabela invalida no backup: {table_name}.")
            columns = {column.name: column for column in model.__table__.columns}
            for row in rows:
                values = {}
                for key, value in row.items():
                    column = columns.get(key)
                    if column is None:
                        continue
                    if value == PROTECTED_VALUE:
                        if key == "password":
                            values[key] = PROTECTED_VALUE
                        continue
                    values[key] = _coerce_value(column, value)
                db.session.add(model(**values))
            db.session.flush()
            restored[table_name] = len(rows)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    counts_after = {
        name: db.session.scalar(select(func.count()).select_from(model))
        for name, model in backup_models
    }
    mismatches = {
        name: {"backup": restored.get(name, 0), "restored": counts_after[name]}
        for name in counts_after
        if counts_after[name] != restored.get(name, 0)
    }
    if mismatches:
        raise RuntimeError(f"Contagens divergentes apos restauracao: {mismatches}")
    return {
        "generated_at": payload.get("generated_at"),
        "restored": restored,
        "verified": not mismatches,
        "credentials_require_reset": bool(restored.get("users")),
    }
