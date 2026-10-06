"""Public CPU HTTP/SDK and installed CLI evidence. Never uses the default library.

First prepare an isolated library using verify_m2_public.py. This script does not
pull/convert/prepare implicitly and deliberately runs serially with other tests.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

import anthropic
import httpx
import openai


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_ready(client, process):
    until = time.monotonic() + 15
    while time.monotonic() < until:
        check(process.poll() is None, "Service exited during startup")
        try:
            result = client.get("/health")
            if result.status_code == 200 and result.json()["ready"]:
                return result.json()
        except httpx.TransportError:
            pass
        time.sleep(0.05)
    raise RuntimeError("Service startup deadline exceeded")


def run(cli, data_dir, image):
    evidence = {
        "device": "cpu",
        "linux_systemd": "not_run",
        "npu": "not_run",
        "sdk": {name: version(name) for name in ("openai", "anthropic", "fastapi", "uvicorn")},
    }
    with tempfile.TemporaryDirectory(prefix="rkai-m3-http-verification-") as temp:
        config = Path(temp) / "config.toml"
        config.write_text(f"data_dir = {json.dumps(str(data_dir))}\nallow_cpu_fallback = true\n")
        with socket.socket() as port_picker:
            port_picker.bind(("127.0.0.1", 0))
            port = port_picker.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        for sig in (signal.SIGTERM, signal.SIGINT):
            logs = Path(temp) / f"service-{sig.name}.log"
            with logs.open("wb") as log:
                process = subprocess.Popen(
                    [
                        str(cli),
                        "--config",
                        str(config),
                        "--profile",
                        "debug" if sig == signal.SIGINT else "release",
                        "serve",
                        "--port",
                        str(port),
                    ],
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
                try:
                    with httpx.Client(base_url=base, trust_env=False, timeout=30) as transport:
                        health = wait_ready(transport, process)
                        check(health["model"]["state"] == "unloaded", "Lazy load failed")
                        aliases = transport.get("/v1/models").json()["data"]
                        check(len(aliases) >= 3, "Prepared public model aliases missing")
                        collision = subprocess.run(
                            [
                                str(cli),
                                "--config",
                                str(config),
                                "--profile",
                                "debug" if sig == signal.SIGINT else "release",
                                "serve",
                                "--port",
                                str(port),
                            ],
                            capture_output=True,
                            timeout=10,
                        )
                        check(collision.returncode == 4, "Port collision did not fail clearly")
                        if sig == signal.SIGINT:
                            # A second start validates reuse after orderly shutdown.
                            native = transport.post(
                                "/api/v1/infer",
                                files={
                                    "metadata": (
                                        None,
                                        json.dumps(
                                            {
                                                "model": "verify/mobilenet",
                                                "capability": "classification",
                                                "input": {"image": "media:image"},
                                            }
                                        ),
                                    ),
                                    "image": ("image.jpg", image.read_bytes(), "image/jpeg"),
                                },
                            )
                            check(native.status_code == 200, "Second service native request failed")
                            evidence["restart"] = "passed"
                        else:
                            oa = openai.OpenAI(
                                api_key="local-unused",
                                base_url=base + "/v1",
                                max_retries=0,
                                http_client=httpx.Client(trust_env=False),
                            )
                            an = anthropic.Anthropic(
                                api_key="local-unused",
                                base_url=base,
                                max_retries=0,
                                http_client=httpx.Client(trust_env=False),
                            )
                            try:
                                embedding = oa.embeddings.create(
                                    model="verify/minilm",
                                    input=["a red cat", "a blue car"],
                                    encoding_format="float",
                                )
                                check(
                                    len(embedding.data) == 2
                                    and len(embedding.data[0].embedding) == 384,
                                    "Embedding SDK shape mismatch",
                                )
                                check(embedding.usage is None, "Fabricated embedding usage")
                                messages = [{"role": "user", "content": "Once upon a time"}]
                                complete = oa.chat.completions.create(
                                    model="verify/stories",
                                    messages=messages,
                                    max_tokens=16,
                                    temperature=0,
                                )
                                check(complete.choices[0].message.content, "Empty chat SDK result")
                                check(complete.usage is None, "Fabricated chat usage")
                                chunks = list(
                                    oa.chat.completions.create(
                                        model="verify/stories",
                                        messages=messages,
                                        max_tokens=16,
                                        stream=True,
                                    )
                                )
                                check(
                                    sum(c.choices[0].finish_reason is not None for c in chunks)
                                    == 1,
                                    "OpenAI stream terminal mismatch",
                                )
                                answer = an.messages.create(
                                    model="verify/stories", messages=messages, max_tokens=16
                                )
                                check(answer.content[0].text, "Empty Anthropic SDK result")
                                check(
                                    answer.usage.input_tokens is None
                                    and answer.usage.output_tokens is None,
                                    "Fabricated Anthropic usage",
                                )
                                with an.messages.stream(
                                    model="verify/stories", messages=messages, max_tokens=16
                                ) as stream:
                                    streamed_text = "".join(stream.text_stream)
                                    final = stream.get_final_message()
                                check(
                                    streamed_text == final.content[0].text
                                    and final.stop_reason == "max_tokens",
                                    "Anthropic SDK stream accumulation failed",
                                )
                                native = transport.post(
                                    "/api/v1/infer",
                                    files={
                                        "metadata": (
                                            None,
                                            json.dumps(
                                                {
                                                    "model": "verify/mobilenet",
                                                    "capability": "classification",
                                                    "input": {"image": "media:image"},
                                                }
                                            ),
                                        ),
                                        "image": ("image.jpg", image.read_bytes(), "image/jpeg"),
                                    },
                                )
                                check(
                                    native.status_code == 200, "Classification HTTP request failed"
                                )
                                check(
                                    native.json()["capability_result"]["labels"][0]["index"] == 812,
                                    "Public classification top-1 mismatch",
                                )
                                # CLI must obey the live service lease and host lock.
                                busy = subprocess.run(
                                    [
                                        str(cli),
                                        "--config",
                                        str(config),
                                        "run",
                                        "-m",
                                        "verify/stories",
                                        "--input",
                                        "public test",
                                        "--json",
                                    ],
                                    capture_output=True,
                                    timeout=15,
                                )
                                check(busy.returncode == 4, "CLI bypassed host execution lock")
                                evidence["http"] = {
                                    "classification_top1": 812,
                                    "embedding_dimension": 384,
                                    "openai_chat": "passed",
                                    "openai_sse": "passed",
                                    "anthropic_message": "passed",
                                    "anthropic_sdk_stream_helper": "passed",
                                    "cli_device_lock": "passed",
                                    "unknown_usage": "null_or_omitted",
                                }
                            finally:
                                oa.close()
                                an.close()
                        with transport.stream(
                            "POST",
                            "/api/v1/infer",
                            json={
                                "model": "verify/stories",
                                "capability": "chat",
                                "input": {
                                    "messages": [{"role": "user", "content": "Once upon a time"}]
                                },
                                "parameters": {"max_tokens": 512},
                                "stream": True,
                            },
                        ) as result:
                            check(result.status_code == 200, "In-flight signal request failed")
                            events = []
                            sent = False
                            for line in result.iter_lines():
                                if not line.startswith("data: "):
                                    continue
                                event = json.loads(line[6:])
                                events.append(event)
                                if (
                                    event["type"] == "output"
                                    and event.get("kind") == "delta"
                                    and not sent
                                ):
                                    check(
                                        transport.get("/health").json()["busy"],
                                        "Signal test missed actual in-flight execution",
                                    )
                                    process.send_signal(sig)
                                    sent = True
                            check(sent, "Signal was not sent after output")
                            terminal = [
                                e
                                for e in events
                                if e["type"] in {"completed", "failed", "cancelled"}
                            ]
                            check(
                                len(terminal) == 1 and terminal[0]["type"] == "cancelled",
                                "Signal did not produce one cancellation terminal",
                            )
                            evidence[sig.name + "_inflight"] = "cancelled_after_output_no_replay"
                        code = process.wait(timeout=20)
                        # Uvicorn re-raises the received signal after orderly shutdown.
                        check(
                            code in (0, -int(sig), 130 if sig == signal.SIGINT else 0),
                            f"Unexpected service shutdown status: {code}",
                        )
                        evidence[sig.name] = {"exit_code": code, "orderly_shutdown": "passed"}
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
            check("Traceback" not in logs.read_text(), "Service emitted an unexpected traceback")
            check("Once upon a time" not in logs.read_text(), "Input leaked into service logs")
        # A new CLI execution proves stopped service releases the host lock and installation lease.
        result = subprocess.run(
            [
                str(cli),
                "--config",
                str(config),
                "run",
                "-m",
                "verify/mobilenet",
                "--input",
                json.dumps({"image": str(image)}),
                "--json",
            ],
            capture_output=True,
            timeout=20,
        )
        check(result.returncode == 0, "CLI did not regain device admission after shutdown")
        evidence["cli_after_shutdown"] = "passed"
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = run(args.cli.resolve(), args.data_dir.resolve(), args.image.resolve())
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
