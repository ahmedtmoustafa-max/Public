"""Streaming speech-to-text.

Optional by design. With no provider configured the agent still works -- it
just can't read menus, so it relies on a scripted key sequence plus the
audio-only human detector.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Protocol

import websockets

from .settings import Settings

log = logging.getLogger(__name__)

TextHandler = Callable[[str, bool], Awaitable[None]]


class Transcriber(Protocol):
    enabled: bool

    async def start(self) -> None: ...
    async def feed(self, audio: bytes) -> None: ...
    async def close(self) -> None: ...


class NullTranscriber:
    """Swallows audio and never produces text."""

    enabled = False

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def start(self) -> None:
        return None

    async def feed(self, audio: bytes) -> None:
        return None

    async def close(self) -> None:
        return None


class DeepgramTranscriber:
    """Streams mu-law straight to Deepgram -- no resampling needed."""

    enabled = True

    def __init__(self, settings: Settings, on_text: TextHandler):
        self.settings = settings
        self.on_text = on_text
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._reader: asyncio.Task | None = None
        self._closed = False

    def _url(self) -> str:
        params = {
            "encoding": "mulaw",
            "sample_rate": "8000",
            "channels": "1",
            "model": self.settings.deepgram_model,
            "punctuate": "true",
            "smart_format": "true",
            "interim_results": "false",
            # Fairly aggressive endpointing: we want each menu prompt or agent
            # sentence delivered promptly, not batched up.
            "endpointing": "400",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"wss://api.deepgram.com/v1/listen?{query}"

    async def start(self) -> None:
        if not self.settings.deepgram_api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is not set.")
        self._ws = await websockets.connect(
            self._url(),
            additional_headers={
                "Authorization": f"Token {self.settings.deepgram_api_key}"
            },
            max_size=None,
            ping_interval=5,
        )
        self._reader = asyncio.create_task(self._read_loop())
        log.info("deepgram stream open")

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if message.get("type") not in (None, "Results"):
                    continue
                alternatives = message.get("channel", {}).get("alternatives", [])
                if not alternatives:
                    continue
                text = (alternatives[0].get("transcript") or "").strip()
                if text:
                    await self.on_text(text, bool(message.get("is_final")))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - transcription is best-effort
            if not self._closed:
                log.warning("deepgram stream ended: %s", exc)

    async def feed(self, audio: bytes) -> None:
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(audio)
        except Exception as exc:  # noqa: BLE001
            log.debug("deepgram send failed: %s", exc)

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def build_transcriber(settings: Settings, on_text: TextHandler) -> Transcriber:
    """Return a started transcriber, falling back to the null one on failure."""
    if settings.asr_provider != "deepgram" or not settings.deepgram_api_key:
        return NullTranscriber()
    transcriber = DeepgramTranscriber(settings, on_text)
    try:
        await transcriber.start()
        return transcriber
    except Exception as exc:  # noqa: BLE001
        log.warning("falling back to audio-only detection: %s", exc)
        return NullTranscriber()
