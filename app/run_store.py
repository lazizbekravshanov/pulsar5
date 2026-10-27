"""Where a run lives between requests.

A run is planned on one instance and revised, decided and read on whichever
instance gets the next request. `RUNS` in the orchestrator is a cache; this is
the truth behind it. Every change to a run writes its record here, and a miss
in the cache reads it back.

Two backends behind one interface:

- `FileRunStore` keeps one JSON file per run under `PULSAR5_LOGS_DIR/runs`.
  Local servers and the tests use it.
- `VercelBlobRunStore` keeps the same JSON in a private Vercel Blob store and
  is chosen whenever `BLOB_READ_WRITE_TOKEN` is set, which on the hosted demo
  it is. It speaks the same REST calls as `@vercel/blob` (the token names the
  store, writes go to the blob API with `x-vercel-blob-access: private`, reads
  come from the store's private hostname), so nothing about the store is
  specific to this client. The token is read from the environment, never from
  code, and never appears in a record, a trace, a log or an error.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import LOGS_DIR, Settings, load_settings

TOKEN_ENV = "BLOB_READ_WRITE_TOKEN"
BLOB_API_URL = "https://vercel.com/api/blob"
BLOB_API_VERSION = "13"
BLOB_PREFIX = "pulsar5/runs"


class RunStoreError(RuntimeError):
    """The store could not be reached, or refused the request."""


class RunStore(Protocol):
    name: str

    def save(self, run_id: str, record: dict[str, Any]) -> None: ...

    def load(self, run_id: str) -> dict[str, Any] | None: ...

    def describe(self) -> dict[str, str]: ...


def safe_name(run_id: str) -> str:
    """A run id as a file or blob name: no separators, nothing to escape with."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)


# --------------------------------------------------------------------- file


class FileRunStore:
    name = "file"

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()

    def _path(self, run_id: str) -> Path:
        return self.directory / f"run-{safe_name(run_id)}.json"

    def save(self, run_id: str, record: dict[str, Any]) -> None:
        path = self._path(run_id)
        tmp = path.with_suffix(".json.tmp")
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(record), encoding="utf-8")
            tmp.replace(path)

    def load(self, run_id: str) -> dict[str, Any] | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunStoreError(f"run {run_id!r} could not be read from {path}: {exc}") from exc

    def describe(self) -> dict[str, str]:
        return {"backend": self.name, "location": str(self.directory)}


# --------------------------------------------------------------------- blob


class VercelBlobRunStore:
    name = "vercel_blob"

    def __init__(self, token: str, *, client: httpx.Client | None = None, timeout: float = 10.0) -> None:
        # A read-write token is `vercel_blob_rw_<store id>_<secret>`; the store
        # id is what the private read hostname is built from.
        parts = token.split("_")
        store_id = parts[3] if token.startswith("vercel_blob_rw_") and len(parts) > 4 else ""
        if not store_id:
            raise ValueError(f"{TOKEN_ENV} is set but does not look like a Vercel Blob read-write token")
        self._token = token
        self._store_id = store_id
        self._client = client or httpx.Client(timeout=timeout)

    @staticmethod
    def _pathname(run_id: str) -> str:
        return f"{BLOB_PREFIX}/{safe_name(run_id)}.json"

    def save(self, run_id: str, record: dict[str, Any]) -> None:
        headers = {
            "authorization": f"Bearer {self._token}",
            "x-api-version": BLOB_API_VERSION,
            "x-vercel-blob-store-id": self._store_id,
            "x-vercel-blob-access": "private",
            "x-content-type": "application/json",
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1",
        }
        try:
            response = self._client.put(
                f"{BLOB_API_URL}/",
                params={"pathname": self._pathname(run_id)},
                content=json.dumps(record).encode("utf-8"),
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise RunStoreError(f"Vercel Blob could not be reached to save run {run_id!r}: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise RunStoreError(f"Vercel Blob refused to save run {run_id!r}: {self._reason(response)}")

    def load(self, run_id: str) -> dict[str, Any] | None:
        # The store's own hostname serves private blobs to the bearer of the
        # token; `cache=0` so a read sees the overwrite the last request made.
        url = f"https://{self._store_id}.private.blob.vercel-storage.com/{self._pathname(run_id)}"
        try:
            response = self._client.get(
                url, params={"cache": "0"}, headers={"authorization": f"Bearer {self._token}"}
            )
        except httpx.HTTPError as exc:
            raise RunStoreError(f"Vercel Blob could not be reached to load run {run_id!r}: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RunStoreError(f"Vercel Blob refused to load run {run_id!r}: {self._reason(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise RunStoreError(f"run {run_id!r} in Vercel Blob is not JSON") from exc

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        reason = f"HTTP {response.status_code}"
        try:
            error = response.json().get("error") or {}
        except ValueError:
            return reason
        if error.get("code"):
            reason += f" {error['code']}"
        if error.get("message"):
            reason += f": {error['message']}"
        return reason

    def describe(self) -> dict[str, str]:
        return {"backend": self.name, "location": f"{BLOB_PREFIX}/ (private)"}


# ---------------------------------------------------------------- selection


def open_run_store(settings: Settings | None = None) -> RunStore:
    """The blob store when its token is in the environment, else files."""
    settings = settings or load_settings()
    if settings.blob_read_write_token:
        return VercelBlobRunStore(settings.blob_read_write_token)
    return FileRunStore(LOGS_DIR / "runs")


__all__ = [
    "TOKEN_ENV",
    "FileRunStore",
    "RunStore",
    "RunStoreError",
    "VercelBlobRunStore",
    "open_run_store",
    "safe_name",
]
