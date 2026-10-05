from decimal import Decimal as D

from app.domain.analytics import (
    MIN_PEER_GROUP_SIZE,
    EmployeePay,
    composition,
    find_outliers,
    peer_groups,
)


def pay(employee_id, total, title="Engineer", level="L3", country="IN"):
    return EmployeePay(employee_id, title, level, country, D(total))


def test_peer_group_is_title_level_and_country():
    groups = peer_groups(
        [pay(1, 100), pay(2, 100, country="US"), pay(3, 100, level="L4"), pay(4, 100)]
    )
    assert sorted(len(g) for g in groups.values()) == [1, 1, 2]


def test_outliers_use_80_and_120_percent_of_peer_median():
    # median 100 → band is [80, 120]; boundaries are not outliers
    group = [
        pay(1, 100),
        pay(2, 100),
        pay(3, 100),
        pay(4, 80),
        pay(5, 120),
        pay(6, 79),
        pay(7, 121),
    ]

    outliers = {o.employee_id: o for o in find_outliers(group)}

    assert set(outliers) == {6, 7}
    assert outliers[6].direction == "below"
    assert outliers[7].direction == "above"
    assert outliers[6].peer_median == D(100)


def test_small_peer_groups_are_not_evaluated():
    # The same extreme pay is flagged in a group of 5 but ignored in a group of 4.
    normal = [pay(i, 100) for i in range(MIN_PEER_GROUP_SIZE - 1)]
    extreme = pay(99, 1000)

    assert [o.employee_id for o in find_outliers([*normal, extreme])] == [99]
    assert find_outliers([*normal[:-1], extreme]) == []


def test_groups_are_evaluated_independently():
    india = [pay(i, 100) for i in range(5)]
    us = [
        pay(10 + i, 1000, country="US") for i in range(5)
    ]  # high, but normal for US peers
    assert find_outliers(india + us) == []


def test_composition_shares_sum_to_one():
    shares = composition(
        [("fixed", D(600)), ("bonus", D(200)), ("bonus", D(100)), ("equity", D(100))]
    )
    assert shares == {"fixed": D("0.6"), "bonus": D("0.3"), "equity": D("0.1")}
    assert sum(shares.values()) == 1


def test_composition_of_nothing_is_empty():
    assert composition([]) == {}
