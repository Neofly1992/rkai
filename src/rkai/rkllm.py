"""RKLLM callback execution through a header-compiled bridge, isolated in worker."""

import codecs
import ctypes as c
import queue
import threading

from .errors import RkaiError

CALL_NORMAL, CALL_WAITING, CALL_FINISH, CALL_ERROR = 0, 1, 2, 3
CALLBACK_QUEUE_EVENTS = 64


class RkllmBackend:
    def __init__(self, path, config, runtime, adapter=None):
        self.context = c.c_void_p()
        self.config = config
        self.lib = c.CDLL(str(runtime.parent / "rkllm_bridge.so"))
        self.callback_type = c.CFUNCTYPE(c.c_int, c.c_char_p, c.c_int, c.c_void_p)
        self.lib.rkai_llm_open.argtypes = [c.c_char_p, c.c_int, c.POINTER(c.c_void_p)]
        self.lib.rkai_llm_run.argtypes = [
            c.c_void_p,
            c.c_char_p,
            c.c_int,
            c.c_float,
            self.callback_type,
            c.c_void_p,
        ]
        self.lib.rkai_llm_abort.argtypes = [c.c_void_p]
        self.lib.rkai_llm_close.argtypes = [c.c_void_p]
        for name in ("open", "run", "abort", "close"):
            getattr(self.lib, "rkai_llm_" + name).restype = c.c_int
        code = self.lib.rkai_llm_open(
            str(path).encode(), config.context_length, c.byref(self.context)
        )
        if code:
            raise RkaiError("native_error", f"RKLLM initialization returned {code}", 3)
        self.evidence = {
            "runtime": "1.3.1",
            "provider": "rockchip-npu",
            "header_sha256": "4ad19442de58df1b7d1ed0884ed75b8d4b5107cc4e75b7e44900b1f95a32c57c",
        }

    def generate(self, prompt, request, cancel):
        events = queue.Queue(maxsize=CALLBACK_QUEUE_EVENTS)
        error = []
        decoder = codecs.getincrementaldecoder("utf-8")()

        @self.callback_type
        def callback(raw, state, userdata):
            try:
                if cancel.is_set():
                    return 1
                text = decoder.decode(raw or b"", final=state == CALL_FINISH)
                events.put((state, text), timeout=10)
                return 0
            except BaseException:
                error.append("callback_error")
                cancel.set()
                return 1

        def execute():
            try:
                code = self.lib.rkai_llm_run(
                    self.context,
                    prompt.encode(),
                    request["max_tokens"],
                    request["temperature"],
                    callback,
                    None,
                )
                if code:
                    error.append("native_error")
            finally:
                finished.set()

        finished = threading.Event()
        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        text = ""
        saw_finish = False
        try:
            while not finished.is_set() or not events.empty():
                if cancel.is_set():
                    self.lib.rkai_llm_abort(self.context)
                try:
                    state, delta = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if state == CALL_ERROR:
                    error.append("native_error")
                if delta and not cancel.is_set():
                    text += delta
                    yield "delta", {"text": delta}
                if state == CALL_FINISH:
                    saw_finish = True
            thread.join()
            if error or not saw_finish:
                if not cancel.is_set():
                    raise RkaiError(
                        "native_error", "RKLLM callback/run did not complete successfully"
                    )
                return
            # SDK does not expose an unambiguous stop-vs-length enum.
            yield "result", {"text": text, "finish_reason": "unknown"}
        finally:
            if thread.is_alive():
                self.lib.rkai_llm_abort(self.context)
                thread.join(timeout=5)
                if thread.is_alive():
                    # Parent deadline terminates this worker; never destroy a live context.
                    import os

                    os._exit(71)

    def close(self):
        if self.context.value:
            code = self.lib.rkai_llm_close(self.context)
            self.context = c.c_void_p()
            if code:
                raise RkaiError("native_error", f"RKLLM destroy returned {code}")
