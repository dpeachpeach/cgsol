"""The receiver's three defences: signature, delivery id, sender."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

from orchestrator.webhooks import (
    Debouncer,
    DeliveryDedup,
    classify_sender,
    is_bot_sender,
    is_self_sender,
    verify_signature,
)

SECRET = "shhh"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_accepts_only_the_real_thing() -> None:
    body = b'{"action":"labeled"}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(body, "wrong"))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=deadbeef")
    assert not verify_signature(SECRET, b'{"action":"opened"}', sign(body))


def test_unset_secret_is_an_unsecured_local_run() -> None:
    assert verify_signature("", b"anything", None)


def test_delivery_is_processed_once() -> None:
    dedup = DeliveryDedup(ttl=60)
    assert dedup.seen("d-1") is False
    assert dedup.seen("d-1") is True
    assert dedup.seen("d-2") is False


def test_expired_delivery_is_allowed_again() -> None:
    dedup = DeliveryDedup(ttl=0)
    assert dedup.seen("d-1") is False
    assert dedup.seen("d-1") is False


async def test_burst_collapses_into_one_batch() -> None:
    """20 seeded issues are 20 webhooks in three seconds and one triage batch."""
    batches: list[set[int]] = []

    async def flush(batch: set[int]) -> None:
        batches.append(batch)

    debouncer = Debouncer(0.05, flush)
    for number in range(1, 21):
        await debouncer.add(number)
    await asyncio.sleep(0.15)

    assert batches == [set(range(1, 21))]


async def test_flush_now_does_not_fire_on_an_empty_window() -> None:
    calls: list[set[int]] = []

    async def flush(batch: set[int]) -> None:
        calls.append(batch)

    await Debouncer(10, flush).flush_now()
    assert calls == []


def test_bot_senders_are_filtered() -> None:
    """Without this the state machine feeds its own label writes back to itself."""
    logins = ["devin-ai-integration[bot]"]
    assert is_bot_sender({"sender": {"login": "devin-ai-integration[bot]"}}, logins)
    assert is_bot_sender({"sender": {"login": "github-actions[bot]"}}, logins)
    assert not is_bot_sender({"sender": {"login": "dpeachpeach"}}, logins)
    assert not is_bot_sender({}, logins)


def test_the_app_is_a_third_identity_not_just_another_bot() -> None:
    """As a PAT the orchestrator was indistinguishable from the human who minted
    it; as an App it authors events as `<slug>[bot]`. Both of those are wrong
    answers on their own — our writes are not human intent, but they are also
    not inert, because `make seed` files the backlog under our own identity."""
    logins = ["devin-ai-integration[bot]"]
    slug_login = "cgsol-orchestrator[bot]"

    def classify(login: str) -> str:
        return classify_sender({"sender": {"login": login}}, logins, slug_login)

    assert classify(slug_login) == "self"
    assert classify("devin-ai-integration[bot]") == "bot"
    assert classify("github-actions[bot]") == "bot"
    assert classify("dpeachpeach") == "human"


def test_without_an_app_slug_nothing_is_self() -> None:
    """PAT mode cannot tell its own writes apart, and must behave as it did."""
    assert classify_sender({"sender": {"login": "dpeachpeach"}}, [], "") == "human"
    assert not is_self_sender({"sender": {"login": "dpeachpeach"}}, "")
