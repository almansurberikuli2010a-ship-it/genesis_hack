# AuditGym 3.3 — RL-среда для дообучения ИИ-аудитора финансовой отчётности

Воспроизводимый симулятор аудита. Генератор создаёт синтетическую компанию за один отчётный
период: главную книгу, счета, платежи, курсы валют и учётную политику. В книги внесены
**скрытые финансовые ошибки** с записанной причиной. Агент исследует записи через узкий
read-only интерфейс инструментов и отправляет выводы с доказательствами. Оценщик в отдельном
процессе сопоставляет их с эталоном общим matcher'ом и выдаёт терминальную награду
`clip(2*F1 - 1 - 0.002*inspection_steps, -1, 1)`.

Это **среда для возможного дообучения модели**: веса она не обновляет. Платные API, GPU и сеть
не нужны — только стандартная библиотека Python ≥ 3.11. Код, policy.json, текст задачи,
сообщения агенту, отчёты и документация — на русском. Имена полей, ID и коды ошибок —
латиницей, как в ТЗ. Все компании, люди, счета и суммы вымышлены.

---

## Единая команда проверки

```bash
python3 -m auditgym check
```

Нужен любой `python3` версии 3.11 или новее; в образе — `python3.11 -m auditgym check`.
То же через make: `make check PY=python3` (по умолчанию `PY=python3.11`).

Работает офлайн примерно за 80 секунд (Python 3.13, macOS; в Docker-образе на 2 CPU — около 115 секунд). Код возврата `0` означает, что всё прошло. Отчёты:
`reports/check_report.md` и `.json` (версии, git-коммит, итоги разделов, базовые политики,
разбор сбоев, ресурсы), `reports/jury_scorecard.md` и `.json`.

В контейнере — официальный прогон без сети:

```bash
docker build -t auditgym .
```

```bash
docker run --rm --network none --cpus 2 --memory 4g auditgym
```

В образ `.git` не копируется (`.dockerignore`), поэтому коммит для
`reports/check_report.json → git.commit` передаётся аргументом сборки:
`docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) -t auditgym .` (он попадает в
переменную образа `AUDITGYM_GIT_COMMIT`; `dirty` в образе — `null`). Без аргумента официальная
команда работает как прежде, коммит — `null`. Отчёты на GitHub собраны workflow из
`.github/workflows/`: `docker-check.yml` (сборка образа с `--build-arg GIT_COMMIT=${{ github.sha }}`
и официальный `docker run`) и `update-reports.yml` (`check` на Linux с Landlock и коммит
`reports/` в `main`); оба запускаются вручную (`workflow_dispatch`) или пушем в свою ветку.

Зависимостей нет: `requirements.txt` пуст намеренно, в `pyproject.toml` указано
`dependencies = []`. Образ закреплён точной версией `python:3.11.11-slim-bookworm`, UTF-8 задан
явно, код в образе принадлежит root, писать пользователь прогона может только в `reports/`.
**Проверено запуском**: в образе (Python 3.11.11, Linux, Landlock ABI 8) официальная
команда печатает «ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ» за 113 с, песочница процесса политики —
`landlock-abi8`, все 178 тестов (на тот момент) выполнены без пропусков, запись в код среды
запрещена (`PermissionError` у пользователя uid 10001). Текущие 188 тестов выполнены без
пропусков на Linux, Python 3.11, Landlock (workflow `update-reports`, коммит `c72f07c`,
`reports/check_report.json`). Лимиты ТЗ: 2 CPU, 4 ГБ RAM, без GPU, 60 с на эпизод,
100 шагов. Измерено `check` (Python 3.13, macOS): пик памяти оркестратора 191 МБ (вместе с
тестами в том же процессе), процесса среды 79 МБ, самый долгий эпизод 0.09 с.

**На Windows** команда тоже работает (`python -m auditgym check`; проверено запуском: Windows 10,
Python 3.12.10, «ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ» за 183 с), но песочницы процесса политики там нет: `check`
печатает `песочница процесса политики: process`, политика работает только в отдельном процессе
(`policy_sandbox` = `"none: нет песочницы для win32"`), 6 тестов песочницы пропускаются с причиной,
пик памяти не измеряется (модуля `resource` в Windows нет: в `resources` — `null` и пометка
`peak_rss_note`). Официальный прогон — в контейнере.

Для жюри есть короткая инструкция по проверке каждого критерия: [JURY.md](JURY.md).

### Все команды

Все команды запускаются из корня проекта.

| Команда | Назначение |
|---|---|
| `python3 -m auditgym check` | единая проверка: тесты, детерминизм, данные, базовые политики, утечки (и в опубликованных траекториях), парные проверки, репетиция оценки жюри |
| `python3 -m auditgym jury` | репетиция оценки по разделу 10 ТЗ: чёрный ящик, 20 свежих «скрытых» эпизодов, таблица баллов |
| `python3 -m auditgym generate --seed 101 102 --config '{"preset":"interacting"}' --out DIR` | эпизоды для **любых** seed и конфигураций → `DIR/public`, `DIR/grader` |
| `python3 -m auditgym verify DIR` | **независимая** проверка данных и эталона **только по файлам** |
| `python3 -m auditgym grade DIR --findings f.json --steps N` | оценить `findings.json` общим matcher'ом |
| `python3 -m auditgym pair --seed 1 2 --config cfg.json` | парные проверки: повтор, смена ID и порядка, внешнее преобразование файлов |
| `python3 -m auditgym transform DIR --episode EP-… --out DIR2` | переименовать все ID и перемешать строки экспортированного эпизода |
| `python3 -m auditgym run --policy reconciler --split dev [--mode train\|eval] [--trajectories out.jsonl]` | прогнать политику |
| `python3 -m auditgym demo > reports/demo_transcript.txt` | пошаговый показ работы среды (записанный показ — в `reports/demo_transcript.txt`) |
| `python3 -m auditgym export` / `trajectories` | пересобрать `episodes/` и `trajectories/trajectories.jsonl` |
| `python3 -m unittest discover -s tests -t .` | только тесты (191; на macOS 3 теста Landlock пропускаются) |
| `python3 scripts/mutation_test.py` | мутационный тест самой проверки (около 2 мин) |

---

## Для жюри: `judge_adapter.py` (единый способ подключения)

Адаптер реализует интерфейс из определений проверок. **Рабочая директория — корень проекта**
(в Docker-образе — `/app`, это `WORKDIR`). Адаптер сам добавляет корень в `sys.path`; процессу
жюри достаточно `import judge_adapter` из корня (или `PYTHONPATH=/app`). Пример в контейнере:

```bash
docker run --rm --network none --cpus 2 --memory 4g -v "$PWD/jury:/jury" auditgym python -c "import judge_adapter as J; J.generate_episode(123, {'preset': 'mixed'}, '/jury/ep1')"
```

Каталог вывода должен быть доступен на запись пользователю образа (uid 10001).

### Точки входа

```python
import judge_adapter as J

J.generate_episode(seed, team_config, "/jury/ep1")   # плоский каталог из 8 файлов
env = J.make_env()                                    # среда и оценщик — в отдельном процессе
obs, info = env.reset(0, {"mode": "closed", "episode_dir": "/jury/ep1"})
obs, reward, terminated, truncated, info = env.step(
    {"name": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 50}})
obs, reward, terminated, truncated, info = env.step({"name": "submit", "args": {"findings": findings}})
J.score_episode("/jury/ep1", findings, inspection_steps)   # закрытый вызов оценщика
env.close()
```

- **`generate_episode(seed, config, output_dir)`** — обёртка над генератором команды.
  `config` — конфиг генератора (раздел «Конфигурация»); объект с ключом `team_config` тоже
  принимается. Каталог плоский: `manifest.json, ledger.csv, invoices.jsonl, payments.jsonl,
  fx.csv, policy.json` (открытые, форматы §03) и закрытые `truth.json, provenance.json`.
  Других файлов нет: книги до внесения ошибок восстанавливаются из `provenance.json`.
- **`make_env()`** → объект с `reset`, `step`, `close` (и `with`). Среда и оценщик работают в
  дочернем процессе `auditgym.server`; эталон в память вызывающего процесса не попадает.
- **`reset(seed, config)`**, `config = {"mode": "closed" | "train", "episode_dir": str?,
  "team_config": dict?}`:
  - есть `episode_dir` — загружается ровно этот каталог (записи и ID как в файлах; seed и
    team_config не используются); повторный reset начинает его заново;
  - нет `episode_dir` — эпизод генерируется по `seed` и `team_config`;
  - `mode` по умолчанию `"closed"`: награда политике — числовой `0` на каждом шаге, включая
    последний, `info = {}`; `"train"`: на последнем шаге — терминальная награда и
    `info.grading` (valid, reward, P, R, F1, inspection_steps).
- **`step(action)`**, `action = {"name": <действие>, "args": {...}}` (или JSON-строка с таким
  объектом). Имена: `list_records(table, cursor, limit)`, `get_record(table, id)`,
  `get_document(doc_id)`, `calculator(expression)`, `submit(findings)`. Таблицы: `ledger`,
  `invoices`, `payments`, `fx`. `id` в get_record — `row_id` | `invoice_id` или `doc_id` |
  `payment_id` или `doc_id` | `fx_id`.
- **Наблюдения.** Первое: `{"task", "manifest", "policy", "remaining_steps": 100}`. Следующие:
  `{"result", "error", "remaining_steps"}`; при ошибке `result = null`.
  - `list_records` → `{"records": [...], "next_cursor": null | "<table>:<смещение>"}`; первый
    cursor — `null`, страница не больше 50 записей, курсор стабилен;
  - `get_record`, `get_document` → сама запись или документ;
  - `calculator` → десятичная строка (`"0.3"` для `0.1+0.2`);
  - `submit` → `{"accepted": true}` (без оценки правильности) или ошибка структуры.
- **`score_episode(episode_dir, findings, inspection_steps)`** — общий matcher и награда по
  файлам каталога. Валидный ответ → `{"tp", "fp", "fn", "precision", "recall", "f1",
  "return"}`; неверная структура → `"return": -1.0`, остальные поля `null`, ошибка — в журнале
  оценщика. `findings=None` — ответа не было (лимит шагов или времени): пустые выводы.
- **Журнал оценщика**: `judge_adapter.GRADER_LOG` (список), логгер `auditgym.judge` и, если
  задана переменная окружения `AUDITGYM_GRADER_LOG`, строка JSON в этом файле. Внутри среды
  журнал — `env.grader_report()["log"]` (только для оркестратора).
- **Базовые политики через адаптер**: `J.baseline("random" | "reconciler" | "flag_all" |
  "oracle", episode_dir)` и `J.run_on_episode(policy, episode_dir)` — прогон и оценка так, как
  это делает жюри. Oracle строит выводы из `truth.json` только на стороне жюри и отправляет их
  обычным submit. `run_on_episode(..., isolated=True)` (по умолчанию) запускает политику в её
  ограниченном процессе, как `run_policy`: каталог эпизода, файлы оценщика и код для записи ей
  закрыты, в результате есть `policy_sandbox`; `isolated=False` — доверенный запуск в процессе
  жюри.

### Коды ошибок

Ошибка — `{"code", "message"}`: код латиницей, сообщение на русском не длиннее 119 символов,
без путей, seed и закрытых данных. Каждая ошибка, кроме `episode_finished`, расходует шаг.

| Код | Когда |
|---|---|
| `invalid_action` | оболочка не `{"name", "args"}`: не объект, нет `name`, лишние ключи, неразборчивый JSON |
| `unknown_action` | `name` не из пяти действий |
| `invalid_arguments` | неверный или лишний аргумент: `args` не объект, неизвестная таблица, `limit` не целое ≥ 1, лишние ключи (сообщение называет не больше трёх, каждый не длиннее 16 символов) |
| `not_found` | нет записи или документа с таким ID |
| `invalid_cursor` | курсор другой таблицы, неверного формата или вне диапазона |
| `calculator_error` | выражение вне арифметической грамматики, деление на ноль, слишком большое число |
| `invalid_submission` | неверная структура `findings`: эпизод завершён (`terminated=true`), награда −1 у оценщика |
| `time_limit` | истекли 60 с от начала reset: `truncated=true`, пустой ответ |
| `episode_finished` | шаг после завершения эпизода; ничего не меняет |

### Пограничные решения

- submit — одно из 100 действий, но не входит в `inspection_steps`; все предыдущие шаги, в том
  числе ошибочные, входят. Submit на действии 100 принимается.
- 100 действий без submit — `truncated=true` и пустой ответ.
- Лимит времени — 60 с **от начала reset**, включая время политики.
- Повторная отправка после завершения не меняет зафиксированный результат.

---

## Соответствие блокам официальных определений проверок

| Блок | Как жюри прогоняет у нас | Чем покрыто в проекте |
|---|---|---|
| **1. Контракт, C01–C20** | `make_env()` → `reset` на фикстурах C, E, L, U через `episode_dir`, действия в оболочке `{"name","args"}` | `tests/test_judge_adapter.py`, класс `Block1Contract` (`test_C01…test_C20`); ещё 20 проверок через `SubprocessEnv` — `jury`, «Контракт и изоляция» |
| **2. Закрытые эпизоды, D01–D20** | `generate_episode(seed, team_config, dir)` с конфигами из таблицы ниже; независимая сверка по 8 файлам | `tests/test_judge_pairs.py`, `MatrixD.test_D01_D20_generated_and_valid` (`datachecks.check_records` по файлам: равенства до внесения, объявленное расхождение после, перепроводка, достаточность каждого набора доказательств); `check` → `data_and_truth` (220 эпизодов) |
| **3. Matcher и награда, 1–20 и M01–M12** | `score_episode(dir, findings, inspection_steps)` | `tests/test_judge_adapter.py`: `Block3MatcherReward` (`test_01…test_20`), `OpenExamplesM` (`test_M01…test_M12`); `tests/test_matcher_reward.py` |
| **4. Парные проверки, P01–P20** | копия каталога жюри: перестановка записей и/или взаимно однозначная замена ID (в truth и provenance тоже) → `reset(…, {"episode_dir": копия})`, oracle с новыми ID и тем же числом шагов | `tests/test_judge_pairs.py`, `PairsP`: `test_P01_P05_permutation`, `test_P06_P10_id_remap`, `test_P11_P20_permutation_and_remap`; ещё `pair` и `check` → `paired_checks` |
| **Инженерия** | см. таблицу в [JURY.md](JURY.md) | `check` пишет версии и коммит; `trajectories.jsonl`; разбор сбоев ниже |

### D01–D20 конфигом генератора

Только документированными ключами конфига (`auditgym/config.py`); незаданные ключи — по
умолчанию (в том числе отвлекающие случаи по умолчанию, если `distractors` не указан). Каждая
ячейка `team_config` — полный JSON, который `judge_adapter.generate_episode(seed, config,
output_dir)` принимает как есть. Ровно эти конфиги собирает `tests/test_judge_pairs.py`
(`D_MATRIX`).

| Эпизоды | Сценарий | `team_config` |
|---|---|---|
| D01 | обычная сверка, чисто | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "distractors": {"split_payment": 0, "multi_invoice_payment": 0, "partial_open": 0, "refund": 0, "recurring_amount": 0, "tz_boundary": 0, "unremitted_payment": 0, "next_period_doc": 0, "same_day_repeat": 0, "prior_period_doc": 0, "boundary_fx_partial": 0}}` |
| D02 | корректно разбитый платёж | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "distractors": {"split_payment": 3, "multi_invoice_payment": 0, "partial_open": 0, "refund": 0, "recurring_amount": 0, "tz_boundary": 0, "unremitted_payment": 0, "next_period_doc": 0, "same_day_repeat": 0, "prior_period_doc": 0, "boundary_fx_partial": 0}}` |
| D03 | корректный возврат | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "distractors": {"split_payment": 0, "multi_invoice_payment": 0, "partial_open": 0, "refund": 2, "recurring_amount": 0, "tz_boundary": 0, "unremitted_payment": 0, "next_period_doc": 0, "same_day_repeat": 0, "prior_period_doc": 0, "boundary_fx_partial": 0}}` |
| D04 | законные повторяющиеся суммы | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "distractors": {"split_payment": 0, "multi_invoice_payment": 0, "partial_open": 0, "refund": 0, "recurring_amount": 2, "tz_boundary": 0, "unremitted_payment": 0, "next_period_doc": 0, "same_day_repeat": 2, "prior_period_doc": 0, "boundary_fx_partial": 0}}` |
| D05 | валютная и периодная граница | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "distractors": {"split_payment": 0, "multi_invoice_payment": 0, "partial_open": 0, "refund": 0, "recurring_amount": 0, "tz_boundary": 4, "unremitted_payment": 0, "next_period_doc": 2, "same_day_repeat": 0, "prior_period_doc": 1, "boundary_fx_partial": 2}, "foreign_share": 1.0}` |
| D06 | дубль счёта | `{"errors": {"duplicate_invoice": 1, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}}` |
| D07 | дубль счёта, только базовая валюта | `{"errors": {"duplicate_invoice": 1, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "foreign_share": 0.0}` |
| D08 | дубль счёта, 2 ошибки | `{"errors": {"duplicate_invoice": 2, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}}` |
| D09 | пропущенное распределение | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 1, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "variants": {"payment_allocation": "missing"}}` |
| D10 | частичное распределение | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 1, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "variants": {"payment_allocation": "partial"}}` |
| D11 | частичное распределение, 2 ошибки | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 2, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "variants": {"payment_allocation": "partial"}}` |
| D12 | курс другой даты | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 1, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "variants": {"fx_rate": "wrong_date"}}` |
| D13 | перевёрнутый курс (есть и `rounded_rate`) | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 1, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "variants": {"fx_rate": "inverted_rate"}}` |
| D14 | документ прошлого периода проведён поздно | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 1}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "variants": {"period_cutoff": "prior_period"}}` |
| D15 | документ следующего периода проведён задним числом | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 1}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 0}, "variants": {"period_cutoff": "next_period"}}` |
| D16 | сценарий 1: частичный валютный платёж на границе периода | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 1, "masked_duplicate": 0}}` |
| D17 | сценарий 1, только валютные счета | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 1, "masked_duplicate": 0}, "foreign_share": 1.0}` |
| D18 | сценарий 1, 2 случая | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 2, "masked_duplicate": 0}}` |
| D19 | сценарий 2: замаскированный дубль | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 1}}` |
| D20 | сценарий 2, 2 случая | `{"errors": {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}, "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 2}}` |

Пресеты: `clean`, `clean_hard`, `interacting`, `interacting_partial_fx_boundary`,
`interacting_masked_duplicate`, `four_types`, `mixed`, `heavy` (12 ошибок), `only_<тип>`.

### Соответствие четырёх типов ошибок

Канонические `affected_ids`, сумма и наборы доказательств опубликованы в
`policy.json → finding_types` и закодированы в `auditgym/render.py` (эталон) и
`auditgym/matcher.py` (сопоставление). Сумма — в базовой валюте эпизода, `currency` — её код.

| Тип (формулировка ТЗ) | Варианты (`variants`) | `affected_ids` | `expected_amount_minor` | Допустимые наборы доказательств |
|---|---|---|---|---|
| `duplicate_invoice` — дубли счетов | точная копия счёта (клиент, момент, сумма, строки с PO) под новым номером | оба `invoice_id` | проводка 1200 одной копии | `{doc_id оригинала, doc_id копии, строка}`, строка — 1200 или 4000 любой из копий (4 набора) |
| `payment_allocation` — пропущенное или частичное распределение | `missing` (вся расписанная сумма на 2900), `partial` (часть на 2900); в сценарии 2 — зачёт на чужой счёт | `[payment_id, invoice_id из invoice_refs]` | ошибочная кредитовая строка, как проведена | `{doc_id платежа, эта строка}` |
| `fx_rate` — неверная дата или курс конвертации | `wrong_date`, `rounded_rate`, `inverted_rate` (только для пар около паритета) | `[invoice_id]` или `[payment_id]` | `\|проведено − верно\|` по основной строке | `{doc_id, основная строка, fx_id курса даты по политике}`; у счёта основная строка — 1200 или 4000 |
| `period_cutoff` — ошибка отнесения к периоду | `prior_period` (проведён поздно), `next_period` (проведён задним числом; счёт или оплата) | `[invoice_id]` или `[payment_id]` | основная строка, проведённая в периоде | `{doc_id, основная строка}`; у счёта — 1200 или 4000 |

`"random"` (по умолчанию) — вариант выбирается генератором; фактический вариант каждой ошибки
— в `provenance.mutations[].variant`. Сценарии взаимодействия свои варианты не меняют.

---

## Matcher и награда (код: `auditgym/matcher.py`)

Правила — «Точные соглашения matcher» определений проверок; они же опубликованы в
`policy.json → matching`.

- **Структура** (неверная → весь submit −1, ошибка — в журнале оценщика, политике — только
  `invalid_submission`): неверный тип значения; нет обязательного поля (`episode_id`,
  `findings`, шесть полей вывода); больше 30 выводов; больше 10 элементов **исходного** списка
  `evidence_ids` (до удаления повторов); `explanation` длиннее 1000 кодовых точек Unicode;
  `claimed_amount_minor` не JSON-целое (bool, строка, дробное — неверный тип); `episode_id` не
  равен `manifest.episode_id`. Лишние ключи игнорируются.
- **ID — точные строки**: с учётом регистра и пробелов, без синонимов `doc_id ⇄
  invoice_id/payment_id`, без приведения чисел.
- **`affected_ids`** — множество: порядок и повторы не важны; должно точно совпасть с
  каноническим набором ошибки.
- **`evidence_ids`** — множество; каждый ID обязан существовать в открытых записях эпизода
  (`row_id`, `doc_id`, `invoice_id`, `payment_id`, `fx_id`), иначе вывод не сопоставляется (FP);
  хотя бы один допустимый набор входит целиком; лишние существующие ID не мешают.
  `invoice_id`/`payment_id` — существующие ID, но `doc_id` в наборе не заменяют.
- **`type`** — точная строка из четырёх; неизвестная строка — FP, не ошибка структуры.
- **`currency`** — точное совпадение с кодом валюты эталона, без смены регистра и обрезки.
- **Сумма**: `|claimed − expected| ≤ τ`. **Источник τ** для ошибки эталона: главный —
  открытый `policy.json → matching.amount_tolerance_minor[<type>]` этого эпизода; если такого
  ключа нет — `truth.json → tolerance_minor` ошибки; если нет и его — τ = 0. Закрытое значение
  из `truth.json` опубликованный τ не расширяет и не сужает. Генератор пишет в оба файла
  **τ = 0** для всех типов: суммы эталона — точный пересчёт по политике, документированного
  округления сверх него нет.
- **Неоднозначность**: двудольный граф (повторы выводов — отдельные вершины), паросочетание
  максимальной мощности; среди равных — лексикографически наименьшая последовательность пар
  `(issue_id, индекс вывода с 0)`, отсортированная по `issue_id` (`matcher.select_pairs`).
  TP — число пар, FP = выводов − TP, FN = ошибок − TP. Повтор верного вывода — FP.
- **Метрики**: P, R, F1 точно по ТЗ, включая крайние случаи (пустой эталон и пустой ответ → F1=1).
- **Общая награда**: `clip(2*F1 - 1 - 0.002*inspection_steps, -1, 1)`. **Экспериментальная**
  (половина за верные тип и ID при неверной сумме или доказательствах) показывается отдельно и
  никогда не подменяет общую.

---

## Структура репозитория

```
judge_adapter.py      адаптер жюри: generate_episode, make_env, step {"name","args"}, score_episode
auditgym/
  config.py           версии, конфигурация, пресеты, variants, строгая проверка, предел документов
  money.py            деньги, курсы, время (сторона генератора)
  story.py            шаг 1: скрытая история компании и корректные отвлекающие случаи
  posting.py          шаг 2: двойная запись по опубликованной политике (план счетов)
  injection.py        шаг 3: внесение ошибок как записанных причинных изменений
  render.py           шаг 4: публичные ID, порядок, эталон, provenance с полным диффом книги
  generator.py        generate(seed, config) с детерминированными повторными попытками
  policy_text.py      policy.json, текст задачи, справка по действиям (на русском)
  env.py              AuditEnv: reset/step, 5 инструментов, лимиты, режимы train/eval, эпизод из каталога
  server.py           среда и оценщик в отдельном процессе (JSON lines), два режима запуска
  matcher.py          общий matcher, метрики, терминальная и экспериментальная награда (открытый код)
  calculator.py       песочница десятичной арифметики
  audit_rules.py      НЕЗАВИСИМЫЙ проверяющий: равенства, перепроводка, эталонный аудитор, доказательства
  provenance_tools.py книги до ошибок из provenance.json
  datachecks.py       проверки данных и эталона (по объектам или по файлам)
  policies.py         базовые политики: random, reconciler, flag_all, oracle + демо rule_auditor
  adapters.py         run_policy: любая политика в отдельном процессе с песочницей
  landlock.py         Landlock (Linux, ctypes): белый список путей процесса политики, no_new_privs, non-dumpable
  runner.py           цикл эпизода, наборы прогонов, траектории, сторожевой таймер
  export.py           запись и чтение эпизодов (public/grader и плоский каталог), UTF-8
  transform.py        безвредное преобразование файлов эпизода (ID + порядок)
  jury.py             репетиция оценки по разделу 10
configs/              train.json, dev.json — открытые seed и конфигурации (config_version 3.3)
episodes/             10 train + 10 dev: public/<id>/*; grader/<id>/truth.json, provenance.json, pre_injection/
trajectories/         trajectories.jsonl (train/dev; 104 прогона, из них 12 по лимиту шагов и 18 в режиме eval)
schemas/              JSON Schema draft-04: action, findings, truth, observation
scripts/              mutation_test.py
tests/                191 тестов, в том числе по официальным определениям (test_judge_adapter, test_judge_pairs)
reports/              check_report, jury_scorecard, demo_transcript
DESIGN.md             записка о дизайне
JURY.md               как проверить каждый критерий
```

---

## Версии

| Что | Значение | Где |
|---|---|---|
| среда (`ENV_VERSION`), пакет | 3.3.0 | `auditgym/env.py`, `pyproject.toml`; `info.env_version` после reset |
| генератор (`GENERATOR_VERSION`) | 3.3.0 | `auditgym/config.py`; `manifest.generator_version`, `provenance.generator_version` |
| формат данных и схем (`SCHEMA_VERSION`) | 3.2 | `auditgym/config.py`; `manifest.schema_version`, `truth.schema_version` |
| формат конфига (`CONFIG_VERSION`) | 3.3 | `auditgym/config.py`; `configs/*.json` и каждый их эпизод; `manifest.config_version` |
| политика | 3.3 | `policy.json → policy_version` |

- Генератор случайных чисел истории зависит от `(generator_version, seed, экономическая
  часть конфига, attempt)` через медленный ключ истории (scrypt, см. «Эпизод и форматы
  данных»): новая версия генератора меняет все эпизоды. `check` сверяет хэши
  эпизодов с опубликованными `episodes/*/index.json` (там же seed и конфиг каждого).
- Конфиг с другой `config_version` отвергается (`ConfigError`).
- `reports/check_report.json` пишет все четыре версии, Python, платформу и git-коммит с
  признаком незакоммиченных правок; каждая строка `trajectories.jsonl` — `env_version`,
  `generator_version` и `mode`. `check` проваливается, если траектории собраны другой версией.
- История: **3.0.0** — первая версия (коммит `de26f77`). **3.1** — изоляция политики в
  `run_policy`, точное выполнение конфига, `provenance.actual`, `fx_id_format`, схемы draft-04
  (отдельно не публиковалась). **3.2** — официальные определения проверок: `judge_adapter.py`,
  плоский каталог эпизода, matcher с τ = 0 и точными ID, ключ конфига `variants`, ошибка
  `invalid_submission`, лимит времени от начала reset. **3.3** — песочница Landlock на Linux,
  закрытый каталог эпизода в `run_on_episode`; генератор без открытых признаков ошибок, новый
  отвлекающий случай `boundary_fx_partial` (поэтому `CONFIG_VERSION` 3.3), медленный ключ
  истории, предел 400 документов при целевых 200. В `policy.json → posting_rules.refund`
  записаны правила возврата, закрывающего счёт в ноль, и переплаты (поэтому `policy_version`
  3.3); формат данных не менялся: `SCHEMA_VERSION` остался 3.2.

---

## Подключение политик

```python
from auditgym import make_env, run_policy

# 1) Цикл в стиле Gymnasium (внутренний формат действий: {"type": ..., аргументы})
with make_env(mode="train") as env:              # make_env(mode="eval", isolated=True) — отдельный процесс
    obs, info = env.reset(seed=1, config={"preset": "mixed"})
    obs, reward, terminated, truncated, info = env.step({"type": "list_records", "table": "ledger"})

# 2) Любая политика: функция obs -> action или объект с begin/act
result = run_policy(my_policy, seed=123, config={"preset": "interacting"}, mode="eval")
result["grader"]["reward"], result["grader"]["f1"], result["trajectory"], result["diagnostics"]["elapsed_s"]
```

**`run_policy(isolated=True)`** (по умолчанию) — официальный запуск:
- среда и оценщик — в одном отдельном процессе, **политика — в другом, свежем**. Туда
  передаются сама политика (pickle; функции и классы, которые другой процесс не найдёт по
  имени — из `__main__`, `lambda`, вложенные, — передаются **по значению**, кодом) и
  наблюдения; обратно — только действия. Политику, которую нельзя передать, отвергает
  `TypeError` с подсказкой;
- политике **недоступны**: seed и config эпизода, клиент среды с токеном оценщика, итог эпизода
  (`grader_report`), пути к файлам эпизода; `run_policy` нельзя вызвать из процесса политики;
- рабочая папка (cwd) и `TMPDIR` процесса политики — **личная временная папка**, удаляемая
  после эпизода; процесс завершается вместе со всеми порождёнными им процессами;
- **переменные окружения оркестратора `AUDITGYM_*`** (например, `AUDITGYM_GRADER_LOG`)
  процессу политики не передаются: из переменных с этим префиксом у него есть только
  `AUDITGYM_POLICY_PROCESS`, которую ставит сам `run_policy`. Остальное окружение — как у
  оркестратора, кроме `PYTHONPATH` (корень проекта), `PYTHONDONTWRITEBYTECODE=1` и
  `TMPDIR`/`TMP`/`TEMP` (личная папка);
- **macOS**: процесс политики запускается в песочнице `sandbox-exec`: запись только в эту
  временную папку (и в уже открытые stdout/stderr), чтение `truth.json`, `provenance.json`,
  `pre_injection/`, `grader/EP-*/` и каталога эпизода из config (`episode_dir`,
  `episode_files.root`) запрещено (по каноническому пути: регистр, ссылки и `..` не помогают),
  сигналы — только своим процессам;
- **Linux**: процесс политики сам накладывает на себя **Landlock** (`auditgym/landlock.py`, ядро
  ≥ 5.13, без root, через ctypes; версия ABI определяется) **до загрузки кода политики**, вместе с
  `PR_SET_NO_NEW_PRIVS`. Это белый список: читать можно только установку Python и системные
  библиотеки, пакеты и модули из `sys.path` (из каталога в `sys.path` открываются только
  подкаталоги-пакеты с `__init__` и файлы модулей), `/dev/null` и `/proc/self`; писать — только в
  личную папку. Закрыты `episodes/` проекта, `episode_dir` и `episode_files.root` из config,
  журнал оценщика (`AUDITGYM_GRADER_LOG`), найденные в пакетах файлы оценщика, `/proc` чужих
  процессов, ptrace, запуск shell и других программ; с ABI 6 — сигналы чужим процессам. Процесс
  среды с эталоном и токеном помечен `PR_SET_DUMPABLE=0`: его `/proc/<pid>` и ptrace закрыты для
  других процессов того же пользователя. Если Landlock недоступен (старое ядро, seccomp),
  политика работает как раньше, только в отдельном процессе, а предупреждение пишется в журнал
  `auditgym.policy`. Код среды в Docker-образе принадлежит root и не записывается и без этого;
- что действовало, видно в `result["policy_sandbox"]`: `"sandbox-exec"`, `"landlock-abiN"` или
  `"none: причина"`; `check` печатает действующую песочницу и число пропущенных тестов;
- лимит 60 с соблюдается и для зависшей политики (`truncated=true`, пустой ответ); исключение в
  политике — неверное действие (шаг израсходован);
- результат: `summary`, `grader` (итог оценщика), `grader_log`, `diagnostics` (время эпизода —
  отдельно от итога, поэтому `grader` при повторе совпадает точно), `trajectory`.

`isolated=False` — **доверенный режим**: политика и среда в процессе оркестратора, изоляции
нет. `check`, `jury` и `run` запускают встроенные (доверенные) политики в процессе
оркестратора, а среду с оценщиком — в отдельном процессе (с `run --in-process` — тоже в
процессе оркестратора).

**Ответ получает только oracle**, которую создаёт сама среда по явному запросу: строка
`"oracle"` или `make_policy("oracle")` (`BASELINE_ADAPTERS["oracle_grader_only"]`). Проверка — по
тождеству объекта в реестре: `Oracle()`, созданный снаружи, подкласс или подмена `__class__`
ответа не получают.

Адаптеры базовых политик (раздел 06): `auditgym.adapters.BASELINE_ADAPTERS` —
`random_explorer`, `simple_reconciliation`, `flag_all_attack`, `oracle_grader_only`; в протоколе
жюри — `judge_adapter.baseline(...)`.

### Протокол процесса среды (`auditgym.server`, JSON lines)

Один JSON-объект на строку; канал — только ASCII. Команды: `reset`, `step`, `grader_report`,
`oracle`, `close` (описание — в `auditgym/server.py`). Два режима:

- `python -m auditgym.server --mode eval` — **открытый, для внешних агентов** на любом языке:
  `reset`, `step` и `close` без токена; команды оценщика (`grader_report`, `oracle`) запрещены
  (`{"ok": false, "error": "forbidden"}`).
- `python -m auditgym.server --mode eval --token-stdin` — **режим оркестратора** (так процесс
  запускает `SubprocessEnv`): первая строка stdin — токен оценщика; `reset`, `grader_report`,
  `oracle` и `close` требуют его, без токена доступен только `step`. Токен не попадает ни в
  окружение, ни в аргументы процесса.

Строка не длиннее 16 МиБ (`MAX_REQUEST_CHARS`). Слишком длинная или неразборчивая строка с
командой `step` верхнего уровня — неверное действие (ограниченная ошибка, шаг израсходован);
остальные такие строки — `{"ok": false, "error": "invalid request"}`. Действие можно передать
JSON-строкой не длиннее 1 000 000 символов (`MAX_ACTION_CHARS`); длиннее, глубже допустимой
вложенности или неразборчивое — неверное действие, а не исключение. Лишние аргументы:
`invalid_arguments` с сообщением `лишние аргументы (N): a, b, c, …` — не больше трёх имён, каждое
не длиннее 16 символов. `AuditEnv.close()` (повторный вызов безопасен) и `with AuditEnv() as env`
— как в Gymnasium; `SubprocessEnv` тоже контекстный менеджер.

### Схемы (`schemas/`, JSON Schema draft-04)

`action.schema.json` (внутренний формат действия), `findings.schema.json` (ответ),
`truth.schema.json` (эталон), `observation.schema.json` (наблюдения и info `AuditEnv`; ключей
эталона в наблюдении нет — `additionalProperties: false`). Draft-04 выбран намеренно: в нём
`integer` — число без дробной части в записи (`50.0` отвергается), как в коде.
`tests/test_schemas.py` сверяет схемы с поведением кода на наборах обычных и крайних действий,
ответов и наблюдений целого эпизода.

---

## Эпизод и форматы данных

Эпизод — одна компания и один месяц. Измерено на 520 эпизодах (конфиг по умолчанию и все 12
пресетов, по 40 seed):
- 424–609 строк книги;
- 201–290 бухгалтерских записей, то есть ≥ 200 при любом прочтении слова «проводки»;
- 106–167 документов;
- 0–12 ошибок.

В опубликованных train/dev — 453–623 строки, 215–287 записей, 117–166 документов. Число
обычных счетов подбирается под предлагаемые ТЗ 200 документов (`TARGET_DOCUMENTS`); заказанные
отвлекающие случаи и ошибки выполняются точно и могут увести эпизод выше, но не выше 400
документов (`MAX_DOCUMENTS`) для любого принятого конфига. Самый тяжёлый допустимый конфиг
(`heavy`, все виды случаев по 12, максимум счетов, клиентов и фона) даёт 299 документов, 1022
строки книги и 475 записей; `rule_auditor` решает его с F1 = 1 за 32 шага.

**Генератор без открытых признаков ошибок.** Документы, на которые потом вносятся ошибки, по
времени, секундам, смещению, сумме, количеству и позиции не отличаются от корректных: у всех
платежей одна модель времени (рабочие часы или, с долей 25 % — `ROUND_THE_CLOCK`, — любое время
суток вплоть до конца периода), секунды у всех моментов случайны, документы-цели берут обычные
даты и строки. У сценария `partial_fx_boundary` есть корректный двойник `boundary_fx_partial`
(тот же конструктор, платёж проведён верно). Правило «по одним документам», находящее частичный
валютный платёж в последний локальный день у смены даты по UTC, без двойника находило бы
только сценарий; теперь его точность 0.5 (24 сценария и 24 двойника на 24 эпизодах,
`tests/test_generator_data.SurfaceFeatures`): отличить ошибку можно только по книге.

**Медленный ключ истории.** Случайность истории выводится из `story_key = scrypt(версия, seed,
экономическая часть конфига)` (n = 2¹³, r = 8, 8 МиБ, `util.slow_h`; `id_salt` и `order_seed` в
него не входят), `story_hash = h(story_key, attempt)`. Перебор seed по открытому наблюдению
(`episode_id`, название компании) стоит около 15 мс и 8 МиБ на кандидата: 10⁶ seed — около
4 ядро-часов вместо нескольких секунд у прежнего быстрого хэша.

**Открытые записи** (`episodes/<split>/public/<episode_id>/` или плоский каталог адаптера):

- `manifest.json`: `schema_version`, `generator_version`, `config_version`, `episode_id`,
  `company_name`, `base_currency`, `currency_exponents`, `period_start`/`period_end`
  (полуинтервал), `timezone`, `counts` (строки, записи, документы), правила округления,
  `csv_null_token`, `fx_id_format`.
- `ledger.csv`: `row_id, timestamp, account, debit_minor, credit_minor, currency, entity_id, invoice_id, payment_id`.
  Все строки в базовой валюте. Счета — с кодом и русским названием, например
  `1200 Дебиторская задолженность`. Есть фоновые операционные проводки без документов: аренда,
  зарплата, комиссии банка. Строк с нулевыми дебетом и кредитом нет (`policy.json →
  posting.zero_rows`).
- `invoices.jsonl`: `doc_id, invoice_id, entity_id, issued_at, due_at, currency, total_minor, line_items`.
- `payments.jsonl`: `doc_id, payment_id, timestamp, currency, amount_minor, invoice_refs`.
  `invoice_refs` — список `[{invoice_id, amount_minor}]` или явный `null`.
- `fx.csv`: `date, source_currency, target_currency, rate`. Столбца ID нет: ID строки курса
  собирается по `manifest.fx_id_format` (`FX-{date}-{source_currency}-{target_currency}`), тот же
  шаблон — в `policy.json → fx.fx_id_template`. При смене ID он не меняется.
- `policy.json`: план счетов, выбор даты курса, границы периода, округление, правила проводок,
  определение каждого типа вывода, правила matcher и τ.
  Правило `posting_rules.payment` («платёж, закрывающий счёт в ноль, списывает остаток 1200
  целиком; разница — на 7800») действует и для возврата, и это записано в
  `policy.json → posting_rules.refund`: возврат (отрицательная строка `invoice_refs`), который
  закрывает счёт в ноль, тоже списывает остаток 1200 целиком, а разница уходит на 7800.
  Переплата (сумма платежа больше остатка счёта) законна и оставляет кредитовое сальдо 1200 до
  возврата.

**Пример проводки на реальных числах** — опубликованный эпизод `episodes/train/public/EP-JML6UDQ7JL`
(базовая валюта EUR, часовой пояс компании `+00:00`, счёт `INV-FDLR5CT` в CHF: переплата, затем
возврат). Курсы из `fx.csv`: CHF→EUR 1.00392 на 2026-12-14, 1.0054 на 2026-12-15, 1.00294 на
2026-12-16; везде `ROUND_HALF_EVEN`.

| Документ | Расчёт по политике | Строки книги |
|---|---|---|
| счёт `INV-FDLR5CT`, 30720 CHF, 2026-12-14 | 30720 × 1.00392 = 30840.4 → 30840 | Дт 1200 30840 / Кт 4000 30840 |
| платёж `PAY-V7S7PPG`, 34367 CHF (переплата), 2026-12-15 | 1000: 34367 × 1.0054 → 34553; 1200: по курсу счёта 34367 × 1.00392 → 34502; разница 51 | Дт 1000 34553 / Кт 1200 34502 / Кт 7800 51; сальдо 1200 = −3662 |
| возврат `PAY-QWO5PG6`, −3647 CHF, 2026-12-16 (`11:09+08:00` = `03:09` UTC, дата по поясу компании) | 1000: 3647 × 1.00294 → 3658; счёт закрывается в ноль, поэтому 1200 = остаток **3662**, а не пересчёт 3647 × 1.00392 → 3661; разница 4 | Кт 1000 3658 / Дт 1200 3662 / Кт 7800 4; сальдо 1200 = 0 |

Сверка, которая пересчитывает возврат по курсу (3661), разойдётся с книгой на 1 единицу; правило
«закрывающий в ноль документ списывает остаток 1200 целиком» даёт ровно книгу.

Форматы значений: время — ISO-8601 со смещением, принадлежность к периоду — по моменту
времени; деньги — целые минимальные единицы, разрядность в `currency_exponents` (JPY=0, KWD=3);
курсы — десятичные строки, конвертация `ROUND_HALF_EVEN(amount × rate × 10^(exp_base −
exp_src))`; отсутствующие ссылки — `null` (в CSV буквально `null`); все файлы — UTF-8.

**Закрытые файлы оценщика** (`episodes/<split>/grader/<episode_id>/` или в плоском каталоге):

- `truth.json`: `issue_id, type, affected_ids, expected_amount_minor, currency, tolerance_minor (0), evidence_sets, mutation_id`.
- `provenance.json`: seed, конфигурация, шаги генерации; каждое изменение с описанием на
  русском, вариантом и проводками до и после; **полный дифф книги** `ledger_diff` (каждая
  изменённая строка с причиной `direct` или `consequence`); `documents_added`,
  `reverse_procedure`; отвлекающие случаи; **`actual`** — что из конфига выполнено и как
  (клиенты и фактическая доля валютных, обычные счета, случаи и ошибки по видам, добавленные
  документы-цели, числа документов и строк); `canonical_ids` — «публичный ID → сущность истории».
- `pre_injection/` (только в `episodes/`): книги до внесения ошибок; в плоском каталоге их нет —
  они восстанавливаются из `provenance.json`.

### Конфигурация (`config_version: "3.3"`)

```json
{"preset": "mixed",
 "errors": {"duplicate_invoice": 1, "payment_allocation": 1, "fx_rate": 2, "period_cutoff": 1},
 "interactions": {"partial_fx_boundary": 1, "masked_duplicate": 0},
 "variants": {"payment_allocation": "partial", "fx_rate": "random", "period_cutoff": "next_period"},
 "distractors": {"split_payment": 2, "multi_invoice_payment": 2, "partial_open": 2, "refund": 1,
                 "recurring_amount": 1, "tz_boundary": 2, "unremitted_payment": 1,
                 "next_period_doc": 2, "same_day_repeat": 1, "prior_period_doc": 1,
                 "boundary_fx_partial": 1},
 "n_customers": [8, 16], "n_invoices": [60, 95], "background_entries": [95, 130],
 "foreign_share": 0.45, "id_salt": "default", "order_seed": 0}
```

- Все ключи необязательны; неизвестные ключи и значения вне границ дают `ConfigError` с
  сообщением на русском. Не больше 12 ошибок на эпизод; сценарий взаимодействия считается за 2.
- `errors`, `interactions` и `distractors` выполняются **точно**: не хватает подходящей цели —
  генератор добавляет в историю корректный документ-цель (он есть и в книгах до внесения, и в
  `provenance.actual.supplied_targets`); не хватает свободного счёта — случай строится на своём.
- `n_invoices` — число **обычных** счетов до отвлекающих случаев и ошибок; уменьшается, чтобы
  эпизод остался около 200 документов, и в любом случае — чтобы их было не больше 400
  (фактическое — `provenance.actual.regular_invoices`).
- `foreign_share` — вероятность для каждого клиента вести счета в иностранной валюте; только
  если заказаны `fx_rate` или `partial_fx_boundary`, а валютного клиента не выпало, первый
  клиент становится валютным (фактическая доля — в `provenance.actual`).
- `variants` — вариант независимых ошибок (таблица типов выше); без ключа или `"random"` —
  выбор генератора.
- Предел 400 документов: конфиг, которому даже без обычных счетов может понадобиться больше
  (`config.document_bound`: цена каждого случая и ошибки в документах), отвергается
  `ConfigError`. При нынешних ценах граница самого тяжёлого конфига в опубликованных границах —
  ровно 400, поэтому принимается любой такой конфиг.
- `id_salt` и `order_seed` — безвредные преобразования: меняют ID и порядок, не экономику.
- Фаззинг (Python 3.13, 3.3.0): 1000 случайных экстремальных конфигураций (0–4 ошибки каждого
  типа, 0–2 сценария, 0–6 случаев каждого вида, случайные размеры, доля валют и варианты) — все
  1000 приняты, сгенерированы и прошли все проверки данных (`check_episode`), максимум 177
  документов и 358 записей. С 0–12 случаями каждого вида — все 300 из 300 приняты и прошли
  проверки (0 % отказов `ConfigError`; в 3.2 было 99 из 300), максимум 238 документов.

---

## Ошибки, взаимодействия, отвлекающие случаи

Сначала строится согласованная скрытая история, затем она проводится по опубликованной
политике и проверяются равенства. Только потом вносятся ошибки как записанные причинные
изменения. Четыре типа и их варианты — в таблице «Соответствие четырёх типов ошибок».

**Взаимодействующие сценарии:**
1. `partial_fx_boundary` — **частичный платёж в другой валюте на границе периода**: последний
   локальный день, а дата по UTC другая. Взят курс даты по UTC (`fx_rate`), и часть
   расписанной суммы оставлена на 2900 (`payment_allocation`). Сам частичный платёж законен.
2. `masked_duplicate` — счёт продублирован, а оплата оригинала зачтена на дубль
   (`duplicate_invoice` + `payment_allocation`). Дубль выглядит оплаченным, оригинал открытым.

**Корректные отвлекающие случаи** — против каждого простого правила есть контрпример:

| Простое правило | Контрпример |
|---|---|
| дубль = тот же клиент и сумма | `recurring_amount`, `same_day_repeat` |
| период по напечатанной дате | `tz_boundary` |
| любой документ вне периода — ошибка | `next_period_doc`, `prior_period_doc` (правильно **не** проведены) |
| любая проводка 2900 — ошибка | `unremitted_payment` (`invoice_refs = null`) |
| частичный валютный платёж у границы периода — ошибка | `boundary_fx_partial` (двойник сценария `partial_fx_boundary`, проведён верно) |
| открытый счёт = нераспределённый платёж | соблазн угадать связь |
| частичный, разбитый, общий платёж или возврат — ошибка | `partial_open`, `split_payment`, `multi_invoice_payment`, `refund` |
| строка книги без документа — ошибка | фоновые операционные проводки (6xxx) |

---

## Что проверяет `check` (по критериям оценки ТЗ)

| Раздел | Критерий | Что именно проверяется |
|---|---|---|
| `unit_tests` (191) | все | официальные определения C01–C20, D01–D20, 1–20, M01–M12, P01–P20; reset/step, схемы, лимиты, завершение, неверный ввод, тайм-аут, режимы, атакующие ответы, изоляция политики (sandbox-exec и Landlock), гарантии генератора и отсутствие открытых признаков ошибок. `check` печатает действующую песочницу политики и число пропущенных тестов (на macOS пропускаются 3 теста Landlock, в Docker — ни одного) |
| `determinism` | Воспроизводимость | одинаковые seed+config → побайтно одинаковый эпизод; совпадение с опубликованными `episodes/*/index.json` |
| `data_and_truth` (220 эпизодов) | Данные и эталон | книги до ошибок **восстанавливаются из provenance.json** и проходят все равенства; у каждого изменения книги есть причина; **независимая перепроводка** совпадает с чистой книгой полностью, а с книгой после внесения — везде, кроме объявленных ошибок; эталонный аудитор не находит ничего на чистых книгах и ровно эталон на книгах с ошибками; каждый набор доказательств сам по себе доказывает ошибку |
| `baselines` | Надёжность награды | 4 базовые политики и скриптовый аудитор в режиме **eval** в **отдельном процессе**: `flag_all < reconciler < rule_auditor ≤ oracle = 1.0` |
| `no_leakage` | Контракт и изоляция | каждое наблюдение прогонов `check` и опубликованного `trajectories.jsonl` — на закрытые ключи и issue_id; версия траекторий совпадает с текущей |
| `paired_checks` (30) | Воспроизводимость | 10 точных повторов; 10 смен ID и порядка через конфиг; 10 внешних преобразований файлов |
| `jury_rehearsal` | все пять | репетиция оценки жюри, см. ниже |

### Репетиция оценки жюри и мутационный тест

`jury` реализует **наше прочтение** раздела 10 как чёрный ящик: 20 проверок контракта против
среды в отдельном процессе, 20 свежих «скрытых» эпизодов (5 чистых, 5 с взаимодействующими
ошибками, 10 смешанных) с проверкой только по файлам, 20 проверок награды через среду (τ берётся
из опубликованного policy.json), 20 парных проверок и 5 пунктов инженерии. Это самооценка, а не
гарантия баллов. Текущий результат: **100 / 100** (`reports/jury_scorecard.md`).

`scripts/mutation_test.py` по одному вносит **настоящие баги** во временную копию проекта и
запускает `jury`. Каждый должен снять баллы в своём критерии (измерено на 3.3.0):

| Внесённый баг | Что поймало | Итог |
|---|---|---|
| matcher не проверяет доказательства | Надёжность 19/20 | 98.8 |
| страница больше 50 строк | Контракт 19/20 | 98.8 |
| эталон в `info` после `reset` | Контракт 19/20 | 98.8 |
| усечение вместо банковского округления | Данные 0/20, Воспроизводимость 17/20 | 72.8 |
| дата курса по UTC вместо часового пояса компании | Данные 4/20 | 80.0 |
| допуск 10% вместо опубликованного τ = 0 | Надёжность 19/20 | 98.8 |
| недетерминированный генератор | Воспроизводимость 5/20, Контракт 19/20 | 87.5 |
| сопоставление без «один к одному» | Надёжность 18/20 | 97.5 |
| награда видна политике в режиме eval | Контракт 19/20 | 98.8 |
| provenance теряет изменённые строки | Данные 8/20 | 85.0 |
| платёж без расписки проведён не по политике (на 1 минимальную единицу) | Данные 0/20 | 75.0 |

Пойманы **11 из 11**. Ради двух из этих мутантов проверяющая сторона (`audit_rules.py`) **не
использует общий с генератором код** денег и времени: конвертация — на точных дробях с явным
банковским округлением, даты и границы периода — ручной арифметикой смещений UTC.

---

## Базовые политики (режим eval, отдельный процесс)

`train` (10 эпизодов, 39 ошибок, 2 чистых). Время — среднее на эпизод, с (оркестратор, вместе с
reset в процессе среды):

| политика | P | R | F1 | возврат | шаги | время, с | TP | FP | FN |
|---|---|---|---|---|---|---|---|---|---|
| oracle (только у оценщика) | 0.80* | 0.80* | **1.000** | **1.000** | 1.0 | 0.018 | 39 | 0 | 0 |
| rule_auditor (скриптовый, без эталона) | 0.80* | 0.80* | 1.000 | 0.966 | 18.1 | 0.025 | 39 | 0 | 0 |
| reconciler (простой алгоритм сверки) | 0.222 | 0.683 | 0.327 | −0.374 | 18.1 | 0.026 | 35 | 106 | 4 |
| random (seed 0) | 0.000 | 0.000 | 0.200 | −0.606 | 17.2 | 0.041 | 0 | 10 | 39 |
| flag_all (атака) | 0.000 | 0.000 | 0.000 | −1.000 | 4.6 | 0.020 | 0 | 300 | 39 |

`dev` (10 эпизодов, 34 ошибки, 2 чистых):

| политика | P | R | F1 | возврат | шаги | время, с | TP | FP | FN |
|---|---|---|---|---|---|---|---|---|---|
| oracle | 0.80* | 0.80* | 1.000 | 1.000 | 1.0 | 0.017 | 34 | 0 | 0 |
| rule_auditor | 0.80* | 0.80* | 1.000 | 0.966 | 18.2 | 0.025 | 34 | 0 | 0 |
| reconciler | 0.200 | 0.708 | 0.295 | −0.438 | 18.2 | 0.026 | 30 | 100 | 4 |
| random | 0.000 | 0.000 | 0.100 | −0.806 | 22.5 | 0.041 | 0 | 6 | 34 |
| flag_all | 0.000 | 0.000 | 0.000 | −1.000 | 4.5 | 0.019 | 0 | 300 | 34 |

\* На чистых эпизодах по ТЗ P=R=0 (нулевой знаменатель), но F1=1; отсюда среднее 0.8.
Источник — `reports/check_report.md`; время немного меняется от прогона к прогону.

**Высокая награда oracle сама по себе баллов не даёт и не доказывает пригодность для
реального аудита.**

---

## Измеренный разбор сбоев

### Сбои базовой политики (`reports/check_report.json → failure_analysis`, dev)

Простой reconciler даёт 100 FP:

| Источник ложных срабатываний | FP |
|---|---|
| обычные записи без ловушки (курс напечатанной даты и т. п.) | 26 |
| повторяющиеся суммы (`recurring_amount`) | 23 |
| документы на границе периода (`tz_boundary`) | 23 |
| повторные заказы в один день (`same_day_repeat`) | 12 |
| платежи без расписки (`unremitted_payment`) | 12 |
| частичные валютные платежи у границы (`boundary_fx_partial`) | 3 |
| прочее (`split_payment` 1) | 1 |

Из 4 его FN (3 `payment_allocation`, 1 `fx_rate`) 3 — во взаимодействующих сценариях. Скриптовый `rule_auditor` по одним открытым записям получает F1=1: задачи разрешимы,
доказательств достаточно.

### Атаки на изоляцию: до и после исправления

Внешний аудит (команда, коммит `de26f77`, версия 3.0.0) запускал атакующие решения через
`run_policy(mode="eval")` на пяти задачах: T1 — train 1002 (чистая), T2 — train 1003, T3 — dev
2005, T4 — dev 2009, T5 — seed 987654, пресет `heavy`. Каждое решение — дважды. После
исправления те же решения запущены снова на 3.3.0: на macOS (Python 3.13, `sandbox-exec`) и в
Docker-образе (Linux, Python 3.11.11, Landlock ABI 8, внешняя обвязка подключена только на
чтение). В репозитории те же атаки повторяют тесты (доверенный запуск `isolated=False`
показывает, что атака настоящая, официальный — что она закрыта); в Docker все они проходят,
ни один не пропущен:

| Атака | Что делает | Было | Стало: macOS | Стало: Linux (Docker) | Воспроизвести |
|---|---|---|---|---|---|
| stack-seed | читает seed и config из стека оркестратора, пересоздаёт эталон генератором | 1.0 на T2–T5 (3.0.0) | −1 на T2–T5 | −1 (T2) | `python3 -m unittest tests.test_policy_sandbox.GapsOfPreviousAudit.test_gap1_stack_seed` |
| gc-oracle | находит в памяти клиента среды с токеном и запрашивает ответ oracle | 1.0 на T2–T5 (3.0.0) | −1 на T2–T5 | −1 (T5) | `… GapsOfPreviousAudit.test_gap2_gc_oracle` |
| patch-report | подменяет `grader_report` в своём процессе | `run_policy` вернул 1.0 и «все TP» при пустом ответе (3.0.0) | −1, методы оркестратора не изменены | −1 (T5) | `… GapsOfPreviousAudit.test_gap3_patch_report` |
| tamper-grader | дописывает `matcher.py`; следующие эпизоды оцениваются изменённым кодом | 1.0 у пустых и неверных ответов (`t2_empty`, `t5_empty`, `t5_cheat_format_only`, `t3_wrong_type`) (3.0.0) | −1 у всех четырёх в обоих повторах, `matcher.py` не изменён | тест в копии кода: −1, копия не изменена | `… GapsOfPreviousAudit.test_gap4_tamper_grader_code_in_copy` |
| disk-truth | читает `episodes/*/grader/*/truth.json` | 1.0 на T1–T4 (3.0.0) | −1 на T2–T4 | −1 на T2–T4 | `python3 -m unittest tests.test_policy_sandbox.GraderFilesOnDisk` |
| зависшая политика в адаптере жюри | `judge_adapter.run_on_episode`: политика думает 8 с при лимите 2 с | прогон держался 8.2 с, лимит срабатывал только на следующем шаге (3.3.0 до правки) | остановлена в момент лимита, `truncated=true`, −1 | то же | `python3 -m unittest tests.test_judge_adapter.BaselinesThroughAdapter.test_hanging_policy_stopped_at_time_limit` |
| episode_dir через адаптер жюри | политика `judge_adapter.run_on_episode` знает путь к каталогу эпизода и читает его `truth.json` | 1.0: политика работала в процессе жюри (3.2.0) | −1, каталог закрыт | −1, каталог закрыт | `… GraderFilesOnDisk.test_judge_adapter_episode_dir_closed_to_policy` |
| crash-action | действие — JSON-строка с вложенностью от 10 000 уровней | прогон падал (`RemoteError: internal error`) на T1, T2, T5 (3.0.0) | ограниченная ошибка, шаг израсходован; −1 на T2 и T5 после 100 шагов (на чистой T1 — 0.8, см. ограничения) | −1 (T2) | `python3 -m unittest tests.test_policy_sandbox.BrokenActionsThroughProcesses tests.test_env_contract` |
| /proc, ptrace, сигналы | читает `/proc/<pid>` процесса среды, шлёт сигналы, запускает shell | не проверялось | `/proc` нет; сигналы — только своим процессам | `PermissionError` на всё; процесс среды non-dumpable | `python3 -m unittest tests.test_policy_sandbox.LinuxLandlock` (только Linux) |

`seed-from-id` (восстановить seed по `episode_id` для опубликованного пресета и соли по
умолчанию) в 3.2.0 давал 1.0 на T5 за 1 шаг. В 3.3.0 то же решение получает −1 на macOS и в
Docker: его формула ключа устарела, а перебор по новому медленному ключу укладывает в 60 с
эпизода около 4000 seed на ядро. Политика без ответа на чистой T1 по-прежнему получает 0.8. Оба
случая — в «Честных ограничениях».

### Сбои, найденные независимым аудитом 22 ноября 2026: до и после исправления

Аудит запускал только точки входа `judge_adapter` и свою сверку данных, написанную по тексту
`policy.json` без кода проекта (D01–D20 на секретных seed, все пресеты и случайные конфиги).

| Сбой (измерено) | Причина | Исправление | Повторная проверка |
|---|---|---|---|
| Конфиг D01–D05 из таблицы README `{"errors": 0, …}` давал `ConfigError: errors должен быть объектом` | в README была сокращённая запись вместо JSON | 20 строк с полным JSON, равным `D_MATRIX` (коммит `c72f07c`) | `tests/test_audit_fixes.py`: все 20 конфигов README генерируются и равны `D_MATRIX` |
| Независимая сверка расходилась с книгой на 1 минимальную единицу (строка 1200/7800 возврата, D06 и 5 эпизодов train/dev) | правило «закрывающий в ноль документ списывает остаток 1200» было записано в политике только для оплаты | правило возврата и переплаты в `policy.json → posting_rules.refund`, `policy_version` 3.3 (коммит `fa1a19c`); пример — «Пример проводки на реальных числах» | `tests/test_policy_refund_rule.py`: перепроводка по тексту политики совпадает с книгой; сверка аудита — 98/98 эпизодов без расхождений |
| В Docker-отчёте `git.commit = null` | `.git` не копируется в образ (`.dockerignore`) | аргумент сборки `GIT_COMMIT` → `AUDITGYM_GIT_COMMIT` (коммит `c72f07c`) | `tests/test_audit_fixes.py` (`git_state` без git) и прогон `.github/workflows/docker-check.yml` |

После исправлений: `check` на Linux (Python 3.11, Landlock) — 191 тест без пропусков, все
разделы пройдены (`reports/check_report.json`); симулятор жюри аудита — 78/78 проверок блоков 1,
3, 4.

---

## Траектории

`trajectories/trajectories.jsonl` — одна строка на шаг: `episode_id, policy, split, step,
action, observation, reward, terminated, truncated` и `mode, env_version, generator_version`.
104 прогона на train и dev (2744 шага):
- 80 прогонов в режиме `train`: random, reconciler, flag_all, rule_auditor на всех 20 эпизодах;
- 6 прогонов `random_no_submit` (случайный исследователь без ответа) в режиме `train`: эпизод
  заканчивается **по лимиту 100 шагов** (`truncated=true`, пустой ответ);
- 18 прогонов в режиме **`eval`** (rule_auditor, reconciler, random_no_submit на трёх первых
  эпизодах каждого split): награда политике 0, `info` пуст; 6 из них — по лимиту шагов.

Прогоны oracle не публикуются: их действия построены из эталона. `check` сканирует все
наблюдения опубликованного файла на закрытые ключи и issue_id и сверяет версию. Слова
`issue_id` и `expected_amount_minor` в `policy.json` (и в наблюдениях с ним) — текст
опубликованных правил matcher, а не утечка: сканер `check` ищет значения `issue_id` эталона, а не
само слово.

---

## Честные ограничения

Каждое ограничение связано со сценарием, который его показывает.

- **Seed восстановить дорого, но не невозможно.** История выводится из медленного ключа
  (scrypt, ~15 мс и 8 МиБ на кандидата): за 60 с эпизода перебирается около 4000 seed на ядро,
  10⁶ seed — около 4 ядро-часов. Заранее посчитанная таблица для опубликованных пресетов и малых
  seed остаётся возможной; название компании и период от `id_salt` не зависят, поэтому одна
  секретная соль от такой таблицы не спасает. Для закрытой проверки нужны большие случайные seed
  (`secrets.randbits(63)`) и секретная `id_salt` — рекомендация в [JURY.md](JURY.md).
- **Без Landlock защиты сверх отдельного процесса нет**: на Linux со старым ядром (< 5.13) или
  с запретом Landlock в seccomp политика работает только в отдельном процессе, а в журнал
  `auditgym.policy` и в `policy_sandbox` пишется причина
  (`tests.test_policy_sandbox.SandboxFallback`). При ABI < 6 сигналы чужим процессам не
  блокируются.
- **На Windows песочницы процесса политики нет**: `check` там проходит, но политика работает
  только в отдельном процессе (`policy_sandbox` = `"none: нет песочницы для win32"`): файлы
  эталона на диске и код среды ей доступны, тесты песочницы пропускаются с причиной. Официальный
  прогон — в контейнере, где действует Landlock.
- **Сеть процессу политики не ограничена** (Landlock этой версии TCP не закрывает); официальный
  прогон всё равно идёт с `docker run --network none`.
- **На Linux данные рядом с кодом политики ей не видны**: из каталогов `sys.path` открываются
  только пакеты с `__init__` и файлы модулей. Политике, которая читает свои файлы данных или
  пакет без `__init__`, их нужно положить в пакет.
- **Код участника, исполняемый в процессе оркестратора до `begin`/`act`** — импорт модуля
  решения и создание объекта политики (`make_policy()` в его файле) — **не изолирован**: он
  работает с правами оркестратора. Изолированы только `begin` и `act` после передачи политики в
  её процесс.
- **Тайм-аут или лимит шагов на чистой компании дают 0.8**, а не ≤ 0: пустой ответ на пустом
  эталоне — F1 = 1, и `2·1 − 1 − 0.002·100 = 0.8`. Так требует формула ТЗ (`noop_timeout`,
  `crash-action` на T1 → 0.8); экспериментальная награда это тоже не меняет.
- **Эпизоды с большим числом отвлекающих случаев больше предлагаемых 200 документов**: случаи
  выполняются точно, поэтому самый тяжёлый допустимый конфиг даёт 299 документов (предел — 400).
- `jury` — **самооценка** по нашему прочтению раздела 10; официальные определения покрыты
  тестами `test_judge_adapter` и `test_judge_pairs`, но прогон жюри может отличаться.
- Синтетические книги с упрощённым планом счетов: дебиторка, выручка, курсовые разницы и
  несколько операционных расходов; нет кредиторки, НДС, начислений и переоценки. Это
  программная задача, не аудиторское заключение.
- Суммы ошибок определены опубликованным соглашением (`policy.json → finding_types`), τ = 0:
  ответ, посчитанный по другому соглашению об округлении, не совпадёт. Честный аудитор,
  написанный отдельно по тексту policy.json, получает ровно максимум `1 − 0.002·шаги` на
  5 + 240 + 36 эпизодах внешнего аудита.
- Дубли — точные копии; «нечёткие» дубли не генерируются. Один часовой пояс на компанию, без
  перехода на летнее время.
- Эталонный аудитор и генератор следуют одной опубликованной политике: F1=1 доказывает
  согласованность и разрешимость, но не единственность трактовки. Общий код денег и времени при
  этом намеренно разделён.
- Обучение весов не проводилось, заявлений об улучшении модели нет.

---

## Использование ИИ-инструментов

Среда — генератор, оценщик и matcher, адаптер жюри, тесты и документация — написана с помощью
Claude (Anthropic, модели семейства Opus) в Claude Code. Команда ставила задачи, проверяла
результат, проводила аудит атакующими решениями (внешний набор: 64 решения на пяти задачах,
изменение оценщика с диска) и принимала решения: что считать пробелом, что исправлять, какие
ограничения оставить. Цифры в этом README получены запуском команд из репозитория.
