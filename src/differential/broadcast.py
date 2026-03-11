"""Broadcasts trace events via Supabase Realtime HTTP API."""

import logging

import httpx

log = logging.getLogger(__name__)


class Broadcaster:
    """Fire-and-forget broadcast of trace events to a Supabase Realtime channel."""

    def __init__(self, run_id: str, supabase_url: str, supabase_key: str):
        self.channel = f"trace:{run_id}"
        self._url = f"{supabase_url}/realtime/v1/api/broadcast"
        self._headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=5.0)

    async def send(self, event: str, payload: dict) -> None:
        """POST a broadcast event. Fire-and-forget; errors are logged, not raised."""
        body = {
            "messages": [{
                "topic": self.channel,
                "event": event,
                "payload": payload,
            }]
        }
        try:
            resp = await self._client.post(
                self._url, json=body, headers=self._headers
            )
            if resp.status_code >= 400:
                log.warning(
                    "Broadcast failed: status=%d, body=%s",
                    resp.status_code, resp.text[:200],
                )
        except Exception as e:
            log.debug("Broadcast error (non-fatal): %s", e)

    async def close(self) -> None:
        await self._client.aclose()
