# Consilium — AI‑юробюро (RU‑право)

Дипломный проект в частном образовательном сообществе. Цель — превратить поток юридических документов в управляемый процесс: факты, проверенные ссылки на нормы и практику, черновики и контроль сроков. Человек‑юрист принимает решения; ИИ снимает рутину.

- Доклад‑идеи: `doc/rfc/rfc-0001-vision-community.md`
- Журнал изменений: `doc/changelog/CHANGELOG.md`
- План работ: `doc/plan/roadmap_v1.md`
- Обратная связь: [issue №1 — Сбор отзывов по RFC‑0001](https://github.com/sabettovich/consilium/issues/1) или комментарии в Telegram‑группе.

## Как участвовать
- Наставники: рецензирование подходов и критериев оценки.
- Участники: разработка модулей, разметка примеров, предложения по улучшениям.

## Документация
- Учебный гайд с лабораторными: `doc/guide/labs_v1.md`

### Полезные страницы UI
- Загрузка через форму: `http://localhost:8003/intake`
- Админ-список документов: `http://localhost:8003/admin/docs`

## Связь
- Telegram‑группа: AI mindset {circle} (ссылка по приглашению; запросите доступ у автора).

## Лицензия
MIT — см. файл `LICENSE`.

## Автор
sabet — sabettovich@gmail.com

---

## Быстрый старт (локально)

- Установить зависимости (рекомендуется venv):
  ```bash
  pip install -r requirements.txt
  ```
- Скопировать `config/.env.example` в корень как `.env`, заполнить значения (см. ниже).
- Загрузить переменные в текущую сессию:
  ```bash
  set -a; source ./.env; set +a
  ```
- Запустить сервис Resolver (FastAPI):
  ```bash
  uvicorn app.main:app --port 8003 --reload
  ```

Smoke‑тест регистрации файла:
```bash
curl -s -X POST http://localhost:8003/api/docs/register \
  -F matter_id=2023-AR-0001 \
  -F class=intake \
  -F title='demo' \
  -F file=@README.md | jq .
```
Ожидается `201` и JSON: `{ doc_id, permalink, sha256, storage, storage_ref }`.

Проверка редиректа и целостности:
```bash
DOC_ID=<из ответа>
curl -i "http://localhost:8003/doc/${DOC_ID}" | sed -n '1,5p'
curl -s -X POST "http://localhost:8003/api/docs/${DOC_ID}/verify" | jq .
```

## Переменные окружения

См. шаблон `config/.env.example`. Минимум для Этапа A:

- Google Drive:
  - `GOOGLE_APPLICATION_CREDENTIALS` — путь к JSON сервисного аккаунта.
  - `GDRIVE_ROOT_FOLDER_ID` — ID корневой папки `/Matters`.
  - `GDRIVE_ROOT_PATH` — путь в Drive (обычно `/Matters`).
  - `GDRIVE_OAUTH_CLIENT`, `GDRIVE_OAUTH_TOKEN` — OAuth для Drive (персональный доступ), пути можно задавать с `$HOME`.
- Resolver:
  - `BASE_ID_URL` — базовый URL для формирования permalink (напр. `http://localhost:8003`).
- Уведомления (журнал):
  - `NOTIF_ENABLE=1`, `NOTIF_LOG_PATH=./logs/notifications.log`.
- Email (SMTP):
  - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`, `EMAIL_TO`.
  - Для Gmail нужен App Password (включите 2FA → App passwords) и `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`.
- Matrix:
  - `MATRIX_HOMESERVER` (например, `https://matrix-client.matrix.org`), `MATRIX_USER`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID` (вид `!abcdef:server`).

### OCR
- Языки и параметры распознавания (по умолчанию подходят для RU/EN):
  ```env
  OCR_LANGS=rus+eng     # языки tesseract
  OCR_DPI=300           # разрешение растрирования для pdftoppm/pdftocairo
  OCR_MAX_PAGES=35      # максимум страниц для OCR за один прогон
  DEBUG_OCR=false       # подробные логи OCR и debug-эндпоинты
  ```
  Применить и перезапустить:
  ```bash
  set -a; source ./.env; set +a
  uvicorn app.main:app --port 8003 --reload
  ```

### OCR — быстрые команды
- Очередь OCR (auto: текстовый PDF → pdftotext, скан → tesseract):
  ```bash
  export DOC_ID="D-..."
  curl -s -X POST http://localhost:8003/api/ocr/enqueue -F doc_id="$DOC_ID" | jq .
  sleep 20
  curl -s "http://localhost:8003/api/docs/$DOC_ID" | jq '.origin_meta.ocr_info'
  curl -s "http://localhost:8003/api/docs/$DOC_ID/text" | jq '{text: (.text[0:800])}'
  ```
- Форс изображений (на случай проблемных сканов):
  ```bash
  curl -s -X POST http://localhost:8003/api/ocr/enqueue -F doc_id="$DOC_ID" -F mode=image | jq .
  ```
- Admin re‑OCR:
  ```bash
  curl -s -X POST "http://localhost:8003/api/admin/ocr/requeue?doc_id=$DOC_ID&mode=auto" | jq .
  curl -s -X POST "http://localhost:8003/api/admin/ocr/requeue_batch?matter_id=2023-AR-0001&mode=auto" | jq .
  ```
- Дубликаты `doc_id`:
  ```bash
  curl -s http://localhost:8003/api/admin/docs/duplicates | jq .
  ```

Перезагрузка переменных:
```bash
set -a; source ./.env; set +a
```

## Уведомления (Email и Matrix)

Сервис пишет события в файл `logs/notifications.log` и, при наличии настроек, отправляет уведомления:

- Событие: `doc_registered` (регистрация документа)
  - Email: письмо с темой `[Consilium] Registered {DocID}` и телом:
    ```
    Matter: {MatterID}
    Title: {Title}
    DocID: {DocID}
    Link:  {Permalink}
    ```
  - Matrix: сообщение в комнату с тем же содержанием (plain‑text + simple HTML).

Проверка доставки после запуска сервиса:
```bash
curl -s -X POST http://localhost:8003/api/docs/register \
  -F matter_id=2023-AR-0001 \
  -F class=intake \
  -F title='notify-test' \
  -F file=@README.md | jq .
tail -n 10 logs/notifications.log
```
Если Email/Matrix не сконфигурированы — ошибок в API не будет, события останутся в логе. Ошибки доставки логируются как `email_error`/`matrix_error`.

