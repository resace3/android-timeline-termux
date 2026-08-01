"""Outbound batch upload.

The phone is always the client: it opens an HTTPS connection to the Home
Assistant app and never listens on a socket. Retries are idempotent because
every event carries a stable ``event_id`` and every batch a stable
``batch_id`` -- replaying either is a no-op server-side.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from . import COLLECTOR_VERSION, PROTOCOL_VERSION, USER_AGENT
from .config import Config
from .database import Outbox
from .models import Acknowledgement, Batch, Event, iso_utc, utc_now
from .redaction import redact_headers

__all__ = ["UploadError", "UploadResult", "Uploader"]

logger = logging.getLogger(__name__)

_INGEST_PATH = "/api/v1/events/batch"
_HEALTH_PATH = "/api/v1/health"

#: Status codes worth retrying. 4xx (other than 408/429) mean the request
#: itself is wrong, so retrying would just burn battery.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class UploadError(Exception):
    """An upload attempt failed."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(slots=True)
class UploadResult:
    batches_sent: int = 0
    events_accepted: int = 0
    events_rejected: int = 0
    events_outstanding: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batches_sent": self.batches_sent,
            "events_accepted": self.events_accepted,
            "events_rejected": self.events_rejected,
            "events_outstanding": self.events_outstanding,
            "errors": list(self.errors),
        }


class Uploader:
    """Uploads pending events from the outbox to the Home Assistant app."""

    def __init__(
        self,
        config: Config,
        outbox: Outbox,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.outbox = outbox
        self._sleep = sleep
        # Jitter only. Nothing security-relevant is derived from this stream.
        self._rng = rng or random.Random()  # noqa: S311 - retry jitter, not crypto

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext | None:
        """TLS context. Verification is on unless test mode disabled it.

        ``config.load_config`` already refuses ``verify_tls = false`` outside
        an explicitly-flagged test environment, so reaching the unverified
        branch in production is not possible via configuration alone.
        """
        if urlparse(self.config.server.base_url).scheme != "https":
            return None
        if self.config.server.verify_tls:
            return ssl.create_default_context()
        logger.warning("TLS certificate verification is DISABLED -- test mode only")
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _headers(self, *, batch_id: str, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "X-Device-ID": self.config.device.device_id,
            "X-Batch-ID": batch_id,
            "X-Protocol-Version": str(PROTOCOL_VERSION),
            "X-Collector-Version": COLLECTOR_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _request(
        self,
        path: str,
        *,
        method: str = "POST",
        body: bytes | None = None,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, Any]]:
        url = urljoin(self.config.server.base_url.rstrip("/") + "/", path.lstrip("/"))
        # The scheme is constrained to http(s) by config validation, and
        # plaintext http is additionally gated behind test mode, so `file:`
        # and friends cannot reach this call.
        request = urllib.request.Request(url, data=body, method=method)  # noqa: S310
        for key, value in headers.items():
            request.add_header(key, value)

        # Log the redacted header set only -- the Authorization value must
        # never reach a log file, a CI artifact or a bug report.
        logger.debug("%s %s headers=%s", method, _safe_url(url), redact_headers(headers))

        try:
            # The scheme is constrained to http(s) by config validation, and
            # plaintext http is additionally gated behind test mode.
            with urllib.request.urlopen(  # noqa: S310 - scheme validated in config  # noqa: S310 - scheme validated in config
                request,
                timeout=self.config.server.timeout_seconds,
                context=self._ssl_context(),
            ) as response:
                raw = response.read()
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = int(exc.code)
            payload = _decode_json(raw)
            retryable = status in _RETRYABLE_STATUS
            detail = str(payload.get("detail") or payload.get("error") or "")[:200]
            raise UploadError(
                f"server returned HTTP {status}: {detail}".strip(),
                retryable=retryable,
                status=status,
            ) from exc
        except urllib.error.URLError as exc:
            raise UploadError(f"network error: {exc.reason}", retryable=True) from exc
        except (TimeoutError, ssl.SSLError, OSError) as exc:
            raise UploadError(f"transport error: {exc}", retryable=True) from exc

        return status, _decode_json(raw)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def check_health(self) -> dict[str, Any]:
        """GET the server health endpoint. Sends no credentials."""
        _, payload = self._request(
            _HEALTH_PATH,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        return payload

    def send_batch(self, batch: Batch, token: str) -> Acknowledgement:
        """POST one batch and parse the acknowledgement."""
        body = json.dumps(batch.to_dict(), separators=(",", ":")).encode("utf-8")
        headers = self._headers(batch_id=batch.batch_id, token=token)

        if (
            self.config.server.compression
            and len(body) >= self.config.server.compression_min_bytes
        ):
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"

        if len(body) > self.config.server.max_batch_bytes:
            raise UploadError(
                f"batch body {len(body)} bytes exceeds the configured limit "
                f"of {self.config.server.max_batch_bytes}",
                retryable=False,
            )

        _, payload = self._request(_INGEST_PATH, body=body, headers=headers)
        try:
            return Acknowledgement.from_dict(payload)
        except ValueError as exc:
            raise UploadError(
                f"malformed acknowledgement: {exc}", retryable=False
            ) from exc

    def upload_pending(self, *, max_batches: int = 10) -> UploadResult:
        """Drain the outbox, one batch at a time, with backoff on failure.

        Stops at the first non-retryable error so a bad token or a schema
        rejection cannot spin. The local queue is preserved either way.
        """
        result = UploadResult()
        token = self.config.server.token()

        for _ in range(max_batches):
            claim = self.outbox.claim_batch(
                self.config.device.device_id,
                max_events=self.config.server.max_batch_events,
                max_bytes=self.config.server.max_batch_bytes,
            )
            if claim is None:
                break
            batch_id, events = claim
            if not events:
                self.outbox.release_batch(batch_id)
                break

            batch = Batch(
                batch_id=batch_id,
                device_id=self.config.device.device_id,
                events=events,
                created_time_utc=iso_utc(utc_now()),
                collector_version=COLLECTOR_VERSION,
            )

            try:
                ack = self._send_with_retries(batch, token)
            except UploadError as exc:
                # Queue is intact: events go back to pending for next time.
                self.outbox.record_batch_attempt(batch_id, str(exc))
                self.outbox.release_batch(batch_id)
                result.errors.append(str(exc))
                break

            outcome = self.outbox.complete_batch(
                batch_id,
                accepted_ids=sorted(ack.stored_ids),
                rejected=ack.rejected,
                dead_letter_after_attempts=self.config.upload.dead_letter_after_attempts,
            )
            result.batches_sent += 1
            result.events_accepted += outcome["accepted"]
            result.events_rejected += outcome["rejected"]
            result.events_outstanding += outcome["outstanding"]

            if len(events) < self.config.server.max_batch_events:
                break

        return result

    # ------------------------------------------------------------------
    # retry policy
    # ------------------------------------------------------------------

    def _send_with_retries(self, batch: Batch, token: str) -> Acknowledgement:
        attempts = self.config.upload.max_attempts
        last: UploadError | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self.send_batch(batch, token)
            except UploadError as exc:
                last = exc
                self.outbox.record_batch_attempt(batch.batch_id, str(exc))
                if not exc.retryable or attempt == attempts:
                    raise
                delay = self.backoff_delay(attempt)
                logger.warning(
                    "upload attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                self._sleep(delay)

        raise last or UploadError("upload failed", retryable=True)  # pragma: no cover

    def backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with symmetric jitter, capped by config.

        Jitter matters on a phone: without it every queued device retries in
        lockstep after a network outage ends.
        """
        base = self.config.upload.initial_backoff_seconds * (2 ** (attempt - 1))
        capped = min(base, self.config.upload.max_backoff_seconds)
        ratio = self.config.upload.jitter_ratio
        if ratio <= 0:
            return capped
        spread = capped * ratio
        return max(0.0, capped + self._rng.uniform(-spread, spread))


def _decode_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_url(url: str) -> str:
    """Strip any query string before logging -- tokens never belong in URLs."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def events_from_dicts(raw: list[dict[str, Any]]) -> list[Event]:
    """Helper for tests and the ``export`` command."""
    return [Event.from_dict(item) for item in raw]
