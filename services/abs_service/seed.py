from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from services.abs_service.db import SessionLocal
from services.abs_service.models import Account, Customer, InvestmentProduct, Operation, Position


async def _add_missing(session: AsyncSession, model: type[Any], rows: list[Any]) -> None:
    for row in rows:
        primary_key: Any = row.id
        if await session.get(model, primary_key) is None:
            session.add(row)


async def seed_database() -> None:
    """Idempotently provision all demo identities, including existing volumes."""
    async with SessionLocal() as session:
        customers = [
            Customer(
                id="customer-001",
                full_name="Петров Иван Сергеевич",
                gender="male",
                client_type="retail",
                vip=True,
                email="ivan.petrov@example.test",
                phone="+7 900 000-00-01",
            ),
            Customer(
                id="customer-002",
                full_name="Соколова Анна Викторовна",
                gender="female",
                client_type="retail",
                vip=False,
                email="anna.sokolova@example.test",
                phone="+7 900 000-00-02",
            ),
            Customer(
                id="customer-003",
                full_name="Орлов Михаил Андреевич",
                gender="male",
                client_type="premium",
                vip=True,
                email="mikhail.orlov@example.test",
                phone="+7 900 000-00-03",
            ),
            Customer(
                id="customer-004",
                full_name="Смирнова Елена Павловна",
                gender="female",
                client_type="retail",
                vip=False,
                email="elena.smirnova@example.test",
                phone="+7 900 000-00-04",
            ),
        ]
        accounts = [
            Account(
                id="account-001",
                customer_id="customer-001",
                number="RU10000000000000000001",
                kind="checking",
                currency="RUB",
                balance=Decimal("245320.40"),
            ),
            Account(
                id="account-002",
                customer_id="customer-001",
                number="RU10000000000000000002",
                kind="savings",
                currency="RUB",
                balance=Decimal("780000.00"),
            ),
            Account(
                id="account-003",
                customer_id="customer-001",
                number="RU10000000000000000003",
                kind="checking",
                currency="USD",
                balance=Decimal("1250.00"),
            ),
            Account(
                id="account-004",
                customer_id="customer-002",
                number="RU20000000000000000001",
                kind="checking",
                currency="RUB",
                balance=Decimal("113450.75"),
            ),
            Account(
                id="account-005",
                customer_id="customer-002",
                number="RU20000000000000000002",
                kind="savings",
                currency="RUB",
                balance=Decimal("350000.00"),
            ),
            Account(
                id="account-006",
                customer_id="customer-003",
                number="RU30000000000000000001",
                kind="checking",
                currency="RUB",
                balance=Decimal("920500.00"),
            ),
            Account(
                id="account-007",
                customer_id="customer-004",
                number="RU40000000000000000001",
                kind="checking",
                currency="RUB",
                balance=Decimal("68420.10"),
            ),
        ]
        products = [
            InvestmentProduct(
                id="ofz-26238",
                name="ОФЗ 26238",
                product_type="bond",
                currency="RUB",
                price=Decimal("982.40"),
                risk_level="low",
                description="Государственная облигация с фиксированным купоном.",
            ),
            InvestmentProduct(
                id="etf-moex",
                name="Фонд на индекс МосБиржи",
                product_type="fund",
                currency="RUB",
                price=Decimal("156.70"),
                risk_level="medium",
                description="Биржевой фонд широкого рынка российских акций.",
            ),
            InvestmentProduct(
                id="gold-rub",
                name="Фонд денежного золота",
                product_type="fund",
                currency="RUB",
                price=Decimal("212.10"),
                risk_level="medium",
                description="Инструмент, следующий за ценой золота; доходность не гарантирована.",
            ),
        ]
        now = datetime.now(UTC)
        operations = [
            Operation(
                id="operation-001",
                account_id="account-001",
                amount=Decimal("7350.00"),
                currency="RUB",
                direction="outgoing",
                category="groceries",
                counterparty="Перекрёсток",
                card_mask="**** 4242",
                description="Покупка",
                created_at=now - timedelta(days=1),
            ),
            Operation(
                id="operation-002",
                account_id="account-001",
                amount=Decimal("180000.00"),
                currency="RUB",
                direction="incoming",
                category="salary",
                counterparty="ООО Технологии",
                card_mask=None,
                description="Заработная плата",
                created_at=now - timedelta(days=3),
            ),
            Operation(
                id="operation-003",
                account_id="account-001",
                amount=Decimal("2890.50"),
                currency="RUB",
                direction="outgoing",
                category="transport",
                counterparty="РЖД",
                card_mask="**** 4242",
                description="Билет",
                created_at=now - timedelta(days=5),
            ),
            Operation(
                id="operation-004",
                account_id="account-004",
                amount=Decimal("4200.00"),
                currency="RUB",
                direction="outgoing",
                category="education",
                counterparty="Онлайн-курсы",
                card_mask="**** 1002",
                description="Подписка",
                created_at=now - timedelta(days=2),
            ),
            Operation(
                id="operation-005",
                account_id="account-006",
                amount=Decimal("310000.00"),
                currency="RUB",
                direction="incoming",
                category="salary",
                counterparty="АО Аналитика",
                card_mask=None,
                description="Заработная плата",
                created_at=now - timedelta(days=4),
            ),
            Operation(
                id="operation-006",
                account_id="account-007",
                amount=Decimal("1760.00"),
                currency="RUB",
                direction="outgoing",
                category="pharmacy",
                counterparty="Аптека",
                card_mask="**** 4004",
                description="Покупка",
                created_at=now - timedelta(days=1),
            ),
        ]
        positions = [
            Position(
                id="position-001",
                customer_id="customer-001",
                product_id="ofz-26238",
                quantity=Decimal("12"),
                average_price=Decimal("975.10"),
            ),
            Position(
                id="position-002",
                customer_id="customer-003",
                product_id="gold-rub",
                quantity=Decimal("30"),
                average_price=Decimal("205.00"),
            ),
        ]

        await _add_missing(session, Customer, customers)
        await session.flush()
        await _add_missing(session, Account, accounts)
        await _add_missing(session, InvestmentProduct, products)
        await session.flush()
        await _add_missing(session, Operation, operations)
        await _add_missing(session, Position, positions)
        await session.commit()
