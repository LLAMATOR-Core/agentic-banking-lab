# Runbook

## Запуск и готовность

```bash
cp .env.example .env
# заполните OPENAI_API_KEY и HASDATA_API_KEY
docker compose up --build -d
docker compose ps
```

LLM обязательна для Banking Agent, Travel Agent и Memory Orchestrator. Embedding-модель
обязательна для memory recall и promotion. `OPENAI_BASE_URL` может указывать на единый
OpenAI-compatible endpoint с Chat Completions и Embeddings. Если локальный
провайдер не требует аутентификацию, задайте непустой placeholder в `OPENAI_API_KEY`.
Задайте `EMBEDDING_MODEL`; по умолчанию используется `text-embedding-3-small`.
Один travel turn включает несколько последовательных LLM/MCP шагов, поэтому внутренний
`TRAVEL_HTTP_TIMEOUT_SECONDS` по умолчанию равен 180 секундам.

Сначала готовы PostgreSQL/MongoDB, затем Keycloak и прикладные сервисы; Langfuse дополнительно ждёт ClickHouse, Redis и SeaweedFS. Проверки:

```bash
curl -fsS http://localhost:8080/realms/agentic-banking/.well-known/openid-configuration >/dev/null
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8001/health
curl -fsS http://localhost:8002/health
curl -fsS http://localhost:8003/health
curl -fsS http://localhost:8004/health
curl -fsS http://localhost:8101/health
curl -fsS http://localhost:8104/health
curl -fsS http://localhost:8105/health
curl -fsS http://localhost:8106/health
curl -fsS http://localhost:8106/mcp \
  -H "Authorization: Bearer ${FLIGHTS_MCP_AUTH_TOKEN:-local-flights-mcp}" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"ready","method":"tools/list","params":{}}'
```

Если UI открылся до готовности Keycloak, подождите и обновите страницу. Для CI используйте
`scripts/smoke_test.py`, который ждёт readiness и требует доступную LLM.

## Обновление существующего стенда на SeaweedFS и Flights MCP

Не копируйте `.env.example` поверх заполненного `.env`. Удалите старые `AVIASALES_*`,
добавьте `FLIGHTS_MCP_AUTH_TOKEN`, `SEAWEEDFS_ACCESS_KEY`, `SEAWEEDFS_SECRET_KEY` из примера.
Compose также имеет локальные значения по умолчанию для этих трёх переменных.
S3 endpoint для Langfuse внутри сети — `http://seaweedfs:8333`, для браузера —
`http://localhost:9090`; bucket `langfuse` создаётся автоматически. Порт старой консоли
9091 больше не используется. Readiness делает подписанный запрос к bucket: после старта
можно проверить `docker compose ps seaweedfs` и `docker compose logs seaweedfs`.

Старый volume `agentic-banking_minio-data` несовместим с форматом SeaweedFS и не
подключается к новому контейнеру. Он не удаляется при обновлении. Если нужны прежние
S3-объекты трасс/медиа, сначала перенесите их через S3, пока старый MinIO ещё работает.
Для стандартных имён контейнеров и локальных credentials:

```bash
docker compose stop langfuse-web langfuse-worker
docker compose -f docker-compose.yml -f infra/seaweedfs-migration.yml up -d --wait seaweedfs
docker exec \
  -e MIGRATION_ACCESS_KEY="${SEAWEEDFS_ACCESS_KEY:-langfuse}" \
  -e MIGRATION_SECRET_KEY="${SEAWEEDFS_SECRET_KEY:-local-seaweedfs-secret}" \
  agentic-banking-minio-1 sh -c '
    mc alias set legacy http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null &&
    mc alias set seaweed http://seaweedfs:8333 "$MIGRATION_ACCESS_KEY" "$MIGRATION_SECRET_KEY" >/dev/null &&
    mc mirror --overwrite legacy/langfuse seaweed/langfuse
  '
```

Если ключи менялись, передайте в shell значения из вашего `.env`. При ошибке копирования
сначала устраните её; не переключайте Langfuse на неполное хранилище. После успешного
переноса остановите два заменяемых контейнера и примените основной Compose:

```bash
docker stop agentic-banking-minio-1 agentic-banking-aviasales-mcp-1
docker compose up --build -d --remove-orphans
docker compose ps
```

`--remove-orphans` удаляет контейнеры старых сервисов и освобождает порты 9090/8106,
сохраняя named volumes. Новый `seaweedfs-data` хранит и объекты, и метаданные.
При новом развёртывании этап переноса не нужен. Проверьте после обновления `/health`
Flights MCP, отказ 401 без bearer и наличие синтетических рейсов в travel-ответе;
для Langfuse — приём новой трассы и открытие старой после переноса.

## Логи

```bash
docker compose logs -f --tail=200 banking-agent abs-mcp abs-mcp-vulnerable abs-api
docker compose logs -f --tail=200 travel-agent flights-mcp duckduckgo-mcp
docker compose logs -f --tail=200 memory-service memory-orchestrator mongo
docker compose logs -f --tail=200 keycloak
docker compose logs -f --tail=200 langfuse-web langfuse-worker
```

HTTP API errors не должны содержать token/secret. Для причин Keycloak 401 сначала декодируйте claims локально без публикации полного токена и проверьте `iss`, `azp`, `aud`, `exp`.

## Типовые проблемы

### UI показывает `ИИ-модель недоступна`

Проверьте `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `EMBEDDING_MODEL` и
доступность endpoint из Docker
network. Для сервера на хосте macOS обычно нужен URL вида
`http://host.docker.internal:<port>/v1`, а не `localhost`. Если совместимый провайдер не
поддерживает `response_format={"type":"json_object"}`, установите `OPENAI_JSON_MODE=false`:
требование вернуть JSON Schema останется в prompt, а ответ всё равно будет проверен Pydantic.

После изменения конфигурации пересоздайте три сервиса:

```bash
docker compose up -d --build memory-service memory-orchestrator travel-agent banking-agent
```

Ответы `503/llm_unavailable` означают отсутствие связи, неверный ключ, rate limit или ошибку
провайдера. `502/llm_invalid_response` означает, что модель вернула невалидный JSON или
несуществующий ID. Скрытого fallback нет.

### Realm не обновился

Startup import пропускает уже существующий realm, поэтому `keycloak-configurator` выполняет
идемпотентное обновление через Admin API. Проверьте
`docker compose logs keycloak-configurator`: успешный контейнер завершается с code 0. Сброс
volumes для обновления realm не требуется.

### `invalid issuer`

Браузерный issuer намеренно `http://localhost:8080/realms/agentic-banking`. Контейнеры получают JWKS по внутреннему URL, но проверяют внешний issuer. Не меняйте только одну из переменных `KEYCLOAK_ISSUER`/hostname.

### `Calling client is not allowed` / invalid audience

Проверьте нужный token flow и логи `keycloak-configurator`. Для Token Exchange исходный
user token обязан содержать `bank-mcp` в `aud`; exchanged token — `abs-api`; его `azp` —
`bank-mcp`. Не передавайте client-credentials token в secure MCP.

### Нет инвестиционных новостей

Проверьте egress/DNS контейнера `duckduckgo-mcp`. Это soft dependency: портфель и продукты
продолжают работать, в data будет пустой `news`.

### Booking или Flights MCP недоступен

Для Booking проверьте `HASDATA_API_KEY`, credits и egress контейнера `travel-agent` к
`mcp.hasdata.com`. Для Flights MCP проверьте совпадение `FLIGHTS_MCP_AUTH_TOKEN` у
`travel-agent` и `flights-mcp`, `/health` на порту 8106 и `docker compose logs flights-mcp`.
Если переменная не задана в shell для curl-примера, используйте локальное значение
`local-flights-mcp` из `.env.example`. Внешняя сеть и токены авиакомпаний моку не нужны.
Ответы всегда синтетические (`mode=mock-external-mcp`); 401 без отдельного секрета ожидаем.
Ошибка источника попадает в `search_errors` и не подменяется локальным fallback.

### Langfuse не принимает spans

Сравните `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` у приложений с init keys Langfuse и проверьте `http://localhost:3001`. Для Langfuse SDK v4 identity задаётся через `propagate_attributes`: `user_id` — отдельное поле trace, `agent_id` — metadata и tag `agent:<id>`, `conversation_id` — session. SDK работает best-effort: сбой collector не ломает операции. После остановки collector часть spans может быть потеряна.

### Диалог сохранён, но migration памяти не завершён

Проверьте `docker compose logs memory-orchestrator` и коллекцию MongoDB `memory_jobs`.
`pending` означает ожидание или retry worker, `processing` — текущий LLM-вызов, `completed`
содержит `promoted_count`, `failed` после трёх попыток — тип и текст ошибки. Working history сохраняется до постановки job,
поэтому недоступность LLM при фоновой миграции не должна удалять реплики или ломать переход
между сессиями.

По умолчанию scheduler запускает collection раз в 1800 секунд. Для немедленной обработки
получите client-credentials token `memory-operator` и вызовите control plane:

```bash
MEMORY_OPERATOR_TOKEN=$(curl -fsS \
  http://localhost:8080/realms/agentic-banking/protocol/openid-connect/token \
  -d grant_type=client_credentials \
  -d client_id=memory-operator \
  -d client_secret="$KEYCLOAK_MEMORY_OPERATOR_SECRET" | jq -r .access_token)

curl -fsS http://localhost:8004/api/v1/orchestrator/collect \
  -H "Authorization: Bearer $MEMORY_OPERATOR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"banking-agent","limit":100}'
```

Banking Agent обращается только к `memory-service :8002`; отсутствие у него audience
`memory-orchestrator` является ожидаемым и проверяется smoke-тестом.

### Недостаточно памяти Docker

Langfuse v4 использует ClickHouse, Redis и SeaweedFS. Эти контейнеры входят в обязательный
профиль стенда: отключение их или Langfuse лишает эксперименты проверяемой трассировки.
Увеличьте лимит памяти Docker либо временно остановите другие локальные контейнеры.

## Тесты и качество

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check services tests scripts
pytest -q
python scripts/smoke_test.py
```

`smoke_test.py --confirm` выполняет перевод 1 RUB между двумя счетами того же demo customer и повторяет confirmation для проверки идемпотентности.

## Данные и сброс

`docker compose down` сохраняет volumes. `make reset` удаляет PostgreSQL, MongoDB, Langfuse, ClickHouse, Redis и SeaweedFS data. Для учебного бэкапа остановите запись и сохраните named volumes штатными средствами Docker; для production используйте нативные PostgreSQL/MongoDB/ClickHouse backup procedures.

## Изменение провайдера

При замене поискового провайдера не меняйте tool name/input schema без новой версии
контракта. Сначала добавьте contract tests, затем provider implementation и только потом
переключите Compose image. Мок-бронирование должно оставаться локальным и всегда возвращать
`external_side_effect=false`.
