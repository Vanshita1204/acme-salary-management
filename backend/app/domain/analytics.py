"""Pay-insight formulas (§5). Pure functions: no database or network access.

Phase 12 computes these SQL-side at scale; this module is the reference definition the
SQL is tested against, and the home of the thresholds.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

# Outlier definition (§5): below 80% or above 120% of the peer-group median, evaluated
# only for peer groups of at least 5 so small groups don't produce misleading flags.
OUTLIER_LOW = Decimal("0.8")
OUTLIER_HIGH = Decimal("1.2")
MIN_PEER_GROUP_SIZE = 5


@dataclass(frozen=True)
class EmployeePay:
    employee_id: int
    job_title: str
    job_level: str
    country: str
    total: Decimal  # total compensation, already in the reporting currency


@dataclass(frozen=True)
class Outlier:
    employee_id: int
    total: Decimal
    peer_median: Decimal
    ratio: Decimal  # total / peer_median
    direction: str  # "below" or "above"


PeerKey = tuple[str, str, str]


def peer_key(pay: EmployeePay) -> PeerKey:
    """Peer group (§5): same job title, level and country."""
    return (pay.job_title, pay.job_level, pay.country)


def peer_groups(pays: Iterable[EmployeePay]) -> dict[PeerKey, list[EmployeePay]]:
    groups: dict[PeerKey, list[EmployeePay]] = defaultdict(list)
    for pay in pays:
        groups[peer_key(pay)].append(pay)
    return dict(groups)


def find_outliers(pays: Iterable[EmployeePay]) -> list[Outlier]:
    """Employees paid below OUTLIER_LOW or above OUTLIER_HIGH of their peer median."""
    outliers: list[Outlier] = []
    for group in peer_groups(pays).values():
        if len(group) < MIN_PEER_GROUP_SIZE:
            continue
        peer_median = median(p.total for p in group)
        if peer_median == 0:
            continue
        for pay in group:
            ratio = pay.total / peer_median
            if ratio < OUTLIER_LOW or ratio > OUTLIER_HIGH:
                direction = "below" if ratio < OUTLIER_LOW else "above"
                outliers.append(
                    Outlier(pay.employee_id, pay.total, peer_median, ratio, direction)
                )
    return outliers


def composition(
    amounts_by_category: Iterable[tuple[str, Decimal]],
) -> dict[str, Decimal]:
    """Share of compensation per category, as fractions summing to 1.

    Takes (category, annualized amount) pairs — open-ended categories, not a fixed list.
    """
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for category, amount in amounts_by_category:
        totals[category] += amount
    grand_total = sum(totals.values(), Decimal(0))
    if grand_total == 0:
        return {}
    return {category: amount / grand_total for category, amount in totals.items()}
