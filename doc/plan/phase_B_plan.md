# Этап B — план работ (шаги и подзадачи)

Дата: 2025-10-05
Статус: черновик (утверждён к началу)

## Обзор
- B1: Фоновые проверки SHA256 и отчёт целостности
- B2: Вшивка DocID/MatterID/SHA256 в PDF/DOCX метаданные
- B3: Мини‑ACL (client-token) для чтения doc и метаданных
- B4: Admin‑вью (список docs + verify)
- B5: Docassemble вебхуки (приём черновиков)

## Чек-лист статуса
- [x] B1.1: Спецификация формата отчёта (JSONL + агрегат)
- [x] B1.2: Планировщик (периодический воркер)
- [x] B1.3: Batch‑verify исполнитель
- [x] B1.4: Хранение результатов (logs/reports_integrity.jsonl)
- [x] B1.5: Эндпойнт GET /api/reports/integrity
- [x] B1.6: Документация и команды проверки
- [x] B2: Вшивка DocID в PDF/DOCX (утилита и гайд)
- [x] B3: Мини‑ACL (client‑token)
- [ ] B4: Admin‑вью
- [ ] B5: Docassemble вебхуки

---

## B1. Отчёт целостности (SHA256)

- **B1.1 Спецификация формата**
  - Отчёт в `logs/reports_integrity.jsonl` (JSON Lines), запись на каждый запуск:
    ```json
    {"ts":"ISO","doc_id":"...","matter_id":"...","status":"registered|delivered|archived","result":{"match":true,"sha256_current":"...","sha256_stored":"..."}}
    ```
  - Агрегация для ответа API: последние записи на DocID; фильтры `matter_id`,`status`,`only_failed`.

- **B1.2 Планировщик**
  - Встроенный таймер в процессе (FastAPI startup task), период `.env` `INTEGRITY_INTERVAL_MIN=60`.
  - Лимиты: `INTEGRITY_BATCH=50` (docs за тик), `INTEGRITY_INCLUDE_STATUSES=registered,delivered`.

- **B1.3 Исполнитель batch‑verify**
  - Обход `docs` по фильтрам; скачивание `gdrive.download_file_content(storage_ref)`; sha256; сравнение.
  - Логирование в `reports_integrity.jsonl` + метка времени.

- **B1.4 Эндпойнт отчёта**
  - `GET /api/reports/integrity?matter_id=&status=&doc_id=&only_failed=&limit=` — агрегированный ответ по последним записям на DocID.

- **B1.5 Документация**
  - Гайд в `doc/guide/labs_v1.md` (Лаба: отчёт целостности), примеры `curl` и ответов.

---

## B2. Вшивка DocID в PDF/DOCX

- **B2.1 Библиотеки**
  - PDF: `pikepdf` или `pypdf` (+ XMP через `libxmp`/минимальный словарь), DOCX: `python-docx` (Core/Custom Properties).

- **B2.2 Утилита**
  - `scripts/embed_metadata.py --file path --doc-id D-... --matter-id 2023-... --sha256 ...` → создаёт копию `*_with_meta.ext`.

- **B2.3 Интеграция (опц.)**
  - Хук в `deliver` для генерации версии с метаданными и загрузки в `Client_Share/`.

---

## B3. Мини‑ACL (client-token)

- **B3.1 Конфиг**: `.env` `CLIENT_READ_TOKEN=<uuid>`
- **B3.2 Guard**: dependency на `GET/HEAD /doc/{id}` и `GET /api/docs/{id}` — требовать `X-Client-Token` при наличии токена.
- **B3.3 Исключения**: локалка (`127.0.0.1`), админ‑флаг, `status=archive`.
- **B3.4 Документация**: примеры вызова.

---

## B4. Admin‑вью

- Простая страница `/admin/docs` (Jinja2): таблица `doc_id,title,status,updated_at,[verify]`.

---

## B5. Docassemble вебхуки

- `POST /api/hooks/docassemble` → маппинг на `register` (`origin=generated`).

---

## Готовность к старту
- A: завершён, отчёт `doc/guide/acceptance_A.md` — есть.
- B: начинаем с B1.1 (спецификация) → B1.2 (планировщик) → B1.3/1.4 (исполнитель+эндпойнт) → B1.5 (доки).
