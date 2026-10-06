"""Local FastAPI transport. The service owner, not ASGI tasks, owns native work."""

import asyncio
import socket
import sqlite3
import time
from contextlib import asynccontextmanager
from importlib.metadata import version

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from .errors import RkaiError, database_error
from .http_input import READ_CHUNK_BYTES, Multipart, json_object
from .http_protocol import Stream, error_body, parse_request, response, sse, status
from .service import (
    HTTP_OUTPUT_BYTES,
    INPUT_MEMORY_FACTOR,
    POLL_SECONDS,
    Job,
    Service,
    installed_models,
)

__version__ = version("rkai")
LISTEN_BACKLOG = 128  # Kernel connection queue, distinct from inference admission slots.
ASGI_SHUTDOWN_SECONDS = 10  # Native cleanup is joined separately by lifespan, never detached.


async def uncancellable(coro):
    """Transport cancellation cannot detach cleanup from its actual resource owner."""
    task = asyncio.ensure_future(coro)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def owned_io(function, *args):
    """A cancelled await still joins its file operation before cleanup touches it."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await uncancellable(task)
        raise


class Delivery:
    """Own the reservation through sending and join cleanup on every exit path."""

    def __init__(self, deadline, drain_seconds, cleanup):
        self.deadline, self.drain_seconds, self.cleanup = deadline, drain_seconds, cleanup
        self.failed = False

    def remaining(self):
        # A failure terminal may be sent after the execution deadline only once
        # teardown is proven; its diagnostic write still has a bounded drain time.
        return (
            self.drain_seconds
            if self.failed
            else min(self.drain_seconds, max(0, self.deadline - time.monotonic()))
        )

    async def run(self, operation, receive, send, *, iterator=None):
        async def bounded_send(message):
            left = self.remaining()
            if left <= 0:
                raise TimeoutError
            await asyncio.wait_for(send(message), left)

        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                pass

        writer = asyncio.create_task(operation(bounded_send))
        disconnect = asyncio.create_task(disconnected())
        try:
            # JSON has no iterator waiting for native cleanup; bound its entire
            # response operation, including delays before the first send.
            timeout = None if iterator is not None else self.remaining()
            await asyncio.wait(
                (writer, disconnect), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if writer.done():
                try:
                    writer.result()
                except (OSError, TimeoutError):
                    pass
        finally:
            for task in (writer, disconnect):
                if not task.done():
                    task.cancel()
            await uncancellable(asyncio.gather(writer, disconnect, return_exceptions=True))
            try:
                if iterator is not None:
                    await uncancellable(iterator.aclose())
            finally:
                await uncancellable(self.cleanup())


class OwnedJSONResponse(JSONResponse):
    def __init__(self, content, delivery, **kwargs):
        super().__init__(content, **kwargs)
        self.delivery = delivery

    async def __call__(self, scope, receive, send):
        async def write(bounded_send):
            await super(OwnedJSONResponse, self).__call__(scope, receive, bounded_send)

        await self.delivery.run(write, receive, send)


class OwnedStreamingResponse(StreamingResponse):
    """Monitor disconnect for every ASGI version, even before iterator startup."""

    def __init__(self, iterator, delivery, **kwargs):
        super().__init__(iterator, **kwargs)
        self.delivery = delivery

    async def __call__(self, scope, receive, send):
        await self.delivery.run(self.stream_response, receive, send, iterator=self.body_iterator)


def create_app(config):
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(config)
        try:
            await app.state.service.start()
            yield
        finally:
            await uncancellable(app.state.service.close())

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RkaiError)
    async def application_error(request, error):
        protocol = "anthropic" if request.url.path == "/v1/messages" else "openai"
        return JSONResponse(error_body(protocol, error), status_code=status(error))

    @app.exception_handler(ClientDisconnect)
    async def disconnected(request, error):
        # The peer is gone; suppress an expected disconnect traceback after joined cleanup.
        return await application_error(request, RkaiError("cancelled", "Client disconnected", 130))

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, error):
        return await application_error(request, database_error(error))

    @app.exception_handler(OSError)
    async def file_error(request, error):
        return await application_error(
            request, RkaiError("io_failed", "Managed file operation failed")
        )

    @app.get("/health")
    async def health():
        service = app.state.service
        alive = service.thread.is_alive()
        return {
            "status": "healthy" if alive else "failed",
            "version": __version__,
            "ready": alive and service.admission.accepting,
            "busy": service.active is not None,
            "requests": service.admission.requests,
            "model": service.model_state,
        }

    @app.get("/v1/models")
    async def models():
        service = app.state.service
        if not service.catalog_guard.acquire(blocking=False):
            raise RkaiError("busy", "Model catalog verification is already running", 4)
        try:
            rows = await owned_io(installed_models, config)
        finally:
            service.catalog_guard.release()
        loaded = service.model_state
        return {
            "object": "list",
            "data": [
                {
                    "id": row["id"],
                    "object": "model",
                    "created": None,
                    "owned_by": "local",
                    "capabilities": row["capabilities"],
                    "file_state": row["file_state"],
                    "execution_validation": row["execution_validation"],
                    "loaded": loaded.get("model") == row["id"] and loaded["state"] == "ready",
                }
                for row in rows
            ],
        }

    async def input_data(request, protocol, reservation, deadline, owned):
        content_type = request.headers.get("content-type", "").lower()
        multipart = content_type.startswith("multipart/form-data")
        if multipart and protocol != "native":
            raise RkaiError("unsupported_parameter", "Multipart requires native infer", 2)
        if not multipart and content_type.split(";")[0] != "application/json":
            raise RkaiError("invalid_input", "Expected application/json or native multipart", 2)
        limits = config.http.limits
        budget = limits.multipart_bytes if multipart else limits.json_bytes
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) < 0:
                    raise ValueError
                if int(length) > budget:
                    raise RkaiError("input_too_large", "HTTP body exceeds configured budget", 2)
            except ValueError as error:
                raise RkaiError("invalid_input", "Invalid Content-Length", 2) from error
        if multipart:
            # Original boundary is case-sensitive.
            def construct():
                parser = Multipart(
                    request.headers["content-type"], limits, reservation, config.data_dir
                )
                owned.append(parser)
                return parser

            parser = await owned_io(construct)
        raw = bytearray()
        count = 0
        iterator = request.stream().__aiter__()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RkaiError("request_timeout", "Request deadline exceeded", 1)
            try:
                chunk = await asyncio.wait_for(anext(iterator), left)
            except StopAsyncIteration:
                break
            if not app.state.service.admission.accepting:
                raise RkaiError("service_closing", "Service is shutting down", 4)
            count += len(chunk)
            if count > budget:
                raise RkaiError("input_too_large", "HTTP body exceeds configured budget", 2)
            if multipart:
                for offset in range(0, len(chunk), READ_CHUNK_BYTES):
                    await owned_io(parser.parser.write, chunk[offset : offset + READ_CHUNK_BYTES])
            else:
                reservation.add(memory=len(chunk) * INPUT_MEMORY_FACTOR)
                raw.extend(chunk)
        if multipart:
            return await owned_io(parser.finish)
        return await owned_io(json_object, raw), None

    async def wait_event(request, job):
        # Exactly one task reads ASGI disconnect after the request body is consumed.
        # StreamingResponse takes over that responsibility once headers are sent.
        task = asyncio.create_task(job.next())
        try:
            while not task.done():
                if await request.is_disconnected():
                    job.cancel()
                    raise ClientDisconnect()
                if app.state.service.stopping.is_set():
                    job.cancel()
                await asyncio.sleep(POLL_SECONDS)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def handle(request, protocol):
        service = app.state.service
        reservation = service.admission.acquire()
        job = None
        owned = []
        transferred = False
        deadline = time.monotonic() + config.http.request_seconds

        async def cleanup():
            if job:
                await job.reap()
            for parser in owned:
                await asyncio.to_thread(parser.close)
            reservation.close()

        delivery = Delivery(deadline, config.http.drain_seconds, cleanup)

        try:
            # Bounded output and parsed response copies count even while queued.
            reservation.add(memory=HTTP_OUTPUT_BYTES * INPUT_MEMORY_FACTOR)
            data, media = await input_data(request, protocol, reservation, deadline, owned)
            model, capability, payload, stream = parse_request(protocol, data, media)
            job = Job(model, capability, payload, config.http, deadline)
            service.submit(job)
            first = await wait_event(request, job)
            if first is None or first["type"] != "accepted":
                raise RkaiError("execution_failed", "Execution did not accept request")
            if stream:
                mapper = Stream(protocol, model, first)

                async def events():
                    try:
                        event = first
                        while event is not None:
                            if event["type"] in {"failed", "cancelled"}:
                                await job.wait_stopped()
                                delivery.failed = True
                            for frame in mapper.map(event):
                                yield frame
                            if event["type"] in {"completed", "failed", "cancelled"}:
                                break
                            event = await job.next()
                    except RkaiError as error:
                        await job.reap()
                        delivery.failed = True
                        if protocol == "native":
                            yield sse(
                                {
                                    "type": "cancelled" if error.exit_code == 130 else "failed",
                                    "request_id": first["request_id"],
                                    "sequence": mapper.sequence + 1,
                                    "error": error_body(protocol, error)["error"],
                                },
                                "cancelled" if error.exit_code == 130 else "failed",
                            )
                        else:
                            yield sse(
                                error_body(protocol, error),
                                "error" if protocol == "anthropic" else None,
                            )

                transferred = True
                return OwnedStreamingResponse(
                    events(),
                    delivery,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
                )
            result = terminal = None
            while (event := await wait_event(request, job)) is not None:
                if event["type"] == "output" and event["kind"] == "result":
                    result = event["payload"]
                if event["type"] in {"completed", "failed", "cancelled"}:
                    terminal = event
                    break
            if terminal is None or terminal["type"] != "completed":
                err = (terminal or {}).get("error") or {
                    "code": "cancelled",
                    "message": "Request cancelled",
                    "exit_code": 130,
                }
                raise RkaiError(err["code"], err["message"], err["exit_code"])
            result_response = OwnedJSONResponse(response(protocol, model, first, result), delivery)
            transferred = True
            return result_response
        except TimeoutError as error:
            raise RkaiError("request_timeout", "Request deadline exceeded", 1) from error
        except (ValueError, KeyError) as error:
            raise RkaiError("invalid_input", "Invalid HTTP input", 2) from error
        finally:
            if not transferred:
                await uncancellable(cleanup())

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await handle(request, "openai")

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        return await handle(request, "embeddings")

    @app.post("/v1/messages")
    async def messages(request: Request):
        return await handle(request, "anthropic")

    @app.post("/api/v1/infer")
    async def infer(request: Request):
        return await handle(request, "native")

    return app


class Server(uvicorn.Server):
    def handle_exit(self, sig, frame):
        # Stop application admission and native work before Uvicorn waits for HTTP tasks.
        if hasattr(self.config.app.state, "service"):
            self.config.app.state.service.stop()
        super().handle_exit(sig, frame)


def serve(config):
    app = create_app(config)
    settings = config.http
    family = socket.AF_INET6 if settings.host == "::1" else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((settings.host, settings.port))
            listener.listen(LISTEN_BACKLOG)
            listener.setblocking(False)
        except OSError as error:
            raise RkaiError("listen_failed", "Cannot bind configured loopback port", 4) from error
        server = Server(
            uvicorn.Config(
                app,
                host=settings.host,
                port=settings.port,
                workers=1,
                access_log=False,
                log_config=None,
                timeout_graceful_shutdown=ASGI_SHUTDOWN_SECONDS,
            )
        )
        server.run(sockets=[listener])
        if not server.started:
            raise RkaiError("service_start_failed", "Service startup failed", 3)
        return 0
