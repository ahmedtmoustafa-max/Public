"""HTTP surface: control API, TwiML webhooks, media socket."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from phone_agent import app as app_module
from phone_agent.settings import get_settings
from phone_agent.telephony import Telephony
from synth import melodic_music, silence, speech_like

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agent.test")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")
    monkeypatch.setenv("MY_PHONE_NUMBER", "+15551110000")
    monkeypatch.setenv("VALIDATE_TWILIO_SIGNATURES", "false")
    monkeypatch.setenv("NOTIFY_SMS", "false")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "calls.db"))

    placed: list[tuple] = []
    monkeypatch.setattr(
        Telephony,
        "originate",
        lambda self, call_id, to, timeout: placed.append((call_id, to)) or "CA-test",
    )
    monkeypatch.setattr(Telephony, "interrupt", lambda self, sid, url: True)
    monkeypatch.setattr(Telephony, "hangup", lambda self, sid: None)
    monkeypatch.setattr(
        Telephony, "dial_user", lambda self, call_id, to, conf: "CA-bridge"
    )

    get_settings.cache_clear()
    with TestClient(app_module.app) as test_client:
        test_client.placed = placed
        yield test_client
    get_settings.cache_clear()


def start_call(client, **overrides):
    payload = {
        "to": "+18005551212",
        "mode": "script",
        "keys": "1,0",
        "label": "Test Line",
    }
    payload.update(overrides)
    return client.post("/calls", json=payload)


# --- control API -----------------------------------------------------------


def test_healthz_reports_configuration(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["asr"] == "none"
    assert body["live_calls"] == 0


def test_placing_a_call_returns_its_state(client):
    response = start_call(client)
    assert response.status_code == 201, response.text
    call = response.json()["call"]
    assert call["phase"] == "created"
    assert call["to"] == "+18005551212"
    assert client.placed == [(call["id"], "+18005551212")]


def test_emergency_numbers_are_refused(client):
    response = start_call(client, to="911")
    assert response.status_code == 400
    assert "blocked" in response.json()["detail"]


def test_script_mode_requires_keys(client):
    response = start_call(client, keys="")
    assert response.status_code == 400
    assert "keys" in response.json()["detail"]


def test_auto_mode_requires_speech_to_text(client):
    response = start_call(client, mode="auto", keys="")
    assert response.status_code == 400
    assert "DEEPGRAM_API_KEY" in response.json()["detail"]


def test_call_can_be_fetched_and_cancelled(client):
    call_id = start_call(client).json()["call"]["id"]

    assert client.get(f"/calls/{call_id}").json()["call"]["id"] == call_id
    assert len(client.get("/calls").json()["live"]) == 1

    assert client.delete(f"/calls/{call_id}").status_code == 200
    assert client.get("/calls").json()["live"] == []
    # It survives in history.
    assert client.get(f"/calls/{call_id}").json()["call"]["phase"] == "cancelled"


def test_unknown_call_is_a_404(client):
    assert client.get("/calls/nope").status_code == 404
    assert client.delete("/calls/nope").status_code == 404


def test_concurrency_limit_is_enforced(client, monkeypatch):
    monkeypatch.setattr(app_module.runtime.settings, "max_concurrent_calls", 1)
    assert start_call(client).status_code == 201
    response = start_call(client)
    assert response.status_code == 429


# --- TwiML webhooks --------------------------------------------------------


def test_start_webhook_opens_the_media_stream(client):
    call_id = start_call(client).json()["call"]["id"]
    response = client.post(f"/twiml/start?call_id={call_id}", data={}, headers=FORM)
    assert response.status_code == 200
    assert "<Stream" in response.text
    assert "wss://agent.test/ws/media" in response.text
    assert f'value="{call_id}"' in response.text


def test_next_webhook_idles(client):
    call_id = start_call(client).json()["call"]["id"]
    client.post(f"/twiml/start?call_id={call_id}", data={}, headers=FORM)
    response = client.post(f"/twiml/next?call_id={call_id}", data={}, headers=FORM)
    assert "<Pause" in response.text


def test_webhooks_for_unknown_calls_hang_up(client):
    response = client.post("/twiml/next?call_id=ghost", data={}, headers=FORM)
    assert "<Hangup" in response.text


def test_bridge_webhook_announces_and_joins_the_conference(client):
    call_id = start_call(client).json()["call"]["id"]
    response = client.post(f"/twiml/bridge?call_id={call_id}", data={}, headers=FORM)
    assert "Test Line" in response.text
    assert "<Conference" in response.text
    assert 'endConferenceOnExit="true"' in response.text


def test_status_callback_retires_a_finished_call(client):
    call_id = start_call(client).json()["call"]["id"]
    response = client.post(
        f"/twilio/status?call_id={call_id}",
        data={"CallStatus": "completed"},
        headers=FORM,
    )
    assert response.status_code == 204
    assert client.get("/calls").json()["live"] == []


def test_voicemail_detection_ends_the_call(client):
    call_id = start_call(client).json()["call"]["id"]
    client.post(
        f"/twilio/amd?call_id={call_id}",
        data={"AnsweredBy": "machine_end_beep"},
        headers=FORM,
    )
    assert client.get(f"/calls/{call_id}").json()["call"]["phase"] == "failed"


def test_signature_is_required_when_validation_is_on(client, monkeypatch):
    call_id = start_call(client).json()["call"]["id"]
    monkeypatch.setattr(
        app_module.runtime.settings, "validate_twilio_signatures", True
    )
    response = client.post(f"/twiml/next?call_id={call_id}", data={}, headers=FORM)
    assert response.status_code == 403


# --- media socket ----------------------------------------------------------


def _frames_to_ulaw(frames):
    """Encode float frames back to mu-law payloads, as Twilio would send them."""
    import audioop

    import numpy as np

    payloads = []
    for frame in frames:
        pcm = (np.clip(frame, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        payloads.append(audioop.lin2ulaw(pcm, 2))
    return payloads


def test_media_socket_drives_detection_through_to_a_bridge(client):
    call_id = start_call(client, mode="watch", keys="").json()["call"]["id"]

    script = melodic_music(25.0) + speech_like(3.0) + silence(3.0)
    with client.websocket_connect("/ws/media") as socket:
        socket.send_text(
            json.dumps(
                {
                    "event": "start",
                    "start": {"customParameters": {"callId": call_id}},
                }
            )
        )
        for payload in _frames_to_ulaw(script):
            socket.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "media": {"payload": base64.b64encode(payload).decode()},
                    }
                )
            )
        socket.send_text(json.dumps({"event": "stop"}))

    call = client.get(f"/calls/{call_id}").json()["call"]
    assert call["phase"] in {"human_detected", "bridging", "bridged"}
    assert any("Human detected" in e["text"] for e in call["transcript"])


def test_media_socket_closes_on_an_unknown_call(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/media") as socket:
            socket.send_text(
                json.dumps(
                    {"event": "start", "start": {"customParameters": {"callId": "ghost"}}}
                )
            )
            socket.receive_text()


def test_a_saved_playbook_is_used_and_said_so(client, tmp_path):
    app_module.runtime.playbooks.learn(
        label="Saved Clinic",
        number="+18005551212",
        keys="2,w9",
        goal="reception",
        call_id="earlier",
    )
    call = start_call(client, keys="", mode="auto", label="").json()["call"]
    assert call["mode"] == "script"
    assert call["label"] == "Saved Clinic"
    assert any("playbook" in e["text"] for e in call["transcript"])
