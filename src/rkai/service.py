"""Fixed-thread application owner around the existing execution authority.

SQLite and infer generators never cross threads. Only Session.cancel (its locked
control API) crosses into the owner. Transport buffers contain copied DTOs.
"""

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from collections import deque

from .errors import RkaiError
from .execution import Session
from .ipc import LOAD_TIMEOUT_SECONDS, QUEUE_BYTES, QUEUE_EVENTS
from .storage import Library

POLL_SECONDS = 0.01
# Conservative accounting for decoded strings, dicts and JSON temporary copies.
INPUT_MEMORY_FACTOR = 8
HTTP_OUTPUT_BYTES = 1024 * 1024


class Admission:
    def __init__(self, config):
        self.config = config
        self.guard = threading.Lock()
        self.requests = self.memory = self.disk = 0
        self.accepting = True

    def acquire(self):
        with self.guard:
            if not self.accepting:
                raise RkaiError("service_closing", "Service is shutting down", 4)
            if self.requests >= self.config.queue_capacity + 1:
                raise RkaiError("busy", "Request queue is full", 4)
            self.requests += 1
        return Reservation(self)

    def change(self, memory=0, disk=0):
        with self.guard:
            if (
                self.memory + memory > self.config.memory_bytes
                or self.disk + disk > self.config.staging_bytes
            ):
                raise RkaiError("resource_admission", "HTTP buffering budget exceeded", 4)
            self.memory += memory
            self.disk += disk

    def stop(self):
        with self.guard:
            self.accepting = False


class Reservation:
    def __init__(self, admission):
        self.admission = admission
        self.memory = self.disk = 0

    def add(self, memory=0, disk=0):
        self.admission.change(memory, disk)
        self.memory += memory
        self.disk += disk

    def close(self):
        self.admission.change(-self.memory, -self.disk)
        self.memory = self.disk = 0
        with self.admission.guard:
            self.admission.requests -= 1


class Job:
    def __init__(self, model, capability, data, config, deadline):
        self.id = uuid.uuid4().hex
        self.model, self.capability, self.data = model, capability, data
        self.config = config
        self.deadline = deadline
        self.cancelled = threading.Event()
        self.cancel_sent = threading.Event()
        self.cancel_error = None
        self.done = threading.Event()
        self.condition = threading.Condition()
        self.events = deque()
        self.bytes = 0
        self.error = None
        self.load_error = None
        self.session = None
        self.request_id = None
        self.cancel_pending = None

    def timeout(self):
        self.cancel(RkaiError("request_timeout", "Request deadline exceeded", 1))

    def cancellation(self):
        return self.cancel_error or RkaiError("cancelled", "Request cancelled", 130)

    def cancel(self, error=None):
        self.cancelled.set()
        with self.condition:
            if error is not None and self.cancel_error is None:
                self.cancel_error = error
            self.condition.notify_all()
            session = self.session
            request_id = self.request_id
            send_cancel = (
                session is not None and request_id is not None and not self.cancel_sent.is_set()
            )
            if send_cancel:
                self.cancel_sent.set()
        if self.cancel_pending is not None:
            self.cancel_pending(self)
        if send_cancel:
            # Locked native control may wait for process teardown; never on the ASGI loop.
            threading.Thread(
                target=session.cancel, args=(request_id,), name="rkai-cancel", daemon=True
            ).start()

    def put(self, event):
        if event["type"] == "accepted":
            with self.condition:
                self.request_id = event["request_id"]
        size = len(json.dumps(event, ensure_ascii=False).encode())
        until = min(self.deadline, time.monotonic() + self.config.drain_seconds)
        with self.condition:
            while len(self.events) >= QUEUE_EVENTS or self.bytes + size > HTTP_OUTPUT_BYTES:
                if self.cancelled.is_set():
                    raise self.cancellation()
                left = until - time.monotonic()
                if left <= 0:
                    raise RkaiError("backpressure", "HTTP consumer did not drain output", 4)
                self.condition.wait(min(left, POLL_SECONDS))
            if self.cancelled.is_set():
                raise self.cancellation()
            self.events.append((event, size))
            self.bytes += size
            self.condition.notify_all()

    async def wait_stopped(self):
        # Completion includes generator cleanup and actual native teardown on failure.
        while not self.done.is_set():
            await asyncio.sleep(POLL_SECONDS)

    async def next(self):
        while True:
            if time.monotonic() >= self.deadline:
                self.timeout()
                await self.wait_stopped()
                raise RkaiError("request_timeout", "Request deadline exceeded", 1)
            with self.condition:
                if self.events:
                    event, size = self.events.popleft()
                    self.bytes -= size
                    self.condition.notify_all()
                    return event
                if self.done.is_set():
                    if self.error:
                        raise self.error
                    return None
            await asyncio.sleep(POLL_SECONDS)

    async def reap(self):
        self.cancel()
        await self.wait_stopped()
        with self.condition:
            self.events.clear()
            self.bytes = 0
        self.data = None


class Service:
    def __init__(self, config):
        self.config = config
        self.admission = Admission(config.http)
        self.jobs = queue.Queue(maxsize=config.http.queue_capacity + 1)
        self.catalog_guard = threading.Lock()
        self.submission_guard = threading.Lock()
        self.stopping = threading.Event()
        self.started = threading.Event()
        self.start_error = None
        self.session = None
        self.active = None
        self.model_state = {"state": "unloaded", "model": None}
        self.thread = threading.Thread(target=self._own, name="rkai-execution", daemon=False)
        self.thread.start()

    async def start(self):
        while not self.started.is_set():
            await asyncio.sleep(POLL_SECONDS)
        if self.start_error:
            raise self.start_error

    def submit(self, job):
        with self.submission_guard:
            if self.stopping.is_set():
                error = RkaiError("service_closing", "Service is shutting down", 4)
                job.error = error
                job.done.set()
                raise error
            job.cancel_pending = self._cancel_pending
            try:
                self.jobs.put_nowait(job)
            except queue.Full as cause:
                error = RkaiError("busy", "Request queue is full", 4)
                job.error = error
                job.done.set()
                raise error from cause

    def _cancel_pending(self, job):
        # Queue removal and actor dequeue share the same mutex. Once dequeued, only
        # the actor may mark completion; a queued job owns no Session/native work.
        with self.jobs.mutex:
            try:
                self.jobs.queue.remove(job)
            except ValueError:
                return
            self.jobs.unfinished_tasks -= 1
            self.jobs.not_full.notify()
            job.error = job.cancellation()
            job.done.set()

    def stop(self):
        self.admission.stop()
        with self.submission_guard:
            self.stopping.set()
        with self.jobs.mutex:
            pending = list(self.jobs.queue)
        for job in pending:
            job.cancel()
        if self.active:
            self.active.cancel()

    async def close(self):
        self.stop()
        while self.thread.is_alive():
            await asyncio.sleep(POLL_SECONDS)
        self.thread.join()

    def _load(self, library, model, capability, job=None):
        # Resolve each turn so an updated alias cannot silently serve the old snapshot.
        with library.lease(model) as identity:
            effective = (
                self.session.capability if self.session and capability is None else capability
            )
            key = (model, effective, identity)
        if self.session and (self.session.closed or self.key != key):
            self.session.close()
            self.session = None
        if self.session is None:
            self.model_state = {"state": "loading", "model": model}
            try:
                self.session = Session(
                    library,
                    model,
                    capability,
                    load_cancel=job.cancelled if job else self.stopping,
                    load_timeout=max(0.001, job.deadline - time.monotonic())
                    if job
                    else LOAD_TIMEOUT_SECONDS,
                )
                self.key = (model, self.session.capability, identity)
            except BaseException as error:
                self.model_state = {"state": "failed", "model": model}
                request_ended = job is not None and (
                    job.cancelled.is_set() or time.monotonic() >= job.deadline
                )
                if isinstance(error, RkaiError) and not request_ended:
                    # Share model failures, never an individual client's cancellation.
                    with self.jobs.mutex:
                        for pending in self.jobs.queue:
                            if pending.model == model and pending.capability == capability:
                                pending.load_error = error
                raise
        self.model_state = {
            "state": "ready",
            "model": model,
            "plan": self.session.plan.id,
            "device": self.session.plan.target.accelerator,
        }
        return self.session

    def _retire(self):
        while self.session is not None and not self.session.closed:
            try:
                self.session.close()
            except RkaiError as error:
                if error.code != "cleanup_unverified":
                    raise
                self.admission.stop()
                self.stopping.set()
                self.model_state = {
                    "state": "cleanup_failed",
                    "model": self.key[0] if hasattr(self, "key") else None,
                }
                logging.getLogger("rkai.service").error("cleanup=unverified lease=retained")
                # Keep the owner and request reservation until the actual group stops.
                time.sleep(1)

    def _execute(self, library, job):
        stream = None
        timer = threading.Timer(max(0, job.deadline - time.monotonic()), job.timeout)
        timer.daemon = True
        timer.start()
        try:
            if self.stopping.is_set() or job.cancelled.is_set():
                raise job.cancellation()
            if job.load_error is not None:
                raise job.load_error
            session = self._load(library, job.model, job.capability, job)
            job.session = session
            if job.cancelled.is_set():
                session.close()
                raise job.cancellation()
            stream = session.infer(job.data)
            for event in stream:
                job.put(event)
            # Generator must reach its finally before allowing the next request.
        except BaseException as error:
            if getattr(error, "session", None) is not None:
                self.session = error.session
            self._retire()
            if job.cancel_error is not None:
                error = job.cancel_error
            job.error = (
                error
                if isinstance(error, RkaiError)
                else RkaiError("execution_failed", "Execution failed; resources stopped")
            )
        finally:
            timer.cancel()
            if stream is not None:
                stream.close()
            if self.session and self.session.closed:
                self.session = None
                self.model_state = {"state": "unloaded", "model": None}
            job.session = None
            job.done.set()

    def _own(self):
        try:
            with Library(self.config) as library:
                from .http_input import recover_uploads

                recover_uploads(library.root)
                # Worst-case transport reservation also counts against model load budget.
                library.execution_overhead_bytes = self.config.http.memory_bytes + QUEUE_BYTES * 2
                if self.config.http.preload:
                    self._load(library, self.config.http.preload, None)
                self.started.set()
                while not self.stopping.is_set() or not self.jobs.empty():
                    try:
                        job = self.jobs.get(timeout=POLL_SECONDS)
                    except queue.Empty:
                        continue
                    self.active = job
                    try:
                        self._execute(library, job)
                    finally:
                        self.active = None
                        self.jobs.task_done()
                if self.session:
                    self._retire()
                    self.session = None
        except BaseException as error:
            if getattr(error, "session", None) is not None:
                self.session = error.session
            if self.session is not None:
                self._retire()
            self.start_error = (
                error
                if isinstance(error, RkaiError)
                else RkaiError("service_start_failed", "Service application owner failed")
            )
            self.stop()
            self.started.set()
            while not self.jobs.empty():
                job = self.jobs.get_nowait()
                job.error = self.start_error
                job.done.set()


def installed_models(config):
    # A short, separately owned connection: not the actor's SQLite object.
    with Library(config) as library:
        return library.models()
