"""Tests for the API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from differential.api.app import app


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_list_projects(api_client):
    resp = await api_client.get("/api/projects")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_llm_exchange_with_null_round(api_client, tmp_db, scout_call):
    """Exchanges without a round (e.g. closing review) should serialize cleanly."""
    exchange_id = await tmp_db.save_llm_exchange(
        call_id=scout_call.id,
        phase='review',
        system_prompt='test',
        user_message='test',
        response_text='test',
        tool_calls=[],
        round_num=None,
    )

    resp = await api_client.get(f'/api/llm-exchanges/{exchange_id}')
    assert resp.status_code == 200
    assert resp.json()['round'] is None

    resp = await api_client.get(f'/api/calls/{scout_call.id}/llm-exchanges')
    assert resp.status_code == 200
    assert any(e['round'] is None for e in resp.json())
