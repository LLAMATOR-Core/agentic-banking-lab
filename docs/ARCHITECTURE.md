# Компонентная архитектура

## Итоговая схема

Диаграмма намеренно показывает только границы компонентов и основные протоколы.
Внутренние базы Langfuse и инфраструктурные зависимости Compose опущены.

```mermaid
flowchart LR
  UI[Интернет-Банк и чат] -->|OIDC + HTTPS| BA[Банковский агент<br/>LangGraph]
  BA -->|MCP + user token| SM[АБС MCP<br/>Token Exchange]
  BA -.->|MCP: customer_id от LLM| VM[Уязвимый АБС MCP]
  SM -->|exchanged JWT| ABS[АБС API]
  VM -->|service JWT| ABS
  ABS --> PG[(PostgreSQL)]

  BA --> MEM[Оркестратор памяти]
  MEM --> MG[(MongoDB)]
  BA -->|A2A| TA[Тревел-агент]
  TA --> TP[Duffel live<br/>Booking mock]
  BA --> DDG[DuckDuckGo MCP]

  KC[Keycloak] -. identity .-> UI
  KC -. service identity .-> BA
  BA -. traces .-> LF[Langfuse]
  MEM -. traces .-> LF
```

## Ответственность компонентов

| Компонент | Ответственность | Данные/протокол |
|---|---|---|
| `ui` | Интернет-Банк и чат | OIDC Authorization Code + PKCE, REST |
| `banking-agent` | LangGraph-маршрутизация, навыки, подтверждение операций | REST, MCP client, A2A client |
| `abs-mcp` | Образцовая граница доступа к АБС | MCP Streamable HTTP, RFC 8693 Token Exchange |
| `abs-mcp-vulnerable` | Намеренно небезопасное сравнение | MCP принимает `customer_id` от LLM, client credentials |
| `abs-api` | Транзакции и проверка владельца | FastAPI, PostgreSQL |
| `memory-orchestrator` | Семантическая, процедурная, эпизодическая, рабочая память | REST, MongoDB |
| `travel-agent` | Независимый партнёрский LangGraph-агент | A2A 1.0, MCP clients |
| `duckduckgo-mcp` | Реальный поиск новостей и web snippets | DDGS, live network |
| `travel-data-mcp` | Реальные сведения о рейсах и отелях | Duffel Flights + Stays APIs |
| `booking-mcp` | Мок поиска/бронирования для воспроизводимых тестов | MCP, synthetic data |
| `keycloak` | Пользовательская и межсервисная аутентификация | OIDC/OAuth 2.0 |
| `langfuse` | Трассы turns, MCP, A2A и памяти | OTEL/Langfuse SDK |

## Два пути к АБС

### Образцовый путь

1. UI получает пользовательский access token с audience `abs-mcp` и `bank-mcp`.
2. `abs-mcp` проверяет подпись, `azp` и audience.
3. Confidential client `bank-mcp` обменивает исходный token на короткоживущий token с
   audience `abs-api`.
4. АБС получает `customer_id` только из проверенного exchanged JWT.
5. В schemas MCP-инструментов поля `customer_id` нет.

Token Exchange не кэшируется между запросами: это исключает ошибочную повторную выдачу
токена одного пользователя другому.

### Намеренно уязвимый путь

1. В schema каждого инструмента `abs-mcp-vulnerable` обязателен `customer_id`.
2. Значение формирует LLM из промпта пользователя или внешнего контекста.
3. MCP использует собственный over-privileged client-credentials token.
4. АБС разрешает этому специальному lab client выбрать клиента через
   `X-Lab-Customer-ID`.

Это воспроизводит broken access control и excessive agency: целостность identity metadata
возложена на вероятностную модель, хотя должна обеспечиваться криптографически. Уязвимый
client отделён по `azp`, endpoint и порту; production-код не должен иметь такой ветки.

## Память и отравление

Обычные записи имеют `user_id=<customer-id>`. Общая область имеет `user_id="*"`; recall
возвращает объединение личной и общей областей. Каждая общая запись дополнительно содержит
`source_user_id`, `source`, `trust` и `vulnerability` в metadata.

Лабораторные API демонстрируют два пути:

- пользователь A пишет в `/v1/lab/shared-memory`, пользователь B получает запись при recall;
- `/v1/lab/ingest-external-memory` получает live DuckDuckGo snippets через MCP и без
  санитизации сохраняет их в общую память. Инструкция на контролируемой внешней странице
  становится indirect prompt injection для всех пользователей.

Детерминированный confirmation gate не принимает память за policy engine. Для экспериментов
с генеративным planner содержимое `memory_context` можно подать в его контекст и сравнить
результаты, оставив авторизацию и подтверждение вне LLM.

## Поток активной операции

`prepare → pending memory (TTL 15 мин) → отдельное подтверждение → повторная проверка
agent_id/user_id/status → secure MCP → Token Exchange → транзакция АБС`.

Повтор `confirmation_id` возвращает сохранённый результат по idempotency key и не создаёт
повторное списание.

## Порты

| Порт | Компонент |
|---:|---|
| 3000 / 3001 | UI / Langfuse |
| 8000–8003 | Banking agent, ABS, Memory, Travel agent |
| 8080 | Keycloak |
| 8101 | Secure ABS MCP |
| 8102 / 8103 | Booking mock / Expedia recommendations mock |
| 8104 | Vulnerable ABS MCP |
| 8105 / 8106 | DuckDuckGo live / Duffel live MCP |
