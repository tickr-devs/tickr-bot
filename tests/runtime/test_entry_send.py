"""Entry send in status-v1: the first link of the protection chain, read from the journal."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal as D

from tickr.api import contract as api
from tickr.api.history import entry_send
from tickr.engine.core import SendState
from tickr.runtime import entries, service

from .test_protection import Rig


def _at(rig: Rig, kind: str, cid: str) -> datetime:
    return datetime.fromisoformat(rig.store.journal(kind, cid)[0].at_utc)


def _status(rig: Rig) -> api.StatusV1:
    return api.loads(api.StatusV1, (rig.tmp_path / "status.json").read_bytes())


def test_an_entry_is_sent_once_with_its_journal_times(tmp_path):
    """Fake-broker loop: the strategy enters on pass 1, the entry fills on pass 3; status
    shows the entry leg's intent, attempt and first fill read, and exactly one send."""
    rig = Rig(tmp_path)
    rig.actions[3] = lambda: rig.broker.fill(rig.entry(), D(2), D(10))
    rig.run(6)
    cid = rig.entry()
    reads = rig.store.journal("order-observed", cid)
    assert reads[0].body["filled_quantity"] == "0"  # the working read comes first
    filled = next(datetime.fromisoformat(r.at_utc) for r in reads
                  if D(r.body["filled_quantity"]) > 0)
    (position,) = _status(rig).positions
    assert position.entry_cause is None
    assert position.entry == api.EntrySend(
        cid, _at(rig, "order-intent", cid), _at(rig, "order-attempted", cid),
        api.SendOutcome.ACKNOWLEDGED, api.SendOutcome.ACKNOWLEDGED, 1, filled)
    assert position.entry.intent_at <= position.entry.sent_at < position.entry.filled_at
    assert cid.startswith("ap-") and cid.endswith("-t1-e1")  # the bot's own id, nothing else

    # Closed: the same values on today's trade, cached from then on.
    (stop,) = [o for o in rig.broker.open_orders() if o.order_type == "STOP_LOSS"]
    rig.broker.fill(stop.client_order_id, D(2), D("9.80"))  # stopped out
    rig.run(3)
    (trade,) = _status(rig).trades_today
    assert (trade.entry, trade.entry_cause) == (position.entry, None)
    assert "t1" in rig.service._entry
    # The daily-loss path never reads it.
    (plain,) = service.trades_today(rig.engine, rig.now)
    assert plain.entry_cause == "not read on this path" and plain.entry.sends is None


def test_an_unknown_send_outcome_shows_unknown_even_once_resolved(tmp_path):
    rig = Rig(tmp_path)
    rig.broker.script("unknown")  # accepted, reply lost: resolved by the next broker read
    rig.actions[4] = lambda: rig.broker.fill(rig.entry(), D(2), D(10))
    rig.run(8)
    cid = rig.entry()
    assert rig.engine.order(cid).send_state is SendState.ACKNOWLEDGED  # resolved by a read
    (position,) = _status(rig).positions
    assert position.entry.send_outcome is api.SendOutcome.UNKNOWN
    assert position.entry.latest_outcome is api.SendOutcome.ACKNOWLEDGED  # confirmed by a read
    assert position.entry.sends == 1 and position.entry.filled_at is not None
    assert position.entry_cause is None


def test_a_second_attempt_row_is_shown_never_clamped(tmp_path):
    rig = Rig(tmp_path)
    rig.actions[2] = lambda: rig.broker.fill(rig.entry(), D(2), D(10))
    rig.run(3)
    cid = rig.entry()
    with rig.store.transaction():  # not something the engine writes
        rig.store.append("order-attempted", {}, key=cid)
    entry, cause = entry_send(rig.store, "t1")
    assert entry.sends == 2 and cause is None
    assert entry.send_outcome is api.SendOutcome.ACKNOWLEDGED  # the first attempt's outcome
    assert entry.latest_outcome is api.SendOutcome.ATTEMPTED  # the last row


def test_null_fields_carry_their_cause(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.recover()
    rig.engine._send = lambda cid: None  # type: ignore[method-assign] # crash before attempt
    trade_id = rig.engine.open_trade("ABC", D(1), D(10), D("9.80"), D(1))
    rig.store.close()
    rig.open()
    rig.engine.recover()  # never attempted: absent
    entry, cause = entry_send(rig.store, trade_id)
    assert (entry.client_order_id, entry.sent_at, entry.sends, entry.filled_at) == (
        rig.entry(), None, 0, None)
    assert entry.send_outcome is api.SendOutcome.ABSENT and entry.intent_at is not None
    assert entry.latest_outcome is api.SendOutcome.ABSENT
    assert cause == ("never attempted: no order-attempted row; "
                     "no order-observed row shows the entry filled")
    assert entry_send(rig.store, "t404") == (
        api.EntrySend(None, None, None, None, None, None, None),
        "trade t404 has 0 entry orders, expected 1")


def test_a_refused_entry_shows_refused(tmp_path):
    rig = Rig(tmp_path)
    rig.broker.script("refuse")
    rig.run(2)
    entry, cause = entry_send(rig.store, "t1")
    assert entry.send_outcome is api.SendOutcome.REFUSED and entry.sends == 1
    assert entry.latest_outcome is api.SendOutcome.REFUSED
    assert cause == "no order-observed row shows the entry filled"


def test_a_malformed_row_shows_null_journals_once_and_the_loop_goes_on(tmp_path):
    rig = Rig(tmp_path)
    rig.actions[2] = lambda: rig.broker.fill(rig.entry(), D(2), D(10))
    rig.run(3)
    cid = rig.entry()
    with rig.store.transaction():  # not something the engine writes: no filled_quantity
        rig.store.append("order-observed", {"state": "FILLED"}, key=cid)
    reserved = _status(rig).open_reservations_usd
    passes = rig.passes
    rig.run(3)
    assert rig.passes == passes + 3  # the loop went on
    status = _status(rig)
    (position,) = status.positions
    assert position.entry == api.EntrySend(None, None, None, None, None, None, None)
    assert position.entry_cause.startswith("journal unreadable: MalformedJournal: ")
    assert position.stop.state is api.StopState.VERIFIED  # protection untouched
    assert status.open_reservations_usd == reserved
    (failed,) = rig.store.journal(service.ENTRY_SEND_FAILED)  # once, not once per pass
    assert failed.key == "t1" and failed.body["error"] == "MalformedJournal"
    rig.store.close()
    rig.open()  # a restart journals it no second time
    rig.evaluator.enter = False
    rig.run(2)
    assert len(rig.store.journal(service.ENTRY_SEND_FAILED)) == 1
    assert entries.realized_loss_today(rig.engine, rig.now) == 0
