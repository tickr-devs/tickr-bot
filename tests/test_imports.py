from aperture.broker.base import Broker, OutcomeUnknown, Refused
from aperture.strategy.base import Check, Decision, Verdict
from aperture.types import TERMINAL_STATES, OrderState


def test_interfaces_import():
    assert Broker and OutcomeUnknown and Refused


def test_terminal_states():
    assert OrderState.FILLED.terminal
    assert not OrderState.UNKNOWN.terminal
    assert OrderState.ACCEPTED not in TERMINAL_STATES


def test_decisive_check_is_first_failure():
    checks = (
        Check("a", None, "==", None, True),
        Check("b", None, "==", None, False),
        Check("c", None, "==", None, False),
    )
    d = Decision("s", "1", "X", Verdict.IGNORE, checks)
    assert d.decisive.rule == "b"
