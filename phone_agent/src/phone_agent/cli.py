"""Command line front end.

    python -m phone_agent.cli call +18005551212 --label "Air Canada" --keys 1,w3,0
    python -m phone_agent.cli call +18005551212 --goal "reschedule my appointment"
    python -m phone_agent.cli watch <call-id>
    python -m phone_agent.cli playbooks
    python -m phone_agent.cli doctor --live
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

DEFAULT_SERVER = os.environ.get("PHONE_AGENT_URL", "http://localhost:8080")

PHASE_BLURB = {
    "created": "dialling",
    "navigating": "working through the menu",
    "on_hold": "in the queue",
    "human_detected": "A PERSON ANSWERED",
    "bridging": "RINGING YOUR PHONE -- PICK UP",
    "bridged": "connected, over to you",
    "completed": "call ended",
    "failed": "call failed",
    "cancelled": "cancelled",
}


def _fmt(seconds: float) -> str:
    seconds = int(seconds or 0)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def cmd_call(args: argparse.Namespace) -> int:
    mode = args.mode
    if mode is None:
        mode = "script" if args.keys else ("watch" if args.watch_only else "auto")

    payload = {
        "to": args.number,
        "goal": args.goal,
        "mode": mode,
        "keys": args.keys or "",
        "label": args.label or "",
        "playbook": args.playbook or "",
    }
    if args.callback:
        payload["callback_number"] = args.callback
    if args.max_seconds:
        payload["max_seconds"] = args.max_seconds

    try:
        response = httpx.post(f"{args.server}/calls", json=payload, timeout=30)
    except httpx.HTTPError as exc:
        print(f"Could not reach the server at {args.server}: {exc}", file=sys.stderr)
        return 1

    if response.status_code >= 400:
        print(f"Rejected: {_detail(response)}", file=sys.stderr)
        return 1

    call = response.json()["call"]
    print(f"Calling {call['label'] or call['to']} -- call id {call['id']}")
    print("You can close this window; you'll be notified and your phone will ring.\n")
    return follow(args.server, call["id"]) if not args.no_follow else 0


def cmd_watch(args: argparse.Namespace) -> int:
    return follow(args.server, args.call_id)


def follow(server: str, call_id: str) -> int:
    last_line = ""
    seen = 0
    while True:
        try:
            response = httpx.get(f"{server}/calls/{call_id}", timeout=15)
        except httpx.HTTPError:
            time.sleep(2)
            continue
        if response.status_code == 404:
            print("Call not found.", file=sys.stderr)
            return 1

        call = response.json()["call"]
        for entry in call.get("transcript", [])[seen:]:
            marker = {"them": "  <<", "agent": "  >>", "system": "   ·"}.get(
                entry["speaker"], "   ·"
            )
            print(f"{marker} {entry['text']}")
        seen = len(call.get("transcript", []))

        phase = call["phase"]
        line = (
            f"[{_fmt(call['elapsed_seconds'])}] {PHASE_BLURB.get(phase, phase)}"
            + (f" (held {_fmt(call['hold_seconds'])})" if call["hold_seconds"] else "")
        )
        if line != last_line:
            print(line)
            last_line = line

        if phase in {"bridged", "completed", "failed", "cancelled"}:
            return 0 if phase in {"bridged", "completed"} else 1
        time.sleep(2)


def cmd_doctor(args: argparse.Namespace) -> int:
    from phone_agent.doctor import live_checks, report, static_checks, worst
    from phone_agent.settings import get_settings

    settings = get_settings()
    checks = static_checks(settings)
    if args.live:
        checks += live_checks(settings)

    print("Configuration:\n")
    print(report(checks))
    verdict = worst(checks)
    print()
    if verdict == "fail":
        print("Not ready yet -- fix the FAIL lines above.")
        return 1
    if not args.live:
        print("Looks right. Run `doctor --live` to check it against Twilio too.")
    else:
        print("Ready. Try a call you don't mind interrupting first.")
    return 0


def cmd_playbooks(args: argparse.Namespace) -> int:
    response = httpx.get(f"{args.server}/playbooks", timeout=15)
    books = response.json()["playbooks"]
    if not books:
        print("No playbooks saved yet. They're written automatically after a call "
              "in auto mode reaches a person.")
        return 0
    for book in books:
        flag = " (learned)" if book.get("learned") else ""
        print(f"{book['id']}{flag}\n  {book.get('label') or '-'} "
              f"{book.get('number') or ''}\n  keys: {book.get('keys') or '-'}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    response = httpx.delete(f"{args.server}/calls/{args.call_id}", timeout=15)
    if response.status_code >= 400:
        print(f"Failed: {_detail(response)}", file=sys.stderr)
        return 1
    print("Hung up.")
    return 0


def _detail(response: httpx.Response) -> str:
    try:
        return json.dumps(response.json().get("detail", response.text))
    except ValueError:
        return response.text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phone-agent")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    sub = parser.add_subparsers(dest="command", required=True)

    call = sub.add_parser("call", help="place a call")
    call.add_argument("number", help="E.164 number, e.g. +18005551212")
    call.add_argument("--goal", default="Reach a live human agent.")
    call.add_argument("--label", help="friendly name for notifications")
    call.add_argument("--keys", help="scripted key sequence, e.g. '1,w3,0'")
    call.add_argument("--playbook", help="saved playbook id to use")
    call.add_argument("--mode", choices=["auto", "script", "watch"])
    call.add_argument("--watch-only", action="store_true",
                      help="don't touch the menu, just watch for a human")
    call.add_argument("--callback", help="ring this number instead of MY_PHONE_NUMBER")
    call.add_argument("--max-seconds", type=int)
    call.add_argument("--no-follow", action="store_true")
    call.set_defaults(func=cmd_call)

    watch = sub.add_parser("watch", help="follow a call in progress")
    watch.add_argument("call_id")
    watch.set_defaults(func=cmd_watch)

    cancel = sub.add_parser("cancel", help="hang up a call")
    cancel.add_argument("call_id")
    cancel.set_defaults(func=cmd_cancel)

    doctor = sub.add_parser("doctor", help="check the configuration")
    doctor.add_argument(
        "--live", action="store_true", help="also verify the Twilio account"
    )
    doctor.set_defaults(func=cmd_doctor)

    books = sub.add_parser("playbooks", help="list saved key sequences")
    books.set_defaults(func=cmd_playbooks)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
