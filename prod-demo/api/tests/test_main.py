from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_quote_uses_shared_library() -> None:
    response = client.post("/quote", json={"subtotal": 100, "tax_rate": 0.075})
    assert response.status_code == 200
    assert response.json()["total"] == "107.50"
