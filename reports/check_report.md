# Отчёт проверки

среда 3.0.0, генератор 3.0.0, python 3.11.18, 49.9 с

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
| flag_all | 0.000 | 0.000 | 0.000 | -1.000 | -0.989 | 4.4 | 0.031 | 0 | 300 | 39 |
| oracle | 0.800 | 0.800 | 1.000 | 1.000 | 1.000 | 1.0 | 0.025 | 39 | 0 | 0 |
| random | 0.000 | 0.000 | 0.100 | -0.804 | -0.804 | 18.3 | 0.036 | 0 | 10 | 39 |
| reconciler | 0.254 | 0.690 | 0.360 | -0.307 | -0.307 | 17.3 | 0.039 | 34 | 87 | 5 |
| rule_auditor | 0.800 | 0.800 | 1.000 | 0.967 | 0.967 | 17.3 | 0.039 | 39 | 0 | 0 |

## Базовые политики на dev (режим eval, отдельный процесс)

| политика | P | R | F1 | возврат | эксп. возврат | шаги | время, с | TP | FP | FN |
|---|---|---|---|---|---|---|---|---|---|---|
| flag_all | 0.000 | 0.000 | 0.000 | -1.000 | -1.000 | 4.7 | 0.036 | 0 | 300 | 34 |
| oracle | 0.800 | 0.800 | 1.000 | 1.000 | 1.000 | 1.0 | 0.030 | 34 | 0 | 0 |
| random | 0.000 | 0.000 | 0.200 | -0.607 | -0.607 | 21.9 | 0.043 | 0 | 12 | 34 |
| reconciler | 0.188 | 0.683 | 0.283 | -0.461 | -0.461 | 18.1 | 0.044 | 28 | 106 | 6 |
| rule_auditor | 0.800 | 0.800 | 1.000 | 0.966 | 0.966 | 18.1 | 0.046 | 34 | 0 | 0 |

## Ресурсы

```json
{
  "max_episode_time_s": 0.0654,
  "max_steps_used": 31,
  "peak_rss_mb_orchestrator": 72.8,
  "peak_rss_mb_env_process": 44.0,
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
      "fx_rate": 4,
      "period_cutoff": 4,
      "payment_allocation": 2,
      "duplicate_invoice": 2
    },
    "fp_by_distractor": {
      "ordinary_record": 9,
      "multi_invoice_payment": 1,
      "partial_open": 1,
      "tz_boundary": 1
    }
  },
  "reconciler": {
    "fn_by_type": {
      "fx_rate": 3,
      "payment_allocation": 3
    },
    "fn_in_interaction": 6,
    "fp_by_type": {
      "duplicate_invoice": 33,
      "period_cutoff": 25,
      "payment_allocation": 10,
      "fx_rate": 38
    },
    "fp_by_distractor": {
      "recurring_amount": 23,
      "same_day_repeat": 11,
      "tz_boundary": 24,
      "unremitted_payment": 10,
      "multi_invoice_payment": 3,
      "ordinary_record": 27,
      "partial_open": 2,
      "real_issue_wrong_fields": 5,
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
      "ordinary_record": 226,
      "tz_boundary": 26,
      "split_payment": 2,
      "partial_open": 14,
      "real_issue_wrong_fields": 2,
      "next_period_doc": 12,
      "recurring_amount": 14,
      "refund": 4
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
