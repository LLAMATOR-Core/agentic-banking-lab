# MCP и A2A

## MCP

Собственные MCP-серверы предоставляют stateless Streamable HTTP endpoint `POST /mcp`,
protocol version `2025-06-18`, и методы `initialize`, `tools/list`, `tools/call`. Внешний
Aviasales MCP использует stateful Streamable HTTP: клиент выполняет handshake, отправляет
`notifications/initialized` и сохраняет `Mcp-Session-Id`. Клиент понимает JSON и SSE ответы,
а также `structuredContent` и JSON внутри text content.

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
| `:8105/mcp` | live DuckDuckGo для Banking и Travel Agent | `news_search`, `text_search` |
| `:8106/mcp` | self-hosted Aviasales MCP | `search_flights` и 12 read-only lookup/price tools |
| `https://mcp.hasdata.com/mcp?apis=booking` | remote Booking MCP | search + property details |

## A2A 1.0

Travel agent публикует card на `/.well-known/agent-card.json`. Preferred interface — `HTTP+JSON`, protocol `1.0`, endpoint base `/a2a/v1`. Реализована обязательная для демо синхронная операция `POST /a2a/v1/message:send` с media type `application/a2a+json` и header `A2A-Version: 1.0`.

Это LLM-агент: перед MCP-вызовами модель формирует structured tool plan, а после получения
результатов выбирает и ранжирует ссылки. Оба LLM-шага используют официальный `openai` SDK и
`OPENAI_BASE_URL`. При недоступности модели A2A endpoint возвращает явный
`503/llm_unavailable`; банковский агент сохраняет эту ошибку до UI.

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

## Реальные MCP источников и mock booking

Для рейсов Compose собирает `stufently/aviasales-mcp` из закреплённого upstream commit и запускает его
в Streamable HTTP режиме на `:8106/mcp`. Travel Agent сначала вызывает `tools/list`, адаптирует
канонические поля к опубликованной schema, затем вызывает `search_flights`. Серверу нужен
`AVIASALES_API_TOKEN`; его Data API использует кэш недавних поисков, поэтому цена не означает
live-наличие. Сервер read-only и не бронирует.

Для отелей Travel Agent напрямую подключён к remote MCP
`https://mcp.hasdata.com/mcp?apis=booking` с `x-api-key`. Используется read-only tool
`hasdata_booking_search_getBookingSearchResults`; нужен `HASDATA_API_KEY`. Ответ HasData
разворачивается из MCP content, ограничивается по размеру и маркируется как
`untrusted-external` до передачи LLM.

`POST /v1/bookings/mock` реализован самим Travel Agent:
он создаёт идентификатор `mock-*`, возвращает `external_side_effect=false` и ничего не
отправляет поставщику. Планирование поездки мок-бронирование автоматически не вызывает.
Запрос о месте без дат и просьбы искать инвентарь получает `request_type=destination_info`.
Travel Agent вызывает `duckduckgo-mcp.text_search`, передаёт найденные snippets LLM и сам
формирует рекомендации. Списков достопримечательностей и destination-specific шаблонов в коде
нет. Для явного поиска билетов/отеля LLM извлекает IATA-коды, даты и остальные параметры; если
обязательных данных нет, модель формулирует уточняющий вопрос вместо подстановки маршрута.

DuckDuckGo вынесен из процесса агента в отдельный MCP. `mode=live` и
`content_trust=untrusted-external` сохраняются в structured result. Ошибка сети не
подменяется synthetic news: инвестиционные продукты/портфель продолжают работать, а
`news=[]` показывает деградацию.

`AVIASALES_API_TOKEN` принадлежит контейнеру Aviasales MCP и не передаётся Travel Agent.
Между Travel Agent и этим MCP используется отдельный bearer secret. `HASDATA_API_KEY`
необходим Travel Agent как credential удалённого MCP; redaction и запрет trace body для
headers обязательны.

Стабильность контракта позволяет студентам реализовать другой A2A-клиент или MCP-провайдер без доступа к коду соседнего агента.
