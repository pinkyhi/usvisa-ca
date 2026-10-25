"""Pure phase allocation, independent of the broker and wall-clock timers."""

from datetime import datetime, timedelta
from uuid import UUID

from usvisa.protocol.messages import INTERVAL_SECONDS, utc


def assign_phases(node_ids: set[UUID], previous: dict[UUID, datetime], *,
                  now: datetime) -> dict[UUID, datetime]:
    """Keep surviving cyclic order and one anchor; append new IDs deterministically.

    Anchors may be in the past. Integer microseconds avoid accumulated rounding
    drift when 180 seconds is not divisible by the membership count.
    """
    if not node_ids:
        return {}
    survivors = sorted(node_ids & previous.keys(), key=lambda node: (previous[node], str(node)))
    newcomers = sorted(node_ids - previous.keys(), key=str)
    order = survivors + newcomers
    anchor = utc(previous[survivors[0]]) if survivors else utc(now)
    period_us = INTERVAL_SECONDS * 1_000_000
    return {node: anchor + timedelta(microseconds=period_us * index // len(order))
            for index, node in enumerate(order)}
