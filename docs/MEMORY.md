# Многоуровневая память

Система персистентной памяти отделена от банковской и travel-систем сетями и
проектом Langfuse `Persistent Memory`. Внутри неё два HTTP-сервиса над одной MongoDB. Область записи задают
обязательные `agent_id` и `user_id`; дополнительный идентификатор области не используется.

- `memory-service :8002` — data plane, доступный `banking-agent`. Он хранит текущий диалог,
  pending actions и кандидаты из внешних источников, а долгосрочные слои только читает.
- `memory-orchestrator :8004` — control plane с отдельной identity `memory-operator`. Это
  LLM-агент, который единолично создаёт и изменяет episodic, semantic и procedural память.

Банк обращается только к шлюзу `memory-api :8002`. Через него недоступны MongoDB
и операторский API оркестратора. Внутренние `memory-service` и `memory-orchestrator`
принадлежат системе памяти, а не банковскому агенту.

Среди предметных агентов к API памяти подключён только банковский. Тревел-агент
не хранит здесь свою историю или воспоминания. Подключение второй системы требует
контракта контекста, отдельной идентичности и проверки областей доступа;
эта работа включена в [примерный план курса](COURSE_PLAN.md).

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

## Контекст банковского LLM

В начале каждого turn Banking Agent параллельно получает:

- профиль через secure MCP `get_customer_info` с пользовательским JWT; `id` профиля
  обязан совпадать с аутентифицированным клиентом;
- всю сохранённую историю `user`/`assistant` текущего `conversation_id` с фильтрами
  `agent_id=banking-agent`, `user_id=<текущий клиент>` и хронологическим порядком;
- semantic recall долгосрочных слоёв `episodic`, `semantic`, `procedural`: по 8 записей
  каждого слоя, до 24 суммарно. Это релевантная выборка с действующими scope/TTL filters,
  а не выгрузка всей MongoDB. Поиск ранжирует до 500 последних кандидатов каждого слоя.

Все банковские LLM-шаги (`route_intent`, `extract_travel_request`, `compose_response`,
а в уязвимой сборке также `select_abs_customer`)
получают единый JSON-контекст: `current_date`, `current_datetime`, `timezone`,
`customer_profile`, `conversation_history`, `memory_context`. Снимок даты/времени
делается на каждый turn в `BANK_TIMEZONE` (по умолчанию `Europe/Moscow`) и сохраняется
между его LLM-шагами. Базовый профиль содержит только `full_name`, `gender`,
`client_type`, `vip`; токен, телефон и email туда не включаются. Профиль не кэшируется
между пользователями. Если пользователь явно запросил профиль, его результат навыка
может содержать контактные поля как обычные данные АБС.

История передаётся полностью в `conversation_history`, без лимита последних N сообщений.
Новое сообщение передаётся отдельно в `user_message`, а после ответа обе новые реплики
сохраняются в working memory. Контексты разных сессий не объединяются; между сессиями
действует только долгосрочный recall. `memory_context` сохраняет содержимое **всех**
выбранных записей без дополнительного общего лимита 8 и без обрезки до 1500 символов;
`source`/`trust` остаются видны модели. Working memory не дублируется в recall, потому
что история уже получена отдельным полным чтением.

На шаге ответа LLM дополнительно получает `trusted_skill_draft` и `skill_data` с
результатами навыка. Полная история, профиль и память банковского агента не передаются
партнёрскому Travel Agent или внешним поисковым MCP. Лимит контекстного окна остаётся
ограничением выбранной модели: приложение не сокращает историю молча; ошибка провайдера
возвращается как явная ошибка LLM.

Проверка `tests/test_agent_context.py` перехватывает HTTP-запросы OpenAI SDK во время
полного запуска банковского графа и проверяет 40 реплик, 24 длинных воспоминания из трёх
слоёв, персонализацию, дату на границе суток UTC/Москва и отсутствие JWT/контактов в
базовом контексте. Счётчики в Langfuse позволяют сверять объём контекста без записи
самих промптов: `conversation_messages`, `memory_records`, `memory_layers`,
`has_customer_profile`, `current_date`, `timezone`.

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

Оркестратор обрабатывает очередь двумя способами:

1. по расписанию с интервалом `MEMORY_ORCHESTRATION_INTERVAL_SECONDS` из `.env`:
   по умолчанию 1800 секунд; для короткого опыта можно поставить `10`, а `0`
   отключает периодический запуск;
2. операторский `POST /api/v1/orchestrator/collect` либо
   `POST /api/v1/episodic/agent/collect/{thread_id}`.

Ручной запрос работает при любом режиме расписания и дожидается результата обработки.
Он принимает фильтры `agent_id`, `user_id`, `conversation_id` и `limit` от 1 до 500.
Обрабатываются только ожидающие задания; завершённые не запускаются повторно,
долговременная память не очищается. Для нового опыта материал нужно заново поставить
в очередь. Настройка, проверка `/health` и пример запроса для отдельного диалога —
в [конфигурации](CONFIGURATION.md#обработка-долговременной-памяти).

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

Для ручной обработки получите `MEMORY_OPERATOR_TOKEN` по [инструкции](RUNBOOK.md#токены-для-лабораторных-запросов):

```bash
curl -fsS http://localhost:8004/api/v1/orchestrator/collect \
  -H "Authorization: Bearer $MEMORY_OPERATOR_TOKEN" \
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
