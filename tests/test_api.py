"""Tests for the API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_list_projects(api_client):
    resp = await api_client.get("/api/projects")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
