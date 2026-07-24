"""Cleanup helpers for images attached to chat sessions."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from src.constants import GENERATED_IMAGES_DIR

logger = logging.getLogger(__name__)


def _database_models():
    """Import DB models at call time so early import stubs cannot stick here."""
    from core.database import ChatMessage, GalleryImage, SessionLocal

    return ChatMessage, GalleryImage, SessionLocal


def _session_owner(db, session_id: str):
    from core.database import Session as DbSession

    row = db.query(DbSession.owner).filter(DbSession.id == session_id).first()
    return getattr(row, "owner", None) if row is not None else None


def _auth_disabled() -> bool:
    from src.auth_helpers import _auth_disabled as _impl

    return _impl()


def _generated_image_path_for_cleanup(filename: str) -> Path | None:
    if not isinstance(filename, str) or not filename:
        return None
    name = Path(filename).name
    if name != filename or name in {".", ".."}:
        return None
    root = Path(GENERATED_IMAGES_DIR).resolve()
    path = (root / name).resolve()
    try:
        if os.path.commonpath([str(root), str(path)]) != str(root):
            return None
    except Exception:
        return None
    return path


def _image_filename_from_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        return ""
    match = re.search(r"/api/generated-image/([^?#/]+)", url)
    return match.group(1) if match else ""


def session_image_refs(db, session_id: str) -> tuple[set[str], set[str]]:
    """Return gallery image ids and generated-image filenames referenced by a chat."""
    ChatMessage, GalleryImage, _ = _database_models()
    image_ids: set[str] = set()
    filenames: set[str] = set()

    rows = db.query(GalleryImage).filter(GalleryImage.session_id == session_id).all()
    for img in rows:
        if img.id:
            image_ids.add(str(img.id))
        if img.filename:
            filenames.add(str(img.filename))

    messages = db.query(ChatMessage.meta_data).filter(ChatMessage.session_id == session_id).all()
    for row in messages:
        raw = getattr(row, "meta_data", None)
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except Exception:
            continue
        events = meta.get("tool_events") if isinstance(meta, dict) else None
        if not isinstance(events, list):
            continue
        for ev in events:
            if not isinstance(ev, dict):
                continue
            image_id = ev.get("image_id")
            if image_id:
                image_ids.add(str(image_id))
            filename = _image_filename_from_url(ev.get("image_url") or ev.get("url") or "")
            if filename:
                filenames.add(filename)

    return image_ids, filenames


def cleanup_session_images(session_id: str, db=None) -> int:
    """Soft-delete Gallery rows and unlink generated files owned by a chat."""
    _, GalleryImage, SessionLocal = _database_models()
    owns_db = db is None
    db = db or SessionLocal()
    try:
        image_ids, filenames = session_image_refs(db, session_id)
        query = db.query(GalleryImage).filter(GalleryImage.session_id == session_id)
        if image_ids or filenames:
            from sqlalchemy import and_, or_

            # Ids and filenames come out of chat message metadata, which is
            # caller-supplied (POST /api/session/{sid}/inject_messages stores
            # it verbatim). Matching on them alone let a chat delete gallery
            # rows it does not own, so keep the reference match but require the
            # row to belong to this session's owner and to not already be
            # attached to a different chat.
            ref_clauses = []
            if image_ids:
                ref_clauses.append(GalleryImage.id.in_(list(image_ids)))
            if filenames:
                ref_clauses.append(GalleryImage.filename.in_(list(filenames)))

            referenced = and_(
                or_(*ref_clauses),
                or_(
                    GalleryImage.session_id.is_(None),
                    GalleryImage.session_id == session_id,
                ),
            )
            # Same three cases the gallery's own _owner_filter handles: exact
            # match for a real owner, unrestricted for single-user/auth-off
            # where there is no boundary, and fail closed in between — a
            # session with no owner while auth is on (created before auth was
            # turned on, or legacy) must not reach rows that belong to someone.
            owner = _session_owner(db, session_id)
            if owner:
                referenced = and_(referenced, GalleryImage.owner == owner)
            elif not _auth_disabled():
                referenced = and_(referenced, GalleryImage.owner.is_(None))

            query = db.query(GalleryImage).filter(
                or_(GalleryImage.session_id == session_id, referenced)
            )

        images = query.all()
        removed = 0
        for img in images:
            img.is_active = False
            if img.filename:
                path = _generated_image_path_for_cleanup(img.filename)
                if path and path.exists():
                    try:
                        path.unlink()
                    except Exception as exc:
                        logger.warning(
                            "Could not remove generated image %s for deleted session %s: %s",
                            img.filename,
                            session_id,
                            exc,
                        )
            removed += 1

        if owns_db and images:
            db.commit()
        return removed
    except Exception as exc:
        if owns_db:
            db.rollback()
        logger.warning("Failed to clean images for deleted session %s: %s", session_id, exc)
        return 0
    finally:
        if owns_db:
            db.close()
