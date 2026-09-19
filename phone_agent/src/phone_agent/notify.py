"""Getting your attention the moment a human picks up.

Three independent channels, all optional, all fired at once: a push
notification (ntfy or Pushover), an SMS, and -- the one that actually matters
-- your phone ringing. Push and SMS are the heads-up; the ringing phone *is*
the handoff.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .settings import Settings
from .telephony import Telephony

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings, telephony: Telephony):
        self.settings = settings
        self.telephony = telephony

    async def human_answered(self, label: str, hold_seconds: float, call_id: str) -> None:
        minutes, seconds = divmod(int(hold_seconds), 60)
        waited = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
        title = f"{label}: a person is on the line"
        body = (
            f"Pick up -- your phone is ringing now.\n"
            f"Waited {waited} on hold. Call {call_id}."
        )
        await self._fan_out(title, body, priority="high")

    async def call_failed(self, label: str, reason: str) -> None:
        await self._fan_out(f"{label}: call ended", reason, priority="default")

    async def _fan_out(self, title: str, body: str, priority: str) -> None:
        tasks = []
        if self.settings.notify_ntfy_topic:
            tasks.append(self._ntfy(title, body, priority))
        if self.settings.notify_pushover_token and self.settings.notify_pushover_user:
            tasks.append(self._pushover(title, body, priority))
        if self.settings.notify_sms and self.settings.my_phone_number:
            tasks.append(self._sms(f"{title}\n{body}"))
        if not tasks:
            log.info("notification (no channel configured): %s -- %s", title, body)
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.warning("notification channel failed: %s", result)

    async def _ntfy(self, title: str, body: str, priority: str) -> None:
        url = f"{self.settings.notify_ntfy_server.rstrip('/')}/{self.settings.notify_ntfy_topic}"
        headers = {
            "Title": title,
            "Priority": "urgent" if priority == "high" else "default",
            "Tags": "telephone_receiver",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, content=body.encode(), headers=headers)
            response.raise_for_status()

    async def _pushover(self, title: str, body: str, priority: str) -> None:
        payload = {
            "token": self.settings.notify_pushover_token,
            "user": self.settings.notify_pushover_user,
            "title": title,
            "message": body,
            "priority": 1 if priority == "high" else 0,
            "sound": "persistent" if priority == "high" else "pushover",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.pushover.net/1/messages.json", data=payload
            )
            response.raise_for_status()

    async def _sms(self, text: str) -> None:
        await asyncio.to_thread(
            self.telephony.send_sms, self.settings.my_phone_number, text[:1500]
        )
