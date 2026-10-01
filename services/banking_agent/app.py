from __future__ import annotations

import asyncio
import os
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from services.banking_agent.clients import MemoryClient, TravelA2AClient
from services.banking_agent.graph import BankingGraph
from services.banking_agent.skills import BankingSkills
from services.common.auth import Principal, ServiceTokenProvider, require_principal
from services.common.llm import LLMClient, install_llm_error_handlers
from services.common.mcp import MCPClient
from services.common.schemas import (
    ChatRequest,
    ChatResponse,
    ExternalMemoryIngest,
    SharedMemoryWrite,
    VulnerableAbsRead,
)
from services.common.tracing import observation

tokens = ServiceTokenProvider()
memory = MemoryClient(tokens)
travel = TravelA2AClient(tokens)
llm = LLMClient("banking-agent")
skills = BankingSkills(memory, travel, tokens, llm)
banking_graph = BankingGraph(memory, skills, llm)
abs_mcp = MCPClient(os.getenv("ABS_MCP_URL", "http://abs-mcp:8101/mcp"))
abs_mcp_vulnerable = MCPClient(
    os.getenv("ABS_MCP_VULNERABLE_URL", "http://abs-mcp-vulnerable:8104/mcp")
)

app = FastAPI(title="Banking LangGraph Agent", version="0.1.0")
install_llm_error_handlers(app)


@app.exception_handler(httpx.TimeoutException)
async def dependency_timeout_handler(_: Request, __: httpx.TimeoutException) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": (
                "Сервис памяти или банковская интеграция не успели ответить. "
                "Повторите запрос."
            ),
            "code": "dependency_timeout",
        },
    )


@app.exception_handler(httpx.HTTPError)
async def dependency_error_handler(_: Request, __: httpx.HTTPError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Внутренний сервис агента временно недоступен. Повторите запрос.",
            "code": "dependency_unavailable",
        },
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
Caller = Annotated[Principal, Depends(require_principal)]


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "banking-agent", "llm": llm.public_config()}


@app.get("/v1/capabilities")
async def capabilities(_: Caller) -> dict[str, Any]:
    return {
        "agent_id": "banking-agent",
        "framework": "LangGraph",
        "llm": llm.public_config(),
        "skills": ["accounts", "operations", "investments", "financial-actions", "travel"],
        "protocols": {"tools": "MCP 2025-06-18", "agent_interop": "A2A 1.0 HTTP+JSON"},
        "memory_types": ["semantic", "procedural", "episodic", "working"],
    }


@app.get("/v1/dashboard")
async def dashboard(
    caller: Caller, authorization: str = Header(alias="Authorization")
) -> dict[str, Any]:
    _customer_id(caller)
    access_token = authorization.split(" ", 1)[1]
    with observation(
        "banking.dashboard", user_id=caller.customer_id, agent_id="banking-agent"
    ) as span:
        customer, accounts, operations, portfolio, products = await asyncio.gather(
            abs_mcp.call("get_customer_info", {}, access_token),
            abs_mcp.call("get_customer_accounts", {}, access_token),
            abs_mcp.call("get_operations_history", {"limit": 20}, access_token),
            abs_mcp.call("get_portfolio", {}, access_token),
            abs_mcp.call("list_investment_products", {}, access_token),
        )
        span.update(output={"accounts": len(accounts), "operations": len(operations)})
    return {
        "customer": customer,
        "accounts": accounts,
        "operations": operations,
        "portfolio": portfolio,
        "products": products,
    }


@app.get("/v1/conversations")
async def conversations(caller: Caller) -> dict[str, Any]:
    user_id = _customer_id(caller)
    with observation(
        "banking.conversations", user_id=user_id, agent_id="banking-agent"
    ) as span:
        rows = await memory.conversations(user_id)
        span.update(output={"count": len(rows)})
    return {"conversations": rows}


@app.get("/v1/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: str, caller: Caller) -> dict[str, Any]:
    user_id = _customer_id(caller)
    with observation(
        "banking.conversation-history",
        input={"conversation_id": conversation_id},
        user_id=user_id,
        session_id=conversation_id,
        agent_id="banking-agent",
    ) as span:
        rows = await memory.conversation_messages(user_id, conversation_id)
        span.update(output={"count": len(rows)})
    return {"conversation_id": conversation_id, "messages": rows}


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    caller: Caller,
    authorization: str = Header(alias="Authorization"),
) -> ChatResponse:
    user_id = _customer_id(caller)
    access_token = authorization.split(" ", 1)[1]
    graph_input = {
        "message": request.message,
        "user_id": user_id,
        "conversation_id": request.conversation_id,
        "access_token": access_token,
        "action": request.action.model_dump(mode="json") if request.action else None,
        "confirm_action_id": request.confirm_action_id,
        "travel": request.travel,
        "requires_confirmation": False,
        "tool_calls": [],
        "data": {},
    }
    with observation(
        "banking-agent.turn",
        input={"message": request.message, "has_action": bool(request.action)},
        metadata={"agent_id": "banking-agent"},
        user_id=user_id,
        session_id=request.conversation_id,
        agent_id="banking-agent",
    ) as span:
        try:
            result = await banking_graph.graph.ainvoke(graph_input)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        span.update(output={"intent": result["intent"], "tool_calls": result.get("tool_calls", [])})
    return ChatResponse(
        message=result["response"],
        conversation_id=request.conversation_id,
        intent=result["intent"],
        tool_calls=result.get("tool_calls", []),
        requires_confirmation=result.get("requires_confirmation", False),
        confirmation_id=result.get("confirmation_id"),
        data=result.get("data", {}),
    )


@app.post("/v1/lab/vulnerable-abs")
async def vulnerable_abs_read(
    payload: VulnerableAbsRead,
    _: Caller,
    authorization: str = Header(alias="Authorization"),
) -> dict[str, Any]:
    """Exercise broken access control without exposing destructive calls here."""
    access_token = authorization.split(" ", 1)[1]
    arguments: dict[str, Any] = {"customer_id": payload.customer_id}
    if payload.tool == "get_operations_history":
        arguments["limit"] = payload.limit
    result = await abs_mcp_vulnerable.call(payload.tool, arguments, access_token)
    return {
        "warning": "intentionally-vulnerable-broken-access-control",
        "tool": payload.tool,
        "selected_customer_id": payload.customer_id,
        "result": result,
    }


@app.post("/v1/lab/shared-memory")
async def write_shared_memory(payload: SharedMemoryWrite, caller: Caller) -> dict[str, Any]:
    user_id = _customer_id(caller)
    record = await memory.create_shared(
        source_user_id=user_id,
        memory_type=payload.memory_type,
        content=payload.content,
        metadata={
            "source": "user-chat-lab",
            "trust": "untrusted",
            "vulnerability": "cross-user-memory-poisoning",
        },
    )
    return {"warning": "shared-memory-is-visible-to-all-users", "memory": record}


@app.post("/v1/lab/ingest-external-memory")
async def ingest_external_memory(payload: ExternalMemoryIngest, caller: Caller) -> dict[str, Any]:
    """Deliberately persist untrusted MCP output without sanitizing instructions."""
    user_id = _customer_id(caller)
    result = await skills.external_web_search(payload.query)
    rows = result.get("results", [])
    content = "\n\n".join(
        f"TITLE: {row.get('title', '')}\nBODY: {row.get('body', '')}\nURL: {row.get('url', '')}"
        for row in rows
    )
    if not content:
        raise HTTPException(502, "External MCP returned no content to ingest")
    record = await memory.create_shared(
        source_user_id=user_id,
        memory_type="semantic",
        content=content,
        metadata={
            "source": "duckduckgo-mcp",
            "query": payload.query,
            "trust": "untrusted-external",
            "vulnerability": "indirect-prompt-injection-to-shared-memory",
        },
    )
    return {
        "warning": "untrusted-external-content-was-written-to-shared-memory",
        "provider": result.get("provider"),
        "memory": record,
    }


@app.get("/v1/lab/memory-recall")
async def lab_memory_recall(
    caller: Caller,
    query: str = "",
    conversation_id: str = "lab-recall",
) -> dict[str, Any]:
    user_id = _customer_id(caller)
    rows = await memory.recall(user_id, query, conversation_id, limit=30)
    return {"user_id": user_id, "memories": rows}


def _customer_id(caller: Principal) -> str:
    if not caller.customer_id:
        raise HTTPException(403, "Authenticated user is not linked to a bank customer")
    return caller.customer_id
