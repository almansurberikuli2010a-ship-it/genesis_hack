# Отчёт проверки

среда 3.3.0, генератор 3.3.0, схемы 3.2, конфиг 3.3; коммит 37d67065d71b + незакоммиченные правки; python 3.13.17 (darwin), 80.1 с

- **unit_tests**: ПРОЙДЕНО
- **determinism**: ПРОЙДЕНО
- **data_and_truth**: ПРОЙДЕНО
- **baselines**: ПРОЙДЕНО
- **no_leakage**: ПРОЙДЕНО
- **paired_checks**: ПРОЙДЕНО
- **jury_rehearsal**: ПРОЙДЕНО

## Базовые политики на train (режим eval, отдельный процесс)

| политика | P | R | F1 | возврат | эксп. возврат | шаги | время, с | TP | FP | FN |
|---|---|---|---|---|---|---|---|---|---|---|
| flag_all | 0.000 | 0.000 | 0.000 | -1.000 | -0.996 | 4.6 | 0.020 | 0 | 300 | 39 |
| oracle | 0.800 | 0.800 | 1.000 | 1.000 | 1.000 | 1.0 | 0.018 | 39 | 0 | 0 |
| random | 0.000 | 0.000 | 0.200 | -0.606 | -0.606 | 17.2 | 0.041 | 0 | 10 | 39 |
| reconciler | 0.222 | 0.683 | 0.327 | -0.374 | -0.362 | 18.1 | 0.026 | 35 | 106 | 4 |
| rule_auditor | 0.800 | 0.800 | 1.000 | 0.966 | 0.966 | 18.1 | 0.025 | 39 | 0 | 0 |

## Базовые политики на dev (режим eval, отдельный процесс)

| политика | P | R | F1 | возврат | эксп. возврат | шаги | время, с | TP | FP | FN |
|---|---|---|---|---|---|---|---|---|---|---|
| flag_all | 0.000 | 0.000 | 0.000 | -1.000 | -1.000 | 4.5 | 0.019 | 0 | 300 | 34 |
| oracle | 0.800 | 0.800 | 1.000 | 1.000 | 1.000 | 1.0 | 0.017 | 34 | 0 | 0 |
| random | 0.000 | 0.000 | 0.100 | -0.806 | -0.806 | 22.5 | 0.041 | 0 | 6 | 34 |
| reconciler | 0.200 | 0.708 | 0.295 | -0.438 | -0.438 | 18.2 | 0.026 | 30 | 100 | 4 |
| rule_auditor | 0.800 | 0.800 | 1.000 | 0.966 | 0.966 | 18.2 | 0.025 | 34 | 0 | 0 |

## Ресурсы

```json
{
  "max_episode_time_s": 0.0934,
  "max_steps_used": 31,
  "peak_rss_mb_orchestrator": 190.6,
  "peak_rss_mb_env_process": 79.1,
  "limits": {
    "time_per_episode_s": 60,
    "steps": 100,
    "ram_gb": 4
  }
}
```

## Разбор сбоев (dev)

```json
{
  "random": {
    "fn_by_type": {
      "duplicate_invoice": 8,
      "fx_rate": 9,
      "payment_allocation": 10,
      "period_cutoff": 7
    },
    "fn_in_interaction": 10,
    "fp_by_type": {
      "payment_allocation": 1,
      "period_cutoff": 2,
      "fx_rate": 2,
      "duplicate_invoice": 1
    },
    "fp_by_distractor": {
      "ordinary_record": 2,
      "same_day_repeat": 1,
      "recurring_amount": 1,
      "tz_boundary": 2
    }
  },
  "reconciler": {
    "fn_by_type": {
      "payment_allocation": 3,
      "fx_rate": 1
    },
    "fn_in_interaction": 3,
    "fp_by_type": {
      "duplicate_invoice": 34,
      "period_cutoff": 26,
      "fx_rate": 29,
      "payment_allocation": 11
    },
    "fp_by_distractor": {
      "same_day_repeat": 12,
      "recurring_amount": 23,
      "tz_boundary": 23,
      "ordinary_record": 26,
      "unremitted_payment": 12,
      "split_payment": 1,
      "boundary_fx_partial": 3
    }
  },
  "flag_all": {
    "fn_by_type": {
      "duplicate_invoice": 8,
      "fx_rate": 9,
      "payment_allocation": 10,
      "period_cutoff": 7
    },
    "fn_in_interaction": 10,
    "fp_by_type": {
      "duplicate_invoice": 80,
      "payment_allocation": 80,
      "fx_rate": 70,
      "period_cutoff": 70
    },
    "fp_by_distractor": {
      "ordinary_record": 214,
      "next_period_doc": 12,
      "tz_boundary": 14,
      "multi_invoice_payment": 20,
      "same_day_repeat": 10,
      "recurring_amount": 12,
      "unremitted_payment": 4,
      "split_payment": 12,
      "partial_open": 2
    }
  },
  "oracle": {
    "fn_by_type": {},
    "fn_in_interaction": 0,
    "fp_by_type": {},
    "fp_by_distractor": {}
  },
  "rule_auditor": {
    "fn_by_type": {},
    "fn_in_interaction": 0,
    "fp_by_type": {},
    "fp_by_distractor": {}
  }
}
```
