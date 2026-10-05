"""CLI input and presentation only; sessions own all execution rules."""

import json
import sys
from contextlib import ExitStack

from .errors import RkaiError
from .execution import Session
from .files import read_file
from .manifest import MAX_MANIFEST_BYTES, no_duplicates, reject_constant


def request_data(capability, text):
    if capability == "chat" and not text.lstrip().startswith("{"):
        return {"messages": [{"role": "user", "content": text}]}
    try:
        data = json.loads(text, object_pairs_hook=no_duplicates, parse_constant=reject_constant)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, RecursionError) as e:
        raise RkaiError("invalid_input", "Capability requires a typed JSON request", 2) from e


def emit(event, json_output):
    if json_output:
        print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)
    elif event["type"] == "accepted":
        print(
            f"plan={event['plan']} device={event['device']} reason={event['selection_reason']}",
            file=sys.stderr,
        )
    elif event["type"] == "output":
        if event["kind"] == "delta":
            print(event["payload"]["text"], end="", flush=True)
        elif "text" not in event["payload"]:
            print(json.dumps(event["payload"], ensure_ascii=False))
    elif event["type"] in ("completed", "cancelled"):
        print()
    elif event["type"] == "failed":
        print(json.dumps({"error": event["error"]}, ensure_ascii=False), file=sys.stderr)


def terminal_exit_code(event):
    if event["type"] == "failed":
        return event["error"]["exit_code"]
    return {"completed": 0, "cancelled": 130}.get(event["type"])


def consume(session, data, json_output):
    stream = session.infer(data)
    code = 0
    last = None
    try:
        while True:
            try:
                event = next(stream)
            except StopIteration:
                return code
            last = event
            terminal_code = terminal_exit_code(event)
            if terminal_code is not None:
                code = terminal_code
            emit(event, json_output)
    except KeyboardInterrupt:
        if last and (terminal_code := terminal_exit_code(last)) is not None:
            return terminal_code
        session.cancel()
        # Reap before returning to a prompt or reporting cancellation.
        session.close()
        if last and last["type"] not in ("completed", "failed", "cancelled"):
            emit(
                {
                    "session_id": session.id,
                    "request_id": last["request_id"],
                    "sequence": last["sequence"] + 1,
                    "type": "cancelled",
                },
                json_output,
            )
        return 130
    finally:
        stream.close()


def run(library, args, json_output):
    with ExitStack() as owned:
        session = owned.enter_context(Session(library, args.model, args.capability, args.allow_cpu))
        if args.interactive:
            if session.capability != "chat":
                raise RkaiError(
                    "unsupported_interactive", "Continuous interaction requires chat capability", 3
                )
            while True:
                try:
                    print("> ", file=sys.stderr, end="", flush=True)
                    text = input()
                except (EOFError, KeyboardInterrupt):
                    return 0
                if text == "/exit":
                    return 0
                if text == "/reset":
                    session.reset()
                    continue
                if not text:
                    continue
                code = consume(
                    session,
                    {"messages": session.history + [{"role": "user", "content": text}]},
                    json_output,
                )
                if code == 130 or session.closed:
                    # A forced native cancellation retires context; reopen before next request.
                    history = session.history
                    session.close()
                    session = owned.enter_context(
                        Session(library, args.model, args.capability, args.allow_cpu)
                    )
                    session.history = history
        else:
            if args.input_file:
                from pathlib import Path

                raw = read_file(
                    Path(args.input_file).expanduser().resolve(), MAX_MANIFEST_BYTES + 1
                )
                text = raw.decode()
            elif args.input is not None:
                text = args.input
            elif sys.stdin.isatty():
                raise RkaiError("missing_input", "Use --input, --input-file or --interactive", 2)
            else:
                text = sys.stdin.read(MAX_MANIFEST_BYTES + 1)
            if len(text.encode()) > MAX_MANIFEST_BYTES:
                raise RkaiError("input_too_large", "Request exceeds 4 MiB", 2)
            return consume(session, request_data(session.capability, text), json_output)
