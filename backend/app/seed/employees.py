"""Synthetic employee population with realistic compensation histories (Phase 3).

Usage (from backend/, on a database that already has the Phase 1.2 reference data):
    python -m app.seed.employees [--count 10000] [--seed 1204] [--as-of YYYY-MM-DD]

Deterministic: the same count, seed, as-of date and pinned Faker version always
produce identical data. Refuses to run if employees already exist, because
compensation history is append-only and can't be cleared selectively — rebuild the
database (`alembic downgrade base && alembic upgrade head`, then the 1.2 seeds) instead.
"""

import argparse
import random
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from faker import Faker
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import (
    ChangeReason,
    Company,
    CompensationType,
    Country,
    Department,
    Employee,
    JobLevel,
    JobTitle,
)
from app.seed.loaders import EMPLOYEE_LOADERS, METHODS, RECORD_LOADERS, Method
from app.seed.org_structure import DEPARTMENT_TITLES, JOB_TITLES, LEVEL_CODES

DEFAULT_COUNT = 10_000
DEFAULT_SEED = 1204
BATCH_SIZE = 5_000  # employees per chunk
DEFAULT_METHOD: "Method" = "copy"  # fastest in Scale Lab E1 (docs/PERFORMANCE.md)
EARLIEST_HIRE = date(2012, 1, 1)
REVIEW_MONTH_DAY = (4, 1)  # annual revisions take effect on 1 April
BONUS_MONTH_DAY = (3, 15)  # annual bonus paid on 15 March
CHANGED_BY = "seed"
EMAIL_DOMAIN = "acme.example"


@dataclass(frozen=True)
class CountryProfile:
    weight: int
    locale: str  # Faker locale for names
    l3_base: int  # typical annual base pay for a level-3 employee, local currency
    housing: bool = False  # housing allowance is customary
    transport: bool = False
    meal: bool = False


# Where ACME employs people, with typical pay in local currency (rough market levels,
# not real benchmarks). Currency is each country's default from the reference data.
COUNTRIES: dict[str, CountryProfile] = {
    "IN": CountryProfile(
        30, "en_IN", 2_400_000, housing=True, transport=True, meal=True
    ),
    "US": CountryProfile(18, "en_US", 140_000),
    "GB": CountryProfile(8, "en_GB", 75_000),
    "DE": CountryProfile(6, "de_DE", 80_000),
    "NL": CountryProfile(3, "nl_NL", 75_000),
    "FR": CountryProfile(3, "fr_FR", 65_000, meal=True),
    "ES": CountryProfile(2, "es_ES", 50_000, meal=True),
    "IE": CountryProfile(2, "en_IE", 75_000),
    "PL": CountryProfile(4, "pl_PL", 220_000),
    "SE": CountryProfile(2, "sv_SE", 600_000),
    "CA": CountryProfile(4, "en_CA", 120_000),
    "BR": CountryProfile(4, "pt_BR", 220_000, transport=True, meal=True),
    "MX": CountryProfile(3, "es_MX", 700_000, transport=True),
    "SG": CountryProfile(4, "en_US", 120_000),
    "AE": CountryProfile(3, "en_US", 300_000, housing=True, transport=True),
    "AU": CountryProfile(3, "en_AU", 140_000),
}
# Share of employees in a country paid in USD instead of the local currency
# (an employee based in Dubai can be paid in USD).
USD_PAID_SHARE = {"AE": 0.15, "SG": 0.05, "MX": 0.05}
# Fixed USD value of one local unit for those employees' pay — seed-only constants, so
# the generated data doesn't change with live exchange rates.
SEED_USD_PER_LOCAL = {"AE": 0.27, "SG": 0.74, "MX": 0.055}

# department -> (weight, pay multiplier, titles); names come from app.seed.org_structure.
DEPARTMENT_PAY: dict[str, tuple[int, float]] = {
    "Engineering": (34, 1.00),
    "Product": (8, 1.05),
    "Sales": (12, 0.90),
    "Marketing": (7, 0.85),
    "Finance": (6, 0.90),
    "People": (5, 0.80),
    "Operations": (8, 0.75),
    "Customer Support": (20, 0.60),
}
DEPARTMENTS: dict[str, tuple[int, float, list[str]]] = {
    name: (*DEPARTMENT_PAY[name], titles) for name, titles in DEPARTMENT_TITLES.items()
}

# level -> (weight, pay multiplier vs L3, annual bonus target as share of base)
LEVELS: dict[str, tuple[int, float, float]] = {
    "L1": (12, 0.60, 0.00),
    "L2": (22, 0.80, 0.05),
    "L3": (26, 1.00, 0.08),
    "L4": (20, 1.30, 0.10),
    "L5": (12, 1.70, 0.12),
    "L6": (6, 2.20, 0.15),
    "L7": (2, 3.00, 0.20),
}
EQUITY_LEVELS = {"L4", "L5", "L6", "L7"}
EQUITY_DEPARTMENTS = {"Engineering", "Product"}

# status -> weight
STATUSES = {"active": 86, "on_leave": 4, "terminated": 10}

# The (category, subtype) of every catalog type the seed uses.
TYPE_KEYS = {
    "base": ("fixed", "base"),
    "annual_bonus": ("bonus", "annual"),
    "quarterly_bonus": ("bonus", "quarterly"),
    "variable": ("variable", "annual_target"),
    "rsu": ("equity", "rsu"),
    "housing": ("allowance", "housing"),
    "transport": ("allowance", "transport"),
    "meal": ("allowance", "meal"),
    "internet": ("reimbursement", "phone_internet"),
    "internet_ctc": ("reimbursement", "phone_internet_ctc"),
    "learning": ("reimbursement", "learning"),
    "wellness": ("reimbursement", "wellness"),
}
REASON_CODES = (
    "new_hire",
    "annual_revision",
    "promotion",
    "bonus_payout",
    "equity_grant",
    "correction",
)


@dataclass(frozen=True)
class Catalog:
    company_ids: list[int]
    currencies: dict[str, str]  # country -> default currency
    types: dict[str, int]  # TYPE_KEYS key -> compensation_types.id
    reasons: dict[str, int]  # code -> change_reasons.id
    departments: dict[str, int]  # name -> departments.id
    job_titles: dict[str, int]  # name -> job_titles.id
    job_levels: dict[str, int]  # code -> job_levels.id


def load_catalog(session: Session) -> Catalog:
    """Look up everything the seed depends on, failing clearly if 1.2 hasn't run."""
    company_ids = list(session.scalars(select(Company.id).order_by(Company.id)))
    currencies = dict(
        session.execute(
            select(Country.code, Country.default_currency).where(
                Country.code.in_(COUNTRIES)
            )
        ).all()
    )
    type_rows = session.execute(
        select(CompensationType.id, CompensationType.category, CompensationType.subtype)
    ).all()
    by_key = {(r.category, r.subtype): r.id for r in type_rows}
    reasons = dict(
        session.execute(
            select(ChangeReason.code, ChangeReason.id).where(
                ChangeReason.code.in_(REASON_CODES)
            )
        ).all()
    )

    # CITEXT names: look up case-insensitively, keyed by the seed's spelling.
    def ids_by_name(column, key, names) -> dict[str, int]:
        found = {
            name.lower(): id_
            for name, id_ in session.execute(select(column, key)).all()
        }
        return {n: found[n.lower()] for n in names if n.lower() in found}

    departments = ids_by_name(Department.name, Department.id, DEPARTMENT_TITLES)
    job_titles = ids_by_name(JobTitle.name, JobTitle.id, JOB_TITLES)
    job_levels = ids_by_name(JobLevel.code, JobLevel.id, LEVEL_CODES)

    missing = []
    if not company_ids:
        missing.append("companies (python -m app.seed.companies)")
    missing += [f"country {c}" for c in COUNTRIES if c not in currencies]
    missing += [
        f"compensation type {k}" for k, v in TYPE_KEYS.items() if v not in by_key
    ]
    missing += [f"change reason {c}" for c in REASON_CODES if c not in reasons]
    org = "(python -m app.seed.org_structure)"
    missing += [f"department {n} {org}" for n in DEPARTMENT_TITLES if n not in departments]
    missing += [f"job title {n} {org}" for n in JOB_TITLES if n not in job_titles]
    missing += [f"job level {c} {org}" for c in LEVEL_CODES if c not in job_levels]
    if missing:
        raise SystemExit(
            "Reference data missing — run the Phase 1.2 seeds first:\n  "
            + "\n  ".join(missing)
        )
    return Catalog(
        company_ids=company_ids,
        currencies=currencies,
        types={k: by_key[v] for k, v in TYPE_KEYS.items()},
        reasons=reasons,
        departments=departments,
        job_titles=job_titles,
        job_levels=job_levels,
    )


def ascii_slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return "".join(ch for ch in folded.lower() if ch.isalnum()) or "employee"


def money(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))


def anniversaries(start: date, end: date, month_day: tuple[int, int]) -> Iterator[date]:
    """Every month/day strictly after `start` and on or before `end`."""
    year = start.year
    while True:
        day = date(year, *month_day)
        if day > end:
            return
        if day > start:
            yield day
        year += 1


def quarter_ends(start: date, end: date) -> Iterator[date]:
    for year in range(start.year, end.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            quarter_end = date(year, month, day)
            if start < quarter_end <= end:
                yield quarter_end


class Generator:
    def __init__(
        self, catalog: Catalog, seed: int, as_of: date, email_domain: str = EMAIL_DOMAIN
    ) -> None:
        self.catalog = catalog
        self.email_domain = email_domain
        self.as_of = as_of
        self.rng = random.Random(seed)
        self.fakers: dict[str, Faker] = {}
        for locale in sorted({p.locale for p in COUNTRIES.values()}):
            fake = Faker(locale)
            fake.seed_instance(seed)
            self.fakers[locale] = fake
        # Each company decides once whether its internet reimbursement is inside CTC.
        policy_rng = random.Random(seed + 1)
        self.internet_in_ctc = {
            cid: policy_rng.random() < 0.5 for cid in catalog.company_ids
        }

    def pick(self, options: dict[str, int]) -> str:
        """Weighted choice: {option: weight}."""
        return self.rng.choices(list(options), weights=list(options.values()))[0]

    def hire_date(self) -> date:
        # Skewed toward recent hires: the company has been growing.
        latest = self.as_of - timedelta(days=30)
        span = (latest - EARLIEST_HIRE).days
        return EARLIEST_HIRE + timedelta(days=int(self.rng.triangular(0, span, span)))

    def employee(self, index: int) -> tuple[dict, dict]:
        """One employee row, plus the facts its compensation history is generated from."""
        rng = self.rng
        country = self.pick({c: p.weight for c, p in COUNTRIES.items()})
        profile = COUNTRIES[country]
        currency = self.catalog.currencies[country]
        if rng.random() < USD_PAID_SHARE.get(country, 0):
            currency = "USD"
        department = self.pick({d: spec[0] for d, spec in DEPARTMENTS.items()})
        _, dept_multiplier, titles = DEPARTMENTS[department]
        level = self.pick({lvl: spec[0] for lvl, spec in LEVELS.items()})
        status = self.pick(STATUSES)
        hire = self.hire_date()
        termination = None
        if status == "terminated":
            earliest_exit = hire + timedelta(days=90)
            if earliest_exit >= self.as_of:
                status = "active"
            else:
                termination = earliest_exit + timedelta(
                    days=rng.randrange((self.as_of - earliest_exit).days + 1)
                )

        fake = self.fakers[profile.locale]
        first, last = fake.first_name(), fake.last_name()
        company_id = rng.choice(self.catalog.company_ids)

        target_annual = (
            profile.l3_base
            * LEVELS[level][1]
            * dept_multiplier
            * rng.uniform(0.85, 1.15)
        )
        if (
            rng.random() < 0.03
        ):  # a few clear outliers, so the analytics have something to find
            target_annual *= rng.choice(
                [rng.uniform(0.55, 0.72), rng.uniform(1.3, 1.6)]
            )
        if currency != self.catalog.currencies[country]:
            # Paid in USD: convert the local market level at a fixed, seed-only rate.
            target_annual *= SEED_USD_PER_LOCAL[country]

        row = {
            "company_id": company_id,
            "first_name": first,
            "last_name": last,
            "email": f"{ascii_slug(first)}.{ascii_slug(last)}.{index}@{self.email_domain}",
            "department_id": self.catalog.departments[department],
            "job_title_id": self.catalog.job_titles[rng.choice(titles)],
            "job_level_id": self.catalog.job_levels[level],
            "current_country": country,
            "currency": currency,
            "status": status,
            "hire_date": hire,
            "termination_date": termination,
        }
        facts = {
            "country": country,
            "currency": currency,
            "profile": profile,
            "department": department,
            "level": level,
            "hire": hire,
            "end": termination or self.as_of,
            "target_annual": target_annual,
            "company_id": company_id,
        }
        return row, facts

    def history(self, employee_id: int, facts: dict) -> list[dict]:
        """Every compensation record for one employee, oldest first."""
        rng, types, reasons = self.rng, self.catalog.types, self.catalog.reasons
        hire, end = facts["hire"], facts["end"]
        records: list[dict] = []

        def add(type_key: str, effective: date, amount: float, reason: str) -> None:
            records.append(
                {
                    "employee_id": employee_id,
                    "compensation_type_id": types[type_key],
                    "effective_date": effective,
                    "country": facts["country"],
                    "currency": facts["currency"],
                    "amount": money(amount),
                    "change_reason_id": reasons[reason],
                    "changed_by": CHANGED_BY,
                }
            )

        # Base pay: raises are generated first, then the starting salary is set so the
        # employee lands near their target today.
        reviews = list(anniversaries(hire, end, REVIEW_MONTH_DAY))
        events = []  # (date, multiplier, reason)
        for review in reviews:
            if rng.random() < 0.08:
                events.append((review, rng.uniform(1.12, 1.20), "promotion"))
            else:
                events.append((review, rng.uniform(1.03, 1.09), "annual_revision"))
        growth = 1.0
        for _, multiplier, _ in events:
            growth *= multiplier
        annual = facts["target_annual"] / growth
        base_annual_on = [(hire, annual)]
        add("base", hire, annual / 12, "new_hire")
        for when, multiplier, reason in events:
            annual *= multiplier
            base_annual_on.append((when, annual))
            add("base", when, annual / 12, reason)
            if rng.random() < 0.02 and when + timedelta(days=10) <= end:
                annual *= rng.uniform(0.99, 1.01)  # small fix to a mis-keyed revision
                add("base", when + timedelta(days=10), annual / 12, "correction")

        def base_annual(on: date) -> float:
            current = base_annual_on[0][1]
            for when, amount in base_annual_on:
                if when <= on:
                    current = amount
            return current

        level, department, profile = (
            facts["level"],
            facts["department"],
            facts["profile"],
        )

        # Bonuses: sales gets quarterly payouts and a variable-pay target; everyone else
        # from L2 up gets an annual bonus.
        if department == "Sales":
            add("variable", hire, base_annual(hire) * 0.30, "new_hire")
            for quarter_end in quarter_ends(hire, end):
                payout = base_annual(quarter_end) * 0.30 / 4 * rng.uniform(0.3, 1.8)
                add("quarterly_bonus", quarter_end, payout, "bonus_payout")
        else:
            bonus_target = LEVELS[level][2]
            if bonus_target:
                for payday in anniversaries(
                    hire + timedelta(days=365), end, BONUS_MONTH_DAY
                ):
                    payout = base_annual(payday) * bonus_target * rng.uniform(0.6, 1.4)
                    add("annual_bonus", payday, payout, "bonus_payout")

        # Equity: senior engineering/product staff get a hire grant and annual refreshes.
        if level in EQUITY_LEVELS and department in EQUITY_DEPARTMENTS:
            add("rsu", hire, base_annual(hire) * rng.uniform(0.25, 0.6), "new_hire")
            for grant_day in anniversaries(hire, end, (hire.month, min(hire.day, 28))):
                add(
                    "rsu",
                    grant_day,
                    base_annual(grant_day) * rng.uniform(0.15, 0.4),
                    "equity_grant",
                )

        # Allowances follow base pay: set at hire, revised with each annual review.
        allowances = []
        if profile.housing:
            allowances.append(("housing", rng.uniform(0.15, 0.25)))
        if profile.transport and rng.random() < 0.6:
            allowances.append(("transport", rng.uniform(0.02, 0.04)))
        if profile.meal and rng.random() < 0.5:
            allowances.append(("meal", rng.uniform(0.01, 0.02)))
        for type_key, share in allowances:
            add(type_key, hire, base_annual(hire) / 12 * share, "new_hire")
            for when, _, reason in events:
                if reason == "annual_revision":
                    add(type_key, when, base_annual(when) / 12 * share, reason)

        # Reimbursements: everyone gets internet (inside or outside CTC per company
        # policy); some get learning and wellness budgets.
        internet = (
            "internet_ctc" if self.internet_in_ctc[facts["company_id"]] else "internet"
        )
        add(internet, hire, base_annual(hire) / 12 * 0.01, "new_hire")
        if rng.random() < 0.3:
            add("learning", hire, base_annual(hire) * 0.015, "new_hire")
        if rng.random() < 0.25:
            add("wellness", hire, base_annual(hire) * 0.008, "new_hire")

        return records


def seed_employees(
    session: Session,
    count: int = DEFAULT_COUNT,
    seed: int = DEFAULT_SEED,
    as_of: date | None = None,
    batch_size: int = BATCH_SIZE,
    *,
    method: Method = DEFAULT_METHOD,
    require_empty: bool = True,
    email_domain: str = EMAIL_DOMAIN,
    progress: Callable[[int], None] | None = None,
) -> dict[str, int]:
    """Generate and insert `count` employees with histories; returns row counts.

    Works in chunks of `batch_size` employees — generate, insert employees, insert their
    records, fill their current compensation, commit — so memory stays flat at any
    count. `method` picks how rows are written (see `app.seed.loaders`).

    `require_empty=False` and a distinct `email_domain` let tests seed a small batch
    inside a rolled-back transaction on a database that already has employees.
    """
    as_of = as_of or datetime.now(UTC).date()
    if require_empty and session.scalar(select(func.count()).select_from(Employee)):
        raise SystemExit(
            "Employees already exist. Compensation history is append-only, so seed into "
            "a fresh database: alembic downgrade base && alembic upgrade head, then the "
            "Phase 1.2 seeds and app.seed.org_structure."
        )
    catalog = load_catalog(session)
    generator = Generator(catalog, seed, as_of, email_domain)
    insert_employees, insert_records = EMPLOYEE_LOADERS[method], RECORD_LOADERS[method]

    totals = {"employees": 0, "records": 0, "current": 0}
    for start in range(1, count + 1, batch_size):
        people = [
            generator.employee(i)
            for i in range(start, min(start + batch_size, count + 1))
        ]
        employee_ids = insert_employees(session, [row for row, _ in people])
        records = [
            record
            for employee_id, (_, facts) in zip(employee_ids, people, strict=True)
            for record in generator.history(employee_id, facts)
        ]
        insert_records(session, records)
        totals["current"] += fill_current_compensation(session, employee_ids, as_of)
        totals["employees"] += len(employee_ids)
        totals["records"] += len(records)
        session.commit()
        session.expunge_all()  # keep the ORM loader's identity map from growing
        if progress:
            progress(totals["employees"])
    return totals


def fill_current_compensation(
    session: Session, employee_ids: list[int], as_of: date
) -> int:
    """Latest in-effect record per (employee, type), for the given employees."""
    result = session.execute(
        text(
            """
            INSERT INTO current_compensation
                (employee_id, compensation_type_id, compensation_record_id, effective_date, amount)
            SELECT DISTINCT ON (employee_id, compensation_type_id)
                   employee_id, compensation_type_id, id, effective_date, amount
            FROM compensation_records
            WHERE effective_date <= :as_of AND employee_id = ANY(:employee_ids)
            ORDER BY employee_id, compensation_type_id, effective_date DESC, id DESC
            """
        ),
        {"as_of": as_of, "employee_ids": employee_ids},
    )
    return result.rowcount


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic employees.")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="history runs up to this date (default: today)",
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--method", choices=METHODS, default=DEFAULT_METHOD)
    args = parser.parse_args()

    started = time.perf_counter()

    def report(done: int) -> None:
        if done % 100_000 == 0 or done == args.count:
            print(
                f"  {done:>10,} employees  {time.perf_counter() - started:7.1f}s",
                flush=True,
            )

    with SessionLocal() as session:
        counts = seed_employees(
            session,
            args.count,
            args.seed,
            args.as_of,
            args.batch_size,
            method=args.method,
            progress=report,
        )
    elapsed = time.perf_counter() - started
    print(
        f"Seeded {counts['employees']} employees, {counts['records']} compensation "
        f"records, {counts['current']} current-compensation rows in {elapsed:.1f}s "
        f"({args.method})."
    )


if __name__ == "__main__":
    main()
