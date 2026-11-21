# Как проверить AuditGym (для жюри)

Всё работает без сети и без зависимостей на Python ≥ 3.11. Все команды — из корня проекта
(в Docker-образе — `/app`). Одна команда проверяет всё сразу:

```bash
python3 -m auditgym check
```

В образе: `docker build -t auditgym .` и `docker run --rm --network none --cpus 2 --memory 4g
auditgym` (запускает `python -m auditgym check`). Через make: `make check PY=python3`.
Чтобы в `reports/check_report.json → git.commit` был коммит (`.git` в образ не копируется),
соберите образ с аргументом: `docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) -t auditgym .`;
без него коммит — `null`. Workflow, которыми собраны отчёты, — `.github/workflows/docker-check.yml`
и `update-reports.yml`.

## Подключение по определениям проверок: `judge_adapter.py`

Файл в корне проекта. Точки входа — `generate_episode(seed, config, output_dir)`, `make_env()`
(объект с `reset(seed, config)` и `step(action)`), `score_episode(episode_dir, findings,
inspection_steps)`. Полное описание — README, раздел «Для жюри: judge_adapter.py»: формат
`config` reset (`mode`, `episode_dir`, `team_config`), оболочка `{"name", "args"}`, схема
наблюдений и результата `score_episode`, коды ошибок, журнал оценщика
(`judge_adapter.GRADER_LOG`, логгер `auditgym.judge`, файл из `AUDITGYM_GRADER_LOG`).

```python
import judge_adapter as J
J.generate_episode(seed, {"preset": "mixed", "id_salt": salt}, "/jury/ep1")
env = J.make_env()
obs, info = env.reset(0, {"mode": "closed", "episode_dir": "/jury/ep1"})
obs, reward, terminated, truncated, info = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 50}})
J.score_episode("/jury/ep1", findings, inspection_steps)
```

### Закрытые seed: рекомендация

`episode_id` и название компании выводятся из seed и конфига открытым генератором. С 3.3 ключ
истории медленный (scrypt, ~15 мс и 8 МиБ на кандидата): за 60 с эпизода перебирается около
4000 seed на ядро, 10⁶ seed — около 4 ядро-часов. Для **опубликованного пресета и малых seed**
таблицу можно посчитать заранее, а название компании от `id_salt` не зависит. Поэтому для
закрытых эпизодов нужны оба условия:

```python
import secrets
seed = secrets.randbits(63)              # большой случайный seed, перебор бесполезен
salt = secrets.token_hex(16)             # секретная соль ID (не длиннее 64 символов), хранить у жюри
J.generate_episode(seed, {"preset": "mixed", "id_salt": salt}, out_dir)
```

Большой случайный seed делает перебор и заранее посчитанную таблицу бесполезными; секретная
`id_salt` меняет все ID и `episode_id`, не меняя экономику эпизода. Новые сочетания параметров
конфига (не опубликованные пресеты) дополнительно расширяют перебор.

## Контракт и изоляция (25) — Блок 1, C01–C20

```bash
python3 -m unittest tests.test_judge_adapter.Block1Contract -v
```

Те же 20 тестов, что в определениях, через `judge_adapter` на фикстурах C, E, L, U. Ещё 20
проверок против среды в отдельном процессе даёт `python3 -m auditgym jury`. Изоляция политики
(`run_policy(isolated=True)` и `judge_adapter.run_on_episode`: отдельный процесс, песочница
`sandbox-exec` на macOS и Landlock на Linux) — `python3 -m unittest tests.test_policy_sandbox -v`;
в Docker-образе выполняются все 22 теста, без пропусков. Какая песочница действует, печатает
`check` и пишет поле `policy_sandbox` результата. Свою политику (функцию `obs → action` или объект с
`begin`/`act`) можно запустить и так:

```python
from auditgym import run_policy
r = run_policy(my_policy, seed=123456, config={"preset": "mixed"}, mode="eval")
r["grader"], r["trajectory"]
```

Протокол для агента на другом языке: `python3 -m auditgym.server --mode eval` (открытый режим
JSON lines, команды оценщика запрещены; описан в README и `auditgym/server.py`).

## Данные и эталон (25) — Блок 2, D01–D20

```bash
python3 -m unittest tests.test_judge_pairs.MatrixD -v
```

Тест собирает D01–D20 **только конфигом генератора** (таблица `team_config` — в README) и
проверяет каждый плоский каталог по файлам. Своими seed:

```python
from pathlib import Path
import judge_adapter as J
from auditgym.datachecks import check_records
from auditgym.export import load_flat_episode
J.generate_episode(seed, team_config, "/jury/D09")
print(check_records(*load_flat_episode(Path("/jury/D09"))))   # [] — все условия выполнены
```

`check_records`: книги до внесения восстанавливаются из `provenance.json` и проходят все
равенства; внесение создаёт ровно объявленное расхождение (независимая перепроводка по
политике); truth соответствует реальному изменению; каждый набор доказательств сам по себе
доказывает ошибку; форматы §03.

Тот же разбор в формате `public/` + `grader/` — командами:

```bash
python3 -m auditgym generate --seed 900001 900002 900003 900004 900005 --config '{"preset":"clean"}' --out hidden
python3 -m auditgym generate --seed 900011 900012 900013 900014 900015 --config '{"preset":"interacting"}' --out hidden
python3 -m auditgym verify hidden
```

## Надёжность награды (25) — Блок 3, тесты 1–20 и M01–M12

```bash
python3 -m unittest tests.test_judge_adapter.Block3MatcherReward tests.test_judge_adapter.OpenExamplesM -v
```

Общая фикстура определений (одна ошибка A, 10000 KZT, доказательства {D1, D2}, τ = 0) через
`score_episode`; открытые примеры M01–M12. Свойства раздела 05 и атакующие ответы —
`python3 -m unittest tests.test_matcher_reward -v`. Правила matcher (τ = 0, точные ID и валюта,
множества, несуществующее доказательство → FP, выбор при неоднозначности) — README, «Matcher и
награда»; код — `auditgym/matcher.py`, они же в `policy.json → matching`.

## Воспроизводимость и обобщение (15) — Блок 4, P01–P20

```bash
python3 -m unittest tests.test_judge_pairs.PairsP -v
```

Преобразование как у жюри: копия плоского каталога, перестановка записей и/или взаимно
однозначная замена `row_id, doc_id, invoice_id, payment_id, entity_id` во всех данных, ссылках,
truth и provenance; `fx_id` (date:source:target) не меняется. Сравниваются TP/FP/FN, P, R, F1 и
награда oracle при одинаковом числе шагов, а также точный повтор. Ещё:

```bash
python3 -m auditgym pair --seed 900001 900011 900021 --config '{"preset":"mixed"}'
```

## Инженерия и объяснение (10)

| Пункт | Где смотреть |
|---|---|
| запуск одной командой | `python3 -m auditgym check` (в образе `python3.11 -m auditgym check`); `docker run --network none …` |
| версии конфигураций | `config_version` в `configs/*.json` и manifest; `ENV_VERSION`, `GENERATOR_VERSION`, `SCHEMA_VERSION`, `CONFIG_VERSION` в коде; версии, Python и git-коммит — в `reports/check_report.json`; seed и хэш каждого эпизода — `episodes/*/index.json` |
| журналы траекторий | `trajectories/trajectories.jsonl`: поля раздела 09 + `mode`, `env_version`; есть прогоны по лимиту шагов (`truncated=true`) и в режиме `eval` |
| измеренный разбор сбоев | `reports/check_report.json → failure_analysis`; README, «Измеренный разбор сбоев»: атаки «до → после» с командами |
| честные ограничения | README, «Честные ограничения»; DESIGN.md, «Ограничения» |

## Проверка самой проверки

```bash
python3 scripts/mutation_test.py
```

Скрипт по одному вносит 11 настоящих багов и показывает, в каком критерии каждый снижает баллы.

Среда написана с помощью Claude (Anthropic) в Claude Code; подробно — README, «Использование
ИИ-инструментов».
