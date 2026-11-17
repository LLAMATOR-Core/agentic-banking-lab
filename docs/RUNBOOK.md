# Запуск и диагностика

## Запуск и изменение настроек

Создайте `.env` из `.env.example`, укажите ключ и модели LLM, затем выполните:

```bash
docker compose --env-file .env up -d --build
docker compose --env-file .env ps
```

После любого изменения `.env` повторите команду запуска. Все сервисы стенда, включая
защищённый/уязвимый MCP АБС, банковский сервис и мок авиабилетов, запускаются
без дополнительных параметров.
Режимы подключений описаны в [настройках](CONFIGURATION.md).

`keycloak-configurator` настраивает тестовых пользователей и клиентов. Его завершение с кодом 0 ожидаемо; остальные сервисы должны работать.
Полный запуск конфигуратора восстанавливает демонстрационные профили и пароли
из кода, даже если они были изменены в Keycloak.

## Проверка готовности

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8002/health
curl -fsS http://localhost:8003/health
curl -fsS http://localhost:8004/health
curl -fsS http://localhost:8106/health
```

У банковского агента проверьте `agent_mode` (`orchestrator` по умолчанию) и
`abs_mcp_mode` (используется чатом только в `legacy`), у тревел-агента проверьте `flight_provider`.
У оркестратора памяти проверьте `schedule_seconds` и `schedule_enabled`:
интервал задаётся через `MEMORY_ORCHESTRATION_INTERVAL_SECONDS` в `.env`, `0` отключает
расписание. Ручная обработка остаётся доступна через операторский API;
пример приведён в [настройках памяти](CONFIGURATION.md#обработка-долговременной-памяти).
Для Keycloak откройте [описание OIDC](http://localhost:8080/realms/agentic-banking/.well-known/openid-configuration),
для Langfuse откройте [интерфейс](http://localhost:3001).

Логи отдельных сервисов:

```bash
docker compose --env-file .env logs -f --tail=100 banking-agent abs-mcp travel-agent
docker compose --env-file .env logs --tail=100 keycloak-configurator
docker compose --env-file .env logs --tail=100 memory-service memory-orchestrator
docker compose --env-file .env logs --tail=100 seaweedfs langfuse-web langfuse-worker
```

## Частые проблемы

| Признак | Что проверить |
|---|---|
| Интерфейс открылся, но вход не работает | Готовность Keycloak и результат `keycloak-configurator`; затем обновите страницу |
| `llm_unavailable` | Ключ, адрес и доступность провайдера, названия моделей, лимиты запросов |
| `llm_invalid_response` | Ответ модели не прошёл проверку JSON-схемы; посмотрите соответствующий LLM-шаг в Langfuse |
| Нет долговременных записей | Доступность модели `EMBEDDING_MODEL`, журнал оркестратора и наличие обработанных записей |
| Ошибка поиска Туту | Доступ к `TUTU_MCP_URL` из контейнера `travel-agent` |
| Мок возвращает не ту заметку | `FLIGHTS_MOCK_NOTE` в `.env` и текст выбранного файла в `infra/flights-mcp/` |
| Изменение режима не применилось | Повторный `up -d --build`; также проверьте, не задана ли та же переменная в оболочке |
| JWT без `sub` или отказ входа с `profile`/`email` | Импорт realm и конфигуратор должны содержать `oidc-sub-mapper` у выпускающих токены клиентов и стандартные разрешения `profile`/`email` интерфейса; проверка обязательного `sub` остаётся включённой |
| `invalid issuer` | Внешний issuer `http://localhost:8080/realms/agentic-banking` и настройки hostname Keycloak |
| Ошибка `azp` или `aud` | Назначение токена на конкретном шаге цепочки; исходный пользовательский JWT нельзя напрямую подставлять общему MCP от имени инвестиционного агента |
| Отказ scope или Token Exchange | Успех конфигуратора, назначенные необязательные разрешения OAuth, клиент обмена и `oauth.*` в трассе; сверьте их с матрицей полномочий |
| Инвестиционный навык не читает операции | Прямой отказ ожидаем; доступ к сведениям через общую память проверяется отдельным опытом |
| Запись создана, но другой навык её не видит | `SKILL_MEMORY_MODE`, конкретный клиент, автор, область, ID записи и сходство запроса; UI-область `banking-agent` отделена от `skills.*` |
| Langfuse не принимает трассы | Пара ключей каждой системы соответствует её собственному проекту, готовность ClickHouse, Redis и SeaweedFS |

Если провайдер LLM работает на хосте Docker Desktop, используйте
`http://host.docker.internal:<порт>/v1`. `localhost` внутри контейнера указывает на
сам контейнер. Если провайдер не поддерживает `response_format`, задайте
`OPENAI_JSON_MODE=false`; проверка JSON-ответа по схеме при этом остаётся.

Отсутствие эмбеддингов возвращает ошибку, а не заменяется поиском по ключевым словам.
Ошибки поисковых источников попадают в `search_errors`; Туту не подменяется моком.
Недоступность Langfuse не должна прерывать банковскую операцию, но часть трасс может потеряться.

## Проверка контекста LLM

В режиме оркестратора откройте `orchestrator.route`, `orchestrator.delegate`,
`orchestrator.compose`, `skill.turn`, `skill.sources` и LLM исполнителей. Вход маршрутизатора содержит сообщение и прошлый навык,
а не профиль и долговременную память. У исполнителя проверьте `memory_context`,
его историю, фактический результат инструмента и `data.experiment` ответа.
В `orchestrator.compose` проверьте результаты агентов и решение о дополнительном
вызове или итоговом ответе. После первичного выбора оркестратор использует историю
текущего диалога. Список вызовов находится в `data.orchestration.delegated_agents`,
отдельные результаты в `data.agent_results`.

Для одного запроса откройте `banking-agent.request` в `Banking`. В проекте партнёра
найдите локальную трассу по `upstream_trace_id`, равному банковскому trace ID.
Сверьте `oauth.token-exchange`, `oauth.authorize`, `oauth.scope-check`,
`oauth.resource-check`, а для памяти — `memory.client.call` и
`memory.skill.http.<operation>`. Фоновый перенос ищите в `Persistent Memory`
отдельно по `experiment_id`/`run_id`, job ID и `derived_from_job`.

Проверка доставки и локальных деревьев Langfuse v4:

```bash
docker compose exec -T banking-agent python < scripts/trace_smoke.py
docker compose exec -T travel-agent python - --upstream-trace-id BANK_TRACE_ID < scripts/trace_smoke.py
```

Во второй команде замените `BANK_TRACE_ID` на `trace_id` первого отчёта.
Каждый запуск читает только собственный проект с ключами своего контейнера.
Скрипт проверяет физический и логический корень агентского запроса, наличие
родителей и принадлежность наблюдений системе. Синхронные вызовы памяти
показаны внутри банковской трассы и не создают корни в проекте памяти.
Токены и банковское содержимое не печатаются.

Следующие признаки относятся к `BANKING_AGENT_MODE=legacy`.

В Langfuse откройте шаги `llm.banking-agent.route_intent`, `extract_travel_request`
и `compose_response`. В уязвимом режиме АБС также есть `select_abs_customer`.
Сравните поля метаданных:

- `conversation_messages`: число сохранённых реплик текущего диалога;
- `memory_records` и `memory_layers`: число выбранных воспоминаний и распределение по слоям;
- `has_customer_profile`: наличие базового профиля;
- `current_date` и `timezone`: дата и часовой пояс.

В прежнем банковском графе значения общего контекста одного обращения
должны совпадать. После завершённого
обращения в историю добавляются две реплики. Контракт описан в [описании памяти](MEMORY.md#контекст-банковского-llm).

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
Проверка `smoke_test.py` требует работающего стенда и доступной LLM. В режиме оркестратора проверяются три навыка, включая банковский обзор
и выписку за указанный период.
`python scripts/smoke_test.py --orchestration` проверяет один составной запрос:
тревел-план, банковские остатки и расходы, затем общий ответ оркестратора.
Для отдельной проверки уточнений бюджета поездки без внешнего поиска:
`python scripts/smoke_test.py --travel-budget`. Проверка отправляет запрос про Таиланд,
затем короткий ответ с городом, периодом и длительностью; агент должен сохранить
известные данные и уточнить только число путешественников.
`python scripts/smoke_test.py --phuket-budget` воспроизводит диалог о трёх взрослых,
Пхукете 3-20 декабря 2026 года и исключённом перелёте в Шанхай; проверяет реальный
вызов поиска LED → HKT без обратного рейса и получение результатов источников.
В `legacy` параметр
`--confirm` выполняет перевод 1 RUB между тестовыми счетами и проверяет повтор запроса.

Для проверки OAuth без LLM и отдельного опыта с памятью:

```bash
.venv/bin/python scripts/skill_experiment.py --phase oauth --output artifacts/docker-review/skill-oauth.json
.venv/bin/python scripts/skill_experiment.py --phase memory --output artifacts/docker-review/skill-memory-shared.json
```

Требуются запущенные Keycloak, АБС и MCP; для второй команды также нужны LLM и эмбеддинги.
Подготовка областей, параметры секретов и сравнение с `isolated` описаны в
[EXPERIMENTS.md](EXPERIMENTS.md). Локальные тесты и `docker compose config` не
подтверждают работу Token Exchange в Keycloak или доставку событий в Langfuse.
