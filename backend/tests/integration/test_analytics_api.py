"""Phase 12 (§5): pay insights, computed in SQL and checked against the pure
reference definitions in `app.domain.analytics` on a small known population."""

import statistics
import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.domain.analytics import EmployeePay, composition, find_outliers
from app.jobs.daily import run_daily
from app.models import ChangeReason, CompensationType, ExchangeRate
from app.seed.change_reasons import load_change_reasons
from app.seed.compensation_types import load_compensation_types
from app.seed.reference import load_reference_data
from app.services import pay_changes
from app.services.compensation import utc_today

RATE_DAY = date(2099, 1, 1)  # after any real rate, so these are the latest
RATES = {"USD": Decimal(1), "EUR": Decimal("1.25"), "INR": Decimal("0.0125")}
CENT = Decimal("0.01")
HIRE = "2024-01-15"

# (key, title, level, country, currency, monthly base)
PEOPLE = [
    # Peer group (Engineer, mid, GB): median 5,000 -> 3,800 is below 80%, 6,500 above 120%.
    ("gb1", "eng", "mid", "GB", "EUR", 5000),
    ("gb2", "eng", "mid", "GB", "EUR", 5000),
    ("gb3", "eng", "mid", "GB", "EUR", 5000),
    ("gb4", "eng", "mid", "GB", "EUR", 5000),
    ("gb_low", "eng", "mid", "GB", "EUR", 3800),
    ("gb_high", "eng", "mid", "GB", "EUR", 6500),
    # Same role in India: only 4 people, so the extreme one isn't evaluated.
    ("in1", "eng", "mid", "IN", "INR", 400000),
    ("in2", "eng", "mid", "IN", "INR", 400000),
    ("in3", "eng", "mid", "IN", "INR", 400000),
    ("in_odd", "eng", "mid", "IN", "INR", 100000),
    ("us1", "mgr", "senior", "US", "USD", 10000),
    ("us2", "mgr", "senior", "US", "USD", 12000),
]
# Extra current components: (key, type, amount per period)
EXTRAS = [
    ("gb1", ("allowance", "housing"), 500),  # monthly, in CTC
    ("us1", ("bonus", "annual"), 12000),  # annual, in CTC
    ("us1", ("reimbursement", "wellness"), 1000),  # annual, outside CTC
]


@pytest.fixture
def world(db, client):
    load_reference_data(db)
    load_compensation_types(db)
    load_change_reasons(db)
    db.add_all(
        ExchangeRate(currency=c, rate_to_usd=r, rate_date=RATE_DAY, source="pytest")
        for c, r in RATES.items()
    )
    db.flush()
    tag = uuid.uuid4().hex[:8]
    department = client.post("/departments", json={"name": f"Analytics {tag}"}).json()
    titles = {
        key: client.post("/job-titles", json={"name": f"{key} {tag}"}).json()
        for key in ("eng", "mgr")
    }
    base_rank = 5_000_000 + uuid.uuid4().int % 1_000_000
    levels = {
        key: client.post(
            "/job-levels",
            json={"code": f"{key}-{tag}", "label": key, "rank": base_rank + n},
        ).json()
        for n, key in enumerate(("mid", "senior"))
    }
    company = client.post("/companies", json={"name": f"Analytics {tag}"}).json()
    types = {(t.category, t.subtype): t for t in db.scalars(select(CompensationType))}
    reasons = {r.code: r.id for r in db.scalars(select(ChangeReason))}

    def hire(key, title, level, country, currency, base, status="active"):
        response = client.post(
            "/employees",
            json={
                "company_id": company["id"],
                "first_name": key,
                "last_name": tag,
                "email": f"{key}.{tag}@pytest.example",
                "department_id": department["id"],
                "job_title_id": titles[title]["id"],
                "job_level_id": levels[level]["id"],
                "current_country": country,
                "currency": currency,
                "hire_date": HIRE,
                "base_pay": {"amount": str(base)},
                "changed_by": "pytest",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    people = {p[0]: hire(*p) for p in PEOPLE}
    for key, type_key, amount in EXTRAS:
        add(client, people[key], types[type_key].id, reasons["new_hire"], HIRE, amount)
    # Not active, so never in analytics.
    on_leave = hire("leave", "eng", "mid", "GB", "EUR", 99000)
    client.patch(f"/employees/{on_leave['id']}", json={"status": "on_leave"})
    gone = hire("gone", "eng", "mid", "GB", "EUR", 99000)
    client.post(f"/employees/{gone['id']}/terminate", json={"termination_date": HIRE})
    return {
        "department_id": department["id"],
        "titles": titles,
        "levels": levels,
        "people": people,
        "types": types,
        "reasons": reasons,
        "tag": tag,
    }


def add(client, employee, type_id, reason_id, effective, amount):
    response = client.post(
        f"/employees/{employee['id']}/compensation",
        json={
            "compensation_type_id": type_id,
            "change_reason_id": reason_id,
            "effective_date": effective,
            "amount": str(amount),
            "changed_by": "pytest",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def get(client, world, view, **params):
    response = client.get(
        f"/analytics/{view}", params={"department_id": world["department_id"], **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- the reference: totals worked out in Python from the definitions above ---


def annual_totals() -> dict[str, tuple[Decimal, str]]:
    """key -> (annual CTC in the employee's currency, currency)."""
    totals = {key: (Decimal(base) * 12, cur) for key, _, _, _, cur, base in PEOPLE}
    in_ctc = {("allowance", "housing"): 12, ("bonus", "annual"): 1}  # periods per year
    for key, type_key, amount in EXTRAS:
        if type_key in in_ctc:
            total, cur = totals[key]
            totals[key] = (total + Decimal(amount) * in_ctc[type_key], cur)
    return totals


def in_currency(amount: Decimal, frm: str, to: str) -> Decimal:
    return amount * RATES[frm] / RATES[to]


def reference_pays(reporting: str = "USD") -> list[EmployeePay]:
    totals = annual_totals()
    return [
        EmployeePay(
            employee_id=n,
            job_title=title,
            job_level=level,
            country=country,
            total=in_currency(totals[key][0], totals[key][1], reporting),
        )
        for n, (key, title, level, country, _, _) in enumerate(PEOPLE)
    ]


def close(actual: str, expected: Decimal) -> bool:
    return abs(Decimal(actual) - expected) <= CENT


# --- summary ---


def test_summary_counts_active_employees_in_the_reporting_currency(client, world):
    result = get(client, world, "summary", reporting_currency="EUR")
    pays = [p.total for p in reference_pays("EUR")]

    assert result["headcount"] == len(PEOPLE)  # on-leave and terminated left out
    assert close(result["total_cost"], sum(pays))
    assert close(result["average"], sum(pays) / len(pays))
    assert close(result["median"], statistics.median(pays))
    assert result["reporting_currency"] == "EUR"
    assert result["rates_as_of"] == {c: "2099-01-01" for c in ("EUR", "INR", "USD")}
    assert result["excluded_no_rate"] == 0
    by_country = {row["key"]: row for row in result["by_country"]}
    assert {k: r["headcount"] for k, r in by_country.items()} == {
        "GB": 6,
        "IN": 4,
        "US": 2,
    }
    gb = sum(p.total for p in reference_pays("EUR") if p.country == "GB")
    assert close(by_country["GB"]["total_cost"], gb)
    assert abs(sum(Decimal(r["share"]) for r in result["by_country"]) - 1) < Decimal(
        "0.001"
    )
    [department] = result["by_department"]
    assert department["headcount"] == len(PEOPLE)


def test_status_filter_cannot_bring_in_inactive_employees(client, world):
    assert get(client, world, "summary", status="terminated")["headcount"] == len(
        PEOPLE
    )


def test_directory_filters_narrow_every_view(client, world):
    assert get(client, world, "summary", country="US")["headcount"] == 2
    assert get(client, world, "summary", q="in_odd")["headcount"] == 1


# --- grouped statistics ---


@pytest.mark.parametrize("group_by", ["level", "title", "country"])
def test_grouped_stats_match_the_reference(client, world, group_by):
    result = get(client, world, "stats", group_by=group_by)
    pays = reference_pays()
    field = {"level": "job_level", "title": "job_title", "country": "country"}[group_by]
    expected = {}
    for pay in pays:
        expected.setdefault(getattr(pay, field), []).append(pay.total)

    assert len(result["groups"]) == len(expected)
    for row in result["groups"]:
        if group_by == "country":
            ref_key = row["key"]
        else:  # labels are "<key> <tag>" (titles) or "<key>-<tag>" (levels)
            ref_key = row["label"].replace("-", " ").split(" ")[0]
        values = expected[ref_key]
        assert row["headcount"] == len(values)
        assert close(row["average"], sum(values) / len(values))
        assert close(row["median"], statistics.median(values))
        assert close(row["minimum"], min(values))
        assert close(row["maximum"], max(values))


def test_levels_are_listed_by_rank(client, world):
    labels = [
        row["label"] for row in get(client, world, "stats", group_by="level")["groups"]
    ]
    assert labels == [f"mid-{world['tag']}", f"senior-{world['tag']}"]


# --- role by country ---


def test_role_by_country_compares_one_role_across_countries(client, world):
    result = get(
        client,
        world,
        "role-by-country",
        job_title_id=world["titles"]["eng"]["id"],
        job_level_id=world["levels"]["mid"]["id"],
    )

    by_country = {row["country"]: row for row in result["rows"]}
    assert set(by_country) == {"GB", "IN"}
    for country in ("GB", "IN"):
        values = [p.total for p in reference_pays() if p.country == country]
        assert by_country[country]["headcount"] == len(values)
        assert close(by_country[country]["median"], statistics.median(values))


# --- histogram ---


def test_histogram_bins_cover_everyone_from_min_to_max(client, world):
    result = get(client, world, "histogram", bins=4)
    pays = [p.total for p in reference_pays()]
    lo, hi = min(pays), max(pays)
    width = (hi - lo) / 4
    expected = [0] * 4
    for pay in pays:  # equal-width bins; the maximum belongs to the last one
        expected[min(int((pay - lo) / width), 3)] += 1

    bins = result["bins"]
    assert [b["count"] for b in bins] == expected
    assert sum(expected) == len(PEOPLE)
    assert close(bins[0]["lower"], lo)
    assert close(bins[-1]["upper"], hi)
    for i, b in enumerate(bins):
        assert close(b["lower"], lo + width * i)


def test_histogram_of_one_value_is_one_bin(client, world):
    result = get(client, world, "histogram", q="gb_low")
    assert [b["count"] for b in result["bins"]] == [1]


def test_histogram_of_nobody_has_no_bins(client, world):
    result = get(client, world, "histogram", q="nobody-has-this-name")
    assert result["bins"] == []


@pytest.mark.parametrize("bins", [0, 101])
def test_histogram_bin_count_is_bounded(client, world, bins):
    response = client.get("/analytics/histogram", params={"bins": bins})
    assert response.status_code == 422


# --- outliers ---


def test_outliers_match_the_reference_definition(client, world):
    result = get(client, world, "outliers")
    expected = find_outliers(reference_pays())

    flagged = {row["first_name"]: row for row in result["outliers"]}
    assert set(flagged) == {"gb_low", "gb_high"}
    assert len(expected) == 2
    assert flagged["gb_low"]["direction"] == "below"
    assert flagged["gb_high"]["direction"] == "above"
    reference = {PEOPLE[o.employee_id][0]: o for o in expected}
    for name, row in flagged.items():
        assert close(row["peer_median"], reference[name].peer_median)
        assert abs(Decimal(row["ratio"]) - reference[name].ratio) < Decimal("0.0001")
        assert row["peer_count"] == 6
    # Farthest from the median first, on a ratio scale: 0.76x (|ln| 0.274) is
    # farther than 1.3x (|ln| 0.262), so being paid 24% less outranks 30% more.
    assert [r["first_name"] for r in result["outliers"]] == ["gb_low", "gb_high"]
    assert (result["low_threshold"], result["high_threshold"]) == ("0.8", "1.2")


# --- composition ---


def test_composition_matches_the_reference_and_leaves_out_non_ctc_pay(client, world):
    result = get(client, world, "composition")

    parts = []
    for key, _, _, _, currency, base in PEOPLE:
        parts.append(("fixed", in_currency(Decimal(base) * 12, currency, "USD")))
    gb1_currency = "EUR"
    parts.append(("allowance", in_currency(Decimal(500) * 12, gb1_currency, "USD")))
    parts.append(("bonus", in_currency(Decimal(12000), "USD", "USD")))
    expected = composition(parts)

    shares = {row["category"]: Decimal(row["share"]) for row in result["categories"]}
    assert set(shares) == {"fixed", "allowance", "bonus"}  # no reimbursement
    for category, share in expected.items():
        assert abs(shares[category] - share) < Decimal("0.0001")


# --- missing rates and provider outages ---


def test_employee_without_a_rate_is_counted_as_excluded(db, client, world):
    no_rate = client.post(
        "/employees",
        json={
            "company_id": world["people"]["gb1"]["company_id"],
            "first_name": "kpw",
            "last_name": world["tag"],
            "email": f"kpw.{world['tag']}@pytest.example",
            "department_id": world["department_id"],
            "job_title_id": world["titles"]["eng"]["id"],
            "job_level_id": world["levels"]["mid"]["id"],
            "current_country": "KP",
            "currency": "KPW",  # legal tender, never quoted by the provider
            "hire_date": HIRE,
            "base_pay": {"amount": "1000000"},
            "changed_by": "pytest",
        },
    )
    assert no_rate.status_code == 201, no_rate.text

    result = get(client, world, "summary")

    assert result["headcount"] == len(PEOPLE)
    assert result["excluded_no_rate"] == 1
    assert "KPW" not in result["rates_as_of"]


def test_views_keep_working_on_stored_rates_when_the_provider_fails(db, client, world):
    db.execute(text("UPDATE exchange_rates SET fetched_at = now() - interval '2 days'"))
    failing = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )

    assert not run_daily(db, failing).ok

    result = get(client, world, "summary", reporting_currency="EUR")
    assert close(result["total_cost"], sum(p.total for p in reference_pays("EUR")))
    assert set(result["rates_as_of"].values()) == {"2099-01-01"}


def test_unsupported_reporting_currency_is_422(client, world):
    response = client.get("/analytics/summary", params={"reporting_currency": "ZZZ"})
    assert response.status_code == 422


# --- change report ---


def apply_changes(client, world) -> str:
    """A raise, a correction, a promotion, a change outside total pay, a currency change and a
    relocation; returns today's date (the last two are dated today)."""
    people, types, reasons = world["people"], world["types"], world["reasons"]
    base = types[("fixed", "base")].id
    # A raise counts; a correction a month later doesn't.
    add(client, people["gb1"], base, reasons["annual_revision"], "2025-04-01", 5500)
    add(client, people["gb1"], base, reasons["correction"], "2025-05-01", 5400)
    # A 20% promotion counts.
    add(client, people["in1"], base, reasons["promotion"], "2025-06-01", 480000)
    # Outside CTC: changes no total, so it can't count as a 0% increase.
    add(
        client,
        people["us1"],
        types[("reimbursement", "wellness")].id,
        reasons["policy_change"],
        "2025-07-01",
        1500,
    )
    today = str(utc_today())
    # A currency change isn't an increase (FR-7)...
    switched = client.post(
        f"/employees/{people['us2']['id']}/change-currency",
        json={
            "currency": "EUR",
            "amounts": [{"compensation_type_id": base, "amount": "11000"}],
            "change_reason_id": reasons["market_adjustment"],
            "effective_date": today,
            "changed_by": "pytest",
        },
    )
    assert switched.status_code == 200, switched.text
    # ...and neither is a move that keeps the same pay.
    moved = client.post(
        f"/employees/{people['gb2']['id']}/relocate",
        json={"country": "IE", "effective_date": today, "changed_by": "pytest"},
    )
    assert moved.status_code == 200, moved.text
    return today


def test_change_report_and_average_increase(db, client, world):
    today = apply_changes(client, world)

    report = get(client, world, "changes", date_from="2025-01-01", date_to=today)

    assert report["events"] == 6
    assert report["events_counted"] == 2
    assert report["employees_changed"] == 5
    # gb1's total includes housing: 66,000 -> 72,000 is 9.09%; in1's promotion is 20%.
    assert report["average_increase"] == "14.55"
    reasons_seen = {
        (item["first_name"], item["effective_date"]): item for item in report["items"]
    }
    raise_ = reasons_seen[("gb1", "2025-04-01")]
    assert (raise_["previous_total"], raise_["new_total"]) == ("66000.00", "72000.00")
    assert raise_["percent_change"] == "9.09"
    excluded = {
        item["first_name"]: item["excluded_because"]
        for item in report["items"]
        if not item["counts_as_increase"]
    }
    assert excluded == {
        "gb1": "correction",
        "us1": "outside total compensation",
        "us2": "currency change",
        "gb2": "relocation without a pay change",
    }


def test_change_report_range_and_limit(client, world):
    report = get(client, world, "changes", date_from=HIRE, date_to=HIRE, limit=3)
    # Everyone's hire date: one event each, none counted (no previous pay).
    assert report["events"] == len(PEOPLE)
    assert report["events_counted"] == 0
    assert report["average_increase"] is None
    assert len(report["items"]) == 3
    assert {i["excluded_because"] for i in report["items"]} == {"no previous pay"}


def test_change_report_rejects_a_backwards_range(client, world):
    response = client.get(
        "/analytics/changes",
        params={"date_from": "2025-02-01", "date_to": "2025-01-01"},
    )
    assert response.status_code == 422


# --- before any exchange rate has ever been stored ---


def test_amounts_already_in_the_reporting_currency_need_no_rate(db, client, world):
    """A fresh install, before the first successful fetch, still has answers for USD pay."""
    usd_people = [p for p in PEOPLE if p[4] == "USD"]
    db.execute(text("DELETE FROM exchange_rates"))

    result = get(client, world, "summary", reporting_currency="USD")

    assert result["headcount"] == len(usd_people)
    assert result["excluded_no_rate"] == len(PEOPLE) - len(
        usd_people
    )  # EUR and INR can't be converted
    assert result["rates_as_of"] == {}
    expected = sum(
        in_currency(annual_totals()[p[0]][0], "USD", "USD") for p in usd_people
    )
    assert close(result["total_cost"], expected)
    for view in ("stats", "histogram", "outliers", "role-by-country"):
        assert (
            client.get(
                f"/analytics/{view}", params={"department_id": world["department_id"]}
            ).status_code
            == 200
        )
    composition = get(client, world, "composition", reporting_currency="USD")
    assert {row["category"] for row in composition["categories"]} == {
        "fixed",
        "bonus",
    }  # US employees' pay only
    # Any reporting currency works the same way: people paid in it count, everyone else is
    # left out and counted, never converted at an invented rate.
    eur_people = [p for p in PEOPLE if p[4] == "EUR"]
    other = get(client, world, "summary", reporting_currency="EUR")
    assert other["headcount"] == len(eur_people)
    assert other["excluded_no_rate"] == len(PEOPLE) - len(eur_people)


# --- the employee_totals materialized view (Scale Lab E12) ---


def refresh_view(db):
    db.execute(text("SELECT refresh_employee_totals()"))


@pytest.fixture
def totals_view():
    """Read totals from the materialized view for the duration of one test."""
    settings = get_settings()
    settings.analytics_totals_view = True
    yield
    settings.analytics_totals_view = False


def test_the_stored_totals_give_the_same_answers_as_the_live_computation(
    db, client, world, totals_view
):
    views = [
        ("summary", {}),
        ("stats", {"group_by": "country"}),
        ("histogram", {"bins": 7}),
        ("outliers", {}),
        ("composition", {}),
    ]
    refresh_view(db)
    stored = [get(client, world, view, **params) for view, params in views]
    get_settings().analytics_totals_view = False
    live = [get(client, world, view, **params) for view, params in views]

    assert stored == live


def test_the_stored_totals_are_a_snapshot_until_refreshed(db, client, world, totals_view):
    refresh_view(db)
    snapshot = get(client, world, "summary")
    # A pay change after the snapshot (this person's housing allowance is replaced).
    add(client, world["people"]["gb1"], world["types"][("allowance", "housing")].id, world["reasons"]["new_hire"], HIRE, 10)

    before_refresh = get(client, world, "summary")
    refresh_view(db)
    after_refresh = get(client, world, "summary")

    assert before_refresh == snapshot  # still the old totals
    assert after_refresh["total_cost"] != snapshot["total_cost"]
    assert after_refresh["headcount"] == snapshot["headcount"]


# --- stored pay-change events (Scale Lab E26) ---


@pytest.fixture
def stored_events():
    """Read the pay-changes report from the stored events for the duration of one test."""
    settings = get_settings()
    settings.pay_change_events = True
    yield
    settings.pay_change_events = False


def live_and_stored(db, client, world, **params):
    """The same report computed live and read from the stored events."""
    settings = get_settings()
    settings.pay_change_events = False
    live = get(client, world, "changes", **params)
    settings.pay_change_events = True
    return live, get(client, world, "changes", **params)


def yesterday() -> date:
    return utc_today() - timedelta(days=1)


def dirty_employees(db) -> dict[int, date]:
    return {
        row.employee_id: row.from_day for row in db.execute(select(pay_changes.dirty))
    }


def test_the_report_from_stored_events_equals_the_live_report(
    db, client, world, stored_events
):
    today = apply_changes(client, world)
    pay_changes.backfill(db, yesterday())

    for params in (
        {"date_from": "2025-01-01", "date_to": today},  # reaches today's live tail
        {"date_from": "2025-01-01", "date_to": str(yesterday())},  # stored days only
        {"date_from": HIRE, "date_to": HIRE, "limit": 3},  # one stored day
        {"date_from": today, "date_to": today},  # the live tail alone
        {"date_from": "2025-01-01", "date_to": today, "country": "GB"},  # a filter
        {"date_from": "2025-01-01", "date_to": today, "limit": 1},
    ):
        live, stored = live_and_stored(db, client, world, **params)
        assert stored == live, params
    assert live["events"] > 0


def test_nothing_stored_yet_means_the_live_report_is_used(
    db, client, world, stored_events
):
    apply_changes(client, world)
    assert pay_changes.filled_through(db) is None

    live, stored = live_and_stored(
        db, client, world, date_from="2025-01-01", date_to=str(utc_today())
    )

    assert stored == live and stored["events"] > 0


def test_a_backdated_record_is_reflected_in_the_next_report(
    db, client, world, stored_events
):
    people, types, reasons = world["people"], world["types"], world["reasons"]
    base = types[("fixed", "base")].id
    add(client, people["gb1"], base, reasons["annual_revision"], "2025-04-01", 5500)
    pay_changes.backfill(db, yesterday())
    assert dirty_employees(db) == {}

    # A raise dated in the past arrives after the events were stored: gb1's later events
    # change too, since the total before them is now different.
    add(client, people["gb1"], base, reasons["promotion"], "2025-02-01", 5200)
    assert dirty_employees(db) == {people["gb1"]["id"]: date(2025, 2, 1)}

    live, stored = live_and_stored(
        db, client, world, date_from="2025-01-01", date_to=str(yesterday())
    )

    assert stored == live
    assert {
        i["effective_date"] for i in stored["items"] if i["first_name"] == "gb1"
    } >= {"2025-02-01", "2025-04-01"}
    assert dirty_employees(db) == {}  # recomputed, then cleared


def test_ordinary_writes_do_not_make_anyone_dirty(db, client, world, stored_events):
    people, types, reasons = world["people"], world["types"], world["reasons"]
    base = types[("fixed", "base")].id
    pay_changes.backfill(db, yesterday())

    add(client, people["gb1"], base, reasons["annual_revision"], str(utc_today()), 5500)
    add(
        client,
        people["gb2"],
        base,
        reasons["annual_revision"],
        str(utc_today() + timedelta(days=30)),
        5600,
    )

    assert dirty_employees(db) == {}


def test_the_daily_fill_appends_new_days_and_can_be_repeated(
    db, client, world, stored_events
):
    people, types, reasons = world["people"], world["types"], world["reasons"]
    base = types[("fixed", "base")].id
    pay_changes.backfill(db, yesterday() - timedelta(days=1))
    add(client, people["gb1"], base, reasons["annual_revision"], str(yesterday()), 5500)
    assert pay_changes.filled_through(db) == yesterday() - timedelta(days=1)

    stored = pay_changes.fill_through_yesterday(db)

    assert stored >= 1
    assert pay_changes.filled_through(db) == yesterday()
    # gb1's record was written moments ago and dated inside the days just filled, so the
    # fill lists them for a recheck, closing the window in which a concurrent writer is missed.
    # (Everyone in this test's world was also just created, so they are listed too.)
    assert people["gb1"]["id"] in dirty_employees(db)
    assert pay_changes.fill_through_yesterday(db) == 0  # nothing left to append
    live, stored_report = live_and_stored(
        db, client, world, date_from="2025-01-01", date_to=str(utc_today())
    )
    assert stored_report == live


def test_a_bulk_insert_of_backdated_records_adds_one_dirty_row_per_employee(
    db, client, world, stored_events
):
    people, types, reasons = world["people"], world["types"], world["reasons"]
    base = types[("fixed", "base")].id
    pay_changes.backfill(db, yesterday())
    add(client, people["in1"], base, reasons["annual_revision"], "2025-03-01", 420000)
    add(client, people["in1"], base, reasons["annual_revision"], "2025-01-01", 410000)
    add(client, people["in2"], base, reasons["annual_revision"], "2025-02-01", 420000)

    assert dirty_employees(db) == {
        people["in1"]["id"]: date(
            2025, 1, 1
        ),  # the earliest of in1's two backdated records
        people["in2"]["id"]: date(2025, 2, 1),
    }
