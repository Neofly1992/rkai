"""Drain native output continuously; retain only allowlisted technical categories.

SDK output can contain paths, URLs or prompts. Raw lines never leave this bounded
in-memory reader, including in debug mode. Categories are fixed application text.
"""

import logging
import re
import threading

MAX_LINE_BYTES = 4096
DRAIN_CHUNK_BYTES = 4096
DRAIN_JOIN_SECONDS = 1.0
KNOWN_MODULES = {
    "torch",
    "numpy",
    "rknn",
    "rkllm",
    "onnx",
    "onnxruntime",
    "PIL",
    "tokenizers",
    "setuptools",
    "scikit_build_core",
    "cmake",
    "llama_cpp",
}
PATTERNS = {
    "cuda_toolchain_required": r"CUDA_HOME|CUDA toolkit|Building cuda extension requires",
    "missing_file_or_header": r"No such file or directory|fatal error:.*file not found",
    "cmake_configuration_failed": r"CMake Error",
    "dependency_resolution_failed": r"No solution found|ResolutionImpossible|Failed to resolve",
    "source_download_failed": r"Failed to download|Failed to fetch|ConnectionError|ConnectError",
    "tls_certificate_failed": r"CERTIFICATE_VERIFY_FAILED|certificate verify failed",
    "disk_full": r"No space left on device",
    "unsupported_operator": r"unsupported.*operator|operator.*not supported|op.*not support",
    "compiler_failed": r"compilation terminated|ld:.*error|undefined reference|fatal error:",
    "python_import_failed": r"ModuleNotFoundError|ImportError|No module named",
}


def categories(raw):
    text = raw.decode("utf-8", errors="replace")
    found = {name for name, pattern in PATTERNS.items() if re.search(pattern, text, re.I)}
    for module in KNOWN_MODULES:
        if re.search(r"No module named ['\"]" + re.escape(module) + r"['\"]", text):
            found.add("missing_module:" + module)
    return found


class NativeDiagnostics:
    def __init__(self, streams):
        self.lock = threading.Lock()
        self.tags = set()
        self.threads = []
        for stream in streams:
            thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _drain(self, stream):
        import os

        pending = b""
        try:
            while chunk := os.read(stream.fileno(), DRAIN_CHUNK_BYTES):
                pending = (pending + chunk)[-MAX_LINE_BYTES:]
                with self.lock:
                    self.tags.update(categories(pending))
                # Keep only an unfinished line; never retain an unbounded traceback.
                if b"\n" in pending:
                    pending = pending.rsplit(b"\n", 1)[1]
        finally:
            stream.close()

    def snapshot(self):
        with self.lock:
            return sorted(self.tags)

    def finish(self):
        for thread in self.threads:
            thread.join(timeout=DRAIN_JOIN_SECONDS)
        tags = self.snapshot()
        if tags:
            logging.getLogger("rkai.native").debug("diagnostics=%s", tags)
        return tags


def exception_details(error):
    # Exception messages are not safe; an import name is reported only from this whitelist.
    details = {"diagnostics": sorted(categories(type(error).__name__.encode()))}
    if isinstance(error, ImportError):
        details["diagnostics"] = ["python_import_failed"]
        if error.name in KNOWN_MODULES:
            details["diagnostics"].append("missing_module:" + error.name)
    return details
