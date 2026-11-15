import os

import httpx

from services.common.auth import TokenExchangeProvider
from services.common.llm import raise_upstream_llm_error
from services.common.tracing import observation, trace_headers


class SkillMemoryClient:
    def __init__(self, exchange: TokenExchangeProvider):
        self.exchange = exchange
        self.url = os.getenv("MEMORY_URL", "http://memory-api:8002")

    async def request(self, operation, payload, subject_token):
        token = await self.exchange.exchange(subject_token)
        with observation("skill.memory", input={"operation": operation, **payload}) as span:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{self.url}/api/v1/skills/{operation}",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}", **trace_headers()},
                )
            span.update(output=response.json(), metadata={"http_status": response.status_code})
            raise_upstream_llm_error(response)
            response.raise_for_status()
            return response.json()
