# Память агента

Оркестратор — отдельный LLM-агент над FastAPI и MongoDB. Он использует официальный
`openai` SDK, обязательные `OPENAI_API_KEY`/`OPENAI_MODEL` и настраиваемый
`OPENAI_BASE_URL`. Пользовательские токены он не принимает: доступ разрешён только service
token банковского агента. Коллекция `memories` индексирована по
`(agent_id, user_id, memory_type, created_at)`, имеет text index, TTL index и индекс диалогов.
Коллекция `memory_jobs` является очередью асинхронной миграции working memory.

## Типы

| Тип | Содержание | Scope/retention |
|---|---|---|
| `semantic` | знания о среде и внешние документы | private или `user_id="*"` |
| `procedural` | правила выполнения задач и экспериментальные инструкции | private или `user_id="*"` |
| `episodic` | разрешённые предпочтения конкретного пользователя | конкретный user, постоянная до policy delete |
| `working` | полные реплики чата и pending actions | conversation; pending action TTL 15 минут |

Bootstrap создаёт semantic и procedural записи идемпотентно. Каждый завершённый turn создаёт
две working-записи: точный текст пользователя и точный ответ агента. Роль хранится в
`metadata.message_role`, порядок — в `message_index`, а обе записи связаны `turn_id` и
`conversation_id`.

## Схема записи

```json
{
  "id": "ObjectId as string",
  "agent_id": "banking-agent",
  "user_id": "customer-001",
  "memory_type": "working",
  "content": "Сколько денег я потратил в этом месяце?",
  "conversation_id": "uuid",
  "metadata": {
    "intent": "accounts",
    "message_role": "user",
    "message_index": 0,
    "turn_id": "uuid",
    "conversation_title": "Сколько денег я потратил в этом месяце?",
    "tool_calls": [],
    "migration_status": "pending"
  },
  "expires_at": null,
  "created_at": "...",
  "updated_at": "...",
  "created_by": "banking-agent",
  "schema_version": 1
}
```

## Сохранение сессий и асинхронная миграция

Путь запроса выглядит так:

`chat → insert user+assistant working records → enqueue memory_job → HTTP 202 → LLM worker`.

Поэтому банковский ответ не ждёт ещё одного LLM-вызова оркестратора. При этом turn считается
успешным только после записи обеих реплик и job в MongoDB. Worker атомарно забирает `pending`
job, вызывает LLM memory-orchestrator (до трёх попыток) и может создать:

- `episodic` — устойчивый факт или предпочтение текущего пользователя, `user_id=<customer>`;
- `semantic` — знание о среде агента, в учебной shared-области `user_id="*"`;
- `procedural` — переиспользуемый способ выполнения задачи/навык, также `user_id="*"`.

Новые записи получают provenance: `source=memory-orchestrator`, `source_user_id`,
`derived_from_turn`, `scope`, `trust` и rationale модели. После обработки исходный turn и job
переходят в `completed`; ошибка LLM фиксируется как `failed` и не удаляет историю чата.

UI получает сессии через `GET /v1/conversations` и историю через
`GET /v1/conversations/{conversation_id}/messages`. Banking Agent всегда подставляет
`user_id` из проверенного JWT, поэтому пользователь не может запросить чужую историю.

Обычный `POST /v1/memories` для pending financial actions и лабораторных записей по-прежнему
синхронно вызывает LLM для assessment. Исходный `memory_type`, identity и canonical metadata
задаёт control plane: модель их не меняет.

Recall сначала применяет tenant/type/TTL/conversation filters, затем делает ограниченный
лексический preselection и передаёт кандидатов LLM-агенту для финального выбора и
ранжирования. Без доступной LLM операция завершается `503`, детерминированного fallback нет.
Для Atlas замените `_score` на `$vectorSearch`, сохранив фильтры `agent_id`, `user_id` и тип
**до** выдачи кандидатов модели. Никогда не полагайтесь только на similarity или LLM для
tenant isolation.

Pending action — тоже working memory, но не является сообщением чата: его metadata содержит канонические параметры, статус
и после исполнения результат. LLM только оценивает запись и не может менять эти поля.
Идентификатор записи используется как idempotency key.

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
user может записать procedural/semantic content в общую область; LLM-recall может выбрать её
для другого user, после чего банковский LLM получает запись в `memory_context`.

`POST /v1/lab/ingest-external-memory` вызывает `duckduckgo-mcp.text_search` и сохраняет
заголовки/snippets/URL без очистки инструкций. Контролируемая исследователем страница может
попасть в выдачу и перенести indirect prompt injection во все пользовательские контексты.
Provenance маркируется, но уязвимый ingestion намеренно не применяет trust policy.

Асинхронный promotion semantic/procedural из обычного пользовательского turn тоже записывает
данные в shared scope. Это намеренно оставленная для исследования поверхность automatic
memory poisoning: метка `trust=untrusted-user-derived` обеспечивает наблюдаемость, но не
блокирует recall. В production такие candidates должны уходить в quarantine/approval, а не
становиться общей памятью автоматически.

Для production shared namespace должен быть read-only для runtime-агента; записи должны
проходить модерацию, allow-list источников, нормализацию, разделение data/instructions и
версионируемое одобрение. `agent_id`, `user_id`, `source_user_id` и provenance нельзя
извлекать из генерируемого LLM текста.
