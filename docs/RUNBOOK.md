# Runbook

## Запуск и готовность

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

Сначала готовы PostgreSQL/MongoDB, затем Keycloak и прикладные сервисы; Langfuse дополнительно ждёт ClickHouse, Redis и MinIO. Проверки:

```bash
curl -fsS http://localhost:8080/realms/agentic-banking/.well-known/openid-configuration >/dev/null
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8001/health
curl -fsS http://localhost:8002/health
curl -fsS http://localhost:8003/health
curl -fsS http://localhost:8101/health
curl -fsS http://localhost:8102/health
curl -fsS http://localhost:8103/health
curl -fsS http://localhost:8104/health
curl -fsS http://localhost:8105/health
curl -fsS http://localhost:8106/health
```

Если UI открылся до готовности Keycloak, подождите и обновите страницу. Для CI используйте `scripts/smoke_test.py`, который ждёт readiness.

## Логи

```bash
docker compose logs -f --tail=200 banking-agent abs-mcp abs-mcp-vulnerable abs-api
docker compose logs -f --tail=200 travel-agent travel-data-mcp booking-mcp expedia-mcp
docker compose logs -f --tail=200 duckduckgo-mcp
docker compose logs -f --tail=200 memory-orchestrator mongo
docker compose logs -f --tail=200 keycloak
docker compose logs -f --tail=200 langfuse-web langfuse-worker
```

HTTP API errors не должны содержать token/secret. Для причин Keycloak 401 сначала декодируйте claims локально без публикации полного токена и проверьте `iss`, `azp`, `aud`, `exp`.

## Типовые проблемы

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

### Duffel показывает `unconfigured`

Создайте test access token в Duffel Dashboard, запросите доступ к Stays, заполните
`DUFFEL_ACCESS_TOKEN` в `.env`, затем выполните
`docker compose up -d --build travel-data-mcp travel-agent`. Test environment используется
по умолчанию; production требует отдельного доступа. Никогда не коммитьте реальные ключи.

### Langfuse не принимает spans

Сравните `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` у приложений с init keys Langfuse и проверьте `http://localhost:3001`. SDK работает best-effort: сбой collector не ломает операции. После остановки collector часть spans может быть потеряна.

### Недостаточно памяти Docker

Полный Langfuse v4 использует ClickHouse/Redis/MinIO. Для работы над core можно запустить `make up-core`; агенты продолжат работу, но traces не будут доступны. Для требования полной наблюдаемости используйте `make up`.

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

`docker compose down` сохраняет volumes. `make reset` удаляет PostgreSQL, MongoDB, Langfuse, ClickHouse, Redis и MinIO data. Для учебного бэкапа остановите запись и сохраните named volumes штатными средствами Docker; для production используйте нативные PostgreSQL/MongoDB/ClickHouse backup procedures.

## Изменение провайдера

При замене Booking mock или Duffel не меняйте tool name/input schema без новой версии
контракта. Сначала добавьте contract tests, затем provider implementation и только потом
переключите Compose image. Секреты провайдера не должны попадать в Agent Card, tool result,
MongoDB или Langfuse metadata.
