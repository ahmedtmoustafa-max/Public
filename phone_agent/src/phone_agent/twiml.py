"""TwiML documents the Twilio webhooks return.

The call is driven as a polling loop: while there is nothing to do, we answer
with a `<Pause>` followed by a `<Redirect>` back to ``/twiml/next``. That keeps
the line open and silent (no Twilio hold music polluting the audio we analyse)
and gives us a natural tick. When something *does* need to happen sooner, the
orchestrator interrupts the pause with a REST call redirect -- see
``telephony.Telephony.interrupt``.
"""

from __future__ import annotations

from twilio.twiml.voice_response import Dial, VoiceResponse

#: Pause length while working through a menu -- responsive, a little chatty.
NAV_TICK_SECONDS = 2
#: Pause length while sitting in a queue -- quiet, we interrupt when needed.
HOLD_TICK_SECONDS = 15


#: Named so we can stop it again the moment you are bridged in -- there is
#: no reason for us to keep listening once the call is yours.
STREAM_NAME = "agent-listen"


def start_stream(stream_url: str, call_id: str, next_url: str) -> str:
    """Fork the far end's audio to our WebSocket, then enter the idle loop.

    ``<Start><Stream>`` is one-way and outlives the TwiML document that began
    it, so the audio keeps flowing through every redirect that follows.
    """
    response = VoiceResponse()
    start = response.start()
    stream = start.stream(url=stream_url, track="inbound_track", name=STREAM_NAME)
    stream.parameter(name="callId", value=call_id)
    response.redirect(next_url, method="POST")
    return str(response)


def idle(next_url: str, seconds: int = NAV_TICK_SECONDS) -> str:
    response = VoiceResponse()
    response.pause(length=seconds)
    response.redirect(next_url, method="POST")
    return str(response)


def press(digits: str, next_url: str) -> str:
    """Send DTMF. 'w' inserts a half-second pause between tones."""
    response = VoiceResponse()
    response.play(digits=digits)
    response.redirect(next_url, method="POST")
    return str(response)


def speak(text: str, voice: str, next_url: str) -> str:
    response = VoiceResponse()
    response.say(text, voice=voice)
    response.redirect(next_url, method="POST")
    return str(response)


def join_conference(
    name: str,
    *,
    end_on_exit: bool = False,
    start_on_enter: bool = True,
    status_callback: str | None = None,
    stop_stream: bool = False,
) -> str:
    """Park this leg in a named conference.

    ``wait_url=""`` matters: the default would play Twilio's hold music to the
    person we called while they are the only participant.
    """
    response = VoiceResponse()
    if stop_stream:
        response.stop().stream(name=STREAM_NAME)
    dial = Dial()
    dial.conference(
        name,
        beep="false",
        start_conference_on_enter=start_on_enter,
        end_conference_on_exit=end_on_exit,
        wait_url="",
        status_callback=status_callback,
        status_callback_event="join leave end" if status_callback else None,
        status_callback_method="POST" if status_callback else None,
    )
    response.append(dial)
    return str(response)


def bridge_user(name: str, announcement: str, voice: str) -> str:
    """TwiML for the leg that rings you: say what this is, then connect."""
    response = VoiceResponse()
    if announcement:
        response.say(announcement, voice=voice)
    # Hanging up your own leg ends the whole thing, which is what you want --
    # once you're done talking there is no reason to keep the agent on the line.
    dial = Dial()
    dial.conference(
        name,
        beep="false",
        start_conference_on_enter=True,
        end_conference_on_exit=True,
        wait_url="",
    )
    response.append(dial)
    return str(response)


def hangup(message: str = "", voice: str = "Polly.Matthew-Neural") -> str:
    response = VoiceResponse()
    if message:
        response.say(message, voice=voice)
    response.hangup()
    return str(response)
