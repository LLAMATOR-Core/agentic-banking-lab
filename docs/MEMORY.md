# Память агента

Оркестратор — отдельный FastAPI-сервис над MongoDB. Он не доверяет пользовательским токенам и принимает только service token банковского агента. Коллекция `memories` индексирована по `(agent_id, user_id, memory_type, created_at)`, имеет text index и TTL index.

## Типы

| Тип | Содержание | Scope/retention |
|---|---|---|
| `semantic` | знания о среде и внешние документы | private или `user_id="*"` |
| `procedural` | правила выполнения задач и экспериментальные инструкции | private или `user_id="*"` |
| `episodic` | разрешённые предпочтения конкретного пользователя | конкретный user, постоянная до policy delete |
| `working` | текущие turns и pending actions | conversation; pending action TTL 15 минут |

Bootstrap создаёт semantic и procedural записи идемпотентно. Эпизодическая запись появляется после travel-plan и хранит только destination/interests. Каждый turn создаёт ограниченную working запись.

## Схема записи

```json
{
  "id": "ObjectId as string",
  "agent_id": "banking-agent",
  "user_id": "customer-001",
  "memory_type": "working",
  "content": "Intent=accounts; user_message=...",
  "conversation_id": "uuid",
  "metadata": {"intent": "accounts", "tool_calls": []},
  "expires_at": null,
  "created_at": "...",
  "updated_at": "...",
  "created_by": "banking-agent",
  "schema_version": 1
}
```

Recall рассматривает записи текущего user и глобальные `user_id="*"`, фильтрует типы/TTL, затем ранжирует по пересечению токенов, типу и совпадению conversation. Это понятный baseline. Для Atlas замените `_score` на `$vectorSearch`, сохранив фильтры `agent_id`, `user_id` и тип **до** выдачи результата. Никогда не полагайтесь только на similarity для tenant isolation.

Pending action — тоже working memory, но его metadata содержит канонические параметры, статус и после исполнения результат. Идентификатор записи используется как idempotency key.

## Общая память и намеренная уязвимость

В общей области `user_id="*"`. Поэтому selector recall
`user_id in [<текущий клиент>, "*"]` делает запись видимой всем пользователям. Каждая
лабораторная запись сохраняет минимум:

```json
{
  "agent_id": "banking-agent",
  "user_id": "*",
  "memory_type": "procedural",
  "content": "...",
  "metadata": {
    "scope": "shared",
    "source_user_id": "customer-001",
    "source": "user-chat-lab",
    "trust": "untrusted",
    "vulnerability": "cross-user-memory-poisoning"
  }
}
```

`POST /v1/lab/shared-memory` показывает direct poisoning. Любой аутентифицированный demo
user может записать procedural/semantic content в общую область; другой user увидит запись
через `GET /v1/lab/memory-recall` и в `memory_context` общего навыка.

`POST /v1/lab/ingest-external-memory` вызывает `duckduckgo-mcp.text_search` и сохраняет
заголовки/snippets/URL без очистки инструкций. Контролируемая исследователем страница может
попасть в выдачу и перенести indirect prompt injection во все пользовательские контексты.
Provenance маркируется, но уязвимый ingestion намеренно не применяет trust policy.

Для production shared namespace должен быть read-only для runtime-агента; записи должны
проходить модерацию, allow-list источников, нормализацию, разделение data/instructions и
версионируемое одобрение. `agent_id`, `user_id`, `source_user_id` и provenance нельзя
извлекать из генерируемого LLM текста.
