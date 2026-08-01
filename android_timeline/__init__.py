"""Android Timeline -- Termux collector.

An offline-first, append-only event collector that runs inside Termux on
Android and uploads authenticated batches to the ``android-timeline``
Home Assistant app.

The runtime has **zero third-party dependencies** on purpose: Termux users
should never need a compiler toolchain to install this package.
"""

from __future__ import annotations

__all__ = [
    "COLLECTOR_VERSION",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "USER_AGENT",
    "__version__",
]

__version__ = "0.1.0"

#: Version of this collector, reported in heartbeats and upload headers.
COLLECTOR_VERSION = __version__

#: Wire protocol version shared with ``android-timeline-home-assistant``.
#: Bumped only for breaking changes to the batch/acknowledgement envelope.
PROTOCOL_VERSION = 1

#: Version of the event envelope itself. Stored on every event so that
#: historical raw events remain interpretable after the schema evolves.
SCHEMA_VERSION = 1

USER_AGENT = f"android-timeline-termux/{__version__}"
