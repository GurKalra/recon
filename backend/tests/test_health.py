from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture
def client(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as c:  # runs the lifespan (opens the pool)
        yield c
    get_settings.cache_clear()


def test_health_reports_database_up(client: TestClient):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "db": True}