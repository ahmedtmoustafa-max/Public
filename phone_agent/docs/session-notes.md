# Phone agent — build notes

*Drop this into the Obsidian vault (`Documents/Mind map`) so the next session
picks up where this one left off. The vault itself isn't reachable from the
remote Claude Code container, which is why this is a file in the repo.*

Built: 2026-09-19 · Branch: `claude/phone-call-automation-51zqhk` · Repo: `ahmedtmoustafa-max/Public`

## What exists

`phone_agent/` — a working service that dials a number, works through the phone
tree, waits out the hold queue, and rings your phone when a live human answers.
86 tests, no network needed to run them.

## The decisions worth remembering

**Twilio, not a voice-AI platform.** Bland/Vapi/Retell are built to *replace*
the human on the call. This does the opposite: it holds a seat and hands the
call over. Twilio's primitives (Media Streams, conferences, call redirect) are
the right level for that.

**The call is a polling loop.** While idle, the webhook returns
`<Pause>` + `<Redirect>`. This keeps the line open and *silent* — the default
`<Conference>` would play Twilio's hold music over the audio we're trying to
analyse (`waitUrl=""` matters). To act sooner than the next tick, the server
interrupts the pause with a REST redirect. One mechanism for keypresses,
speech, and bridging.

**`<Start><Stream>` for listening, conference for bridging.** The stream is
one-way and survives every redirect, so audio keeps flowing throughout. It's
named and explicitly stopped at bridge time — once the call is yours, the agent
stops listening.

**Hold-vs-human is not speech-vs-music.** Hold announcements *are* speech. The
three signals that actually work: agent phrasing vs. queue phrasing, repetition
(loops come round again), and expectant silence (a person stops and waits).
The third needs no transcription, which is what makes the Twilio-only modes
viable.

**Three modes, and `script` is the default you want.** For numbers you call
often you already know the keys. `auto` (Claude + Deepgram) is for unfamiliar
menus, and it saves what worked as a playbook so the next call is `script`.

## Bugs the tests caught (all fixed)

- The adaptive noise floor drifted *upward* under sustained audio until hold
  music read as silence — which then looked like a person pausing. Silence is
  now judged against the loud end of a 30 s rolling window, not the quiet end.
  There's a five-minute soak test for it.
- `_modulation_index` measured the 2–10 Hz *share* of envelope energy, which is
  scale-invariant: a flat envelope with jitter scored as high as speech. Now
  measures modulation *depth* relative to mean level. Speech 0.92 vs. melodic
  hold music 0.26.
- Emergency-number blocking used `endswith`, so any number ending in 911/999
  was refused. Now matches the whole number (optionally minus a country code).
- The Claude call ran while holding the audio lock, stalling the detector and
  the transcription feed for the length of a network round trip. Now spawned
  off the lock behind a `_deciding` flag.

## Next time

- Callback-option handling: some queues offer "press 1 and we'll call you
  back". The callback would land on the Twilio number — catch it on an inbound
  webhook and bridge from there. Would turn an hour of held line into nothing.
- Per-number detector tuning (some lines are noisy; `DETECTOR_STRICTNESS` is
  currently global).
- Playbooks are learned but never invalidated — if a company changes its menu,
  the stale sequence keeps getting used. Needs a failure counter.
- Not started: any deployment. Needs a Twilio account and a public HTTPS host
  before the first real call.

## Open question for Ahmed

Where should this run? A laptop that sleeps mid-call drops the call, and hold
queues are exactly when laptops sleep. `fly.toml` is in the repo for that
reason, but it hasn't been deployed.

---

## Update — phone number configured

`MY_PHONE_NUMBER` is set in `phone_agent/.env` on whatever machine runs the
server. **It is not in the repo, and must not go in:** `ahmedtmoustafa-max/Public`
is genuinely public on GitHub (`"private": false`), so a personal mobile there
would be visible to everyone and to every scraper, and history rewriting does
not reliably remove it.

Three things guard that now:

- `.env` and `config/profile.yaml` are gitignored.
- `tests/test_no_leaked_numbers.py` scans every tracked file and fails on any
  phone number outside the reserved 555 range. It caught a near-miss the same
  day it was written: a test of mine used a number one digit-group away from
  the real one, and again when this very note first spelled that number out.
- `scripts/setup.sh` writes `.env` with `chmod 600` and bakes nothing in.

Also added: `phone-agent doctor` (static config checks, plus `--live` to verify
the Twilio account, the outbound number's voice capability, and trial-account
verified-caller limits), and a guard refusing to dial your own number or the
service's own Twilio number.

Still outstanding before a first real call: Twilio account, a public HTTPS
host, and a decision on where this runs.
