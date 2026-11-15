"""Statement amounts cover the requested period, not just the returned page."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.abs_service.app import account_statement
from services.abs_service.models import Account, Base, Customer, Operation


@pytest.fixture
def ledger():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for customer in ("alice", "bob"):
            session.add(
                Customer(
                    id=customer,
                    full_name=customer,
                    gender="other",
                    client_type="retail",
                    email=f"{customer}@example.test",
                    phone="0",
                )
            )
        for account, owner, currency in [
            ("rub", "alice", "RUB"),
            ("usd", "alice", "USD"),
            ("foreign", "bob", "RUB"),
        ]:
            session.add(
                Account(
                    id=account,
                    customer_id=owner,
                    number=account,
                    kind="current",
                    currency=currency,
                    balance=Decimal("1000.00"),
                )
            )
        for op_id, account, currency, direction, amount, created, status in [
            ("before", "rub", "RUB", "outgoing", "99", "2026-11-01T20:59:59", "completed"),
            ("start", "rub", "RUB", "incoming", "50", "2026-11-01T21:00:00", "completed"),
            ("expense", "rub", "RUB", "outgoing", "12.34", "2026-11-02T12:00:00", "completed"),
            ("dollars", "usd", "USD", "outgoing", "4.56", "2026-11-02T13:00:00", "completed"),
            ("pending", "rub", "RUB", "outgoing", "77", "2026-11-02T14:00:00", "pending"),
            ("end", "rub", "RUB", "outgoing", "88", "2026-11-02T21:00:00", "completed"),
            ("other", "foreign", "RUB", "incoming", "999", "2026-11-02T12:00:00", "completed"),
        ]:
            session.add(
                Operation(
                    id=op_id,
                    account_id=account,
                    currency=currency,
                    direction=direction,
                    amount=Decimal(amount),
                    created_at=datetime.fromisoformat(created).replace(tzinfo=UTC),
                    status=status,
                    category="test",
                    counterparty="test",
                )
            )
        session.commit()

        class AsyncSessionAdapter:
            async def scalars(self, query):
                return session.scalars(query)

            async def scalar(self, query):
                return session.scalar(query)

            async def execute(self, query):
                return session.execute(query)

        yield AsyncSessionAdapter()
    engine.dispose()


async def statement(ledger, **kwargs):
    return await account_statement(
        ledger,
        "alice",
        None,
        account_id=kwargs.get("account_id"),
        date_from=date(2026, 11, 2),
        date_to=date(2026, 11, 2),
        limit=kwargs.get("limit", 1000),
        offset=kwargs.get("offset", 0),
    )


@pytest.mark.asyncio
async def test_statement_owns_accounts_applies_local_dates_and_totals_full_period(ledger):
    result = await statement(ledger, limit=1)
    assert [row["id"] for row in result["accounts_current"]] == ["rub", "usd"]
    assert [row["id"] for row in result["operations"]] == ["start"]
    assert result["total_operations"] == 4 and result["has_more"] is True
    assert result["totals_completed_by_currency"] == {
        "RUB": {"incoming": "50.00", "outgoing": "12.34"},
        "USD": {"incoming": "0.00", "outgoing": "4.56"},
    }
    next_page = await statement(ledger, offset=1)
    assert {row["id"] for row in next_page["operations"]} == {"expense", "dollars", "pending"}
    assert next_page["has_more"] is False
    assert next_page["totals_completed_by_currency"] == result["totals_completed_by_currency"]
    rub = await statement(ledger, account_id="rub")
    assert rub["total_operations"] == 3
    assert set(rub["totals_completed_by_currency"]) == {"RUB"}


@pytest.mark.asyncio
async def test_statement_rejects_foreign_account_and_reversed_period(ledger):
    with pytest.raises(HTTPException) as error:
        await statement(ledger, account_id="foreign")
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        await account_statement(
            ledger,
            "alice",
            None,
            date_from=date(2026, 11, 3),
            date_to=date(2026, 11, 2),
        )
    assert error.value.status_code == 400
