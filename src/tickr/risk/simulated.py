"""Platform rules for simulations (backlog T12). SIMULATED, never on the order path.

A simulation (the model's dollar exam, a backtest on stored model answers) admits each
simulated ENTER through `admit_simulated` of `tickr.risk.admission`: the same check functions
the live bot runs, fed from a simulated book of the day instead of broker reads.
`SimulationRules` carries the limits (bot.toml `[risk]`, `[session]`, as live) and names them,
so every simulated result says which rules and values it was computed under.

The book counts a simulated ENTER from its decision time as a working entry (a slot and a
working entry, its planned loss reserved), from its fill as an open position, and frees it at
its exit (or when its entry is missed). A closed trade's result counts in the day's net
realized loss from its exit time, and a losing exit starts the symbol's pause after a loss.
Times decide, not the order trades were recorded in, so a caller may record a whole trade
(entry to exit) before admitting a later decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from tickr.calendar import CalendarCause, CalendarRefused, MarketDay
from tickr.config import Config, GateResult
from tickr.risk.admission import (
    NOT_SIMULATED,
    Admission,
    AdmissionInputs,
    AdmissionLimits,
    BuyingPower,
    Reconciliation,
    admit_simulated,
)
from tickr.risk.ticks import round_to_tick
from tickr.strategy.base import Decision

APPLIED = ("session", "own_state", "decision", "money")
"""What `SIMULATED_CHECKS` covers, by name."""

COOLDOWN_OFF = "off"


@dataclass(frozen=True)
class SimulationRules:
    """The platform rules a simulation obeys and the config values they were read from."""

    limits: AdmissionLimits

    @classmethod
    def from_config(cls, config: Config) -> SimulationRules:
        return cls(AdmissionLimits.from_config(config))

    def values(self) -> dict[str, str]:
        """Every config value the simulated checks read, by its bot.toml key."""
        lim = self.limits
        cooldown = lim.symbol_cooldown_minutes_after_loss
        return {
            "risk.planned_loss_usd": str(lim.planned_loss_usd),
            "risk.position_notional_cap_usd": str(lim.position_notional_cap_usd),
            "risk.max_daily_loss_usd": str(lim.max_daily_loss_usd),
            "risk.min_daily_room_usd": str(lim.min_daily_room_usd),
            "risk.max_open_positions": str(lim.max_open_positions),
            "risk.max_working_entries": str(lim.max_working_entries),
            "risk.symbol_cooldown_minutes_after_loss":
                COOLDOWN_OFF if cooldown is None else str(cooldown),
            "risk.max_entry_rest_bars": str(lim.max_entry_rest_bars),
            "session.last_entry_minutes_before_close":
                str(lim.session.last_entry_minutes_before_close),
            "session.half_days_enabled": str(lim.session.half_days_enabled).lower(),
        }

    def label(self) -> str:
        """One line for reports and logs."""
        values = ", ".join(f"{k}={v}" for k, v in self.values().items())
        return (f"platform rules ({', '.join(APPLIED)}): {values}; "
                f"not simulated: {', '.join(NOT_SIMULATED)}")


@dataclass
class _Entry:
    symbol: str
    reserved_usd: Decimal
    entered_at: datetime
    filled_at: datetime | None = None
    ended_at: datetime | None = None
    pnl_usd: Decimal = Decimal(0)


# What the checks a simulation does not run would read. Each value refuses, so these inputs
# can never pass the live `admit` by mistake.
_NO_GATE = GateResult(False, "simulation: no trading gate")
_NO_BROKER = Reconciliation(False, None, False, True)
_NO_BUYING_POWER = BuyingPower(None, None, Decimal(0))


@dataclass
class SimulatedBook:
    """One New York day of one simulated strategy's entries and trades."""

    rules: SimulationRules
    _entries: list[_Entry] = field(default_factory=list)

    def admit(self, decision: Decision, now: datetime, day: MarketDay | None) -> Admission:
        """Admission of `decision` at `now` (aware) against the book as it stands at `now`.
        `day`: the calendar's answer for the date (None = unknown, refused)."""
        # A simulation runs one strategy, so its own name is the active one.
        limits = replace(self.rules.limits, active_strategy=decision.strategy)
        return admit_simulated(self.inputs(decision, now, day), limits)

    def inputs(self, decision: Decision, now: datetime, day: MarketDay | None,
               ) -> AdmissionInputs:
        """The admission inputs of `decision` at `now`, from the book."""
        live = [e for e in self._entries if e.entered_at <= now
                and (e.ended_at is None or e.ended_at > now)]
        working = [e for e in live if e.filled_at is None or e.filled_at > now]
        holding = [e for e in live if e.filled_at is not None and e.filled_at <= now]
        closed = [e for e in self._entries if e.ended_at is not None and e.ended_at <= now]
        net = sum((e.pnl_usd for e in closed), Decimal(0))
        losses = [e.ended_at for e in closed
                  if e.ended_at is not None and e.symbol == decision.symbol and e.pnl_usd < 0]
        return AdmissionInputs(
            gate=_NO_GATE,
            reconciliation=_NO_BROKER,
            entry_stop=True,
            session=day if day is not None else CalendarRefused(
                CalendarCause.CALENDAR_INVALID, "the simulation's clock has no calendar day"),
            now=now,
            own_open_positions=sum(1 for e in holding if e.symbol == decision.symbol),
            own_working_orders=sum(1 for e in working if e.symbol == decision.symbol),
            open_positions=len(live),
            working_entries=len(working),
            symbol_last_loss_at=max(losses) if losses else None,
            decision=decision,
            limit_price=None if decision.entry_high is None else round_to_tick(decision.entry_high),
            stop_price=None if decision.stop is None else round_to_tick(decision.stop),
            setup_valid=True,
            quote=None,
            last_bar=None,
            previous_quote=None,
            realized_loss_today_usd=max(-net, Decimal(0)),
            open_reservations_usd=sum((e.reserved_usd for e in live), Decimal(0)),
            buying_power=_NO_BUYING_POWER,
            entry_budget_available=False,
            stop_budget_available=False,
        )

    def entered(self, symbol: str, at: datetime, admission: Admission) -> int:
        """Record an admitted entry from `at`; returns its key."""
        reserved = admission.reserved_usd or admission.planned_loss_usd
        assert admission.ok and reserved is not None
        self._entries.append(_Entry(symbol, reserved, at))
        return len(self._entries) - 1

    def filled(self, key: int, at: datetime) -> None:
        self._entries[key].filled_at = at

    def ended(self, key: int, at: datetime, pnl_usd: Decimal) -> None:
        """The entry was missed (`pnl_usd` 0) or its trade closed at `at` with `pnl_usd`."""
        entry = self._entries[key]
        entry.ended_at, entry.pnl_usd = at, pnl_usd
