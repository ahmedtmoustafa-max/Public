# How a call actually runs

A walkthrough of one call, from `POST /calls` to you saying hello.

## 1. Placing the call

`POST /calls` validates the destination (emergency numbers refused outright,
allowlist enforced if set), looks for a saved playbook, and creates a
`CallState` with a unique conference name. Then `Telephony.originate` places
the outbound call with:

- `url=/twiml/start` — what to run when it's answered
- `status_callback=/twilio/status` — lifecycle events
- `machine_detection=Enable` with an async callback — so voicemail is detected
  and the call abandoned rather than sitting there talking to a beep
- `time_limit` — a hard ceiling Twilio enforces even if we crash

## 2. Answered

`/twiml/start` returns:

```xml
<Response>
  <Start>
    <Stream name="agent-listen" track="inbound_track" url="wss://.../ws/media">
      <Parameter name="callId" value="7f2a91c4e0b3"/>
    </Stream>
  </Start>
  <Redirect method="POST">https://.../twiml/next?call_id=7f2a91c4e0b3</Redirect>
</Response>
```

`<Start><Stream>` forks the far end's audio to our WebSocket and keeps running
across every subsequent TwiML document. `track="inbound_track"` is what the
other end is saying.

## 3. The idle loop

`/twiml/next` asks the `CallSession` what to do. With nothing pending:

```xml
<Response>
  <Pause length="2"/>
  <Redirect method="POST">https://.../twiml/next?call_id=...</Redirect>
</Response>
```

Two seconds while navigating (responsive), fifteen while on hold (quiet). The
pause is silence, not hold music — that matters, because Twilio's default
conference `waitUrl` would play music straight into the audio we're analysing.

## 4. Pressing a key

Audio arrives on the WebSocket as 20 ms mu-law frames. `AudioAnalyzer`
aggregates them into one-second windows; `HumanDetector` scores each one; if
Deepgram is configured, transcripts arrive in parallel.

When the session decides to press something, it sets `pending_action` and
**interrupts** the pause via REST:

```python
client.calls(sid).update(url="/twiml/next?call_id=...", method="POST")
```

The call abandons the `<Pause>`, fetches `/twiml/next`, and gets:

```xml
<Response>
  <Play digits="w3"/>
  <Redirect method="POST">https://.../twiml/next?call_id=...</Redirect>
</Response>
```

…and falls straight back into the idle loop. One mechanism covers keypresses,
speech, and the bridge.

## 5. The queue

Once the key sequence is spent (script mode) or the detector sees queue
phrasing, repetition, or sustained non-speech audio, the phase moves to
`on_hold`. Nothing is pressed from here on; the tick lengthens to 15 s.

## 6. A person

The detector fires. In order:

1. Phase → `human_detected`, then `bridging`.
2. A `SPEAK` action is queued and the pause interrupted, so the agent says
   *"this is an automated assistant holding the line…"* — both a disclosure and
   a way to stop them hanging up on dead air.
3. Your phone is dialled on a second leg.
4. Notifications fan out (ntfy, Pushover, SMS).
5. If keys were pressed, the sequence is saved as a playbook.

`/twiml/next` now returns a conference join **with the stream stopped**:

```xml
<Response>
  <Stop><Stream name="agent-listen"/></Stop>
  <Dial><Conference waitUrl="" beep="false" ...>pa-7f2a91c4e0b3</Conference></Dial>
</Response>
```

Your leg gets `/twiml/bridge`: a one-line announcement, then the same
conference with `endConferenceOnExit="true"` — when you hang up, everything
ends.

## Why a conference at all

A conference is the only way to add a second party to a call that's already up.
`<Dial>` on an outbound call would create a child leg, not join the existing
one. Joining by *name* also means the ordering doesn't matter: whoever arrives
first waits in silence.

## Failure modes

| what happens | what the system does |
| --- | --- |
| Voicemail answers | AMD callback fires, call abandoned, you're told |
| Nobody ever picks up | `time_limit` ends the call; you're told |
| You don't answer the bridge | Logged; the agent leg stays up for 45 s more |
| Deepgram drops | Falls back to audio-only detection, call continues |
| Claude is unavailable | Returns `wait`; the call keeps holding |
| Server restarts mid-call | Call is lost — Twilio can't reach `/twiml/next`. In-flight state is memory-only by design; history is in SQLite |
