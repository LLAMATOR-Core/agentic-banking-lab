# Как повторить проверки

Все команды выполняются из корня этого репозитория. Нужны Python 3.12+, Docker Compose v2, доступ к провайдеру модели и свободные порты из `docker-compose.yml`. Рекомендуется не менее 8 ГБ памяти Docker.

## Подготовка

```powershell
py -3 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Последняя команда нужна только при отсутствии `.env`. В локальном `.env` задайте `OPENAI_API_KEY`, `OPENAI_BASE_URL=https://api.deepseek.com`, доступную у провайдера `OPENAI_MODEL`, `ABS_MCP_MODE=secure` и `AUTH_DISABLED=false`. Ключи не добавляются в Git. Для воспроизведения конкретного прогона сверяйте безопасные параметры с его `baseline/manifest.json`, версии Python-зависимостей — с `dependencies.log`, хеши исходников — с `source-manifest.json`.

Профиль проверки использует настоящую локальную модель эмбеддингов `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` и передаёт запросы оркестратора в DeepSeek. Ответы LLM не подменяются. Первая загрузка эмбеддингов требует сети. Подстановки применяются только в явно обозначенных компонентных опытах.

Профиль переопределяет настройки памяти из `.env`: адрес модели — `http://local-models:8100/v1`, эмбеддинги — указанная локальная модель, периодическая обработка — `0` (запуск вручную). В манифесте базового опыта сохранены значения `.env`; эффективные переопределения находятся в `scripts/verification/compose.models.yml`. Запуск тестов автоматически включает UTF-8 и отдельную временную папку, чтобы не зависеть от кодировки и общего каталога pytest в Windows.

```powershell
New-Item -ItemType Directory -Force scripts/verification/runtime/provider
docker compose -p abl-stage-verification -f docker-compose.yml -f scripts/verification/compose.models.yml up -d --build
docker compose -p abl-stage-verification -f docker-compose.yml -f scripts/verification/compose.models.yml ps --all
```

Дождитесь готовности сервисов, успешного завершения `keycloak-configurator` и ответа `/health` банковского API на порту 8000. Профиль задаёт шесть сетей `10.241.0.0/24`–`10.241.5.0/24`; при конфликте с локальной сетью выберите свободные подсети в `compose.models.yml`. У проекта собственные тома. Прогоны меняют только учебные счета и память; повторный запуск не предполагает нулевые остатки. Не запускайте одновременно несколько прогонов.

## Прогон

```powershell
.venv/Scripts/python.exe scripts/verification/reproduce.py --run docs/verification/evidence/run-NEW
```

Каждый раз указывайте новый каталог. Сценарий отказывается перезаписывать существующий. Он запускает pytest, Ruff, исходные HTTP-проверки, опыты с памятью и пять компонентных сценариев. В каждом каталоге автоматически создаётся `HOW_TO_REPEAT.md`. Ошибки сохраняются и приводят к ненулевому коду завершения. Код 0 у компонента не означает, что уязвимостей нет: некоторые опыты намеренно демонстрируют дефекты.

Два опыта с личной памятью включены в полный прогон. Для их отдельного повторения:

```powershell
.venv/Scripts/python.exe scripts/verification/reproduce.py --run docs/verification/evidence/run-PRIVATE-NEW --private-only
```

Без Docker можно выполнить только локальные тесты:

```powershell
.venv/Scripts/python.exe scripts/verification/reproduce.py --run docs/verification/evidence/run-UNIT-NEW --unit-only
```

## Проверяемые доказательства

- `source-manifest.json`: коммит, состояние рабочей копии, SHA-256 исходников и конфигураций без `.env`.
- `execution.json`, `*.log`, `pytest.xml`: реальные команды, время, коды завершения и вывод.
- `baseline/`, `memory/`, `ordinary/`: запросы, HTTP-ответы, снимки и результаты. Поле `scope` задаёт границу вывода.
- `component/`: реальные результаты компонентных опытов; подстановка ключей, ответов или сетевых отказов обозначена внутри.
- `audit.json`: отсутствующие ссылки и шаги с ошибками; `SHA256SUMS.json`: контрольные суммы файлов прогона.

Проверить целостность сохранённых доказательств:

```powershell
.venv/Scripts/python.exe scripts/verification/check_evidence.py docs/verification/evidence/run-NEW
```

Хеши подтверждают целостность, а не истинность выводов. Проверка выводов требует чтения исходных запросов/ответов и повторного выполнения. Ответы модели могут отличаться. Недоступность провайдера отмечается как ошибка или неопределённый исход, а не защита от атаки. Нагрузочные проверки П12 и все подслучаи П01–П14 этим набором не покрываются.

## Остановка

```powershell
docker compose -p abl-stage-verification -f docker-compose.yml -f scripts/verification/compose.models.yml stop
```

Команда сохраняет данные для последующей проверки. Исторический протокол от 12 ноября остаётся архивом и не заменяет новые прогоны.
