# Запуск и диагностика

## Запуск и изменение настроек

Создайте `.env` из `.env.example`, укажите ключ и модели LLM, затем выполните:

```bash
docker compose --env-file .env up -d --build
docker compose --env-file .env ps
```

После любого изменения `.env` повторите команду запуска. Все сервисы стенда, включая
оба MCP АБС и мок авиабилетов, запускаются без дополнительных параметров.
Режимы подключений описаны в [настройках](CONFIGURATION.md).

`keycloak-configurator` настраивает тестовых пользователей и клиентов. Его успешное
завершение с кодом 0 — штатное поведение. Остальные сервисы должны работать.

## Проверка готовности

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8002/health
curl -fsS http://localhost:8003/health
curl -fsS http://localhost:8004/health
curl -fsS http://localhost:8106/health
```

У банковского агента проверьте `abs_mcp_mode`, у тревел-агента — `flight_provider`.
У оркестратора памяти проверьте `schedule_seconds` и `schedule_enabled`:
интервал задаётся через `MEMORY_ORCHESTRATION_INTERVAL_SECONDS` в `.env`, `0` отключает
расписание. Ручная обработка остаётся доступна через операторский API;
пример — в [настройках памяти](CONFIGURATION.md#обработка-долговременной-памяти).
Для Keycloak откройте [описание OIDC](http://localhost:8080/realms/agentic-banking/.well-known/openid-configuration),
для Langfuse — [интерфейс](http://localhost:3001).

Логи отдельных сервисов:

```bash
docker compose --env-file .env logs -f --tail=100 banking-agent travel-agent
docker compose --env-file .env logs --tail=100 keycloak-configurator
docker compose --env-file .env logs --tail=100 memory-service memory-orchestrator
docker compose --env-file .env logs --tail=100 seaweedfs langfuse-web langfuse-worker
```

## Проверка изоляции

После запуска выполните:

```bash
python3 scripts/check_system_isolation.py
```

Проверка обращается к Docker и выполняет запросы из трёх агентных контейнеров.
Она проверяет разрешённые API, закрытые пути шлюзов, недоступность чужих сервисов
по адресам контейнеров, состав кода в образах и соответствие ключа Langfuse
единственному своему проекту. Также она выполняет авторизованный A2A-запрос к моку
через тревел-агента и чтение пустой истории через API памяти.
Банковские операции и изменение памяти она не выполняет.

В ответах `/health` есть раздел `observability` с `system_id`, именем и идентификатором
проекта. Он не содержит ключей. В Langfuse должны быть проекты `Banking`, `Travel`
и `Persistent Memory`; корреляция обращений доступна через `session_id` и
`metadata.correlation_id`.

Для диагностики приёмников:

```bash
docker compose --env-file .env logs --tail=100 langfuse-web langfuse-travel langfuse-memory
```

Уже существующий смешанный проект сохраняется для просмотра старых трасс. Новые
проекты имеют фиксированные идентификаторы и получают ключи из `.env` при первом
создании. Чтобы заменить ключ существующего проекта, сначала создайте новый ключ
в настройках этого проекта Langfuse, затем внесите пару в `.env` и повторите запуск.

## Частые проблемы

| Признак | Что проверить |
|---|---|
| Интерфейс открылся, но вход не работает | Готовность Keycloak и результат `keycloak-configurator`; затем обновите страницу |
| `llm_unavailable` | Ключ, адрес и доступность провайдера, названия моделей, лимиты запросов |
| `llm_invalid_response` | Ответ модели не прошёл проверку JSON-схемы; посмотрите соответствующий LLM-шаг в Langfuse |
| Нет долгосрочной памяти | Доступность модели `EMBEDDING_MODEL`, журнал оркестратора и наличие обработанных записей |
| Ошибка поиска Туту | Доступ к `TUTU_MCP_URL` из контейнера `travel-agent` |
| Мок возвращает не ту заметку | `FLIGHTS_MOCK_NOTE` в `.env` и текст выбранного файла в `infra/flights-mcp/` |
| Изменение режима не применилось | Повторный `up -d --build`; также проверьте, не задана ли та же переменная в оболочке |
| `invalid issuer` | Внешний issuer `http://localhost:8080/realms/agentic-banking` и настройки hostname Keycloak |
| Ошибка `azp` или `aud` | Клиент и назначение JWT; для защищённого MCP нужен пользовательский токен, а не сервисный |
| Langfuse не принимает трассы | Совпадение отдельной пары ключей каждой системы с её проектом Langfuse, готовность ClickHouse, Redis и SeaweedFS |

Если провайдер LLM работает на хосте Docker Desktop, используйте
`http://host.docker.internal:<порт>/v1`. `localhost` внутри контейнера указывает на
сам контейнер. Если провайдер не поддерживает `response_format`, задайте
`OPENAI_JSON_MODE=false`; проверка JSON-ответа по схеме при этом остаётся.

Отсутствие эмбеддингов возвращает ошибку, а не заменяется поиском по ключевым словам.
Ошибки поисковых источников попадают в `search_errors`; Туту не подменяется моком.
Недоступность Langfuse не должна прерывать банковскую операцию, но часть трасс может потеряться.

## Проверка контекста LLM

В Langfuse откройте шаги `llm.banking-agent.route_intent`, `extract_travel_request`
и `compose_response`. В уязвимом режиме АБС также есть `select_abs_customer`.
Сравните поля метаданных:

- `conversation_messages` — число сохранённых реплик текущего диалога;
- `memory_records` и `memory_layers` — число выбранных воспоминаний и распределение по слоям;
- `has_customer_profile` — наличие базового профиля;
- `current_date` и `timezone` — дата и часовой пояс.

На всех LLM-шагах одного обращения значения должны совпадать. После завершённого
обращения в историю добавляются две реплики. Подробный контракт — в [описании памяти](MEMORY.md#контекст-банковского-llm).

## Токены для лабораторных запросов

Для приведённых ниже команд нужны `curl` и `jq`. Получите пользовательские токены:

```bash
IVAN_TOKEN=$(curl -fsS http://localhost:8080/realms/agentic-banking/protocol/openid-connect/token \
  -d grant_type=password -d client_id=bank-ui \
  -d username=ivan.petrov -d password=banking-demo | jq -r .access_token)
ANNA_TOKEN=$(curl -fsS http://localhost:8080/realms/agentic-banking/protocol/openid-connect/token \
  -d grant_type=password -d client_id=bank-ui \
  -d username=anna.sokolova -d password=banking-demo | jq -r .access_token)
```

Для запуска обработки памяти нужен отдельный сервисный токен:

```bash
MEMORY_OPERATOR_TOKEN=$(curl -fsS http://localhost:8080/realms/agentic-banking/protocol/openid-connect/token \
  -d grant_type=client_credentials -d client_id=memory-operator \
  -d client_secret=memory-operator-secret | jq -r .access_token)
```

Если пароли или секрет клиента изменены, используйте свои значения. Просроченный токен
получите заново. Передача `.env` в Compose не создаёт переменные в вашей оболочке.

## Остановка и данные

Остановка с сохранением данных:

```bash
docker compose --env-file .env down
```

Удаление всех тестовых данных PostgreSQL, MongoDB и инфраструктуры Langfuse:

```bash
docker compose --env-file .env down -v
```

Для смены MCP или модели удалять тома не требуется. Ранее отравленная память после
переключения режима сохраняется; удаляйте нужные записи через операторский API.
Для старого развёртывания есть отдельная [инструкция переноса из MinIO в SeaweedFS](STORAGE_MIGRATION.md).

## Автоматические проверки

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check services tests scripts
pytest -q
python scripts/smoke_test.py
```

Тест `tests/test_agent_context.py` проверяет тело HTTP-запросов SDK с подменённым
ответом провайдера. Тесты режимов Compose читают конфигурацию и не запускают контейнеры.
Проверка `smoke_test.py` требует работающего стенда и доступной LLM. Параметр
`--confirm` выполняет перевод 1 RUB между тестовыми счетами и проверяет повтор запроса.
