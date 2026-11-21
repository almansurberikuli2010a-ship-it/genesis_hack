"""Генератор эпизода: история → чистые проводки → внесение ошибок → рендер.

    ep = generate(seed, config)
    ep.public       — то, что могут видеть инструменты агента;
    ep.truth        — эталон (только оценщик);
    ep.provenance   — история генерации и изменений (только оценщик);
    ep.clean_public — книги до внесения ошибок (только оценщик).

История зависит только от (seed, экономическая часть конфига). ``id_salt`` и ``order_seed`` —
безвредные преобразования: меняют ID и порядок, но не экономику, поэтому ответ oracle
сохраняется с точностью до переименования.

Любой конфиг, принятый ``normalize_config``, генерируется: ошибки и отвлекающие случаи
выполняются точно (недостающие цели и счета добавляются в историю как корректные документы),
а предел документов гарантирован последними попытками (config.safe_regular_invoices).
Фактические числа — в provenance.actual.
"""
from __future__ import annotations

from .config import (CONFIG_VERSION, DISTRACTORS, ERROR_TYPES, GENERATOR_VERSION, INTERACTIONS, MAX_ATTEMPTS,
                     MAX_DOCUMENTS, MAX_ISSUES, SCHEMA_VERSION, ConfigError, normalize_config)
from .injection import inject
from .posting import PostingError, post
from .render import Renderer
from .story import GenerationError, StoryBuilder
from .util import canonical_json, h

__all__ = ["Episode", "generate", "ConfigError", "GenerationError", "GENERATOR_VERSION", "SCHEMA_VERSION",
           "CONFIG_VERSION", "ERROR_TYPES", "INTERACTIONS", "DISTRACTORS", "MAX_ISSUES", "MAX_DOCUMENTS"]


class Episode:
    """Контейнер эпизода. В сторону агента передаётся только ``public``."""

    def __init__(self, public, truth, provenance, clean_public, meta):
        self.public = public
        self.truth = truth
        self.provenance = provenance
        self.clean_public = clean_public
        self.meta = meta

    @property
    def episode_id(self) -> str:
        return self.public["manifest"]["episode_id"]

    def digest(self) -> str:
        return h(canonical_json(self.public), canonical_json(self.truth))


def _build(seed: int, cfg: dict, attempt: int) -> Episode:
    b = StoryBuilder(seed, cfg, attempt)
    b.setup()
    b.build_story()
    inject(b)              # может добавить в историю корректные документы-цели
    b.check_unique_lines()
    clean_rows = post(b, b.invoices, b.payments, {}, b.background)
    rows = post(b, b.invoices + b.injected_invoices, b.payments + b.injected_payments, b.overrides, b.background)
    return Renderer(b, clean_rows, rows).render()


def generate(seed: int | None = 0, config=None) -> Episode:
    if seed is None:
        seed = 0
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ConfigError("seed должен быть целым числом")
    cfg = normalize_config(config)
    last = None
    # Детерминированные повторные попытки: если документов больше MAX_DOCUMENTS (или случайная
    # история не подошла), следующая попытка берёт другую историю и не больше обычных счетов;
    # последние попытки берут число счетов, при котором предел гарантирован.
    for attempt in range(MAX_ATTEMPTS):
        try:
            ep = _build(seed, cfg, attempt)
        except (GenerationError, PostingError) as e:
            last = e
            continue
        if ep.public["manifest"]["counts"]["documents"] > MAX_DOCUMENTS:
            last = GenerationError("слишком много документов")
            continue
        return ep
    raise GenerationError("не удалось сгенерировать эпизод за %d попыток: %s" % (MAX_ATTEMPTS, last))
