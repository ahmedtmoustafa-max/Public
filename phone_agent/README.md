# phone-agent

Dials a phone tree for you, works through the menu, sits in the hold queue for
as long as it takes, and **rings your phone the moment a human picks up** — with
the agent already on the line, told to hold on for a moment.

You get your hour back. You still talk to the person yourself.

```
$ phone-agent call +18005551212 --label "Air Canada" --keys 1,w3,0

Calling Air Canada -- call id 7f2a91c4e0b3
You can close this window; you'll be notified and your phone will ring.

   · Dialling Air Canada in script mode.
   · Answered by Air Canada.
   >> press 1
   >> press w3
   >> press 0
   · In the hold queue (key sequence finished). Watching for a human.
[3m12s] in the queue
[41m08s] in the queue
   · Human detected (0.78): spoke for 3s then waited 2s
   >> Hello. This is an automated assistant holding the line on behalf of
      the caller. Please stay on for just a moment while I connect them.
[41m19s] RINGING YOUR PHONE -- PICK UP  (held 38m02s)
```

---

## How it works

```
   you ──POST /calls──▶  phone-agent  ──Twilio REST──▶  ☎  the airline
                             │                              │
                             │   ◀── audio (Media Stream) ──┘
                             │
                    ┌────────┴────────┐
                    │  is that a      │   audio features + optional transcript
                    │  hold loop or   │   ──────────────────────────────────▶
                    │  a person?      │
                    └────────┬────────┘
                             │  a person!
            ┌────────────────┼───────────────────┐
            ▼                ▼                   ▼
      push + SMS      "please hold for      ring your phone,
      to your phone    one moment"          drop you into the
                       (spoken to them)     same conference
```

The call runs as a **polling loop**: while there's nothing to do, the server
answers Twilio with a `<Pause>` and a `<Redirect>`, which keeps the line open
and silent. When the agent needs to press a key, speak, or bridge you in, it
interrupts that pause with a REST redirect. All the decisions live in one state
machine (`orchestrator.py`); the webhooks just ask it what to do next.

Audio is forked to a WebSocket with `<Start><Stream>` so we can listen without
occupying the call. The moment you're bridged in, the stream is stopped —
there's no reason for the agent to keep listening to your conversation.

## Three modes

| mode | what it does | needs |
| --- | --- | --- |
| `script` | Plays a key sequence you supply (`1,w3,0`), then watches the queue. | Twilio only |
| `watch` | Touches nothing; just sits on the line and watches for a human. | Twilio only |
| `auto` | Claude listens to the menu and picks the options itself. | + Anthropic + Deepgram |

**Start with `script`.** For any number you call regularly you already know the
keys, and a fixed sequence is faster and more reliable than any model. `auto` is
for numbers you've never called. When an `auto` call reaches a person, the key
sequence that worked is saved as a **playbook**, so the next call to that number
runs in `script` mode automatically.

## Telling a person from a recording

This is the hard part, and it isn't "speech vs. music" — hold announcements
*are* speech. Three signals separate them:

1. **What they say.** Agents greet you and ask questions; recordings apologise
   about wait times. (`detector.py`, phrase banks.)
2. **Repetition.** Hold loops come around again. People don't.
3. **Expectant silence.** A person says a short thing and then *stops*, waiting
   for an answer. A recording is followed by more recording, or by music.

Signal 3 needs no transcription at all, which is why `script` and `watch` modes
work with nothing but a Twilio account. The audio side measures amplitude
modulation depth in the 2–10 Hz syllable band, pause ratio, and spectral
flatness — see `media.py`.

`DETECTOR_STRICTNESS` (0–1, default `0.55`) trades false alarms against missed
agents. Lower it if it's slow to notice; raise it if your phone rings while a
recording is still talking. The system is deliberately biased toward alerting
you a little early: the agent says "one moment please" to hold the line, so an
early alert costs you a few seconds and a late one costs you the call.

---

## Setup

### 1. Twilio

Sign up at [twilio.com](https://www.twilio.com/), buy a number with **Voice**
capability, and note your Account SID and Auth Token. Expect roughly
**US$1–2/month** for the number and **~US$0.013/minute** for outbound US/Canada
calls — an hour on hold is under a dollar.

### 2. Install

```bash
git clone <this repo> && cd phone_agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env         # then fill it in
cp config/profile.example.yaml config/profile.yaml
```

The two settings that matter most:

- `MY_PHONE_NUMBER` — the phone that rings when a human answers.
- `PUBLIC_BASE_URL` — where Twilio can reach this server, over HTTPS.

### 3. Make it reachable

Twilio fetches TwiML and opens the media WebSocket over the public internet, so
the server needs a public HTTPS address.

**On your Mac, for testing:**

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8080      # prints an https URL
# put that URL in PUBLIC_BASE_URL, then:
PYTHONPATH=src .venv/bin/python -m phone_agent
```

**For real use, deploy it.** A laptop that sleeps mid-call drops the call, and
hold queues are exactly when your laptop sleeps. `fly.toml` and `Dockerfile` are
included:

```bash
fly launch --no-deploy
fly secrets set TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... \
                TWILIO_FROM_NUMBER=... MY_PHONE_NUMBER=... \
                PUBLIC_BASE_URL=https://your-app.fly.dev
fly deploy
```

`min_machines_running = 1` is set deliberately — media streams are long-lived
WebSockets and must not be scaled to zero.

### 4. Get notified

Cheapest and best: install the **ntfy** app (iOS/Android), subscribe to a topic
nobody could guess, and set `NOTIFY_NTFY_TOPIC`. Pushover and SMS also work. All
of them fire at once, and your phone ringing is the real notification anyway.

---

## Using it

```bash
alias phone-agent='PYTHONPATH=src /path/to/.venv/bin/python -m phone_agent.cli'

# A number whose menu you know
phone-agent call +18005551212 --label "Air Canada" --keys 1,w3,0

# A number you've never called; let Claude read the menu
phone-agent call +15195551234 --label "Dr. Patel's office" \
    --goal "reach reception to reschedule an appointment"

# Already transferred and just want to be told when someone picks up
phone-agent call +18005551212 --watch-only

phone-agent watch <call-id>      # follow along
phone-agent cancel <call-id>     # hang up
phone-agent playbooks            # what it has learned
```

There's a live status page at `http://localhost:8080/` showing calls in
progress with their running transcripts.

`--keys` syntax: commas separate menu levels (it waits for each prompt to
finish), `w` is a half-second pause inside a step, `*` and `#` work as expected.
A step of just `w`s sends nothing but time, which is useful when a line ignores
input during its opening announcement: `ww,1,0`.

### The HTTP API

| | |
| --- | --- |
| `POST /calls` | place a call (`to`, `goal`, `mode`, `keys`, `label`) |
| `GET /calls` | live calls plus recent history |
| `GET /calls/{id}` | one call, with its transcript |
| `DELETE /calls/{id}` | hang up |
| `GET /playbooks` | saved key sequences |
| `GET /healthz` | what's configured |

---

## What it will not do

The agent navigates menus. It does not represent you.

- **It won't give out information you haven't listed.** Everything in
  `config/profile.yaml` under `may_disclose` is the complete set of facts it may
  provide. Anything else — a date of birth, a health card number, a PIN — it is
  instructed to wait rather than guess. A tree that demands one is usually about
  to hand you to a person anyway, which is a fine moment for you to take over.
- **It won't commit to anything.** No purchases, no cancellations, no accepting
  terms. Those are yours to make.
- **It won't dial emergency services.** 911/112/999/988 are refused outright,
  allowlist or not. If you need help, call directly.
- **It won't take a callback offer**, since you're not on the line to receive it.

Set `ALLOWED_DESTINATIONS` to an explicit list if this runs anywhere other than
your own machine, and keep `VALIDATE_TWILIO_SIGNATURES=true` — the webhooks are
public URLs that can place and steer calls.

## Before you point it at a real number

- **Disclosure is on by default.** The first thing the agent says to a human is
  that it's an automated assistant holding the line. Leave that on. Several
  jurisdictions now require AI callers to identify themselves, and it's the
  decent thing regardless.
- **Recording.** This doesn't record calls. If you add recording, note that
  Ontario is one-party consent but many US states are all-party — and you may be
  calling into one.
- **One call at a time, to places you actually need.** `MAX_CONCURRENT_CALLS`
  defaults to 3. This is a personal assistant, and repeatedly auto-dialling an
  organisation is both rude and, in bulk, illegal. Some companies' terms also
  prohibit automated access to their phone systems.

## Known limits

- Speech-driven menus ("in a few words, tell me why you're calling") work in
  `auto` mode only, and less reliably than keypad trees.
- Menus that require account authentication will stall by design — that's the
  point at which it waits for you.
- Very noisy lines degrade the audio-only detector. Add Deepgram if a number
  gives you trouble.
- If you don't pick up within 45 seconds, the bridge leg gives up. The agent
  call stays alive, so you can call back into it — but the person will likely
  have hung up.

## Tests

```bash
.venv/bin/python -m pytest        # 85 tests, no network, no Twilio account
```

The detector is tested against synthesised hold music, recorded-announcement
patterns and agent greetings, including a five-minute hold-music soak test and a
full menu → queue → recording → agent sequence. `test_api.py` drives real
audio through the WebSocket end to end.

## Layout

```
src/phone_agent/
  app.py           FastAPI: control API, TwiML webhooks, media socket
  orchestrator.py  the per-call state machine (all the decisions live here)
  detector.py      hold queue vs. live human
  media.py         mu-law decoding, speech/music features
  brain.py         Claude picks the next menu option (auto mode)
  twiml.py         the TwiML documents
  telephony.py     Twilio REST + destination guardrails
  notify.py        ntfy / Pushover / SMS
  playbooks.py     saved and learned key sequences
  cli.py           the command line
```
