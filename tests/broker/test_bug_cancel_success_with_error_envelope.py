"""BUG reproduction (fails until fixed): a cancel answered with a success status but an error
envelope is reported as a sent cancel instead of OutcomeUnknown.

`WebullBroker.cancel` (src/aperture/broker/webull.py:689) returns on any 2xx without looking at
the body. `place` and `replace_stop_price` treat the same answer as "not an acknowledgement" and
raise OutcomeUnknown (read_acknowledgement / read_replace_ack). SdkTransport produces exactly
this shape for a 2xx whose body is not JSON (`{"error_code": "UNPARSEABLE_BODY"}`).

Impact (engine/core.py `_cancel`): outcome "sent" instead of "unknown", so no CANCEL_UNKNOWN
fence or alert; spec-v1 section 9 asks "Unknown cancel outcome: keep the stop, send nothing,
alert". Hard rule 3 (unknown outcome).
"""

from __future__ import annotations

import pytest

from aperture.broker.base import OutcomeUnknown
from aperture.broker.webull import WireResponse

from .test_webull_broker import broker


@pytest.mark.xfail(strict=True, reason=(
    "bug: cancel answered 2xx with an error body counts as sent (webull.py cancel)"))
@pytest.mark.parametrize("body", [
    {"error_code": "UNPARSEABLE_BODY"},
    {"error_code": "OPENAPI_PARAM_ERR", "message": "something else"},
])
def test_cancel_success_status_with_error_envelope_is_outcome_unknown(body):
    b, t, _ = broker(WireResponse(200, body))
    with pytest.raises(OutcomeUnknown):
        b.cancel("ap-1")
    assert len(t.sent) == 1
