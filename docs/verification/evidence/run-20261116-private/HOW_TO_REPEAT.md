# Повторение этого прогона

Подготовка и ограничения: [общая инструкция](../../HOW_TO_REPEAT.md).

Из корня репозитория, с новым именем каталога:

```powershell
.venv/Scripts/python.exe scripts/verification/reproduce.py --run docs/verification/evidence/run-NEW --private-only
```

Точные команды и коды завершения: `execution.json`. Версия и хеши исходников: `source-manifest.json`. Контрольные суммы доказательств: `SHA256SUMS.json`.
Код завершения 0 означает завершение сценария, а не отсутствие уязвимостей. HTTP-опыты и компонентные подстановки различаются в поле scope.
