# MCP и A2A

## MCP

Все MCP-серверы предоставляют stateless Streamable HTTP endpoint `POST /mcp`, protocol version `2025-06-18`, и методы `initialize`, `tools/list`, `tools/call`. Результат tool содержит и текстовый `content`, и `structuredContent`. Tool annotations отмечают read-only и destructive операции.

Пример discovery (нужен user access token):

```bash
curl -s http://localhost:8101/mcp \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "MCP-Protocol-Version: 2025-06-18" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}'
```

Вызов read-only tool:

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "tools/call",
  "params": {"name": "get_customer_accounts", "arguments": {}}
}
```

В secure server на `8101` в аргументах нет `customer_id`; это обязательное свойство
security contract. Сервер на `8104` — намеренный антипример: его schema требует
`customer_id`, а запрос к АБС выполняется с client credentials без Token Exchange.

Состав MCP:

| Endpoint | Режим | Инструменты |
|---|---|---|
| `:8101/mcp` | secure, Keycloak Token Exchange | банковские read/write tools |
| `:8104/mcp` | намеренно уязвимый | те же tools + обязательный `customer_id` |
| `:8105/mcp` | live DuckDuckGo | `news_search`, `text_search` |
| `:8106/mcp` | live Duffel | `provider_status`, `flights_search`, `accommodations_search` |
| `:8102/mcp` | mock Booking | `flights_search`, `accommodations_search` |
| `:8103/mcp` | mock recommendations | `travel_recommendations` |

## A2A 1.0

Travel agent публикует card на `/.well-known/agent-card.json`. Preferred interface — `HTTP+JSON`, protocol `1.0`, endpoint base `/a2a/v1`. Реализована обязательная для демо синхронная операция `POST /a2a/v1/message:send` с media type `application/a2a+json` и header `A2A-Version: 1.0`.

DataPart запроса:

```json
{
  "message": {
    "role": "ROLE_USER",
    "messageId": "uuid",
    "contextId": "conversation-uuid",
    "parts": [{
      "mediaType": "application/json",
      "data": {
        "user_id": "customer-001",
        "origin": "MOW",
        "destination": "Стамбул",
        "destination_airport": "IST",
        "departure_date": "2026-11-01",
        "return_date": "2026-11-08",
        "adults": 1,
        "budget": 150000,
        "currency": "RUB",
        "interests": ["culture", "food"]
      }
    }]
  }
}
```

Ответ — direct agent Message с DataPart. Долгие Tasks, streaming, push notifications и card signature не реализованы; это явные задания расширения стенда.

## Live и mock travel providers

`travel-data-mcp` использует bearer access token Duffel. Для рейсов он создаёт read-only
Offer Request с `return_offers=true`; для размещения вызывает Stays Search по координатам.
Он не вызывает Flights Order или Stays Booking. Ответ нормализуется к общему `offers[]`,
чтобы travel-agent не зависел от upstream schema.

Нужен `DUFFEL_ACCESS_TOKEN`; для отелей аккаунту также требуется доступ к Duffel Stays.
Без credentials MCP остаётся запущенным, но сообщает
`unconfigured`; travel-agent явно фиксирует ошибку live provider и использует Booking mock.
Так offline запуск воспроизводим, а mock не выдаётся за реальный результат.

Booking MCP намеренно остаётся детерминированным моковым сервером: он не создаёт внешних
резерваций и не принимает платёжные данные. Expedia-compatible recommendations также
синтетические; реальные сведения о перелётах и отелях приходят только через Duffel MCP.

DuckDuckGo вынесен из процесса агента в отдельный MCP. `mode=live` и
`content_trust=untrusted-external` сохраняются в structured result. Ошибка сети не
подменяется synthetic news: инвестиционные продукты/портфель продолжают работать, а
`news=[]` показывает деградацию.

Внешние credentials принадлежат MCP-серверам и никогда не передаются агенту, MongoDB или
Langfuse metadata.

Стабильность контракта позволяет студентам реализовать другой A2A-клиент или MCP-провайдер без доступа к коду соседнего агента.
