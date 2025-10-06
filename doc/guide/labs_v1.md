# Учебный гайд и лабораторные работы (Этап A)

Этот гайд моделирует реальные шаги пользователя (юриста) и ассистентов сервиса (Resolver/Notifier). Он помогает понять: как регистрировать документы, получать постоянные ссылки (DocID), проверять целостность, обновлять карточку дела в Obsidian и отсылать уведомления клиенту.

Используемые сущности и файлы:
- Сервис: `app/main.py` (FastAPI Resolver + Notifier)
- Карточка дела: `vault/Matters/2023/2023-AR-0001.md`
- Демоданные: `demo/Matters/`
- Лог уведомлений: `logs/notifications.log`

Подготовка окружения (один раз):
- Заполните `.env` по образцу `config/.env.example`.
- Установите зависимости: `pip install -r requirements.txt`
- Загрузите переменные: `set -a; source ./.env; set +a`

---

## Лаба 1. Запуск сервиса и проверка API

- **Смысл**: убедиться, что Resolver доступен, и все переменные окружения подхватываются.
- **Действия**:
  1) Запуск:
     ```bash
     uvicorn app.main:app --port 8003 --reload
     ```
  2) Проверка API:
     ```bash
     curl -s http://localhost:8003/openapi.json | head -n1
     ```
- **Ожидаемо**: Uvicorn слушает 8003, спецификация отдаётся без ошибок.
- **Почему важно**: без стабильного API дальнейшие сценарии (регистрация/редирект/notify) невозможны.

---
Открой два терминала.
Терминал A — для сервера (запустить и оставить).
Терминал B — для команд curl.

Терминал A: Смысл-поднять FastAPI на 8003.
```bash
uvicorn app.main:app --port 8003 --reload
```

Терминал B: Смысл-проверить API.
```bash
curl -s http://localhost:8003/openapi.json | jq '.info.title, .info.version'
```
- [x] Ok

## Лаба 2. Регистрация документа и получение DocID

- **Смысл**: привязать файл к постоянному идентификатору `DocID` и получить `permalink` — ссылку, которую можно безопасно отдавать клиенту.
- **Что делает система**: сохраняет файл во временный буфер, считает `SHA256`, создаёт структуру папок дела в Google Drive, загружает файл под именем `DocID__Название.ext`, пишет запись в БД (`docs`).
- **Действия**:
  ```bash
  curl -s -X POST http://localhost:8003/api/docs/register \
    -F matter_id=2023-AR-0001 \
    -F class=intake \
    -F title='00_Фабула' \
    -F file=@demo/Matters/Ivanov_vs_Petrov/00_Фабула.md | jq .
  ```
  Скопируйте `doc_id`, `permalink`.
- **Ожидаемо**: HTTP 201 и JSON `{ doc_id, permalink, sha256, storage, storage_ref }`.
- **Типовые ошибки**: неверный `GDRIVE_ROOT_FOLDER_ID`, отсутствует OAuth JSON; см. логи Uvicorn и `logs/notifications.log`.

- [x] Ok

---

## Лаба 3. Редирект по permalink и проверка целостности

- **Смысл**: убедиться, что `permalink` всегда ведёт к актуальному месту файла (даже при перемещениях), и что файл не был изменён.
- **Как работает**: `GET /doc/{DocID}` делает 302 на `webViewLink` Drive. `verify` скачивает контент и сверяет `SHA256` с сохранённым.
- **Действия**:
  ```bash
  DOC_ID=<из Лабы 2>
  curl -i "http://localhost:8003/doc/${DOC_ID}" | sed -n '1,5p'
  curl -s -X POST "http://localhost:8003/api/docs/${DOC_ID}/verify" | jq .
  ```
- **Ожидаемо**: `302` и `match: true`.
- **Почему важно**: постоянные ссылки и контроль целостности — базовые гарантии для дела.

---

- [x] Ok


## Лаба 4. Обновление карточки дела в Obsidian

- **Смысл**: карточка дела — источник истины. В неё заносят DocID и публичные ссылки, видимые всей команде.
- **Действия**:
  1) Откройте `vault/Matters/2023/2023-AR-0001.md`.
  2) В списке документов замените плейсхолдер на реальный DocID и permalink из Лабы 2 (сохраняя техкомментарий `<!--tech: DocID-->`).
- **Ожидаемо**: Obsidian показывает кликабельную ссылку на документ.
- **Почему важно**: единая точка правды — снижает «потерю» файлов и расхождения в коммуникации.

---

- [x] Ok

## Лаба 5. Автоуведомления при регистрации (Email и Matrix)

- **Смысл**: информировать вовлечённых лиц при поступлении документов (содержит пермалинк и DocID).
- **Как работает**: `notify()` пишет событие `doc_registered` в `logs/notifications.log` и при наличии настроек SMTP/Matrix отправляет письмо/сообщение.
- **Действия**:
  ```bash
  curl -s -X POST http://localhost:8003/api/docs/register \
    -F matter_id=2023-AR-0001 -F class=intake -F title='notify-test' \
    -F file=@README.md | jq .
  tail -n 20 logs/notifications.log
  ```
  Проверьте почту и комнату Matrix.
- **Ожидаемо**: письмо и сообщение с:
  ```
  Matter: ...
  Title: ...
  DocID: ...
  Link:  http://localhost:8003/doc/...
  ```
- **Почему важно**: мгновенная обратная связь, без ручных пересылок.

---

- [x] mail Ok
- [ ] matrix Err - токен не активен, автоматически меняется на matrix.org Почему?


## Лаба 6. Выдача результата клиенту (deliver)

- **Смысл**: официальная передача итогового документа клиенту с уведомлениями и журналированием.
- **Как работает**: `POST /api/docs/{doc_id}/deliver` формирует `permalink` и вызывает `notify("result_delivered", ...)` → Email/Matrix + лог `result_delivered`.
- **Действия**:
  ```bash
  DOC_ID=<любой существующий>
  curl -s -X POST "http://localhost:8003/api/docs/${DOC_ID}/deliver" \
    -F message='Готово к выдаче' | jq .
  tail -n 20 logs/notifications.log
  ```
- **Ожидаемо**: `{ ok: true, doc_id, permalink }`; в логе `result_delivered`, `email_sent`, `matrix_sent`.
- **Почему важно**: фиксирует момент выдачи и отправляет клиенту проверенную ссылку.

---

- [x] mail Ok
- [ ] matrix Err, токен не активен, автоматически меняется на matrix.org Почему?

## Лаба 7. Структура папок дела в Google Drive

- **Смысл**: навигация по докам — не только резолвером, но и в Drive. У каждого дела одинаковый шаблон.
- **Как работает**: при первой регистрации по `MatterID` создаются `/Matters/{YEAR}/{MatterID}/` и подпапки.
- **Действия**:
  1) Откройте папку дела на Drive.
  2) Проверьте подпапки: `01_Intake/ 02_Evidence/ 03_Pleadings/ 04_Correspondence/ 05_Court/ 99_Archive/ Client_Share/`.
  3) Найдите загруженный файл по имени `DocID__Название.ext`.
- **Ожидаемо**: структура соответствует шаблону; файл в нужной подпапке.
- **Почему важно**: у команды единый стандарт расположения и именования.

---

- [x] Ok 
  - [x] Быстрый визуальный поиск по DocID прямо в Drive: D-20251004-06CV2NH5DWNX4T76T3VBM5YRA7__00_Фабула.md в 01_Intake/

## Лаба 8. Метаданные и правки (PATCH)

- **Смысл**: корректировки метаданных без повторной загрузки файла.
- **Как работает**: `PATCH /api/docs/{DocID}` обновляет `title`, `storage`, `storage_ref`.
- **Действия**:
  ```bash
  DOC_ID=<существующий>
  curl -s -X PATCH "http://localhost:8003/api/docs/${DOC_ID}" \
    -H "Content-Type: application/json" \
    -d '{"title":"Новое название"}' | jq .
  curl -s "http://localhost:8003/api/docs/${DOC_ID}" | jq .
  ```
- **Ожидаемо**: `{"ok": true}` и обновлённые метаданные.
- **Почему важно**: поддержка переименований и миграций без ломки ссылок.

### Учебные примеры

1) Исправление названия (опечатка/уточнение)

- **Потребность юриста**: заголовок в карточке и отчётах должен быть читаемым и точным, но файл на Drive трогать не нужно.
- **Шаги**:
  ```bash
  DOC_ID=<существующий>
  # Правим title в БД
  curl -s -X PATCH "http://localhost:8003/api/docs/${DOC_ID}" \
    -H "Content-Type: application/json" \
    -d '{"title":"Исковое заявление (уточнено)"}' | jq .
  # Проверяем
  curl -s "http://localhost:8003/api/docs/${DOC_ID}" | jq '.doc_id, .title'
  ```
- **Результат**: в API/отчётах новое название; DocID, permalink и имя файла на Drive — без изменений.

2) Перенос/смена ссылки хранения (storage_ref)

- **Потребность юриста**: документ был перемещён/скопирован в другой ресурс (например, в общую клиентскую папку). Нужно обновить ссылку хранения в системе учёта.
- **Шаги**:
  ```bash
  DOC_ID=<существующий>
  NEW_REF=<новый_ID_файла_в_хранилище>
  # Обновляем только ссылку хранения
  curl -s -X PATCH "http://localhost:8003/api/docs/${DOC_ID}" \
    -H "Content-Type: application/json" \
    -d "{\"storage_ref\":\"${NEW_REF}\"}" | jq .
  # Проверяем
  curl -s "http://localhost:8003/api/docs/${DOC_ID}" | jq '.doc_id, .storage, .storage_ref'
  ```
- **Результат**: Resolver начнёт редиректить `/doc/{DocID}` на новый объект в хранилище. DocID/permalink остаются стабильными.

> Примечание: если требуется именно переименовать файл в Google Drive под новый `title`, это отдельная операция (Drive API `Files.update(name=...)`). В текущей лабе меняем только метаданные учётной записи.

---

- [x] Ok
- [ ] ? Зачем это? Создать учебный пример - демо.

## Лаба 9. Траблшутинг (частые проблемы)

- **Смысл**: быстро находить и устранять проблемы.
- **Проблемы и решения**:
  - 500 при регистрации → проверить `.env` (GDrive/OAuth), трассировку Uvicorn, права на `logs/`.
  - `permalink` ведёт на неправильный порт → обновить `BASE_ID_URL` и перезапустить.
  - Уведомления не приходят → заполнить SMTP/MATRIX переменные; смотреть `email_error`/`matrix_error` в логе.
  - Нет доступа к Drive → проверить расшаривание корневой папки на сервисный аккаунт.

---

## Лаба 9.1. OCR сканов и PDF

- **Смысл**: распознавать текст в текстовых и сканированных PDF, получать извлечённый текст через API.
- **Как работает**:
  - `mode=auto` сам выбирает инструмент:
    - текстовый PDF → `pdftotext`.
    - скан PDF → `pdftoppm`/`pdftocairo` → `tesseract` (языки из `OCR_LANGS`).
  - Авто‑фолбэк: если `pdftotext` вернул пустоту/управляющие символы — переход на `tesseract`.
  - Ограничение по страницам: `OCR_MAX_PAGES` (по умолчанию 35).
  - Предобработка изображений (если установлен ImageMagick `convert`): `grayscale + normalize + contrast-stretch + sharpen` перед tesseract.

### Параметры `.env`
```env
OCR_LANGS=rus+eng
OCR_DPI=300
OCR_MAX_PAGES=35
DEBUG_OCR=false
```

### Эндпоинты
- Поставить документ в очередь OCR:
  ```bash
  export DOC_ID="D-..."
  curl -s -X POST http://localhost:8003/api/ocr/enqueue -F doc_id="$DOC_ID" | jq .
  sleep 20
  curl -s "http://localhost:8003/api/docs/$DOC_ID" | jq '.origin_meta.ocr_info'
  curl -s "http://localhost:8003/api/docs/$DOC_ID/text" | jq '{text: (.text[0:800])}'
  ```
- Форсировать скан‑режим:
  ```bash
  curl -s -X POST http://localhost:8003/api/ocr/enqueue -F doc_id="$DOC_ID" -F mode=image | jq .
  ```
- Admin re‑OCR (один/батч):
  ```bash
  curl -s -X POST "http://localhost:8003/api/admin/ocr/requeue?doc_id=$DOC_ID&mode=auto" | jq .
  curl -s -X POST "http://localhost:8003/api/admin/ocr/requeue_batch?matter_id=2023-AR-0001&mode=auto" | jq .
  ```
- Дубликаты `doc_id`:
  ```bash
  curl -s http://localhost:8003/api/admin/docs/duplicates | jq .
  ```

### Траблшутинг больших сканов
- Если `pdftoppm` висит/падает → в логе будет `[ocr] pdftoppm: code=...`; включён фолбэк `pdftocairo` и увеличенный таймаут.
- Если `tesseract` молчит → проверьте `tesseract --list-langs` (должны быть `eng` и `rus`).
- Для «тяжёлых» сканов установите ImageMagick (`convert`) и оставьте `DEBUG_OCR=true` временно для диагностики.

---

## Контрольный чек‑лист прохождения

- **[x]** Сервис запущен, API отвечает.
- **[x]** Документ зарегистрирован, получены DocID/permalink.
- **[x]** Редирект работает, verify даёт `match: true`.
- **[x]** Карточка дела в Obsidian обновлена ссылкой.
- **[x]** Уведомления Email/Matrix приходят при регистрации.
- **[x]** Deliver отправляет уведомления о выдаче.
- **[x]** Структура папок в Drive соответствует шаблону.
- **[x]** Метаданные можно править через PATCH.

---

## Лаба 10. Отчёт целостности (B1)

- **Смысл**: периодически проверять, что файлы в хранилище не изменились относительно `SHA256` в БД, и видеть сводку.
- **Как работает**: фоновый воркер пишет `logs/reports_integrity.jsonl` (JSONL), эндпойнт агрегирует последние записи по каждому `DocID`.
- **Команды**:
  ```bash
  # свежая сводка по делу
  curl -s "http://localhost:8003/api/reports/integrity?matter_id=2023-AR-0001&limit=50" | jq .

  # только проблемы (несовпадения/ошибки)
  curl -s "http://localhost:8003/api/reports/integrity?only_failed=true&limit=50" | jq .

  # сырой лог
  tail -n 50 logs/reports_integrity.jsonl
  ```
- **ENV (опционально)**: в `.env` можно настроить параметры воркера:
  ```env
  INTEGRITY_INTERVAL_MIN=60
  INTEGRITY_BATCH=50
  INTEGRITY_INCLUDE_STATUSES=registered,delivered
  ```
  Применить и перезапустить сервер:
  ```bash
  set -a; source ./.env; set +a
  uvicorn app.main:app --port 8003 --reload
  ```
- **Ожидаемо**: для текущих демо — `match: true`; при расхождениях запись попадает в выборку `only_failed=true`.

---

- [x] Ok

## Что дальше
## Лаба 11. Client-token (B3)

- **Смысл**: ограничить чтение описаний документов и прямых ссылок `permalink` простым клиентским токеном.
- **Как работает**: если в `.env` задан `CLIENT_READ_TOKEN`, то для `GET/HEAD /doc/{DocID}` и `GET /api/docs/{DocID}` требуется заголовок `X-Client-Token`.
  Исключения: локальные запросы (`127.0.0.1`, `::1`, `localhost`) и документы со статусом `archive`.

- **Шаги**:
  1) Добавьте в `.env`:
     ```env
     CLIENT_READ_TOKEN=demo-client-token
     ```
  2) Примените и перезапустите сервер:
     ```bash
     set -a; source ./.env; set +a
     uvicorn app.main:app --port 8003 --reload
     ```
  3) Проверьте API без токена (должно вернуть 401 вне локалки):
     ```bash
     curl -i "http://localhost:8003/api/docs/${DOC_ID}" | sed -n '1,6p'
     ```
  4) С токеном (200 OK):
     ```bash
     curl -s "http://localhost:8003/api/docs/${DOC_ID}" \
       -H "X-Client-Token: ${CLIENT_READ_TOKEN}" | jq '.doc_id,.status,.title'
     ```
  5) Редирект `/doc/{DocID}` с токеном:
     ```bash
     curl -i "http://localhost:8003/doc/${DOC_ID}" \
       -H "X-Client-Token: ${CLIENT_READ_TOKEN}" | sed -n '1,6p'
     ```
  6) Исключение `archive` (должно пускать без токена):
     ```bash
     curl -s -X PATCH "http://localhost:8003/api/docs/${DOC_ID}" \
       -H "Content-Type: application/json" -d '{"status":"archive"}' | jq .
     curl -i "http://localhost:8003/doc/${DOC_ID}" | sed -n '1,6p'
     ```

- **Ожидаемо**: вне локалки без токена — 401; с правильным токеном — доступ. Для `archive` — доступ без токена.

---

- [x] Ok

## Что дальше

- Добавить авто‑тесты (smoke на register/doc/verify/deliver с моками SMTP/Matrix).
- Добавить админ‑страницу для просмотра БД и перезапуска verify.
- Экспорт отчёта по делу (Markdown с перечнем DocID и дат).

---

## Лаба 12. Docassemble Hook (B5)

- **Смысл**: принимать черновики от внешнего Docassemble и сразу регистрировать их с выдачей `DocID/permalink`.
- **Как работает**: `POST /api/hooks/docassemble` принимает JSON с файлом (base64 или URL). При включённом `DOCASSEMBLE_HOOK_TOKEN` требует заголовок `X-Hook-Token`.

### Подготовка

1) В `.env`:
```env
DOCASSEMBLE_HOOK_TOKEN=demo-hook-token
```
2) Применить и перезапустить:
```bash
set -a; source ./.env; set +a
uvicorn app.main:app --port 8003 --reload
```

### Вариант A: base64
```bash
B64=$(printf "Hello from Docassemble hook\n" | base64 -w0)
curl -s -X POST http://localhost:8003/api/hooks/docassemble \
  -H 'Content-Type: application/json' \
  -H 'X-Hook-Token: demo-hook-token' \
  -d "$(jq -n --arg m '2023-AR-0001' --arg t 'hook_demo.txt' --arg b "$B64" \
        '{matter_id:$m,title:$t,file_base64:$b, class_:"generated", origin_meta:{source:"lab12"}}')" | jq .
```
- **Ожидаемо**: 201 и JSON `{ doc_id, permalink, storage_ref, sha256 }`.

### Вариант B: URL
```bash
curl -s -X POST http://localhost:8003/api/hooks/docassemble \
  -H 'Content-Type: application/json' \
  -H 'X-Hook-Token: demo-hook-token' \
  -d '{"matter_id":"2023-AR-0001","title":"hook_via_url.pdf","file_url":"https://example.com/sample.pdf"}' | jq .
```

### Проверка
```bash
DOC_ID=<из ответа>
curl -s "http://localhost:8003/api/docs/${DOC_ID}" -H "X-Client-Token: ${CLIENT_READ_TOKEN}" | jq '.doc_id,.title,.origin'
```

---

## Лаба 13. Intake статус‑флоу (C1)

- **Смысл**: пройти путь документа, загруженного через `/intake`, через статусы `draft → submitted → triage → registered`, сопровождать ход заметками/сообщениями.
- **Что есть**: форма `/intake` ставит `status=draft`; статусы меняются через `PATCH /api/docs/{DOC_ID}`; фильтры статуса в админке.

### Шаг 1. Загрузка через форму (draft)
```bash
open http://localhost:8003/intake
```
После загрузки получите `DOC_ID` (см. админку или API) и увидите статус `draft`.

Проверка:
```bash
DOC_ID=<из ответа/админки>
curl -s "http://localhost:8003/api/docs/${DOC_ID}" -H "X-Client-Token: ${CLIENT_READ_TOKEN}" | jq '.doc_id,.status,.title'
```

### Шаг 2. Отправка на разбор (submitted)
```bash
curl -s -X PATCH "http://localhost:8003/api/docs/${DOC_ID}" \
  -H "Content-Type: application/json" \
  -d '{"status":"submitted","origin_meta":{"note":"Готово к приёмке"}}' | jq .
```

### Шаг 3. Взять в triage (triage) и добавить теги
```bash
curl -s -X PATCH "http://localhost:8003/api/docs/${DOC_ID}" \
  -H "Content-Type: application/json" \
  -d '{"status":"triage","origin_meta":{"note":"Проверяем комплектность и класс"}}' | jq .
curl -s -X PATCH "http://localhost:8003/api/docs/${DOC_ID}" \
  -H "Content-Type: application/json" \
  -d '{"tags":["intake","priority:normal"]}' | jq .
```

### Шаг 4. Принять в работу (registered) и проверить
```bash
curl -s -X PATCH "http://localhost:8003/api/docs/${DOC_ID}" \
  -H "Content-Type: application/json" \
  -d '{"status":"registered","origin_meta":{"note":"Принято, включаем в отчёты и проверки"}}' | jq .
curl -i "http://localhost:8003/doc/${DOC_ID}" | sed -n '1,5p'
curl -s -X POST "http://localhost:8003/api/docs/${DOC_ID}/verify" | jq .
```

### Замечания и сообщения команде
- Заметки храните в `origin_meta.note`.
- Уведомления: из коробки на `doc_registered` и `deliver`. Для стадий intake можно расширить Notifier.

### Админка
- Фильтр по статусам: `http://localhost:8003/admin/docs?status=draft` и т.п.
- Пагинация: `page`, `per_page`.
