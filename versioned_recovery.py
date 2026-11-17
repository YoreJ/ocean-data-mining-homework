"""History-based query checks for independently versioned station and data services."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Query:
    source: str
    station_id: str
    date: str = "2026-01-15"
    variable: str = "temperature"


@dataclass(frozen=True)
class Context:
    """Trusted environment tokens for the proposed query, None if unknown."""

    namespace: str | None
    registry_revision: str | None
    data_snapshot: str | None


@dataclass(frozen=True)
class Record:
    record_id: str
    query: Query
    context: Context
    code: str


@dataclass(frozen=True)
class Rejection:
    record_id: str
    code: str


def rejection_for(query: Query, context: Context, history: Iterable[Record]) -> Rejection | None:
    """Return the first covering negative fact, or allow the tool to run.

    The caller obtains and pins these tokens before checking the candidate.
    Namespaces and revisions identify immutable registry contents; a snapshot is
    immutable within its source. Keep original records even as tokens change.
    """
    if not context.namespace or not context.registry_revision:
        return None
    for record in history:
        prior = record.context
        if (prior.namespace, prior.registry_revision) != (context.namespace, context.registry_revision):
            continue
        if record.code == "INVALID_ID" and record.query.station_id == query.station_id:
            return Rejection(record.record_id, record.code)
        if (record.code == "NO_RECORD" and record.query == query
                and context.data_snapshot and prior.data_snapshot == context.data_snapshot):
            return Rejection(record.record_id, record.code)
    return None
