# Перенос данных из MinIO в SeaweedFS

Не копируйте `.env.example` поверх заполненного `.env`. Удалите старые `AVIASALES_*`,
добавьте `FLIGHTS_PROVIDER=tutu`, `TUTU_MCP_URL`, `FLIGHTS_MCP_AUTH_TOKEN`,
`SEAWEEDFS_ACCESS_KEY`, `SEAWEEDFS_SECRET_KEY` из примера. Compose имеет значения
по умолчанию и для этих переменных.
S3 endpoint для Langfuse внутри сети — `http://seaweedfs:8333`, для браузера —
`http://localhost:9090`; bucket `langfuse` создаётся автоматически. Порт старой консоли
9091 больше не используется. Проверка готовности делает подписанный запрос к bucket: после старта
можно проверить `docker compose --env-file .env ps seaweedfs` и `docker compose --env-file .env logs seaweedfs`.

Старый volume `agentic-banking_minio-data` несовместим с форматом SeaweedFS и не
подключается к новому контейнеру. Он не удаляется при обновлении. Если нужны прежние
S3-объекты трасс/медиа, сначала перенесите их через S3, пока старый MinIO ещё работает.
Для стандартных имён контейнеров и локальных credentials:

```bash
docker compose --env-file .env stop langfuse-web langfuse-travel langfuse-memory langfuse-worker
docker compose --env-file .env -f docker-compose.yml -f infra/seaweedfs-migration.yml up -d --wait seaweedfs
docker network connect agentic-banking_platform-storage agentic-banking-minio-1
docker exec \
  -e MIGRATION_ACCESS_KEY="${SEAWEEDFS_ACCESS_KEY:-langfuse}" \
  -e MIGRATION_SECRET_KEY="${SEAWEEDFS_SECRET_KEY:-local-seaweedfs-secret}" \
  agentic-banking-minio-1 sh -c '
    mc alias set legacy http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null &&
    mc alias set seaweed http://seaweedfs:8333 "$MIGRATION_ACCESS_KEY" "$MIGRATION_SECRET_KEY" >/dev/null &&
    mc mirror --overwrite legacy/langfuse seaweed/langfuse
  '
```

Временное подключение старого MinIO к `platform-storage` нужно, чтобы он видел
SeaweedFS: новая сеть хранения изолирована от старой сети стенда. Если контейнер уже
подключён к этой сети, повторять `docker network connect` не нужно.

Если ключи менялись, передайте в оболочку значения из вашего `.env`. При ошибке копирования
сначала устраните её; не переключайте Langfuse на неполное хранилище. После успешного
переноса остановите два заменяемых контейнера и примените основной Compose:

```bash
docker stop agentic-banking-minio-1 agentic-banking-aviasales-mcp-1
docker compose --env-file .env up -d --build --remove-orphans
docker compose --env-file .env ps
```

`--remove-orphans` удаляет контейнеры старых сервисов и освобождает порты 9090/8106,
сохраняя named volumes. Новый `seaweedfs-data` хранит и объекты, и метаданные.
При новом развёртывании этап переноса не нужен. Проверьте после обновления `/health`
Travel Agent (`flight_provider=tutu`) и `live-mcp-read-only` в travel-ответе;
проверьте также `/health` мокового MCP на порту 8106;
для Langfuse — приём новой трассы и открытие старой после переноса.

