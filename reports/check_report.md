# Отчёт проверки

среда 3.2.0, генератор 3.2.0, схемы 3.2, конфиг 3.2; коммит f4bf54b2ea1b + незакоммиченные правки; python 3.13.17 (darwin), 62.4 с

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
| flag_all | 0.000 | 0.000 | 0.000 | -1.000 | -1.000 | 4.5 | 0.020 | 0 | 300 | 39 |
| oracle | 0.800 | 0.800 | 1.000 | 1.000 | 1.000 | 1.0 | 0.018 | 39 | 0 | 0 |
| random | 0.000 | 0.000 | 0.200 | -0.605 | -0.605 | 17.9 | 0.027 | 0 | 8 | 39 |
| reconciler | 0.228 | 0.654 | 0.329 | -0.369 | -0.369 | 17.3 | 0.026 | 33 | 103 | 6 |
| rule_auditor | 0.800 | 0.800 | 1.000 | 0.967 | 0.967 | 17.3 | 0.026 | 39 | 0 | 0 |

## Базовые политики на dev (режим eval, отдельный процесс)

| политика | P | R | F1 | возврат | эксп. возврат | шаги | время, с | TP | FP | FN |
|---|---|---|---|---|---|---|---|---|---|---|
| flag_all | 0.000 | 0.000 | 0.000 | -1.000 | -0.996 | 4.4 | 0.019 | 0 | 300 | 34 |
| oracle | 0.800 | 0.800 | 1.000 | 1.000 | 1.000 | 1.0 | 0.017 | 34 | 0 | 0 |
| random | 0.000 | 0.000 | 0.100 | -0.803 | -0.803 | 18.3 | 0.026 | 0 | 8 | 34 |
| reconciler | 0.225 | 0.658 | 0.314 | -0.399 | -0.399 | 17.7 | 0.025 | 28 | 89 | 6 |
| rule_auditor | 0.800 | 0.800 | 1.000 | 0.967 | 0.967 | 17.7 | 0.024 | 34 | 0 | 0 |

## Ресурсы

```json
{
  "max_episode_time_s": 0.0832,
  "max_steps_used": 28,
  "peak_rss_mb_orchestrator": 136.9,
  "peak_rss_mb_env_process": 70.8,
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
      "period_cutoff": 7,
      "payment_allocation": 10
    },
    "fn_in_interaction": 10,
    "fp_by_type": {
      "duplicate_invoice": 4,
      "period_cutoff": 3,
      "payment_allocation": 1
    },
    "fp_by_distractor": {
      "ordinary_record": 5,
      "multi_invoice_payment": 1,
      "tz_boundary": 1,
      "partial_open": 1
    }
  },
  "reconciler": {
    "fn_by_type": {
      "fx_rate": 4,
      "payment_allocation": 2
    },
    "fn_in_interaction": 5,
    "fp_by_type": {
      "duplicate_invoice": 33,
      "period_cutoff": 23,
      "fx_rate": 22,
      "payment_allocation": 11
    },
    "fp_by_distractor": {
      "recurring_amount": 22,
      "same_day_repeat": 11,
      "tz_boundary": 24,
      "multi_invoice_payment": 3,
      "unremitted_payment": 12,
      "ordinary_record": 15,
      "refund": 1,
      "split_payment": 1
    }
  },
  "flag_all": {
    "fn_by_type": {
      "duplicate_invoice": 8,
      "fx_rate": 9,
      "period_cutoff": 7,
      "payment_allocation": 10
    },
    "fn_in_interaction": 10,
    "fp_by_type": {
      "duplicate_invoice": 80,
      "payment_allocation": 80,
      "fx_rate": 70,
      "period_cutoff": 70
    },
    "fp_by_distractor": {
      "ordinary_record": 194,
      "same_day_repeat": 16,
      "partial_open": 16,
      "refund": 8,
      "recurring_amount": 24,
      "multi_invoice_payment": 12,
      "next_period_doc": 4,
      "tz_boundary": 4,
      "real_issue_wrong_fields": 22
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
