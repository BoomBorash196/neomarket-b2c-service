"""Tests for health endpoint."""

from fastapi.testclient import TestClient


def test_health_check(client: TestClient):
    """Test health endpoint returns healthy status."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "service" in data
    assert "version" in data


def test_docs_page(client: TestClient):
    """Test Swagger docs are accessible."""
    response = client.get("/docs")
    assert response.status_code == 200
    assert "FastAPI" in response.text
