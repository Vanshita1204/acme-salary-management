from app.seed import iso_data


def test_every_country_has_a_known_default_currency():
    currencies = iso_data.currencies()
    for code, (name, currency) in iso_data.countries().items():
        assert len(code) == 2 and code.isupper(), code
        assert name
        assert currency in currencies, (code, currency)


def test_currencies_are_iso_4217_codes_with_names():
    for code, name in iso_data.currencies().items():
        assert len(code) == 3 and code.isupper(), code
        assert name


def test_known_mappings():
    countries = iso_data.countries()
    assert countries["US"] == ("United States", "USD")
    assert countries["IN"] == ("India", "INR")
    assert countries["DE"][1] == "EUR"


def test_overrides_win_over_cldr():
    assert iso_data.countries()["BG"][1] == "EUR"


def test_countries_without_currency_are_skipped():
    assert "AQ" not in iso_data.countries()  # Antarctica: no legal tender
