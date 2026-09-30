"""Shared calendar arithmetic: free gaps between busy intervals."""

from datetime import datetime, timedelta


def free_slots(
    busy: list[tuple[datetime, datetime]], start: datetime, end: datetime, minutes: int
) -> list[tuple[datetime, datetime]]:
    """Gaps of at least `minutes` inside [start, end] not covered by any busy interval."""
    shortest = timedelta(minutes=minutes)
    slots, cursor = [], start
    for busy_start, busy_end in sorted(busy):
        if busy_start - cursor >= shortest:
            slots.append((cursor, min(busy_start, end)))
        cursor = max(cursor, busy_end)
        if cursor >= end:
            break
    if end - cursor >= shortest:
        slots.append((cursor, end))
    return [(slot_start, slot_end) for slot_start, slot_end in slots if slot_end > slot_start]
