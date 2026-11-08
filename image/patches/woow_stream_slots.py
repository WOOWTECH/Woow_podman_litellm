"""Woow M4 API-09: a streamed request gives its max_parallel_requests slot back.

Installed as ``litellm/proxy/woow_stream_slots.py`` by ``apply_stream_slot_release.py`` and imported
lazily by the two edits that installer makes in ``proxy/common_request_processing.py``. Standard
library only at module level (lean tests load this file by path); LiteLLM and anyio, both present in
any running proxy, are imported where they are used.

With an SSE keepalive interval (``litellm_settings.sse_keepalive_ping_interval_seconds``, or every
deployment of a model group setting ``keepalive_seconds``), ``open_sse_before_first_byte`` runs the
request, pre-call hooks included, in a task of its own. ``parallel_request_limiter_v3`` keeps the
request's max_parallel_requests slot (with its TPM reservation and rate-limit headers) on a
ContextVar stash that its pre-call hook creates lazily, so inside that task. The request task that
then serves the StreamingResponse never sees it: neither the success callback at the end of the
stream nor the disconnect cleanup releases the slot, which stays taken until the limiter's TTL
(``PARALLEL_REQUEST_SLOT_TTL_SECONDS``, 3600 s).
"""
import asyncio
from collections.abc import Awaitable
import logging
from typing import Final, TypeVar

_LOG: Final = logging.getLogger("LiteLLM Proxy")
_T = TypeVar("_T")


def ensure_request_stash() -> None:
    """Create the rate limiter's per-request stash in the calling (request) task, before it forks.

    The task ``open_sse_before_first_byte`` starts copies the caller's context, so the pre-call hook
    running there fills this same mutable object, and every task later forked from the request task
    (the stream, its success callback, the disconnect cleanup) finds the slot. That is the stash's
    documented design: "a single mutable instance shared by every context forked from the request
    task". An existing stash is kept as it is.
    """
    from litellm.proxy.hooks.parallel_request_limiter_v3 import get_or_create_request_stash

    get_or_create_request_stash()


async def release_slot_if_cancelled(
    proxy_logging_obj: object, user_api_key_dict: object, produce: Awaitable[_T]
) -> _T:
    """Await the request; when it is cancelled, release its slot and reconcile its budget reservation.

    The keepalive relay cancels the request when the client leaves during a time-to-first-token wait
    longer than the interval (after a ``: ping``), while the upstream call is still pending. A
    CancelledError runs no success or failure callback, so nothing else would release them. These
    are the two releases the streaming generator runs on a client disconnect; both are idempotent
    (the stash's acquisition is cleared once released, a finalized reservation is not reconciled
    again). The cancellation itself is re-raised unchanged; any other outcome passes through.
    """
    try:
        return await produce
    except asyncio.CancelledError:
        import anyio  # a proxy dependency; imported here so this module stays standard library only

        with anyio.CancelScope(shield=True):
            await _release_cancelled_request(proxy_logging_obj, user_api_key_dict)
        raise


async def _release_cancelled_request(proxy_logging_obj: object, user_api_key_dict: object) -> None:
    try:
        await proxy_logging_obj._arelease_max_parallel_requests_on_disconnect(user_api_key_dict)
    except Exception as exc:  # noqa: BLE001 - teardown must not replace the cancellation
        _LOG.warning("Woow API-09: max_parallel_requests slot of a cancelled request not released: %s", exc)
    try:
        from litellm.proxy.spend_tracking.budget_reservation import release_budget_reservation_on_cancel

        await release_budget_reservation_on_cancel(getattr(user_api_key_dict, "budget_reservation", None))
    except Exception as exc:  # noqa: BLE001 - teardown must not replace the cancellation
        _LOG.warning("Woow API-09: budget reservation of a cancelled request not reconciled: %s", exc)
