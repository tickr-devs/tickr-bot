"""The balance the platform tests' fake broker answers with, so the entry desk's buying-power
check (risk.admission) admits their entries: the Sandbox account's 4x day buying power on
1,000,000 USD, all cash."""

from decimal import Decimal

from tickr.demo.world import cash_account

FUNDS = cash_account(Decimal(4_000_000))
