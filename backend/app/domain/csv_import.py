"""CSV import validation (FR-5). Pure functions: no database or network access.

The caller supplies everything that would need the database (known companies,
departments, job titles and levels, currencies, countries, existing emails), so the whole file is validated in memory and
every problem is reported with its row, column and reason before anything is saved.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from email_validator import EmailNotValidError, validate_email

MAX_ROWS = 10_000
HEADER_ROW = 1  # spreadsheet numbering: the header is row 1, data starts at row 2

TEMPLATE_COLUMNS = (
    "first_name",
    "last_name",
    "email",
    "company",
    "department",
    "job_title",
    "job_level",
    "country",
    "hire_date",
    "currency",
    "base_pay_amount",
)

MISSING_COLUMN = "missing column"
TOO_MANY_ROWS = "file has {count} rows; the maximum is {limit}"
REQUIRED = "required"
INVALID_EMAIL = "invalid email address"
EMAIL_EXISTS = "an employee with this email already exists"
EMAIL_DUPLICATED = "email also appears on row {row}"
UNKNOWN_COMPANY = "unknown company"
UNKNOWN_DEPARTMENT = "unknown department"
UNKNOWN_JOB_TITLE = "unknown job title"
UNKNOWN_JOB_LEVEL = "unknown job level"
UNKNOWN_COUNTRY = "unknown country"
UNSUPPORTED_CURRENCY = "unsupported currency"
INVALID_DATE = "invalid date, expected YYYY-MM-DD"
INVALID_AMOUNT = "invalid amount"
AMOUNT_NOT_POSITIVE = "base pay amount must be greater than zero"
TOO_MANY_DECIMALS = "amount can have at most 2 decimal places"


@dataclass(frozen=True)
class RowError:
    row: int
    column: str
    reason: str


@dataclass(frozen=True)
class ImportRow:
    row: int
    first_name: str
    last_name: str
    email: str
    company_id: int
    department_id: int
    job_title_id: int
    job_level_id: int
    country: str
    hire_date: date
    currency: str
    base_pay_amount: Decimal


@dataclass(frozen=True)
class ImportContext:
    """Everything validation needs from the database, loaded once by the caller."""

    company_ids: Mapping[str, int]  # company name (any case) -> id
    # Phase 7A reference lists, matched ignoring case. An unknown value is a row
    # error: an import never creates departments, titles or levels.
    department_ids: Mapping[str, int]
    job_title_ids: Mapping[str, int]
    job_level_ids: Mapping[str, int]  # level code -> id
    countries: frozenset[str]
    currencies: frozenset[str]
    existing_emails: frozenset[str]  # lowercase

    @classmethod
    def build(
        cls,
        companies: Mapping[str, int],
        countries: Iterable[str],
        currencies: Iterable[str],
        existing_emails: Iterable[str],
        departments: Mapping[str, int],
        job_titles: Mapping[str, int],
        job_levels: Mapping[str, int],
    ) -> "ImportContext":
        def by_name(entries: Mapping[str, int]) -> dict[str, int]:
            return {name.casefold(): id_ for name, id_ in entries.items()}

        return cls(
            company_ids=by_name(companies),
            department_ids=by_name(departments),
            job_title_ids=by_name(job_titles),
            job_level_ids=by_name(job_levels),
            countries=frozenset(c.upper() for c in countries),
            currencies=frozenset(c.upper() for c in currencies),
            existing_emails=frozenset(e.casefold() for e in existing_emails),
        )


@dataclass
class ImportResult:
    rows: list[ImportRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_import(
    header: Iterable[str], records: list[Mapping[str, str]], context: ImportContext
) -> ImportResult:
    """Validate a whole file. All-or-nothing: callers save `rows` only when `ok`."""
    result = ImportResult()

    present = {h.strip() for h in header}
    for column in TEMPLATE_COLUMNS:
        if column not in present:
            result.errors.append(RowError(HEADER_ROW, column, MISSING_COLUMN))
    if len(records) > MAX_ROWS:
        result.errors.append(
            RowError(
                HEADER_ROW, "", TOO_MANY_ROWS.format(count=len(records), limit=MAX_ROWS)
            )
        )
    if result.errors:
        return result

    first_seen: dict[str, int] = {}
    for index, record in enumerate(records):
        row = HEADER_ROW + 1 + index
        parsed, errors = _validate_row(row, record, context, first_seen)
        result.errors.extend(errors)
        if parsed is not None:
            result.rows.append(parsed)
    return result


def _validate_row(
    row: int,
    record: Mapping[str, str],
    context: ImportContext,
    first_seen: dict[str, int],
) -> tuple[ImportRow | None, list[RowError]]:
    errors: list[RowError] = []

    def fail(column: str, reason: str) -> None:
        errors.append(RowError(row, column, reason))

    values = {c: (record.get(c) or "").strip() for c in TEMPLATE_COLUMNS}
    for column, value in values.items():
        if not value:
            fail(column, REQUIRED)

    email = values["email"]
    if email:
        try:
            email = validate_email(email, check_deliverability=False).normalized
        except EmailNotValidError:
            fail("email", INVALID_EMAIL)
        else:
            key = email.casefold()
            if key in context.existing_emails:
                fail("email", EMAIL_EXISTS)
            elif key in first_seen:
                fail("email", EMAIL_DUPLICATED.format(row=first_seen[key]))
            else:
                first_seen[key] = row

    def lookup(column: str, ids: Mapping[str, int], reason: str) -> int | None:
        found = ids.get(values[column].casefold())
        if values[column] and found is None:
            fail(column, reason)
        return found

    company_id = lookup("company", context.company_ids, UNKNOWN_COMPANY)
    department_id = lookup("department", context.department_ids, UNKNOWN_DEPARTMENT)
    job_title_id = lookup("job_title", context.job_title_ids, UNKNOWN_JOB_TITLE)
    job_level_id = lookup("job_level", context.job_level_ids, UNKNOWN_JOB_LEVEL)

    country = values["country"].upper()
    if country and country not in context.countries:
        fail("country", UNKNOWN_COUNTRY)

    currency = values["currency"].upper()
    if currency and currency not in context.currencies:
        fail("currency", UNSUPPORTED_CURRENCY)

    hire_date = None
    if values["hire_date"]:
        try:
            hire_date = date.fromisoformat(values["hire_date"])
        except ValueError:
            fail("hire_date", INVALID_DATE)

    amount = None
    if values["base_pay_amount"]:
        try:
            amount = Decimal(values["base_pay_amount"].replace(",", ""))
        except InvalidOperation:
            fail("base_pay_amount", INVALID_AMOUNT)
        else:
            if not amount.is_finite():
                fail("base_pay_amount", INVALID_AMOUNT)
            elif amount <= 0:
                fail("base_pay_amount", AMOUNT_NOT_POSITIVE)
            elif amount.as_tuple().exponent < -2:
                fail("base_pay_amount", TOO_MANY_DECIMALS)

    if errors:
        return None, errors
    return (
        ImportRow(
            row=row,
            first_name=values["first_name"],
            last_name=values["last_name"],
            email=email,
            company_id=company_id,
            department_id=department_id,
            job_title_id=job_title_id,
            job_level_id=job_level_id,
            country=country,
            hire_date=hire_date,
            currency=currency,
            base_pay_amount=amount,
        ),
        [],
    )
