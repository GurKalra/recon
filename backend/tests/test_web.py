"""The API can serve the built website, without ever hiding an API 404 behind index.html."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web import mount_web


def _client(tmp_path: Path) -> TestClient:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-abc.js").write_text("console.log(1)")
    (tmp_path / "index.html").write_text("<html>recon</html>")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    app = FastAPI()

    @app.get("/api/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    mount_web(app, tmp_path, hsts=True)
    return TestClient(app)


def test_screens_fall_back_to_index_and_files_are_served(tmp_path):
    c = _client(tmp_path)
    assert "recon" in c.get("/orders").text
    assert c.get("/favicon.svg").text == "<svg/>"
    assets = c.get("/assets/app-abc.js")
    assert assets.status_code == 200
    assert "immutable" in assets.headers["cache-control"]


def test_api_routes_win_and_unknown_api_paths_stay_404(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/ping").json() == {"ok": True}
    assert c.get("/api/nothing").status_code == 404
    assert "recon" not in c.get("/api/nothing").text


def test_headers_and_no_escape_from_the_folder(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "strict-transport-security" in r.headers
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("nope")
    assert "nope" not in c.get("/..%2fsecret.txt").text
    assert "nope" not in c.get("/../secret.txt").text