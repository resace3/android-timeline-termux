"""SMS **metadata** via ``termux-sms-list``.

Disabled by default. Message bodies are never stored unless
``privacy.store_message_bodies`` is explicitly enabled; by default only a
length and a direction survive.
"""

from __future__ import annotations

from typing import Any

from ..models import QualityFlag
from ..redaction import pseudonymize
from .base import Collector, CollectorError, Observation

__all__ = ["SmsCollector"]


class SmsCollector(Collector):
    source = "sms"
    required_commands = ("termux-sms-list",)

    def observe(self) -> list[Observation]:
        if not self.config.privacy.has_salt():
            raise CollectorError(
                "SMS collection requires a pseudonymisation salt; refusing to "
                "store weakly-hashed phone numbers"
            )
        salt = self.config.privacy.salt()

        options = self.settings.options if self.settings else {}
        limit = max(1, min(int(options.get("limit", 50)), 500))

        result = self.runner.run("termux-sms-list", ["-l", str(limit)])
        if not result.ok:
            raise CollectorError(
                f"termux-sms-list exited {result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            )
        entries = result.json()
        if not isinstance(entries, list):
            raise CollectorError("expected a JSON array from termux-sms-list")

        store_bodies = self.config.privacy.store_message_bodies
        observations: list[Observation] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            body = entry.get("body")
            body_text = str(body) if body is not None else ""

            payload: dict[str, Any] = {
                "direction": _direction(str(entry.get("type", ""))),
                "read": bool(entry.get("read", False)),
                "body_length": len(body_text),
                "counterparty_pseudonym": pseudonymize(entry.get("number"), salt),
                "thread_pseudonym": pseudonymize(
                    str(entry.get("threadid", "")) or None, salt
                ),
            }
            if store_bodies:
                payload["body"] = body_text
            if self.config.privacy.store_contact_names:
                sender = entry.get("sender")
                payload["contact_name"] = str(sender) if sender else None

            flags = [QualityFlag.REDACTED] if not store_bodies else []
            observations.append(
                Observation(
                    event_type="sms_record",
                    payload=payload,
                    quality_flags=flags,
                    dedupe_key=str(entry.get("_id", "")) or None,
                )
            )
        return observations


def _direction(value: str) -> str:
    normalised = value.strip().lower()
    if normalised in ("inbox", "received", "incoming"):
        return "incoming"
    if normalised in ("sent", "outbox", "outgoing"):
        return "outgoing"
    if normalised in ("draft", "failed", "queued"):
        return normalised
    return "unknown"
