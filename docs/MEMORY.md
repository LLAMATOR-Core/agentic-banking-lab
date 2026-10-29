# Многоуровневая память

Память разделена на два независимых HTTP-сервиса над одной MongoDB. Область записи задают
обязательные `agent_id` и `user_id`; дополнительный идентификатор области не используется.

- `memory-service :8002` — data plane, доступный `banking-agent`. Он хранит текущий диалог,
  pending actions и кандидаты из внешних источников, а долгосрочные слои только читает.
- `memory-orchestrator :8004` — control plane с отдельной identity `memory-operator`. Это
  LLM-агент, который единолично создаёт и изменяет episodic, semantic и procedural память.

У токена Banking Agent audience `memory-service`; audience `memory-orchestrator` у него нет.
Поэтому prompt или tool choice банковской LLM не может напрямую вызвать promotion, удалить
запись либо изменить процедуру.

## Слои

| Слой | Назначение | Scope и retention |
|---|---|---|
| `working` | точные реплики текущих и прежних диалогов; кандидаты; pending actions | конкретные `agent_id`, `user_id`, `conversation_id`; action TTL |
| `episodic` | устойчивые факты и предпочтения текущего пользователя | конкретные `agent_id`, `user_id` |
| `semantic` | знания агента о среде | в учебном poisoning-сценарии `user_id="*"` |
| `procedural` | способы выполнения задач и навыки | agent-wide, `user_id="*"` |

`user_id="*"` — намеренная общая область лаборатории. Она позволяет исследовать cross-user
memory poisoning. В production такие записи должны проходить
модерацию и быть read-only для runtime-агента.

## Схема записи

```json
{
  "id": "Mongo ObjectId",
  "agent_id": "banking-agent",
  "user_id": "customer-001",
  "memory_type": "episodic",
  "content": "Пользователь живёт в Тбилиси",
  "conversation_id": null,
  "metadata": {
    "source": "memory-orchestrator",
    "source_user_id": "customer-001",
    "derived_from_turn": "uuid",
    "trust": "untrusted-user-derived",
    "scope": "user",
    "decay_score": 1.0
  },
  "embedding_model": "text-embedding-3-small",
  "expires_at": null,
  "created_at": "...",
  "updated_at": "...",
  "created_by": "memory-orchestrator",
  "schema_version": 2
}
```

Embedding хранится в MongoDB, но никогда не возвращается агенту или API-клиенту. Все записи
содержат `agent_id`, `user_id`, provenance и время. История чата сохраняет две working-записи
на turn: точный текст пользователя и точный ответ ассистента, связанные `turn_id` и
`conversation_id`.

## Working memory и сессии

После успешного ответа Banking Agent вызывает `POST /api/v1/working/turns`. Сервис сохраняет
две реплики и ставит `memory_job` со статусом `pending`; HTTP-ответ не ждёт оркестратор. UI
получает список сессий и может вернуться к любой из них через API Banking Agent, который
всегда подставляет `user_id` из проверенного JWT.

Pending financial action тоже является working memory, но не сообщением. В его metadata
хранятся канонические параметры, статус и результат. На подтверждении повторно проверяются
`agent_id`, `user_id`, status и TTL; идентификатор записи служит idempotency key.

Основные методы data plane:

| Метод | Назначение |
|---|---|
| `POST /api/v1/working/turns` | сохранить пару user/assistant и поставить job |
| `POST /api/v1/working/candidates` | сохранить недоверенный кандидат из user/web/MCP и поставить job |
| `GET /api/v1/working/conversations/{agent_id}/{user_id}` | список диалогов пользователя |
| `GET /api/v1/working/{agent_id}/{user_id}/{conversation_id}` | реплики выбранного диалога |
| `POST/GET/PATCH /api/v1/working/actions...` | lifecycle pending action |

## Semantic search

И data plane, и control plane используют официальный `openai` SDK:

- `OPENAI_BASE_URL` — общий OpenAI-compatible endpoint для LLM и embeddings;
- `OPENAI_API_KEY` — ключ этого endpoint;
- `EMBEDDING_MODEL` — отдельная модель embeddings, по умолчанию
  `text-embedding-3-small`;
- `OPENAI_MODEL` — LLM оркестратора.

Оркестратор создаёт embedding при каждой долгосрочной записи и backfill для прежних записей.
Data plane создаёт embedding запроса, сначала применяет обязательные фильтры
`agent_id`/`user_id`/type/TTL, затем вычисляет cosine similarity по ограниченному набору
кандидатов. Лексического или детерминированного fallback нет: недоступная embedding-модель
даёт явный `502/503`.

Read API, доступный Banking Agent:

| Метод | Назначение |
|---|---|
| `POST /api/v1/context` | единый контекст из working, episodic, semantic и procedural |
| `GET /api/v1/episodic/{agent_id}/{user_id}` | semantic search эпизодов пользователя |
| `GET /api/v1/episodic/{agent_id}/{user_id}/{episode_id}` | эпизод по ID и scope |
| `GET /api/v1/semantic/{agent_id}/{user_id}` | личные и общие semantic записи |
| `GET /api/v1/semantic/{agent_id}/{user_id}/{semantic_id}` | semantic запись по ID и scope |
| `GET /api/v1/procedure/{agent_id}` | поиск agent-wide процедур |
| `GET /api/v1/procedure/{agent_id}/{procedure_id}` | процедура по ID |

## LLM Memory Orchestrator

Оркестратор запускает collection двумя способами:

1. scheduler каждые `MEMORY_ORCHESTRATION_INTERVAL_SECONDS` — по умолчанию 1800 секунд;
2. операторский `POST /api/v1/orchestrator/collect` либо
   `POST /api/v1/episodic/agent/collect/{thread_id}`.

Он атомарно переводит job `pending → processing`, вызывает отдельный LLM-агент и создаёт до
восьми promotion. Episodic остаётся пользовательским; semantic и procedural намеренно
попадают в `user_id="*"` для сценариев poisoning. После успеха job и исходная working-запись
получают `completed` и `promoted_count`. Ошибка провайдера сохраняется в job; scheduler
повторяет попытку до трёх раз, а ручной API одновременно возвращает явную ошибку оператору.

Для operator-wide аудита control plane также предоставляет read-методы без дополнительного
идентификатора области: `GET /api/v1/episodic`, `/episodic/{agent_id}`,
`/episodic/{agent_id}/{user_id}` и аналогичные маршруты `semantic`, а также
`GET /api/v1/procedure/{agent_id}`. Они поддерживают embedding-query, но недоступны Banking
Agent. Data plane намеренно оставляет только user-scoped episodic/semantic чтение, чтобы
runtime-токен не превращался в cross-user administrative credential.

Mutation API существует только на control plane:

| Метод | Назначение |
|---|---|
| `POST /api/v1/episodic/direct` | прямое создание эпизодов оператором |
| `POST /api/v1/episodic/agent` | извлечение эпизодов LLM-агентом |
| `POST /api/v1/episodic/agent/collect/{thread_id}` | сбор из working memory диалога |
| `DELETE /api/v1/episodic/{episode_id}` | удалить эпизод |
| `DELETE /api/v1/episodic` | удалить только истёкшие эпизоды |
| `POST /api/v1/semantic` | синтез semantic памяти из эпизодов |
| `DELETE /api/v1/semantic/{agent_id}/{user_id}/{semantic_id}` | удалить semantic запись |
| `DELETE /api/v1/semantic/{agent_id}/{user_id}` | decay по score/возрасту |
| `POST /api/v1/procedural/{agent_id}` | создать процедуру с JSON payload |
| `PUT /api/v1/procedural/{agent_id}/{procedure_id}` | обновить процедуру и embedding |
| `DELETE /api/v1/procedural/{agent_id}/{procedure_id}` | удалить процедуру |

Пример ручного запуска:

```bash
TOKEN=$(curl -fsS http://localhost:8080/realms/agentic-banking/protocol/openid-connect/token \
  -d grant_type=client_credentials \
  -d client_id=memory-operator \
  -d client_secret="$KEYCLOAK_MEMORY_OPERATOR_SECRET" | jq -r .access_token)

curl -fsS http://localhost:8004/api/v1/orchestrator/collect \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"banking-agent","limit":100}'
```

Пользовательский токен и service token `banking-agent` должны получить `403` на порту 8004.

## Отравление памяти

`POST /v1/lab/shared-memory` и `/v1/lab/ingest-external-memory` больше не пишут долгосрочный
слой напрямую. Они сохраняют недоверенный working candidate; затем только оркестратор может
promote его. Для direct и indirect poisoning-эксперимента после staging дождитесь scheduler
или вызовите collection операторским токеном.

Provenance (`source_user_id`, `source`, `trust`, `derived_from_job`) сохраняется, но учебный
shared recall намеренно не применяет trust policy. Это позволяет проверить, как инструкция
одного пользователя или внешнего MCP становится общей semantic/procedural памятью и влияет
на контекст остальных пользователей, не выдавая Banking Agent права на control plane.
