"""compose-from-odysseus must not stage another owner's gallery image.

_load_odysseus_attachment_source used `if owner and img.owner and img.owner !=
owner`, whose middle term skipped the check whenever a gallery row's owner was
NULL/empty. Owner-less rows exist transiently (created while auth was off, or
legacy pre-owner-column rows), so an authenticated user could name such a row's
id and stage/download it through POST /api/email/compose-from-odysseus, even
though the gallery's own _owner_filter never surfaces owner-less rows to an
authenticated caller.
"""
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


@pytest.fixture
def compose_env(tmp_path, monkeypatch):
    import core.database as cdb
    import routes.email_routes as email_routes
    import routes.gallery.gallery_routes as gallery_routes

    engine = create_engine(f"sqlite:///{tmp_path / 'gallery.db'}")
    cdb.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", Session)

    # Every gallery filename resolves to a real file so we can tell an
    # owner-rejection (404 "Image not found") apart from a missing-file 404.
    real_image = tmp_path / "image_bytes.png"
    real_image.write_bytes(b"PNGDATA")
    monkeypatch.setattr(gallery_routes, "_gallery_image_path", lambda _fn: real_image)

    uploads = tmp_path / "compose_uploads"
    uploads.mkdir()
    monkeypatch.setattr(email_routes, "COMPOSE_UPLOADS_DIR", uploads)

    db = Session()
    db.add_all([
        cdb.GalleryImage(id="img-ownerless", filename="ownerless.png", owner=None, is_active=True),
        cdb.GalleryImage(id="img-bob", filename="bob.png", owner="bob", is_active=True),
        cdb.GalleryImage(id="img-alice", filename="alice.png", owner="alice", is_active=True),
    ])
    db.commit()
    db.close()

    router = email_routes.setup_email_routes()
    return _route_endpoint(router, "/api/email/compose-from-odysseus", "POST")


@pytest.mark.asyncio
async def test_ownerless_gallery_image_is_not_stageable_by_another_user(compose_env):
    """The exploit: alice must not stage the owner-less image. Fails pre-fix."""
    with pytest.raises(HTTPException) as exc:
        await compose_env({"kind": "gallery", "id": "img-ownerless"}, owner="alice")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_other_users_owned_gallery_image_stays_blocked(compose_env):
    with pytest.raises(HTTPException) as exc:
        await compose_env({"kind": "gallery", "id": "img-bob"}, owner="alice")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_own_gallery_image_is_stageable(compose_env):
    result = await compose_env({"kind": "gallery", "id": "img-alice"}, owner="alice")
    assert result.get("success") is True


@pytest.mark.asyncio
async def test_single_user_mode_can_still_stage_ownerless_image(compose_env):
    # owner == "" is single-user / auth-off, where there is no boundary.
    result = await compose_env({"kind": "gallery", "id": "img-ownerless"}, owner="")
    assert result.get("success") is True
