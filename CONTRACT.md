# AI Hatshy — контракт модулей и API (внутренний документ для разработки)

Проект: локальный ИИ-секретарь совещаний (кейс HackAlem «Автопротоколирование совещаний с фиксацией поручений»).
Всё работает локально: ASR GigaAM-Multilingual (ONNX), диаризация sherpa-onnx, LLM gemma4 в Ollama (localhost). Никаких внешних API.
Сервер: FastAPI, `uvicorn app.main:app --port 8000`. Python: `.venv/bin/python` (3.12). SQLite: `data/hatshy.db`.

## Раскладка файлов (кто владеет)
- `app/main.py`, `app/db.py`, `app/pipeline/*`, `app/reminders.py` — backend (владелец: главный разработчик)
- `app/export/docx_export.py`, `app/export/pdf_export.py` — экспорт (агент export)
- `web/index.html` — веб-интерфейс (агент web), `web/mobile.html` — мобильный (агент mobile)
- `app/static/` — общая статика: `support.js` (dc-runtime дизайна), `vendor/react*.js`, `fonts/*.woff2`, `fonts.css`
- `samples/`, `scripts/eval.py`, `scripts/make_samples.sh` — тестовые записи и оценка (агент samples)
- Дизайн-исходники (НЕ менять): `web/design_web.dc.html`, `web/design_mobile.dc.html`

## Раздача статики
- `GET /` → `web/index.html`; `GET /m` → `web/mobile.html`; `GET /static/*` → `app/static/*`.
- В страницах НЕ должно быть внешних URL (закрытый контур): React грузится из `/static/vendor/`, шрифты из `/static/fonts.css`.
  Подключение: `<link rel="stylesheet" href="/static/fonts.css">`, затем `<script src="/static/vendor/react.production.min.js"></script><script src="/static/vendor/react-dom.production.min.js"></script><script src="/static/support.js"></script>` (если window.React уже есть, support.js не ходит в CDN).

## Даты и статусы
- `today` — «текущая дата» сервера (для демо её можно подменить: `POST /api/settings {"today":"2026-10-20"}`), формат ISO `YYYY-MM-DD`.
- Статус поручения хранится как `work` | `done`. «Просрочено» вычисляется: `status=='work' && due && due < today`. Поручения без срока (`due=null`) никогда не просрочены, показывать срок как `deadline_raw` или «без срока».
- Приоритет: `high` | `mid` | `low` (в UI: Высокая/Средняя/Низкая). Направление `direction` — строка (напр. «Финансы», «Охрана труда», «Закупки», «Юридическое», «Кадры», «Производство», «Отчётность», «Другое»).
- Цвет человека: `hue` (0–359), в UI `oklch(0.6 0.12 {hue})` как в дизайне. `ini` — инициалы (2 буквы).

## Объекты
Meeting (в списках):
```json
{"id": 1, "title": "Совещание №1", "date": "2026-09-23", "date_label": "23 сен", "duration_s": 274.2, "dur_label": "4:34",
 "source": "file", "source_label": "Файл MP3", "status": "done", "step": 5, "pct": 100, "step_label": "Готово",
 "people": [{"name": "Асхат Ерланович", "ini": "АЕ", "hue": 255}], "langs": "RU KZ MIX", "tasks_count": 11, "unconfirmed": 3,
 "consent": true, "error": null}
```
`status`: `queued` | `processing` | `review` (готово, есть неподтверждённые поручения) | `done` | `error`.
`source`: `file` | `mic` | `conf` (звук конференции), `source_label` — человекочитаемо.

Task:
```json
{"id": 7, "meeting_id": 1, "meeting_title": "Совещание №1", "meeting_date": "2026-09-23", "num": "1-7",
 "title": "Провести полный аудит датчиков утечки газа и СИЗ на 11 площадках",
 "owner": "Нурлан Сагатович", "owner_short": "Нурлан С.", "ini": "НС", "hue": 20, "owner_kind": "person|department|absent|chair",
 "co_owners": ["Тимур Болатович"], "issued_by": "Асхат Ерланович",
 "due": "2026-10-15", "deadline_raw": "к пятнадцатому октября", "status": "work", "priority": "high", "direction": "Охрана труда",
 "conf": 92, "quote": "…точная цитата из транскрипта…", "t": "00:03:20", "seg_idx": 24, "confirmed": false,
 "history": [{"x": "Создано ИИ из протокола, уверенность 92%", "d": "23 сен"}]}
```

Segment: `{"idx": 0, "t": "00:00:12", "start": 12.0, "end": 15.1, "speaker": "SPEAKER_00", "name": "Асхат Ерланович", "hue": 255, "l": "RU", "x": "Коллеги, начинаем…", "task_id": null}` (`l`: `RU` | `KZ` | `RU+KZ`).

Speaker: `{"id": 3, "label": "SPEAKER_00", "name": "Асхат Ерланович", "role": "заместитель председателя правления", "hue": 255, "ini": "АЕ", "pct": 62, "confidence": 0.9}`.

Notification: `{"id": 1, "type": "over|soon|today|info", "title": "Просрочено: …", "text": "Нурлан С. · срок был 15 октября", "channel": "В приложении · Email (локальный outbox)", "time": "09:00", "created": "2026-09-23T09:00:00", "task_id": 7, "meeting_id": 1, "read": false}`.

## API
- `GET /api/state` → `{"today", "meetings":[Meeting], "tasks":[Task], "people":[Person], "notifications":[Notification], "settings":Settings}`
  - Person: `{"name","ini","hue","pos","langs","meetings","open","voice": false}`
  - Settings: `{"today","rules":{"d3":true,"d0":true,"daily":true,"esc":false},"channels":{"inapp":true,"email":true,"sed":false},
     "infra":[{"k","v"}],"models":[{"k","d","v"}],"privacy":{"anon":false}}`
- `GET /api/meetings/{id}` → Meeting + `{"summary":{"short":"…","decisions":["…"],"topics":[{"label":"…","t":"00:00:00"}],"numbers":[{"value":"71%","meaning":"…"}]},
   "speakers":[Speaker], "segments":[Segment], "tasks":[Task]}`
- `POST /api/meetings` (multipart): `file` (обязательно), `title`, `date` (ISO, по умолчанию today), `participants` (опц., имена через запятую), `num_speakers` (опц.), `notify` ("1"/"0"), `source` ("file"|"mic"|"conf") → `{"id"}`. Обработка идёт в фоне.
- `GET /api/meetings/{id}/progress` → `{"status","step","pct","step_label","steps":["Сохранение и декодирование аудио","Диаризация: разделение говорящих","Распознавание речи: RU · KZ · смешанная","Выделение поручений, ответственных и сроков (локальная LLM)","Формирование саммари и протокола"],"error"}`
- `DELETE /api/meetings/{id}` → `{"ok":true}` (удаляет аудио и все данные совещания)
- `GET /api/meetings/{id}/audio` → исходный аудиофайл (для плеера транскрипта)
- `PATCH /api/tasks/{id}` JSON `{"status"?, "confirmed"?, "title"?, "owner"?, "due"?, "priority"?}` → Task
- `POST /api/meetings/{id}/confirm_all` → `{"ok":true}`
- `POST /api/tasks/{id}/remind` → `{"ok":true,"notification":Notification}`
- `POST /api/meetings/{id}/send_extracts` → `{"ok":true,"sent":N,"outbox":["data/outbox/…eml"]}` (письма пишутся в локальный outbox, наружу ничего не уходит)
- `PATCH /api/speakers/{id}` JSON `{"name"}` → `{"ok":true}` (ручное переименование говорящего)
- `GET /api/meetings/{id}/export?fmt=pdf|docx&sections=summary,tasks,transcript,people,sign&anon=0|1` → файл
- `POST /api/settings` JSON `{"today"?, "rules"?, "channels"?, "privacy"?}` → Settings
- `POST /api/reminders/run` → `{"created":N}` (прогон правил напоминаний на дату today)
- `POST /api/notifications/read_all` → `{"ok":true}`
- Live (псевдо-реальное время, запись в браузере через MediaRecorder, timeslice 4000 мс):
  - `POST /api/live/start` JSON `{"title","notify":true,"source":"mic|conf","participants"?}` → `{"id"}` (создаёт meeting со status `processing`, step 0)
  - `POST /api/live/{id}/chunk` (multipart `chunk` = очередной Blob из MediaRecorder, audio/webm) → `{"segments":[{"t":"00:00:04","x":"…","l":"RU"}]}` — сервер дописывает кусок в файл и распознаёт только новый хвост; без диаризации.
  - `POST /api/live/{id}/stop` → `{"ok":true}` — запускает полный пайплайн по всей записи (дальше прогресс через `/progress`).

## Экспорт (агент export)
`app/export/docx_export.py: build_docx(meeting: dict, sections: set[str], anon: bool) -> bytes`
`app/export/pdf_export.py: build_pdf(meeting: dict, sections: set[str], anon: bool) -> bytes`
`meeting` — ровно объект из `GET /api/meetings/{id}` (+ ключ `org`, по умолчанию «АО «Самрук-Қазына Ондеу»» можно не выводить, если нет).
Формат — как у эталонных протоколов организаторов (см. spec): «Протокол совещания», тема, дата/длительность/участники, «Краткое содержание», «Принятые решения», таблица «№ | Поручение | Ответственный | Срок» (срок: дата `dd.mm.yyyy` или deadline_raw или «не указан»), «Стенограмма» с таймкодами и именами (язык реплики), «Участники» (имя, роль, доля речи), подписи «Председатель ____ / Секретарь ____». Шрифт с кириллицей И казахскими буквами (ә ғ қ ң ө ұ ү һ і).
`anon=True` → ФИО заменяются на «Участник 1…N» (стабильно по порядку), в цитатах/стенограмме тоже.
