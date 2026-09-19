"""FastAPI application: control API, Twilio webhooks, and the media socket."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import twiml
from .asr import build_transcriber
from .brain import Brain
from .models import CallMode, CallPhase, CallRequest, CallState
from .notify import Notifier
from .orchestrator import CallSession
from .playbooks import PlaybookStore
from .settings import Settings, get_settings
from .store import CallStore
from .telephony import DestinationRefused, Telephony, check_destination

log = logging.getLogger(__name__)

PROFILE_PATH = Path("config/profile.yaml")


def load_profile() -> dict:
    """Optional file describing what the agent may say about you."""
    if not PROFILE_PATH.exists():
        return {}
    try:
        return yaml.safe_load(PROFILE_PATH.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("could not read %s: %s", PROFILE_PATH, exc)
        return {}


class Runtime:
    """Everything the request handlers need, assembled once at startup."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.telephony = Telephony(settings)
        self.store = CallStore(settings.database_path)
        self.playbooks = PlaybookStore()
        self.notifier = Notifier(settings, self.telephony)
        self.brain = Brain(settings, load_profile())
        self.sessions: dict[str, CallSession] = {}

    @property
    def has_asr(self) -> bool:
        return (
            self.settings.asr_provider == "deepgram"
            and bool(self.settings.deepgram_api_key)
        )

    def session(self, call_id: str) -> CallSession | None:
        return self.sessions.get(call_id)

    def new_session(self, state: CallState) -> CallSession:
        session = CallSession(
            state,
            settings=self.settings,
            telephony=self.telephony,
            notifier=self.notifier,
            brain=self.brain,
            store=self.store,
            playbooks=self.playbooks,
            has_asr=self.has_asr,
        )
        self.sessions[state.id] = session
        self.store.add(state)
        return session

    def drop(self, call_id: str) -> None:
        self.sessions.pop(call_id, None)
        self.store.retire(call_id)


runtime: Runtime = None  # type: ignore[assignment]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runtime
    settings = get_settings()
    runtime = Runtime(settings)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info("phone-agent up; public base url = %s", settings.public_base_url)
    if not runtime.has_asr:
        log.info("no ASR configured -- script/watch modes only, audio-only detection")
    yield


app = FastAPI(title="phone-agent", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# Twilio request authentication
# --------------------------------------------------------------------------


async def require_twilio(request: Request) -> dict:
    """Verify the X-Twilio-Signature header and return the POST body."""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    url = runtime.settings.url(request.url.path)
    if request.url.query:
        url = f"{url}?{request.url.query}"
    signature = request.headers.get("X-Twilio-Signature", "")
    if not runtime.telephony.verify(url, params, signature):
        log.warning("rejected unsigned Twilio request to %s", url)
        raise HTTPException(status_code=403, detail="Bad Twilio signature.")
    return params


def _xml(document: str) -> Response:
    return Response(content=document, media_type="application/xml")


# --------------------------------------------------------------------------
# Control API
# --------------------------------------------------------------------------


@app.post("/calls")
async def create_call(payload: CallRequest) -> JSONResponse:
    settings = runtime.settings

    if runtime.store.active_count() >= settings.max_concurrent_calls:
        raise HTTPException(429, "Too many calls in flight.")

    try:
        destination = check_destination(payload.to, settings)
    except DestinationRefused as exc:
        raise HTTPException(400, str(exc)) from exc
    payload.to = destination

    # Fill in from a saved playbook when one matches and nothing was specified.
    book = (
        runtime.playbooks.get(payload.playbook)
        if payload.playbook
        else runtime.playbooks.find_for_number(destination)
    )
    used_playbook = ""
    if book:
        payload.label = payload.label or book.label
        if not payload.keys and book.keys:
            payload.keys = book.keys
            used_playbook = book.id
            if payload.mode is CallMode.AUTO:
                payload.mode = CallMode.SCRIPT

    if payload.mode is CallMode.AUTO and not (runtime.has_asr and runtime.brain.available):
        raise HTTPException(
            400,
            "auto mode needs DEEPGRAM_API_KEY and ANTHROPIC_API_KEY. Use "
            "mode='script' with keys, or mode='watch'.",
        )
    if payload.mode is CallMode.SCRIPT and not payload.keys:
        raise HTTPException(400, "script mode needs a 'keys' sequence, e.g. '1,w3,0'.")
    if not (payload.callback_number or settings.my_phone_number):
        raise HTTPException(400, "Set MY_PHONE_NUMBER or pass callback_number.")

    state = CallState(request=payload)
    state.conference_name = f"pa-{state.id}"
    session = runtime.new_session(state)

    try:
        state.call_sid = await asyncio.to_thread(
            runtime.telephony.originate,
            state.id,
            destination,
            payload.max_seconds or settings.max_call_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        await session.finish(CallPhase.FAILED, f"Could not place the call: {exc}")
        runtime.drop(state.id)
        raise HTTPException(502, f"Twilio refused the call: {exc}") from exc

    runtime.store.index_sid(state.call_sid, state.id)
    note = f" using playbook '{used_playbook}' ({payload.keys})" if used_playbook else ""
    state.log("system", f"Dialling {state.label} in {payload.mode.value} mode{note}.")
    return JSONResponse({"call": state.summary()}, status_code=201)


@app.get("/calls")
async def list_calls() -> dict:
    return {
        "live": [s.summary() for s in runtime.store.live()],
        "recent": runtime.store.history(limit=15),
    }


@app.get("/calls/{call_id}")
async def get_call(call_id: str) -> dict:
    session = runtime.session(call_id)
    if session:
        return {"call": session.state.summary()}
    for record in runtime.store.history(limit=200):
        if record["id"] == call_id:
            return {"call": record}
    raise HTTPException(404, "No such call.")


@app.delete("/calls/{call_id}")
async def cancel_call(call_id: str) -> dict:
    session = runtime.session(call_id)
    if not session:
        raise HTTPException(404, "No such live call.")
    await session.cancel()
    runtime.drop(call_id)
    return {"call": session.state.summary()}


@app.get("/playbooks")
async def list_playbooks() -> dict:
    return {"playbooks": [b.as_dict() for b in runtime.playbooks.all()]}


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "asr": runtime.settings.asr_provider if runtime.has_asr else "none",
        "brain": runtime.brain.available,
        "live_calls": runtime.store.active_count(),
    }


# --------------------------------------------------------------------------
# TwiML webhooks
# --------------------------------------------------------------------------


@app.post("/twiml/start")
async def twiml_start(request: Request, call_id: str) -> Response:
    await require_twilio(request)
    session = runtime.session(call_id)
    if not session:
        return _xml(twiml.hangup())
    session.answered()
    return _xml(
        twiml.start_stream(
            stream_url=f"{runtime.settings.ws_base_url}/ws/media",
            call_id=call_id,
            next_url=runtime.settings.url(f"/twiml/next?call_id={call_id}"),
        )
    )


@app.post("/twiml/next")
async def twiml_next(request: Request, call_id: str) -> Response:
    await require_twilio(request)
    session = runtime.session(call_id)
    if not session:
        return _xml(twiml.hangup())
    return _xml(session.next_twiml())


@app.post("/twiml/bridge")
async def twiml_bridge(request: Request, call_id: str) -> Response:
    """Served to *your* phone when we ring you."""
    await require_twilio(request)
    session = runtime.session(call_id)
    if not session:
        return _xml(
            twiml.hangup("That call has already ended.", runtime.settings.tts_voice)
        )
    state = session.state
    announcement = f"{state.label}. {runtime.settings.bridge_announcement}"
    return _xml(
        twiml.bridge_user(
            state.conference_name, announcement, runtime.settings.tts_voice
        )
    )


# --------------------------------------------------------------------------
# Twilio status callbacks
# --------------------------------------------------------------------------


@app.post("/twilio/status")
async def twilio_status(request: Request, call_id: str) -> Response:
    params = await require_twilio(request)
    session = runtime.session(call_id)
    if not session:
        return Response(status_code=204)
    status = params.get("CallStatus", "")
    if status == "in-progress":
        session.answered()
    elif status in {"completed", "busy", "failed", "no-answer", "canceled"}:
        phase = (
            CallPhase.COMPLETED
            if status == "completed"
            else CallPhase.FAILED
        )
        reason = "" if status == "completed" else f"Call ended: {status}."
        was_bridged = session.state.phase is CallPhase.BRIDGED
        await session.finish(phase, reason)
        if not was_bridged and status != "completed":
            asyncio.create_task(
                runtime.notifier.call_failed(session.state.label, reason)
            )
        runtime.drop(call_id)
    return Response(status_code=204)


@app.post("/twilio/amd")
async def twilio_amd(request: Request, call_id: str) -> Response:
    """Answering-machine detection result."""
    params = await require_twilio(request)
    session = runtime.session(call_id)
    if not session:
        return Response(status_code=204)
    answered_by = params.get("AnsweredBy", "")
    session.state.log("system", f"Twilio says the call was answered by: {answered_by}.")
    if answered_by in {"machine_end_beep", "machine_end_silence", "machine_end_other"}:
        await asyncio.to_thread(runtime.telephony.hangup, session.state.call_sid)
        await session.finish(CallPhase.FAILED, "Reached voicemail, not a phone tree.")
        asyncio.create_task(
            runtime.notifier.call_failed(session.state.label, "Reached voicemail.")
        )
        runtime.drop(call_id)
    return Response(status_code=204)


@app.post("/twilio/bridge-status")
async def twilio_bridge_status(request: Request, call_id: str) -> Response:
    params = await require_twilio(request)
    session = runtime.session(call_id)
    if not session:
        return Response(status_code=204)
    status = params.get("CallStatus", "")
    if status == "in-progress":
        session.bridged()
    elif status in {"busy", "no-answer", "failed"}:
        session.state.log("system", f"You did not pick up ({status}).")
    return Response(status_code=204)


@app.post("/twilio/conference")
async def twilio_conference(request: Request, call_id: str) -> Response:
    params = await require_twilio(request)
    session = runtime.session(call_id)
    if session:
        session.state.log(
            "system", f"Conference event: {params.get('StatusCallbackEvent', '?')}."
        )
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Media stream
# --------------------------------------------------------------------------


@app.websocket("/ws/media")
async def media_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    session: CallSession | None = None
    transcriber = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                continue

            event = message.get("event")

            if event == "start":
                start = message.get("start", {})
                call_id = (start.get("customParameters") or {}).get("callId", "")
                session = runtime.session(call_id)
                if session is None:
                    log.warning("media stream for unknown call %s", call_id)
                    await websocket.close()
                    return
                session.answered()

                async def on_text(text: str, is_final: bool, s=session) -> None:
                    await s.on_text(text, is_final)

                transcriber = await build_transcriber(runtime.settings, on_text)
                log.info(
                    "media stream open for %s (asr=%s)",
                    call_id,
                    getattr(transcriber, "enabled", False),
                )

            elif event == "media" and session is not None:
                payload = base64.b64decode(message["media"]["payload"])
                # Transcription first: it is a bare socket write, and holding it
                # up behind our own analysis would delay every menu prompt.
                if transcriber is not None:
                    await transcriber.feed(payload)
                await session.on_audio(payload)

            elif event == "stop":
                break

    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("media socket error")
    finally:
        if transcriber is not None:
            await transcriber.close()
        if session is not None:
            log.info("media stream closed for %s", session.state.id)


# --------------------------------------------------------------------------
# A small status page, so you can watch a call without curl
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse((Path(__file__).parent / "dashboard.html").read_text())
