from typing import Any

import pytest

from services.banking_agent.skills import BankingSkills


class Dummy:
    pass


@pytest.fixture
def skills() -> BankingSkills:
    return BankingSkills(Dummy(), Dummy())  # type: ignore[arg-type]


def test_action_allowlist_drops_identity_injection(skills: BankingSkills) -> None:
    result = skills._validate_action(
        "transfer",
        {
            "from_account": "account-001",
            "to_account": "RU10000000000000000002",
            "amount": "10.50",
            "customer_id": "victim",
            "idempotency_key": "attacker-key",
        },
    )
    assert result == {
        "from_account": "account-001",
        "to_account": "RU10000000000000000002",
        "amount": "10.50",
    }


@pytest.mark.parametrize("value", ["0", "-1", "not-money"])
def test_action_rejects_bad_amount(skills: BankingSkills, value: Any) -> None:
    with pytest.raises(ValueError):
        skills._validate_action(
            "transfer",
            {"from_account": "a", "to_account": "b", "amount": value},
        )
