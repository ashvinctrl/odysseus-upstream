"""Owner/session scoping for cleanup_session_images.

The image ids and filenames this cleanup acts on are read out of chat message
metadata, and that metadata is caller-supplied: POST
/api/session/{sid}/inject_messages stores whatever `metadata` object the client
sends. Matching gallery rows on those values alone meant deleting a chat could
deactivate rows and unlink files belonging to another user, or to another chat.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import Base, ChatMessage, GalleryImage, Session
from src import session_image_cleanup


@pytest.fixture()
def env(tmp_path, monkeypatch):
    image_dir = tmp_path / "generated_images"
    image_dir.mkdir()
    monkeypatch.setattr(session_image_cleanup, "GENERATED_IMAGES_DIR", str(image_dir))

    engine = create_engine(
        f"sqlite:///{tmp_path / 'cleanup.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield image_dir, db
    finally:
        db.close()


def _chat(db, sid, owner):
    db.add(Session(id=sid, name=sid, endpoint_url="http://local", model="m", owner=owner))


def _image(db, image_dir, img_id, owner, session_id):
    name = f"{img_id}.png"
    (image_dir / name).write_bytes(b"bytes-" + img_id.encode())
    db.add(GalleryImage(id=img_id, filename=name, prompt=img_id, owner=owner,
                        session_id=session_id, is_active=True))
    return name


def _reference(db, msg_id, session_id, *, image_id=None, filename=None):
    event = {}
    if image_id:
        event["image_id"] = image_id
    if filename:
        event["image_url"] = f"/api/generated-image/{filename}"
    db.add(ChatMessage(id=msg_id, session_id=session_id, role="assistant", content="x",
                       meta_data=json.dumps({"tool_events": [event]})))


def test_other_users_image_is_not_deleted_by_id_reference(env):
    """A chat cannot destroy another user's gallery image by naming its id."""
    image_dir, db = env
    _chat(db, "alice-chat", "alice")
    _chat(db, "mallory-chat", "mallory")
    name = _image(db, image_dir, "alice-img", owner="alice", session_id="alice-chat")
    _reference(db, "m1", "mallory-chat", image_id="alice-img")
    db.commit()

    session_image_cleanup.cleanup_session_images("mallory-chat", db=db)
    db.commit()

    assert (image_dir / name).exists()
    assert db.query(GalleryImage).filter_by(id="alice-img").first().is_active is True


def test_other_users_image_is_not_deleted_by_filename_reference(env):
    """Same boundary, but reached through the image_url filename instead."""
    image_dir, db = env
    _chat(db, "alice-chat", "alice")
    _chat(db, "mallory-chat", "mallory")
    name = _image(db, image_dir, "alice-img", owner="alice", session_id=None)
    _reference(db, "m1", "mallory-chat", filename=name)
    db.commit()

    session_image_cleanup.cleanup_session_images("mallory-chat", db=db)
    db.commit()

    assert (image_dir / name).exists()
    assert db.query(GalleryImage).filter_by(id="alice-img").first().is_active is True


def test_image_belonging_to_another_chat_survives(env):
    """Deleting a chat that merely mentions an image must not delete it."""
    image_dir, db = env
    _chat(db, "chat-1", "alice")
    _chat(db, "chat-2", "alice")
    name = _image(db, image_dir, "img-1", owner="alice", session_id="chat-1")
    _reference(db, "m1", "chat-2", filename=name)
    db.commit()

    session_image_cleanup.cleanup_session_images("chat-2", db=db)
    db.commit()

    row = db.query(GalleryImage).filter_by(id="img-1").first()
    assert (image_dir / name).exists()
    assert row.is_active is True
    assert row.session_id == "chat-1"


def test_own_detached_image_is_still_cleaned(env):
    """The behaviour the reference scan exists for: same owner, no session."""
    image_dir, db = env
    _chat(db, "alice-chat", "alice")
    name = _image(db, image_dir, "img-detached", owner="alice", session_id=None)
    _reference(db, "m1", "alice-chat", image_id="img-detached", filename=name)
    db.commit()

    removed = session_image_cleanup.cleanup_session_images("alice-chat", db=db)
    db.commit()

    assert removed == 1
    assert not (image_dir / name).exists()
    assert db.query(GalleryImage).filter_by(id="img-detached").first().is_active is False


def test_images_attached_to_the_session_are_still_cleaned(env):
    """Rows linked by session_id are cleaned regardless of the reference scan."""
    image_dir, db = env
    _chat(db, "alice-chat", "alice")
    name = _image(db, image_dir, "img-linked", owner="alice", session_id="alice-chat")
    db.commit()

    removed = session_image_cleanup.cleanup_session_images("alice-chat", db=db)
    db.commit()

    assert removed == 1
    assert not (image_dir / name).exists()
    assert db.query(GalleryImage).filter_by(id="img-linked").first().is_active is False


def test_single_user_mode_still_cleans_referenced_images(env, monkeypatch):
    """Owner-less session with AUTH_ENABLED=false keeps the old reach."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    image_dir, db = env
    _chat(db, "solo-chat", None)
    name = _image(db, image_dir, "img-solo", owner=None, session_id=None)
    _reference(db, "m1", "solo-chat", image_id="img-solo")
    db.commit()

    removed = session_image_cleanup.cleanup_session_images("solo-chat", db=db)
    db.commit()

    assert removed == 1
    assert not (image_dir / name).exists()


def test_ownerless_session_cannot_reach_owned_images_when_auth_is_on(env, monkeypatch):
    """A session with no owner is not a wildcard once auth is enabled.

    Sessions created while AUTH_ENABLED=false keep a null owner after the
    operator turns auth on. Those must not be able to reach rows that belong
    to a real user, mirroring how gallery _owner_filter fails closed for an
    auth-enabled null user.
    """
    monkeypatch.setenv("AUTH_ENABLED", "true")
    image_dir, db = env
    _chat(db, "legacy-chat", None)
    alice = _image(db, image_dir, "alice-img", owner="alice", session_id=None)
    orphan = _image(db, image_dir, "orphan-img", owner=None, session_id=None)
    _reference(db, "m1", "legacy-chat", image_id="alice-img")
    _reference(db, "m2", "legacy-chat", image_id="orphan-img")
    db.commit()

    session_image_cleanup.cleanup_session_images("legacy-chat", db=db)
    db.commit()

    # Alice's row is untouched; the genuinely owner-less one is still cleaned.
    assert (image_dir / alice).exists()
    assert db.query(GalleryImage).filter_by(id="alice-img").first().is_active is True
    assert not (image_dir / orphan).exists()
    assert db.query(GalleryImage).filter_by(id="orphan-img").first().is_active is False
