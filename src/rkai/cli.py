import argparse
import json
import logging
import sqlite3
import sys
import traceback
from pathlib import Path

from .config import load
from .doctor import configuration_failure, doctor
from .errors import RkaiError
from .storage import Library


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise RkaiError("invalid_arguments", message, 2)


def parser():
    p = Parser(prog="rkai", description="Model acquisition, conversion and local execution")
    p.add_argument("--config")
    p.add_argument("--data-dir")
    p.add_argument("--profile", choices=("release", "debug"))
    p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="Separate static, dynamic load and explicit probe evidence")
    d.add_argument("--model")
    d.add_argument("--probe", action="store_true")
    d.add_argument("--load-runtime", action="store_true")
    pull = sub.add_parser("pull", help="Acquire a published manifest or import a local manifest")
    pull.add_argument("ref", nargs="?")
    pull.add_argument("--registry")
    pull.add_argument("--target", choices=("rk3588",))
    pull.add_argument("--plan")
    pull.add_argument("--alias")
    operation = pull.add_mutually_exclusive_group()
    operation.add_argument("--resume")
    operation.add_argument("--discard", help="Discard pending bytes/plan without changing aliases")
    delete = sub.add_parser("delete", help="Remove aliases and unreferenced artifacts")
    delete.add_argument("alias")
    delete.add_argument("--purge", action="store_true")
    conversion = sub.add_parser("convert", help="Convert a fixed source in an isolated environment")
    conversion.add_argument("ref")
    conversion.add_argument("--recipe", required=True)
    conversion.add_argument("--registry")
    conversion.add_argument("--output")
    conversion.add_argument("--alias")
    run = sub.add_parser("run", help="Execute an installed snapshot; no downloads")
    run.add_argument("-m", "--model", required=True)
    run.add_argument("--capability")
    inputs = run.add_mutually_exclusive_group()
    inputs.add_argument("--input")
    inputs.add_argument("--input-file")
    inputs.add_argument("--interactive", action="store_true")
    cpu = run.add_mutually_exclusive_group()
    cpu.add_argument("--allow-cpu-fallback", dest="allow_cpu", action="store_true", default=None)
    cpu.add_argument("--no-cpu-fallback", dest="allow_cpu", action="store_false")
    runtime = sub.add_parser("runtime", help="Explicit preparation of managed runtime environments")
    runtime.add_argument("action", choices=("prepare",))
    runtime.add_argument(
        "environment",
        choices=(
            "onnx-cpu",
            "llama-cpu",
            "rknn-convert",
            "rknn-runtime",
            "rkllm-runtime",
            "rkllm-convert",
        ),
    )
    # Result format can be supplied before or after the command.
    for command in (d, pull, delete, conversion, run, runtime):
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return p


def main(argv=None):
    args_raw = sys.argv[1:] if argv is None else argv
    json_output = "--json" in args_raw
    try:
        args = parser().parse_args(args_raw)
        try:
            config = load(args.config, args.data_dir)
        except RkaiError as e:
            if args.command != "doctor":
                raise
            result = configuration_failure(e)
            print(json.dumps(result, ensure_ascii=False) if json_output else _display(result))
            return e.exit_code
        profile = args.profile or config.profile
        level = args.log_level or config.log_level or ("DEBUG" if profile == "debug" else "INFO")
        logging.basicConfig(level=level, format="%(levelname)s %(name)s %(message)s", force=True)
        # Third-party transport diagnostics can include signed URL queries; never propagate them.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("rkai").debug("profile=%s command=%s", profile, args.command)
        if args.command == "doctor":
            result = doctor(config, args.model, args.probe, load_runtime=args.load_runtime)
            exit_code = 3 if result["status"] != "ready" else 0
        else:
            with Library(config) as library:
                if args.command == "pull":
                    if args.resume or args.discard:
                        if args.ref or args.registry or args.target or args.plan or args.alias:
                            raise RkaiError(
                                "invalid_arguments",
                                "--resume/--discard use only their persisted operation",
                                2,
                            )
                        result = (
                            library.resume(args.resume)
                            if args.resume
                            else library.discard(args.discard)
                        )
                    else:
                        if not args.ref:
                            raise RkaiError("invalid_arguments", "pull requires REF or --resume", 2)
                        result = library.pull(
                            args.ref, args.registry, args.target, args.plan, args.alias
                        )
                elif args.command == "convert":
                    from .conversion import convert

                    result = convert(
                        library, args.ref, args.recipe, args.registry, args.output, args.alias
                    )
                elif args.command == "runtime":
                    from .environments import prepare

                    if args.environment.startswith(("rknn", "rkllm")):
                        print(
                            "Rockchip SDK LICENSE applies; fixed official sources: ROCKCHIP.md",
                            file=sys.stderr,
                        )
                    result = prepare(library, args.environment)
                elif args.command == "run":
                    from .run_cli import run

                    return run(library, args, json_output)
                else:
                    result = library.delete(args.alias, args.purge)
                exit_code = 0
        print(json.dumps(result, ensure_ascii=False) if json_output else _display(result))
        return exit_code
    except RkaiError as e:
        _diagnostic(e)
        print(
            json.dumps(e.result(), ensure_ascii=False),
            file=sys.stdout if json_output else sys.stderr,
        )
        return e.exit_code
    except KeyboardInterrupt:
        error = RkaiError("interrupted", "Operation interrupted", 130)
        print(json.dumps(error.result()), file=sys.stdout if json_output else sys.stderr)
        return 130
    except sqlite3.Error as e:
        _diagnostic(e)
        busy = isinstance(e, sqlite3.OperationalError) and "locked" in str(e).lower()
        error = RkaiError(
            "busy" if busy else "database_error",
            "Library database is busy" if busy else "Library database operation failed",
            4 if busy else 1,
        )
        print(json.dumps(error.result()), file=sys.stdout if json_output else sys.stderr)
        return error.exit_code
    except (OSError, ValueError) as e:
        # Public error never contains user paths, URL secrets or native exception details.
        _diagnostic(e)
        error = RkaiError("storage_error", "Local storage operation failed")
        print(json.dumps(error.result()), file=sys.stdout if json_output else sys.stderr)
        return 1


def _display(result):
    if "checks" in result:
        return (
            result["status"]
            + "\n"
            + "\n".join(
                f"{c['status']:7} {c['name']}: {c['evidence']} — {c['remedy']}"
                for c in result["checks"]
            )
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


def _diagnostic(error):
    frames = traceback.extract_tb(error.__traceback__)
    safe_frames = " -> ".join(f"{Path(f.filename).stem}.{f.name}:{f.lineno}" for f in frames)
    logging.getLogger("rkai").debug("failure=%s frames=%s", type(error).__name__, safe_frames)


if __name__ == "__main__":
    sys.exit(main())
