"""Reading parallel lookups that may not have finished.

The harness runs slow KG lookups in parallel within a time budget, then
shuts the pool down with cancel_futures=True so a slow KG never holds a step
up. A lookup still queued at that moment is cancelled, and a cancelled
future counts as done(): calling result() on it raises CancelledError. That
crashed "Finalise the quantity" on the live KG (more lines than workers).
Every read of such a lookup goes through ready().
"""

from __future__ import annotations

from concurrent.futures import Future


def ready(fut: Future | None, default=None):
    """The lookup's result when it finished on its own, else `default`: not
    started, still running, cancelled, or failed with an exception."""
    if fut is None or not fut.done() or fut.cancelled():
        return default
    if fut.exception() is not None:
        return default
    return fut.result()


def finished(fut: Future | None) -> bool:
    """True when the lookup finished on its own (not cancelled, no exception)."""
    return fut is not None and fut.done() and not fut.cancelled() and fut.exception() is None
