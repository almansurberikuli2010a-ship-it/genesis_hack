# Как проверить AuditGym (для жюри)

Всё работает без сети и без зависимостей на Python 3.11. Одна команда проверяет всё сразу:

```bash
python3.11 -m auditgym check
```

Ниже — команда под каждый критерий раздела 10 ТЗ, чтобы проверить его отдельно и на своих
закрытых seed.

## Контракт и изоляция (25)

```bash
python3.11 -m auditgym jury
```

В таблице — 20 проверок против среды в **отдельном процессе**: reset/step, схемы, лимиты,
завершение, неверный ввод, утечки. Подключить свою политику (функцию `obs → action`):

```python
from auditgym import run_policy
r = run_policy(my_policy, seed=123456, config={"preset": "mixed"}, mode="eval")
r["grader"], r["trajectory"]
```

Протокол для агента на другом языке: `python -m auditgym.server --mode eval` (JSON lines,
описан в `auditgym/server.py`).

## Данные и эталон (25)

1. Сгенерировать скрытый набор (5 чистых, 5 с взаимодействующими ошибками, 10 смешанных):

```bash
python3.11 -m auditgym generate --seed 900001 900002 900003 900004 900005 --config '{"preset":"clean"}' --out hidden
```

```bash
python3.11 -m auditgym generate --seed 900011 900012 900013 900014 900015 --config '{"preset":"interacting"}' --out hidden
```

```bash
python3.11 -m auditgym generate --seed 900021 900022 900023 900024 900025 900026 900027 900028 900029 900030 --config '{"preset":"mixed"}' --out hidden
```

2. Проверить **только по файлам**: равенства до ошибки (книги восстанавливаются из
   provenance.json), объявленное расхождение после, независимая перепроводка, достаточность
   каждого набора доказательств:

```bash
python3.11 -m auditgym verify hidden
```

Своё решение можно оценить общим matcher'ом:
`python3.11 -m auditgym grade hidden --findings findings.json --steps N`.

## Надёжность награды (25)

```bash
python3.11 -m unittest tests.test_matcher_reward -v
```

Это 5 обязательных свойств из раздела 05 и атакующие ответы. Ещё 20 проверок через среду
даёт `python3.11 -m auditgym jury`, раздел «Надёжность награды». Код нормализации, допуска и
выбора при неоднозначности открыт: `auditgym/matcher.py`.

## Воспроизводимость и обобщение (15)

```bash
python3.11 -m auditgym pair --seed 900001 900011 900021 --config '{"preset":"mixed"}'
```

Команда делает три проверки:
- точный повтор: записи, переходы и награды совпадают;
- смена ID и порядка через `id_salt`/`order_seed`: эталон и все таблицы совпадают при точном
  сопоставлении ID, награды oracle и аудитора те же;
- внешнее переименование ID и перемешивание строк прямо в файлах (`transform`): среда
  прогоняет оба варианта, награды совпадают.

## Инженерия и объяснение (10)

| Пункт | Где смотреть |
|---|---|
| запуск одной командой | `python3.11 -m auditgym check`; `docker run --network none …` |
| версии конфигураций | `config_version` в `configs/*.json` и manifest; `GENERATOR_VERSION`/`SCHEMA_VERSION` в `auditgym/config.py` |
| журналы траекторий | `trajectories/trajectories.jsonl` |
| измеренный разбор сбоев | `reports/check_report.json → failure_analysis`, таблица в README |
| честные ограничения | README, раздел «Честные ограничения» |

## Проверка самой проверки

```bash
python3.11 scripts/mutation_test.py
```

Скрипт по одному вносит настоящие баги и показывает, в каком критерии каждый снижает баллы.
