"""Public provider protocols, single registry resolver, no ambient credentials."""

import json
import logging
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlencode

import httpx

from .config import Config, Registry
from .errors import RkaiError
from .manifest import COMMIT, MAX_MANIFEST_BYTES, HubSource, parse, relative_path

log = logging.getLogger("rkai.source")
HTTP_TIMEOUT_SECONDS = 30
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_SOURCE_ERROR_BYTES = 16 * 1024


@dataclass(frozen=True)
class Reference:
    registry: str
    repository: str
    selector: str


def resolve_reference(ref: str, registry: str | None, config: Config) -> Reference:
    explicit = None
    if ref.startswith(("hf://", "ms://")):
        explicit, ref = ref.split("://", 1)
    elif ":" in ref:
        explicit, ref = ref.split(":", 1)
    if registry and explicit and registry != explicit:
        raise RkaiError("source_conflict", "REF and --registry identify different sources", 2)
    selected = explicit or registry or config.default_registry
    if selected not in config.registries:
        raise RkaiError("unknown_registry", "Registry is not configured", 2)
    # hf/ms URI names always denote the public sources; forbid rebinding builtins.
    if explicit in ("hf", "ms"):
        builtins = Config().registries
        configured = config.registries[explicit]
        if (configured.kind, configured.origin) != (
            builtins[explicit].kind,
            builtins[explicit].origin,
        ):
            raise RkaiError("source_conflict", "Public URI source cannot be rebound", 2)
    repo, sep, selector = ref.rpartition("@")
    if not sep:
        repo = ref
        selector = "master" if config.registries[selected].kind == "modelscope" else "main"
    try:
        selector = unquote(selector)
        repo = unquote(repo)
        relative_path(repo)
        if not selector or any(c in selector for c in "\x00\\?#"):
            raise ValueError("invalid selector")
    except ValueError as e:
        raise RkaiError("invalid_reference", "Invalid model reference", 2) from e
    return Reference(selected, repo, selector)


def client() -> httpx.Client:
    return httpx.Client(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"Accept-Encoding": "identity", "User-Agent": "rkai/0.1"},
    )


def read_url(http: httpx.Client, url: str, limit: int = MAX_METADATA_BYTES) -> bytes:
    try:
        with http.stream("GET", url) as response:
            if response.is_error:
                details = {"http_status": response.status_code, "source_host": response.url.host}
                # Retain only a numeric business code, never arbitrary upstream text,
                # cookies, query parameters or credentials. A 403 alone is not rate-limit proof.
                raw = bytearray()
                try:
                    for chunk in response.iter_bytes(4096):
                        raw.extend(chunk)
                        if len(raw) > MAX_SOURCE_ERROR_BYTES:
                            break
                except httpx.HTTPError:
                    raw.clear()  # The HTTP status remains evidence even if its body is interrupted.
                if len(raw) <= MAX_SOURCE_ERROR_BYTES:
                    try:
                        body = json.loads(raw)
                    except (ValueError, RecursionError):
                        body = None
                    if isinstance(body, dict) and type(body.get("Code")) is int:
                        details["upstream_code"] = body["Code"]
                code = "not_found" if response.status_code == 404 else "source_http_error"
                message = f"Public source returned HTTP {response.status_code}"
                if "upstream_code" in details:
                    message += f" (upstream code {details['upstream_code']})"
                raise RkaiError(code, message, details=details)
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes(64 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise RkaiError("metadata_too_large", "Source metadata exceeds limit", 2)
            return bytes(data)
    except httpx.HTTPStatusError as e:
        raise RkaiError(
            "source_http_error", f"Public source returned HTTP {e.response.status_code}"
        ) from e
    except httpx.HTTPError as e:
        raise RkaiError("source_unreachable", "Public source request failed") from e


def file_url(kind: str, endpoint: str, repository: str, revision: str, path: str) -> str:
    relative_path(path)
    repo = quote(repository, safe="/")
    if kind == "huggingface":
        return f"{endpoint}/{repo}/resolve/{quote(revision, safe='')}/{quote(path, safe='/')}"
    if kind == "modelscope":
        return f"{endpoint}/api/v1/models/{repo}/repo?" + urlencode(
            {"Revision": revision, "FilePath": path}
        )
    return f"{endpoint}/{quote(path, safe='/')}"


class Provider:
    def __init__(self, http: httpx.Client, settings: Registry):
        self.http = http
        self.settings = settings

    def _metadata(self, url: str) -> dict:
        try:
            info = json.loads(read_url(self.http, url))
        except (ValueError, RecursionError) as e:
            raise RkaiError("invalid_source_metadata", "Provider returned invalid JSON") from e
        if not isinstance(info, dict):
            raise RkaiError("invalid_source_metadata", "Provider metadata must be an object")
        return info

    def fixed_file(self, repo: str, selector: str, path: str) -> tuple[str, list[str]]:
        """Resolve a file at a mutable ref; MS exposes per-file commits."""
        last = None
        for endpoint in [*self.settings.mirrors, self.settings.origin]:
            try:
                if self.settings.kind == "huggingface":
                    url = f"{endpoint}/api/models/{quote(repo, safe='/')}/revision/" + quote(
                        selector, safe=""
                    )
                    info = self._metadata(url)
                    revision = info.get("sha", "")
                elif self.settings.kind == "modelscope":
                    url = f"{endpoint}/api/v1/models/{quote(repo, safe='/')}/repo/files?" + (
                        urlencode(
                            {
                                "Revision": selector,
                                "Recursive": "false",
                                "Root": path.rpartition("/")[0],
                            }
                        )
                    )
                    info = self._metadata(url)
                    if info.get("Success") is False:
                        raise RkaiError("source_http_error", "ModelScope refused metadata request")
                    data = info.get("Data")
                    if info.get("Success") is not True or not isinstance(data, dict):
                        raise RkaiError(
                            "invalid_source_metadata", "Invalid ModelScope response object"
                        )
                    entries = data.get("Files")
                    if not isinstance(entries, list) or any(
                        not isinstance(e, dict) or not isinstance(e.get("Path"), str)
                        for e in entries
                    ):
                        raise RkaiError(
                            "invalid_source_metadata", "Invalid ModelScope file listing"
                        )
                    entry = next((e for e in entries if e["Path"] == path), None)
                    if entry is None:
                        raise RkaiError("not_found", "Requested source file does not exist")
                    revision = entry.get("Revision", "")
                else:
                    # Static source: the manifest digest becomes the snapshot identity.
                    prefix = f"{repo}/{selector}"
                    return prefix, [
                        file_url("https", e, repo, "", prefix + "/" + path)
                        for e in [*self.settings.mirrors, self.settings.origin]
                    ]
                if not isinstance(revision, str):
                    raise RkaiError("invalid_source_metadata", "Provider revision must be text")
                if not COMMIT.fullmatch(revision):
                    raise RkaiError("unresolved_revision", "Provider did not return a fixed commit")
                return revision, [
                    file_url(self.settings.kind, e, repo, revision, path)
                    for e in [*self.settings.mirrors, self.settings.origin]
                ]
            except (RkaiError, ValueError, KeyError, TypeError) as e:
                last = e
                log.warning("metadata endpoint failed (%s)", type(e).__name__)
        if isinstance(last, RkaiError):
            raise last
        raise RkaiError("invalid_source_metadata", "Provider returned invalid metadata") from last

    def manifest(self, ref: Reference) -> tuple[bytes, dict]:
        try:
            revision, urls = self.fixed_file(ref.repository, ref.selector, "manifest.json")
        except RkaiError as e:
            if e.code == "not_found":
                raise RkaiError(
                    "missing_manifest", "Source has no rkai manifest; conversion requires M2"
                ) from e
            raise
        last = None
        for url in urls:
            try:
                raw = read_url(self.http, url, MAX_MANIFEST_BYTES)
                parse(raw)
                return raw, {
                    "registry": ref.registry,
                    "kind": self.settings.kind,
                    "origin": self.settings.origin,
                    "repository": ref.repository,
                    "selector": ref.selector,
                    "revision": revision,
                }
            except RkaiError as e:
                last = e
        if last and last.code == "not_found":
            raise RkaiError(
                "missing_manifest", "Source has no rkai manifest; conversion requires M2"
            )
        raise last or RkaiError("source_unreachable", "Manifest unavailable")


def artifact_urls(source, path: str, config: Config, registry_name: str | None = None) -> list[str]:
    registries = list(config.registries.values())
    if registry_name in config.registries:
        registries.insert(0, config.registries[registry_name])
    if isinstance(source, HubSource):
        origin, kind = source.origin, source.kind
        mirrors = next(
            (r.mirrors for r in registries if r.kind == kind and r.origin == origin),
            [],
        )
        return [
            file_url(kind, e, source.repository, source.revision, path) for e in [*mirrors, origin]
        ]
    origin = source.base_url
    mirrors = next(
        (r.mirrors for r in registries if r.kind == "https" and r.origin == origin),
        [],
    )
    return [file_url("https", e, "", "", path) for e in [*mirrors, origin]]
