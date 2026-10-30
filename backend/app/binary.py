"""Files carried by items (n8n's binary data).

An item keeps only a reference under `item["binary"][<property>]` (default property: "data"):

    {"id": "…", "fileName": "report.pdf", "mimeType": "application/pdf", "fileSize": 18234, "fileExtension": "pdf"}

The bytes live on disk at DATA_DIR/binary/<owner id>/<id>, so execution records stay small and a
workspace can only read its own files. Expressions see it as `$binary.data.fileName`.
"""

import mimetypes
import secrets
from pathlib import Path

from .config import get_settings


def _root() -> Path:
    return Path(get_settings().data_dir).resolve() / "binary"


def _dir(owner_id) -> Path:
    return _root() / (str(owner_id) if owner_id else "shared")


def _id_ok(file_id: str) -> bool:
    return isinstance(file_id, str) and len(file_id) == 32 and all(c in "0123456789abcdef" for c in file_id)


def is_ref(v) -> bool:
    return isinstance(v, dict) and _id_ok(v.get("id", "")) and "fileName" in v


def put(owner_id, data: bytes, file_name: str = "file", mime_type: str | None = None) -> dict:
    limit = get_settings().max_binary_mb * 1024 * 1024
    if len(data) > limit:
        raise ValueError(f"File is larger than the {get_settings().max_binary_mb} MB limit (MAX_BINARY_MB)")
    file_id = secrets.token_hex(16)
    d = _dir(owner_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / file_id).write_bytes(data)
    name = Path(file_name or "file").name or "file"
    mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    ext = Path(name).suffix.lstrip(".") or (mimetypes.guess_extension(mime) or "").lstrip(".")
    return {"id": file_id, "fileName": name, "mimeType": mime, "fileSize": len(data), "fileExtension": ext}


def get(owner_id, ref: dict) -> bytes:
    if not is_ref(ref):
        raise ValueError("Not a file reference")
    path = _dir(owner_id) / ref["id"]
    if not path.is_file():
        raise ValueError(f"File '{ref.get('fileName')}' is no longer available")
    return path.read_bytes()


def find(file_id: str, owner_ids: list | None) -> Path | None:
    """Locate a stored file: in the given workspaces, or anywhere when owner_ids is None (admins)."""
    if not _id_ok(file_id):
        return None
    dirs = [_dir(o) for o in owner_ids] if owner_ids is not None else [p for p in _root().glob("*") if p.is_dir()]
    return next((d / file_id for d in dirs if (d / file_id).is_file()), None)
