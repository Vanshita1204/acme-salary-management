"""Currency conversion. Pure functions: no database or network access."""

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal

CENT = Decimal("0.01")


class MissingRateError(ValueError):
    def __init__(self, currency: str) -> None:
        super().__init__(f"no exchange rate for {currency}")
        self.currency = currency


def rates_to_usd_from_usd_base(
    units_per_usd: Mapping[str, float | str],
) -> dict[str, Decimal]:
    """Invert a USD-based quote (units of currency per 1 USD) into USD value of one unit.

    Goes through str() so binary-float noise from JSON doesn't leak into the Decimal.
    """
    rates = {code: Decimal(str(units)) for code, units in units_per_usd.items()}
    return {code: 1 / units for code, units in rates.items() if units > 0}


def cross_rate(
    from_currency: str, to_currency: str, rates_to_usd: Mapping[str, Decimal]
) -> Decimal:
    """Units of to_currency per one unit of from_currency, going through USD."""
    if from_currency == to_currency:
        return Decimal(1)
    for currency in (from_currency, to_currency):
        if currency not in rates_to_usd:
            raise MissingRateError(currency)
    return rates_to_usd[from_currency] / rates_to_usd[to_currency]


def convert(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    rates_to_usd: Mapping[str, Decimal],
) -> Decimal:
    """Convert amount between currencies, rounded half-even to cents."""
    rate = cross_rate(from_currency, to_currency, rates_to_usd)
    return (amount * rate).quantize(CENT, rounding=ROUND_HALF_EVEN)
