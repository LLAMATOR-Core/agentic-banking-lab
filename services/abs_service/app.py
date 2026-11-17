from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.abs_service.db import SessionLocal, init_database
from services.abs_service.models import (
    Account,
    Customer,
    ExecutedAction,
    InvestmentProduct,
    Operation,
    Position,
)
from services.abs_service.seed import seed_database
from services.common.auth import Principal, require_principal, require_scope
from services.common.banking_policy import RESOURCE_SCOPES
from services.common.tracing import install_experiment_middleware, observation


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_database()
    await seed_database()
    yield


app = FastAPI(title="Automated Banking System", version="0.1.0", lifespan=lifespan)
install_experiment_middleware(app)


async def get_session():
    async with SessionLocal() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
Caller = Annotated[Principal, Depends(require_principal)]


def scoped_customer(
    caller: Caller,
    request: Request,
    x_lab_customer_id: str | None = Header(default=None, alias="X-Lab-Customer-ID"),
) -> str:
    """Resolve identity differently only to make the lab contrast explicit.

    The secure MCP's exchanged token carries customer_id. The deliberately
    vulnerable MCP authenticates as a privileged service and supplies a header
    copied from the LLM-controlled MCP argument.
    """
    if caller.authorized_party == "bank-mcp":
        scope = RESOURCE_SCOPES.get((request.method, request.url.path))
        with observation(
            "oauth.resource-check",
            user_id=caller.customer_id,
            input={
                "azp": caller.authorized_party,
                "resource": request.url.path,
                "method": request.method,
                "required_scope": scope,
            },
        ) as span:
            if scope is None:
                raise HTTPException(403, "Unknown ABS resource")
            try:
                require_scope(caller, scope)
            except HTTPException:
                span.update(output={"allowed": False})
                raise
            span.update(output={"allowed": bool(caller.customer_id)})
        if not caller.customer_id:
            raise HTTPException(403, "Exchanged token has no customer_id claim")
        return caller.customer_id
    if caller.authorized_party == "debug":
        return caller.customer_id
    if caller.authorized_party == "bank-mcp-vulnerable":
        if not x_lab_customer_id or len(x_lab_customer_id) > 64:
            raise HTTPException(400, "Vulnerable path requires X-Lab-Customer-ID")
        return x_lab_customer_id
    raise HTTPException(403, "Calling client cannot select an ABS customer")


CustomerId = Annotated[str, Depends(scoped_customer)]


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_account: str
    to_account: str
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    description: str = Field(default="Перевод", max_length=300)
    idempotency_key: str = Field(min_length=8, max_length=128)


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_account: str
    provider: str = Field(min_length=2, max_length=120)
    account_reference: str = Field(min_length=2, max_length=120)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    idempotency_key: str = Field(min_length=8, max_length=128)


class InvestmentBuyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_account: str
    product_id: str
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    idempotency_key: str = Field(min_length=8, max_length=128)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "abs-api"}


@app.get("/internal/customer")
async def customer_info(session: Session, customer_id: CustomerId, _: Caller) -> dict[str, Any]:
    customer = await session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    return {
        "id": customer.id,
        "full_name": customer.full_name,
        "gender": customer.gender,
        "client_type": customer.client_type,
        "vip": customer.vip,
        "email": customer.email,
        "phone": customer.phone,
    }


@app.get("/internal/accounts")
async def accounts(session: Session, customer_id: CustomerId, _: Caller) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            select(Account)
            .where(Account.customer_id == customer_id)
            .order_by(Account.currency, Account.number)
        )
    ).all()
    return [_account_dict(row) for row in rows]


@app.get("/internal/operations")
async def operations(
    session: Session,
    customer_id: CustomerId,
    _: Caller,
    account_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    owned_ids = select(Account.id).where(Account.customer_id == customer_id)
    query = select(Operation).where(Operation.account_id.in_(owned_ids))
    if account_id:
        query = query.where(Operation.account_id == account_id)
    rows = (await session.scalars(query.order_by(Operation.created_at.desc()).limit(limit))).all()
    return [_operation_dict(row) for row in rows]


@app.get("/internal/statement")
async def account_statement(
    session: Session,
    customer_id: CustomerId,
    _: Caller,
    account_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=1000, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "date_from must not exceed date_to")
    owned = select(Account).where(Account.customer_id == customer_id)
    if account_id:
        owned = owned.where(Account.id == account_id)
    account_rows = (await session.scalars(owned.order_by(Account.id))).all()
    if account_id and not account_rows:
        raise HTTPException(404, "Account not found")
    filters = [Operation.account_id.in_([row.id for row in account_rows])]
    tz = ZoneInfo("Europe/Moscow")
    if date_from:
        filters.append(
            Operation.created_at >= datetime.combine(date_from, time(), tz).astimezone(UTC)
        )
    if date_to:
        # Inclusive local dates, exclusive upper timestamp; safe for subsecond entries.
        filters.append(
            Operation.created_at
            < (datetime.combine(date_to, time(), tz).astimezone(UTC) + timedelta(days=1))
        )
    total = await session.scalar(select(func.count()).select_from(Operation).where(*filters))
    rows = (
        await session.scalars(
            select(Operation)
            .where(*filters)
            .order_by(Operation.created_at, Operation.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    amounts = (
        await session.execute(
            select(Operation.currency, Operation.direction, func.sum(Operation.amount))
            .where(*filters, Operation.status == "completed")
            .group_by(Operation.currency, Operation.direction)
            .order_by(Operation.currency, Operation.direction)
        )
    ).all()
    totals = {}
    for currency, direction, amount in amounts:
        values = totals.setdefault(
            currency, {"incoming": Decimal("0.00"), "outgoing": Decimal("0.00")}
        )
        values[direction] = amount
    return {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "timezone": "Europe/Moscow",
        "accounts_current": [_account_dict(row) for row in account_rows],
        "operations": [_operation_dict(row) for row in rows],
        "total_operations": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total,
        "totals_completed_by_currency": {
            currency: {key: str(value) for key, value in values.items()}
            for currency, values in totals.items()
        },
    }


def _operation_dict(row: Operation) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "amount": str(row.amount),
        "currency": row.currency,
        "direction": row.direction,
        "category": row.category,
        "counterparty": row.counterparty,
        "card_mask": row.card_mask,
        "description": row.description,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


@app.get("/internal/investments/products")
async def investment_products(session: Session, _: CustomerId, __: Caller) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            select(InvestmentProduct)
            .where(InvestmentProduct.active.is_(True))
            .order_by(InvestmentProduct.risk_level, InvestmentProduct.name)
        )
    ).all()
    return [_product_dict(row) for row in rows]


@app.get("/internal/investments/portfolio")
async def portfolio(session: Session, customer_id: CustomerId, _: Caller) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Position, InvestmentProduct)
            .join(InvestmentProduct, InvestmentProduct.id == Position.product_id)
            .where(Position.customer_id == customer_id)
        )
    ).all()
    return [
        {
            "position_id": position.id,
            "product": _product_dict(product),
            "quantity": str(position.quantity),
            "average_price": str(position.average_price),
            "market_value": str((position.quantity * product.price).quantize(Decimal("0.01"))),
        }
        for position, product in rows
    ]


@app.post("/internal/transfers")
async def transfer(
    payload: TransferRequest, session: Session, customer_id: CustomerId, _: Caller
) -> dict[str, Any]:
    existing = await session.get(ExecutedAction, payload.idempotency_key)
    if existing:
        _assert_same_owner(existing, customer_id)
        return existing.result

    source = await _owned_account_for_update(session, customer_id, payload.from_account)
    amount = _money(payload.amount)
    if source.balance < amount:
        raise HTTPException(409, "Insufficient funds")
    target = await session.scalar(
        select(Account).where(Account.number == payload.to_account).with_for_update()
    )
    if target and target.currency != source.currency:
        raise HTTPException(409, "Cross-currency transfer is not supported by this test tool")

    source.balance -= amount
    if target:
        target.balance += amount
    operation = Operation(
        account_id=source.id,
        amount=amount,
        currency=source.currency,
        direction="outgoing",
        category="transfer",
        counterparty=payload.to_account,
        description=payload.description,
    )
    session.add(operation)
    await session.flush()
    result = {
        "status": "completed",
        "operation_id": operation.id,
        "from_account": source.number,
        "to_account": payload.to_account,
        "amount": str(amount),
        "currency": source.currency,
        "balance_after": str(source.balance),
    }
    session.add(
        ExecutedAction(
            idempotency_key=payload.idempotency_key,
            customer_id=customer_id,
            action_type="transfer",
            request=payload.model_dump(mode="json"),
            result=result,
        )
    )
    await session.commit()
    return result


@app.post("/internal/payments")
async def pay_service(
    payload: PaymentRequest, session: Session, customer_id: CustomerId, _: Caller
) -> dict[str, Any]:
    existing = await session.get(ExecutedAction, payload.idempotency_key)
    if existing:
        _assert_same_owner(existing, customer_id)
        return existing.result
    source = await _owned_account_for_update(session, customer_id, payload.from_account)
    amount = _money(payload.amount)
    if source.balance < amount:
        raise HTTPException(409, "Insufficient funds")
    source.balance -= amount
    operation = Operation(
        account_id=source.id,
        amount=amount,
        currency=source.currency,
        direction="outgoing",
        category="service_payment",
        counterparty=payload.provider,
        description=f"{payload.provider}: {payload.account_reference}",
    )
    session.add(operation)
    await session.flush()
    result = {
        "status": "completed",
        "operation_id": operation.id,
        "provider": payload.provider,
        "amount": str(amount),
        "currency": source.currency,
        "balance_after": str(source.balance),
    }
    session.add(
        ExecutedAction(
            idempotency_key=payload.idempotency_key,
            customer_id=customer_id,
            action_type="service_payment",
            request=payload.model_dump(mode="json"),
            result=result,
        )
    )
    await session.commit()
    return result


@app.post("/internal/investments/buy")
async def buy_investment(
    payload: InvestmentBuyRequest, session: Session, customer_id: CustomerId, _: Caller
) -> dict[str, Any]:
    existing = await session.get(ExecutedAction, payload.idempotency_key)
    if existing:
        _assert_same_owner(existing, customer_id)
        return existing.result
    source = await _owned_account_for_update(session, customer_id, payload.from_account)
    product = await session.get(InvestmentProduct, payload.product_id)
    if not product or not product.active:
        raise HTTPException(404, "Investment product not found")
    if source.currency != product.currency:
        raise HTTPException(409, "Account and product currencies differ")
    cost = _money(product.price * payload.quantity)
    if source.balance < cost:
        raise HTTPException(409, "Insufficient funds")
    source.balance -= cost
    position = await session.scalar(
        select(Position)
        .where(Position.customer_id == customer_id, Position.product_id == product.id)
        .with_for_update()
    )
    if position:
        total_quantity = position.quantity + payload.quantity
        position.average_price = _money(
            ((position.average_price * position.quantity) + cost) / total_quantity
        )
        position.quantity = total_quantity
    else:
        position = Position(
            customer_id=customer_id,
            product_id=product.id,
            quantity=payload.quantity,
            average_price=product.price,
        )
        session.add(position)
    result = {
        "status": "completed",
        "product_id": product.id,
        "product_name": product.name,
        "quantity": str(payload.quantity),
        "price": str(product.price),
        "total": str(cost),
        "currency": product.currency,
        "balance_after": str(source.balance),
    }
    session.add(
        ExecutedAction(
            idempotency_key=payload.idempotency_key,
            customer_id=customer_id,
            action_type="investment_buy",
            request=payload.model_dump(mode="json"),
            result=result,
        )
    )
    await session.commit()
    return result


async def _owned_account_for_update(
    session: AsyncSession, customer_id: str, account_reference: str
) -> Account:
    account = await session.scalar(
        select(Account)
        .where(
            Account.customer_id == customer_id,
            (Account.id == account_reference) | (Account.number == account_reference),
        )
        .with_for_update()
    )
    if not account:
        raise HTTPException(404, "Source account not found for this customer")
    if account.status != "active":
        raise HTTPException(409, "Source account is not active")
    return account


def _money(value: Decimal) -> Decimal:
    try:
        return value.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise HTTPException(400, "Invalid money amount") from exc


def _assert_same_owner(action: ExecutedAction, customer_id: str) -> None:
    if action.customer_id != customer_id:
        raise HTTPException(409, "Idempotency key belongs to another customer")


def _account_dict(row: Account) -> dict[str, Any]:
    return {
        "id": row.id,
        "number": row.number,
        "masked_number": f"•••• {row.number[-4:]}",
        "kind": row.kind,
        "currency": row.currency,
        "balance": str(row.balance),
        "status": row.status,
    }


def _product_dict(row: InvestmentProduct) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "product_type": row.product_type,
        "currency": row.currency,
        "price": str(row.price),
        "risk_level": row.risk_level,
        "description": row.description,
    }
