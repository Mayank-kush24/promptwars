"""
Upload archive helper.

Every multipart file accepted by the Flask import endpoints is persisted
to disk under a dated folder structure, indexed in the ``upload_archive``
table, and accompanied by a sidecar JSON so the file is self-describing
even if the database is later migrated or the folder is moved off-box.

Status flow:
    received -> parsed -> success
                       \\-> failed (any stage)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from flask import g, has_request_context, request, session
from sqlalchemy import text
from sqlalchemy.engine import Engine
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


_DEFAULT_ARCHIVE_DIRNAME = os.path.join("uploads", "archive")
_MAX_NAME_LEN = 120
_VALID_STATUSES = {"received", "parsed", "success", "failed"}


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_archive_root() -> Path:
    """Resolve the archive root, honoring ``UPLOAD_ARCHIVE_DIR`` if set."""
    override = os.environ.get("UPLOAD_ARCHIVE_DIR")
    if override:
        root = Path(override).expanduser()
    else:
        root = _project_root() / _DEFAULT_ARCHIVE_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(original: Optional[str]) -> str:
    base = secure_filename(original or "") or "upload"
    if len(base) > _MAX_NAME_LEN:
        stem, dot, ext = base.rpartition(".")
        if dot and len(ext) <= 8:
            keep = _MAX_NAME_LEN - (len(ext) + 1)
            base = (stem[:keep] if keep > 0 else stem[:1]) + "." + ext
        else:
            base = base[:_MAX_NAME_LEN]
    return base


_MODULE_RE = re.compile(r"[^a-z0-9_]+")


def _safe_module(module: str) -> str:
    cleaned = _MODULE_RE.sub("_", (module or "").strip().lower()).strip("_")
    return cleaned or "other"


def _client_ip() -> Optional[str]:
    if not has_request_context():
        return None
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.remote_addr


def _uploaded_by() -> Optional[str]:
    if not has_request_context():
        return None
    try:
        user = getattr(g, "user", None)
        if isinstance(user, dict):
            email = (user.get("email") or "").strip()
            if email:
                return email
            name = (user.get("name") or "").strip()
            if name:
                return name
        return session.get("admin_user") or ("admin" if session.get("admin") else None)
    except RuntimeError:
        return None


@dataclass
class ArchivedUpload:
    """Result of archiving a single ``FileStorage``."""

    id: Optional[int]
    module: str
    original_name: str
    stored_path: str
    absolute_path: str
    size_bytes: int
    sha256: str
    mime_type: Optional[str]
    uploaded_at: datetime
    bytes_: bytes = field(repr=False)

    def fresh_stream(self) -> BytesIO:
        """Return a fresh, rewound in-memory stream of the archived bytes."""
        return BytesIO(self.bytes_)


def archive_upload(
    file_storage: FileStorage,
    *,
    engine: Engine,
    module: str,
    source_route: Optional[str] = None,
    event_id: Optional[int] = None,
) -> ArchivedUpload:
    """
    Read ``file_storage`` fully into memory, persist a copy to the archive
    folder, insert a row into ``upload_archive`` (status ``received``), and
    return an :class:`ArchivedUpload` with a fresh stream the caller can
    feed into the existing ETL parsers.

    The original ``file_storage.stream`` is consumed by this call.
    """
    if file_storage is None:
        raise ValueError("file_storage is required")

    raw = file_storage.stream.read() or b""
    if not isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw)
    raw_bytes = bytes(raw)

    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    size_bytes = len(raw_bytes)

    safe_module = _safe_module(module)
    original_name = file_storage.filename or "upload"
    safe_name = _safe_name(original_name)
    mime_type = file_storage.mimetype or None

    now_utc = datetime.now(timezone.utc)
    ts_compact = now_utc.strftime("%Y%m%dT%H%M%SZ")
    rel_dir = Path(safe_module) / now_utc.strftime("%Y") / now_utc.strftime("%m") / now_utc.strftime("%d")
    filename = f"{ts_compact}__{sha256[:8]}__{safe_name}"
    rel_path = rel_dir / filename

    archive_root = get_archive_root()
    abs_dir = archive_root / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    abs_path = archive_root / rel_path

    tmp_path = abs_path.with_suffix(abs_path.suffix + ".tmp")
    with open(tmp_path, "wb") as fh:
        fh.write(raw_bytes)
    os.replace(tmp_path, abs_path)

    uploaded_by = _uploaded_by()
    client_ip = _client_ip()
    route = source_route or (request.path if has_request_context() else None) or ""

    archive_id: Optional[int] = None
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO upload_archive (
                        module, source_route, original_name, stored_path,
                        size_bytes, sha256, mime_type, uploaded_by, client_ip,
                        event_id, status, uploaded_at
                    )
                    VALUES (
                        :module, :route, :orig, :path,
                        :size, :sha, :mime, :uploader, :ip,
                        :event_id, 'received', :uploaded_at
                    )
                    RETURNING id
                    """
                ),
                {
                    "module": safe_module,
                    "route": route,
                    "orig": original_name,
                    "path": str(rel_path).replace("\\", "/"),
                    "size": size_bytes,
                    "sha": sha256,
                    "mime": mime_type,
                    "uploader": uploaded_by,
                    "ip": client_ip,
                    "event_id": event_id,
                    "uploaded_at": now_utc,
                },
            ).one()
            archive_id = int(row[0])
    except Exception:
        # Filesystem copy already exists; DB indexing failure should not
        # block the import. The sidecar JSON below is still written so the
        # forensic record is preserved.
        archive_id = None

    sidecar = {
        "id": archive_id,
        "module": safe_module,
        "source_route": route,
        "original_name": original_name,
        "stored_path": str(rel_path).replace("\\", "/"),
        "size_bytes": size_bytes,
        "sha256": sha256,
        "mime_type": mime_type,
        "uploaded_by": uploaded_by,
        "client_ip": client_ip,
        "event_id": event_id,
        "status": "received",
        "uploaded_at": now_utc.isoformat(),
    }
    try:
        with open(str(abs_path) + ".meta.json", "w", encoding="utf-8") as fh:
            json.dump(sidecar, fh, indent=2, sort_keys=True)
    except OSError:
        pass

    return ArchivedUpload(
        id=archive_id,
        module=safe_module,
        original_name=original_name,
        stored_path=str(rel_path).replace("\\", "/"),
        absolute_path=str(abs_path),
        size_bytes=size_bytes,
        sha256=sha256,
        mime_type=mime_type,
        uploaded_at=now_utc,
        bytes_=raw_bytes,
    )


def mark_archive_status(
    archive_id: Optional[int],
    status: str,
    *,
    engine: Engine,
    error: Optional[str] = None,
    import_job_id: Optional[int] = None,
    rows_written: Optional[int] = None,
) -> None:
    """Update an existing ``upload_archive`` row. No-op on missing id."""
    if archive_id is None:
        return
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid upload_archive status: {status!r}")

    sets: list[str] = ["status = :status"]
    params: dict[str, Any] = {"id": archive_id, "status": status}
    if error is not None:
        sets.append("error_message = :err")
        params["err"] = error[:2000]
    if import_job_id is not None:
        sets.append("import_job_id = :jid")
        params["jid"] = int(import_job_id)
    if rows_written is not None:
        sets.append("rows_written = :rows")
        params["rows"] = int(rows_written)

    sql = "UPDATE upload_archive SET " + ", ".join(sets) + " WHERE id = :id"
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params)
    except Exception:
        # Status updates are best-effort; never raise out of the import path.
        return
