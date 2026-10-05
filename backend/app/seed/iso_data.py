"""ISO 3166-1 countries and ISO 4217 currencies, sourced from third-party packages.

- pycountry: the ISO 3166-1 country codes, and ISO 4217 currency names.
- babel (Unicode CLDR): English country names, and each country's current legal-tender
  currency (ISO itself doesn't publish a country -> currency mapping).

Upgrading either package is how this data gets updated.
"""

from datetime import UTC, datetime
from functools import cache

import pycountry
from babel import Locale
from babel.numbers import get_territory_currencies

# Corrections for places where babel's CLDR data lags real-world currency changes.
# Remove an entry once a babel release catches up.
CURRENCY_OVERRIDES: dict[str, str] = {
    "BG": "EUR",  # Bulgaria adopted the euro on 2026-01-01.
}


@cache
def countries() -> dict[str, tuple[str, str]]:
    """ISO 3166-1 alpha-2 code -> (English name, default currency code).

    Countries with no legal-tender currency in CLDR (e.g. Antarctica) are left out, since
    every country row needs a default currency.
    """
    english = Locale("en")
    today = datetime.now(UTC).date()
    result: dict[str, tuple[str, str]] = {}
    for country in pycountry.countries:
        code = country.alpha_2
        currency = CURRENCY_OVERRIDES.get(code)
        if currency is None:
            tender = get_territory_currencies(code, start_date=today, end_date=today)
            currency = tender[0] if tender else None
        if currency is None:
            continue
        result[code] = (english.territories.get(code, country.name), currency)
    return result


@cache
def currencies() -> dict[str, str]:
    """ISO 4217 code -> name, for every currency that is some country's default."""
    english = Locale("en")
    result: dict[str, str] = {}
    for code in sorted({currency for _, currency in countries().values()}):
        iso = pycountry.currencies.get(alpha_3=code)
        result[code] = iso.name if iso else english.currencies.get(code, code)
    return result
