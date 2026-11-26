import mimetypes
import os
from datetime import timezone
from io import BytesIO

from flask import send_file
from werkzeug.utils import secure_filename


def _backend():
    return os.getenv("STORAGE_BACKEND", "local").strip().lower()


def _local_root():
    configured = os.getenv("UPLOAD_ROOT", "").strip()
    if configured:
        root = configured
    else:
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
    os.makedirs(root, exist_ok=True)
    return root


def _safe_key(category, filename):
    safe_category = secure_filename(category or "files") or "files"
    safe_name = secure_filename(filename or "")
    if not safe_name:
        raise ValueError("Nome de arquivo invalido.")
    prefix = os.getenv("S3_PREFIX", "ris").strip("/")
    parts = [part for part in (prefix, safe_category, safe_name) if part]
    return "/".join(parts), safe_category, safe_name


def _s3_client():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Instale boto3 para usar STORAGE_BACKEND=s3.") from exc
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
        region_name=os.getenv("S3_REGION") or None,
        aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY") or None,
    )


def _bucket():
    bucket = os.getenv("S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("Configure S3_BUCKET para usar armazenamento externo.")
    return bucket


def _local_path(safe_category, safe_name):
    return os.path.join(_local_root(), safe_category, safe_name)


def _portable_datetime(value):
    if value is not None and getattr(value, "tzinfo", None) is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def stored_file_info(category, filename):
    """Return portable metadata for a stored file without downloading it."""
    key, safe_category, safe_name = _safe_key(category, filename)
    if _backend() == "s3":
        try:
            response = _s3_client().head_object(Bucket=_bucket(), Key=key)
        except Exception:
            return None
        return {
            "name": safe_name,
            "size": int(response.get("ContentLength") or 0),
            "last_modified": _portable_datetime(response.get("LastModified")),
        }

    path = _local_path(safe_category, safe_name)
    if not os.path.exists(path):
        return None
    return {
        "name": safe_name,
        "size": os.path.getsize(path),
        "last_modified": None,
    }


def latest_stored_file(category, prefix="", suffix=""):
    """Return metadata for the newest matching object in external storage."""
    if _backend() != "s3":
        return None
    safe_category = secure_filename(category or "files") or "files"
    storage_prefix = os.getenv("S3_PREFIX", "ris").strip("/")
    key_prefix = "/".join(part for part in (storage_prefix, safe_category, prefix) if part)
    try:
        response = _s3_client().list_objects_v2(Bucket=_bucket(), Prefix=key_prefix)
    except Exception:
        return None
    candidates = []
    for item in response.get("Contents", []):
        name = os.path.basename(item.get("Key") or "")
        if not name or (prefix and not name.startswith(prefix)) or (suffix and not name.endswith(suffix)):
            continue
        candidates.append({
            "name": name,
            "size": int(item.get("Size") or 0),
            "last_modified": _portable_datetime(item.get("LastModified")),
        })
    return max(candidates, key=lambda item: item["name"], default=None)


def save_bytes(data, category, filename, content_type=None):
    key, safe_category, safe_name = _safe_key(category, filename)
    if _backend() == "s3":
        extra = {"ContentType": content_type} if content_type else {}
        _s3_client().upload_fileobj(BytesIO(data), _bucket(), key, ExtraArgs=extra)
        return safe_name
    directory = os.path.join(_local_root(), safe_category)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, safe_name), "wb") as output:
        output.write(data)
    return safe_name


def save_upload(upload, category, filename):
    content_type = getattr(upload, "mimetype", None)
    stream = getattr(upload, "stream", upload)
    stream.seek(0)
    return save_bytes(stream.read(), category, filename, content_type)


def send_stored_file(category, filename, as_attachment=False, download_name=None, mimetype=None):
    key, safe_category, safe_name = _safe_key(category, filename)
    mimetype = mimetype or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    if _backend() == "s3":
        try:
            response = _s3_client().get_object(Bucket=_bucket(), Key=key)
            body = BytesIO(response["Body"].read())
            body.seek(0)
            return send_file(body, mimetype=response.get("ContentType") or mimetype, as_attachment=as_attachment, download_name=download_name or safe_name)
        except Exception:
            path = _local_path(safe_category, safe_name)
            if os.path.exists(path):
                return send_file(path, mimetype=mimetype, as_attachment=as_attachment, download_name=download_name or safe_name)
            raise FileNotFoundError(safe_name)
    path = _local_path(safe_category, safe_name)
    if not os.path.exists(path):
        raise FileNotFoundError(safe_name)
    return send_file(path, mimetype=mimetype, as_attachment=as_attachment, download_name=download_name or safe_name)
