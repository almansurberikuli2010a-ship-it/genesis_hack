"""Шаг 1 генерации: согласованная СКРЫТАЯ ИСТОРИЯ компании.

История — это клиенты, траектория курсов, счета, платежи и фоновые операционные проводки
(аренда, зарплата, комиссии банка). Здесь же создаются правдоподобные КОРРЕКТНЫЕ отвлекающие
случаи. Ошибок история не содержит: они вносятся позже (injection.py) как записанные
причинные изменения.

Документ — словарь:
  счёт:   key, entity, issued, due, currency, lines, total, terms, role, posted
  платёж: key, entity, ts, currency, amount, refs, role, posted
где refs — список (ключ счёта, сумма в валюте платежа) или None (нет расписки),
role — вид отвлекающего случая (для provenance) или None,
posted — должен ли документ проводиться в этом периоде (документы вне периода — нет).

Каждый заказанный отвлекающий случай строится ТОЧНО: если свободного обычного счёта нет или
время оплаты не помещается в период, случай строится на своём счёте в начале периода.
Фактические числа (обычные счета, клиенты в валюте, случаи, добавленные цели) собирает
``actual()``; они попадают в provenance.actual.

Документы, на которые потом вносятся ошибки, по открытым признакам (время, смещение, секунды,
сумма, количество, позиция) не отличаются от корректных: у всех платежей одна модель времени
(``pay_time``: рабочие часы или, с долей ROUND_THE_CLOCK, любое время суток, вплоть до конца
периода), секунды у всех моментов случайны, смещение печати выбирает ``display`` для всех,
документы-цели берут обычные дату и строки. Сценарий partial_fx_boundary и его корректный
двойник boundary_fx_partial строятся одним конструктором (``boundary_pair``).

Ключ истории — медленная KDF (util.slow_h, scrypt, 8 МиБ, ~15 мс) от версии, seed и экономической
части конфига (без id_salt и order_seed): перебор seed по открытому наблюдению стоит столько же на
кандидата.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from . import money
from .config import (DISTRACTORS, FX_RESERVE, GENERATOR_VERSION, MAX_ATTEMPTS, TARGET_DOCUMENTS, safe_regular_invoices,
                     story_part)
from .money import convert_minor, sig_round, tz_from_offset
from .util import canonical_json, h, rng, slow_h


class GenerationError(RuntimeError):
    """Конфигурацию нельзя разместить в выбранной истории (генератор попробует снова)."""


# Последние попытки генерации берут число обычных счетов, при котором предел документов
# гарантирован (config.safe_regular_invoices).
GUARANTEED_ATTEMPTS = 4
# Доля платежей «в любое время суток» (интернет-банк, автоплатежи); остальные — в рабочие часы.
ROUND_THE_CLOCK = 0.25


BASE_CHOICES = ["USD", "EUR", "KZT", "GBP"]
FOREIGN_POOL = ["USD", "EUR", "GBP", "CHF", "JPY", "KWD", "KZT"]
COMPANY_OFFSETS = ["+05:00", "+06:00", "+03:00", "+01:00", "+00:00", "-03:00", "-05:00", "+09:00"]
DISPLAY_OFFSETS = ["+00:00", "+01:00", "+03:00", "+05:00", "+08:00", "+09:00", "+12:00",
                   "-03:00", "-05:00", "-08:00", "-10:00"]

# (название, диапазон количества, диапазон цены за единицу в USD-эквиваленте)
PRODUCTS = [
    ("Консультационные услуги, ч", (4, 40), (60, 260)),
    ("Лицензия на программное обеспечение", (1, 12), (150, 2800)),
    ("Транспортные услуги", (1, 6), (120, 1400)),
    ("Аренда оборудования", (1, 10), (80, 900)),
    ("Обучение сотрудников, мест", (2, 25), (90, 420)),
    ("Сервисный контракт", (1, 1), (600, 7000)),
    ("Подписка на данные", (1, 3), (300, 2500)),
]

# Фоновые операционные проводки (Дт расход / Кт денежные средства), без документов
BACKGROUND = [
    ("6100 Аренда помещений", (800, 4000)),
    ("6200 Заработная плата", (2000, 15000)),
    ("6300 Коммунальные услуги", (100, 900)),
    ("6400 Банковские комиссии", (5, 60)),
    ("6500 Хозяйственные расходы", (20, 400)),
]

_SYL_A = ["Нор", "Вел", "Аст", "Кор", "Мир", "Сел", "Тар", "Бел", "Дун", "Ор", "Зел", "Ка", "Лум", "Сер"]
_SYL_B = ["вейл", "дор", "ана", "икс", "монт", "бек", "ора", "вик", "стад", "лин", "ар", "еон"]
_FORMS = ["ТОО «%s Трейд»", "ТОО «%s Логистик»", "АО «%s Системс»", "ТОО «%s Групп»", "ООО «%s Холдинг»",
          "ТОО «%s Партнёрс»", "ООО «%s Лабс»", "ТОО «%s Снаб»"]


class StoryBuilder:
    """Строит историю; тот же объект затем используется для проводок, внесения ошибок и рендера."""

    def __init__(self, seed: int, cfg: dict, attempt: int = 0):
        self.seed = seed
        self.cfg = cfg
        self.attempt = attempt
        # медленная KDF один раз на (версия, seed, экономика); попытки различаются дешёвым хэшем
        self.story_key = slow_h("story-key", GENERATOR_VERSION, seed, canonical_json(story_part(cfg)))
        self.story_hash = h(self.story_key, attempt)
        self.rng = rng("story", self.story_hash)
        self._keys = {"inv": 0, "pay": 0, "ent": 0, "bg": 0}
        self.invoices: list[dict] = []
        self.payments: list[dict] = []
        self.background: list[dict] = []
        self.assigned: set = set()       # счета, судьба которых уже решена (план оплаты или особая роль)
        self.locked_inv: set = set()     # счета, которые нельзя трогать при внесении ошибок
        self.locked_pay: set = set()     # то же для платежей
        self.unremitted_inv: set = set() # открытые счета, «оплаченные» платежом без расписки
        self.boundary_pairs: list = []   # (счёт, платёж) для сценария partial_fx_boundary
        self.role = None                 # текущая роль отвлекающего случая
        self.n_regular = 0               # фактическое число обычных счетов
        self.distractor_counts = {k: 0 for k in DISTRACTORS}
        self.fresh_distractors = {k: 0 for k in DISTRACTORS}  # случаи, построенные на своём счёте
        self.supplied: list = []         # корректные документы-цели, добавленные для внесения ошибок

    # ------------------------------------------------------------ ключи и курсы
    def next_key(self, kind: str) -> int:
        self._keys[kind] += 1
        return self._keys[kind]

    def rate(self, cur: str, d: date) -> Decimal:
        if cur == self.base:
            return Decimal(1)
        return self.fx[(cur, d)]

    def conv(self, amount: int, cur: str, d: date) -> int:
        return convert_minor(amount, cur, self.base, self.rate(cur, d))

    # ------------------------------------------------------------ компания, период, курсы
    def setup(self) -> None:
        r = self.rng
        self.base = r.choice(BASE_CHOICES)
        self.offset = r.choice(COMPANY_OFFSETS)
        self.tzc = tz_from_offset(self.offset)
        year = r.choice([2025, 2026])
        month = r.randint(1, 12)
        self.p_start = datetime(year, month, 1, tzinfo=self.tzc)
        ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
        self.p_end = datetime(ny, nm, 1, tzinfo=self.tzc)
        self.last_day = (self.p_end - timedelta(days=1)).day
        pool = [c for c in FOREIGN_POOL if c != self.base]
        r.shuffle(pool)
        self.foreign = sorted(pool[: r.choice([2, 2, 3])])
        self.company = r.choice(_FORMS) % (r.choice(_SYL_A) + r.choice(_SYL_B))
        self.customers = []
        for _ in range(r.randint(*self.cfg["n_customers"])):
            cur = r.choice(self.foreign) if r.random() < self.cfg["foreign_share"] else self.base
            self.customers.append({"key": self.next_key("ent"), "currency": cur})
        need_foreign = self.cfg["errors"]["fx_rate"] or self.cfg["interactions"]["partial_fx_boundary"]
        if need_foreign and not any(c["currency"] != self.base for c in self.customers):
            self.customers[0]["currency"] = self.foreign[0]  # ошибкам курса нужен валютный клиент
        # дневная траектория курсов: случайное блуждание, 6 значащих цифр
        d0 = self.p_start.date() - timedelta(days=3)
        d1 = self.p_end.date() + timedelta(days=9)
        self.fx: dict = {}
        for c in self.foreign:
            val = money.REF_USD_VALUE[c] / money.REF_USD_VALUE[self.base]
            val *= Decimal(str(1 + r.uniform(-0.03, 0.03)))
            d = d0
            while d <= d1:
                val *= Decimal(str(1 + r.gauss(0, 0.004)))
                self.fx[(c, d)] = sig_round(val, 6)
                d += timedelta(days=1)
        self.fx_dates = (d0, d1)

    # ------------------------------------------------------------ примитивы документов
    def local_dt(self, day: int, h0: int = 8, h1: int = 19) -> datetime:
        r = self.rng
        return datetime(self.p_start.year, self.p_start.month, day, r.randint(h0, h1),
                        r.randint(0, 59), r.randint(0, 59), tzinfo=self.tzc)

    def display(self, dt: datetime, kind: str) -> datetime:
        """Смещение, в котором документ «напечатан» (момент времени тот же)."""
        r = self.rng
        if r.random() < (0.7 if kind == "inv" else 0.5):
            return dt.astimezone(self.tzc)
        off = "+00:00" if (kind == "pay" and r.random() < 0.5) else r.choice(DISPLAY_OFFSETS)
        return dt.astimezone(tz_from_offset(off))

    def make_lines(self, cur: str, product=None, qty=None, label=None) -> list[dict]:
        r = self.rng
        exp = money.exponent(cur)
        prods = [product] if product else r.sample(PRODUCTS, r.choice([1, 1, 2, 3]))
        lines = []
        for i, (name, qrange, prange) in enumerate(prods, 1):
            q = qty if qty is not None else r.randint(*qrange)
            usd = Decimal(r.randint(*prange)) + Decimal(r.choice([0, 0, 50, 99])) / 100
            major = sig_round(usd / money.REF_USD_VALUE[cur], 4)
            unit = max(1, int(major * (Decimal(10) ** exp)))
            po = r.randint(10000, 99999)
            desc = "%s (заказ PO-%d)" % (name, po) if label is None else "%s — %s (заказ PO-%d)" % (name, label, po)
            lines.append({"line_no": i, "description": desc, "quantity": q,
                          "unit_price_minor": unit, "amount_minor": q * unit})
        return lines

    def new_invoice(self, ent: dict, issued: datetime, lines=None, display: bool = True) -> dict:
        lines = lines or self.make_lines(ent["currency"])
        inv = {
            "key": self.next_key("inv"),
            "entity": ent["key"],
            "issued": self.display(issued, "inv") if display else issued,
            "currency": ent["currency"],
            "lines": lines,
            "total": sum(l["amount_minor"] for l in lines),
            "terms": self.rng.choice([14, 30, 30, 45]),
            "role": self.role,
            "posted": True,
        }
        inv["due"] = inv["issued"] + timedelta(days=inv["terms"])
        return inv

    def new_payment(self, ent_key: int, cur: str, ts: datetime, amount: int, refs, display: bool = True) -> dict:
        if self.role:  # счета, участвующие в отвлекающем случае, получают ту же роль
            for ik, _ in refs or []:
                inv = self.invoice(ik)
                if inv["role"] is None:
                    inv["role"] = self.role
        return {
            "key": self.next_key("pay"),
            "entity": ent_key,
            "ts": self.display(ts, "pay") if display else ts,
            "currency": cur,
            "amount": amount,
            "refs": refs,
            "role": self.role,
            "posted": True,
        }

    def invoice(self, key: int) -> dict:
        return next(i for i in self.invoices if i["key"] == key)

    def customer(self, key: int) -> dict:
        return next(c for c in self.customers if c["key"] == key)

    def pay_time(self, after: datetime, dmin: int = 1, dmax: int = 18):
        """Время оплаты после ``after`` внутри периода или None. Время суток — рабочие часы или,
        с долей ROUND_THE_CLOCK, любое (в том числе ночь и последние часы периода); секунды
        случайны и не связаны с секундами счёта."""
        r = self.rng
        t = after + timedelta(days=r.randint(dmin, dmax), hours=r.randint(-3, 5), minutes=r.randint(0, 59))
        loc = t.astimezone(self.tzc)
        if r.random() < ROUND_THE_CLOCK:
            loc = loc.replace(hour=r.randint(0, 23), minute=r.randint(0, 59))
        elif loc.hour < 7 or loc.hour > 20:
            loc = loc.replace(hour=r.randint(8, 18))
        t = loc.replace(second=r.randint(0, 59))
        if t >= self.p_end or t <= after:
            return None
        return t

    def boundary_instant(self) -> datetime:
        """Момент в последний локальный день периода, когда дата по UTC отличается от даты
        компании (для UTC+0 — вечер, который банк валютирует следующим днём). Внутри окна время
        равномерно до секунды, как у платежей «в любое время суток» в ``pay_time``."""
        off_h = int(self.offset[:3])
        if off_h > 0:      # ранние часы: по UTC это ещё предыдущий день
            lo, hi = 0, off_h
        elif off_h < 0:    # поздний вечер: по UTC уже следующий день
            lo, hi = 24 + off_h, 24
        else:              # UTC+0: дата валютирования банка — следующий день
            lo, hi = 16, 23
        return self.p_end - timedelta(days=1) + timedelta(seconds=self.rng.randrange(lo * 3600, hi * 3600))

    def free_invoices(self, latest_day=None) -> list[dict]:
        out = []
        for inv in self.invoices:
            if inv["key"] in self.assigned or not inv["posted"]:
                continue
            if latest_day is not None and inv["issued"].astimezone(self.tzc).day > latest_day:
                continue
            out.append(inv)
        return out

    def next_period_instant(self) -> datetime:
        r = self.rng
        return self.p_end + timedelta(hours=r.randint(2, 96), minutes=r.randint(0, 59), seconds=r.randint(0, 59))

    def prior_period_instant(self) -> datetime:
        r = self.rng
        return self.p_start - timedelta(hours=r.randint(2, 60), minutes=r.randint(0, 59), seconds=r.randint(0, 59))

    # ------------------------------------------------------------ сборка истории
    def build_story(self) -> None:
        r = self.rng
        d, e, it = self.cfg["distractors"], self.cfg["errors"], self.cfg["interactions"]
        n_inv = r.randint(*self.cfg["n_invoices"])
        # держим эпизод около предлагаемых ТЗ TARGET_DOCUMENTS: ~1.8 документа на базовый счёт + прочее
        # (жёсткий предел MAX_DOCUMENTS проверяет generate; последние попытки его гарантируют)
        extras = (2 * sum(d.values()) + 2 * d["recurring_amount"] + 2 * sum(it.values())
                  + 3 * it["masked_duplicate"] + e["duplicate_invoice"] + e["period_cutoff"] + 4)
        n_inv = max(10, min(n_inv, int((TARGET_DOCUMENTS - extras) / 1.8)))
        if self.attempt:  # повторная попытка — компания меньше; последние — с гарантией предела
            safe = safe_regular_invoices(self.cfg)
            if self.attempt >= MAX_ATTEMPTS - GUARANTEED_ATTEMPTS:
                n_inv = min(n_inv, safe)
            else:
                n_inv = min(n_inv, max(safe, int(n_inv * (0.85 ** self.attempt))))
        self.n_regular = n_inv
        for _ in range(n_inv):
            self.invoices.append(self.new_invoice(r.choice(self.customers), self.local_dt(r.randint(1, self.last_day))))
        # запас валютных счетов под заказанные ошибки курса
        need = e["fx_rate"] + FX_RESERVE if e["fx_rate"] else 0
        fents = [c for c in self.customers if c["currency"] != self.base]
        while sum(1 for i in self.invoices if i["currency"] != self.base) < need:
            self.invoices.append(self.new_invoice(r.choice(fents), self.local_dt(r.randint(1, self.last_day))))
        self.build_distractors()
        self.build_boundary_pairs()
        # обычные планы оплаты для остальных счетов
        for inv in list(self.invoices):
            if inv["key"] in self.assigned or not inv["posted"]:
                continue
            self.assigned.add(inv["key"])
            if r.random() < 0.8:
                t = self.pay_time(inv["issued"])
                if t is not None:
                    self.payments.append(self.new_payment(inv["entity"], inv["currency"], t, inv["total"],
                                                          [(inv["key"], inv["total"])]))
        self.build_background()
        self.check_unique_lines()

    def build_background(self) -> None:
        r = self.rng
        exp = money.exponent(self.base)
        for _ in range(r.randint(*self.cfg["background_entries"])):
            account, (lo, hi) = r.choice(BACKGROUND)
            usd = Decimal(r.randint(lo, hi)) + Decimal(r.randint(0, 99)) / 100
            major = sig_round(usd / money.REF_USD_VALUE[self.base], 4)
            self.background.append({"key": self.next_key("bg"), "ts": self.local_dt(r.randint(1, self.last_day), 7, 20),
                                    "account": account, "amount": max(1, int(major * (Decimal(10) ** exp)))})

    def actual(self) -> dict:
        """Фактические числа истории (что из конфига выполнено и как)."""
        n_foreign = sum(1 for c in self.customers if c["currency"] != self.base)
        return {
            "customers": len(self.customers),
            "foreign_customers": n_foreign,
            "foreign_share": round(n_foreign / len(self.customers), 4),
            "regular_invoices": self.n_regular,
            "background_entries": len(self.background),
            "distractors": dict(self.distractor_counts),
            "distractors_on_own_invoice": {k: v for k, v in self.fresh_distractors.items() if v},
        }

    def check_unique_lines(self) -> None:
        seen = set()
        for inv in self.invoices:
            sig = canonical_json([inv["entity"], inv["currency"], inv["lines"]])
            if sig in seen:
                raise GenerationError("случайный дубль счёта в чистой истории")
            seen.add(sig)

    # ------------------------------------------------------------ отвлекающие случаи (все корректны)
    def build_distractors(self) -> None:
        """Каждый заказанный случай строится ровно один раз: сначала на свободном обычном счёте,
        а если его нет или оплата не помещается в период — на своём счёте в начале периода."""
        d = self.cfg["distractors"]
        for kind in ("split_payment", "multi_invoice_payment", "partial_open", "refund", "recurring_amount",
                     "tz_boundary", "unremitted_payment", "same_day_repeat", "next_period_doc", "prior_period_doc",
                     "boundary_fx_partial"):
            self.role = kind
            fn = getattr(self, "d_" + kind)
            for i in range(d[kind]):
                if not fn(i):
                    if not fn(i, fresh=True):
                        raise GenerationError("не удалось построить отвлекающий случай %s" % kind)
                    self.fresh_distractors[kind] += 1
                self.distractor_counts[kind] += 1
        self.role = None

    def early_invoice(self, ent=None) -> dict:
        """Свой счёт случая в первые дни периода: любая оплата после него помещается в период."""
        r = self.rng
        inv = self.new_invoice(ent or r.choice(self.customers), self.local_dt(r.randint(1, 5)))
        self.invoices.append(inv)
        return inv

    def pick_free(self, latest_day, fresh):
        """Свободный обычный счёт, выставленный не позже ``latest_day``, или, при ``fresh``, свой
        счёт случая в начале периода."""
        if fresh:
            return self.early_invoice()
        cands = self.free_invoices(latest_day=latest_day)
        return self.rng.choice(cands) if cands else None

    def d_split_payment(self, i, fresh=False):
        r = self.rng
        inv = self.pick_free(self.last_day - 8, fresh)
        if inv is None:
            return False
        t1 = self.pay_time(inv["issued"], 1, 3)
        t2 = self.pay_time(t1, 1, 4) if t1 else None
        if not t2:
            return False
        a = max(1, int(inv["total"] * Decimal(str(r.uniform(0.3, 0.7)))))
        self.assigned.add(inv["key"])
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t1, a, [(inv["key"], a)]))
        b = inv["total"] - a
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t2, b, [(inv["key"], b)]))
        return True

    def d_multi_invoice_payment(self, i, fresh=False):
        r = self.rng
        new = None
        if fresh:
            ent = r.choice(self.customers)
            groups = [[self.early_invoice(ent), self.early_invoice(ent)]]
        else:
            groups = {}
            for inv in self.free_invoices(latest_day=self.last_day - 5):
                groups.setdefault((inv["entity"], inv["currency"]), []).append(inv)
            groups = [g for g in groups.values() if len(g) >= 2]
        if not groups:
            cands = self.free_invoices(latest_day=self.last_day - 5)
            if not cands:
                return False
            base_inv = r.choice(cands)
            new = self.new_invoice(self.customer(base_inv["entity"]), self.local_dt(r.randint(1, self.last_day - 5)))
            self.invoices.append(new)
            groups = [[base_inv, new]]
        a, b = r.sample(r.choice(groups), 2)
        t = self.pay_time(max(a["issued"], b["issued"]), 1, 3)
        if not t:
            if new is not None:  # счёт, созданный для неудавшегося случая, в историю не идёт
                self.invoices.remove(new)
            return False
        self.assigned.update({a["key"], b["key"]})
        self.payments.append(self.new_payment(a["entity"], a["currency"], t, a["total"] + b["total"],
                                              [(a["key"], a["total"]), (b["key"], b["total"])]))
        return True

    def d_partial_open(self, i, fresh=False):
        r = self.rng
        inv = self.pick_free(self.last_day - 3, fresh)
        if inv is None:
            return False
        t = self.pay_time(inv["issued"], 1, 6)
        if not t:
            return False
        a = max(1, int(inv["total"] * Decimal(str(r.uniform(0.2, 0.8)))))
        self.assigned.add(inv["key"])
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t, a, [(inv["key"], a)]))
        return True

    def d_refund(self, i, fresh=False):
        r = self.rng
        inv = self.pick_free(self.last_day - 9, fresh)
        if inv is None:
            return False
        t1 = self.pay_time(inv["issued"], 1, 3)
        t2 = self.pay_time(t1, 1, 4) if t1 else None
        if not t2:
            return False
        x = max(1, int(inv["total"] * Decimal(str(r.uniform(0.02, 0.12)))))
        self.assigned.add(inv["key"])
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t1, inv["total"] + x,
                                              [(inv["key"], inv["total"] + x)]))
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t2, -x, [(inv["key"], -x)]))
        return True

    def d_recurring_amount(self, i, fresh=False):
        r = self.rng
        ent = r.choice(self.customers)
        prod = r.choice(PRODUCTS)
        q = r.randint(*prod[1])
        start = r.randint(1, max(1, self.last_day - 16))
        template = self.make_lines(ent["currency"], product=prod, qty=q, label="неделя 1")[0]
        for w in range(3):
            line = dict(template, description="%s — неделя %d (заказ PO-%d)" % (prod[0], w + 1, r.randint(10000, 99999)))
            self.invoices.append(self.new_invoice(ent, self.local_dt(min(self.last_day, start + 7 * w)), lines=[line]))
        return True

    def d_tz_boundary(self, i, fresh=False):
        """Документ у границы периода: момент внутри, а напечатанная дата — в соседнем месяце."""
        r = self.rng
        as_payment = i % 2 == 1
        end_side = as_payment or r.random() < 0.5
        if end_side:
            instant = self.p_end - timedelta(minutes=r.randint(25, 170), seconds=r.randint(0, 59))
            want = self.p_end.date()
        else:
            instant = self.p_start + timedelta(minutes=r.randint(25, 170), seconds=r.randint(0, 59))
            want = self.p_start.date() - timedelta(days=1)
        offs = list(DISPLAY_OFFSETS)
        r.shuffle(offs)
        disp = next((instant.astimezone(tz_from_offset(o)) for o in offs
                     if instant.astimezone(tz_from_offset(o)).date() == want), None)
        if disp is None:
            return False
        if as_payment:
            inv = self.pick_free(self.last_day - 2, fresh)
            if inv is None:
                return False
            self.assigned.add(inv["key"])
            self.payments.append(self.new_payment(inv["entity"], inv["currency"], disp, inv["total"],
                                                  [(inv["key"], inv["total"])], display=False))
            return True
        inv = self.new_invoice(r.choice(self.customers), disp, display=False)
        self.invoices.append(inv)
        if not end_side:  # счёт в начале периода успевают оплатить
            self.assigned.add(inv["key"])
            t = self.pay_time(inv["issued"], 2, 10)
            if t:
                self.payments.append(self.new_payment(inv["entity"], inv["currency"], t, inv["total"],
                                                      [(inv["key"], inv["total"])]))
        return True

    def d_unremitted_payment(self, i, fresh=False):
        """Платёж без расписки на сумму открытого счёта: связь угадывать нельзя."""
        inv = self.pick_free(self.last_day - 4, fresh)
        if inv is None:
            return False
        t = self.pay_time(inv["issued"], 1, 5)
        if not t:
            return False
        self.assigned.add(inv["key"])
        self.unremitted_inv.add(inv["key"])
        if inv["role"] is None:
            inv["role"] = "unremitted_payment"
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t, inv["total"], None))
        return True

    def d_same_day_repeat(self, i, fresh=False):
        """Два одинаковых заказа в один день: та же сумма, разные PO и время."""
        r = self.rng
        ent = r.choice(self.customers)
        prod = r.choice(PRODUCTS)
        q = r.randint(*prod[1])
        day = r.randint(1, self.last_day)
        line = self.make_lines(ent["currency"], product=prod, qty=q)[0]
        h1 = r.randint(8, 12)
        for hh in (h1, h1 + r.randint(2, 6)):
            l2 = dict(line, description="%s (заказ PO-%d)" % (prod[0], r.randint(10000, 99999)))
            when = datetime(self.p_start.year, self.p_start.month, day, hh, r.randint(0, 59), r.randint(0, 59),
                            tzinfo=self.tzc)
            self.invoices.append(self.new_invoice(ent, when, lines=[l2]))
        return True

    def d_next_period_doc(self, i, fresh=False):
        """Документ следующего периода (последующая оплата или счёт после закрытия) — НЕ проводится."""
        r = self.rng
        after = self.next_period_instant()
        cands = self.free_invoices() if i % 2 == 1 else []
        if cands:
            inv = r.choice(cands)
            self.assigned.add(inv["key"])
            p = self.new_payment(inv["entity"], inv["currency"], after, inv["total"], [(inv["key"], inv["total"])])
            p["posted"] = False
            self.payments.append(p)
            self.locked_pay.add(p["key"])
            return True
        inv = self.new_invoice(r.choice(self.customers), after)
        inv["posted"] = False
        self.assigned.add(inv["key"])
        self.invoices.append(inv)
        self.locked_inv.add(inv["key"])
        return True

    def d_prior_period_doc(self, i, fresh=False):
        """Счёт прошлого периода: проведён в прошлых книгах, в этом периоде НЕ проводится."""
        inv = self.new_invoice(self.rng.choice(self.customers), self.prior_period_instant())
        inv["posted"] = False
        self.assigned.add(inv["key"])
        self.invoices.append(inv)
        self.locked_inv.add(inv["key"])
        return True

    def d_boundary_fx_partial(self, i, fresh=False):
        """Корректный двойник сценария partial_fx_boundary: тот же конструктор (``boundary_pair``),
        но платёж проведён верно — курс даты компании, вся расписанная сумма зачтена на счёт.
        Без клиентов в иностранной валюте — в базовой валюте (частичный платёж у границы)."""
        fents = [c for c in self.customers if c["currency"] != self.base] or self.customers
        self.boundary_pair(self.rng.choice(fents))
        return True

    # ------------------------------------------------------------ документы-цели для внесения ошибок
    def large_lines(self, cur: str, usd: int) -> list[dict]:
        """Строки обычного счёта (``make_lines``) с суммой не меньше ``usd`` в долларах: выборка из
        того же распределения повторяется, количество и цены остаются в обычных диапазонах товаров."""
        floor_minor = Decimal(usd) / money.REF_USD_VALUE[cur] * (Decimal(10) ** money.exponent(cur))
        for _ in range(64):
            lines = self.make_lines(cur)
            if sum(l["amount_minor"] for l in lines) >= floor_minor:
                return lines
        raise GenerationError("не удалось подобрать строки счёта на сумму от %d USD" % usd)

    def supply_invoice(self, foreign: bool = False, min_usd: int = 0, currencies=None, issued=None) -> dict:
        """Корректный обычный счёт — цель ошибки, если подходящей в истории нет. Дата и строки —
        из того же распределения, что у обычных счетов. Документ принадлежит истории (есть и в
        книгах до внесения) и записывается в provenance. ``currencies`` — ограничить клиентов
        этими валютами; ``issued`` — заданный момент выставления."""
        r = self.rng
        pool = [c for c in self.customers if c["currency"] != self.base] if foreign else self.customers
        if currencies is not None:
            pool = [c for c in pool if c["currency"] in currencies]
        if not pool:
            raise GenerationError("нет клиента в иностранной валюте для ошибки курса")
        ent = r.choice(pool)
        if issued is None:
            issued = self.local_dt(r.randint(1, self.last_day))
        inv = self.new_invoice(ent, issued, lines=self.large_lines(ent["currency"], min_usd) if min_usd else None)
        self.invoices.append(inv)
        self.assigned.add(inv["key"])
        self.supplied.append(("inv", inv["key"]))
        return inv

    def supply_paid_invoice(self) -> tuple:
        """Корректные счёт и его полная оплата одним платежом — цель ошибки распределения. Дата
        счёта — обычная, при которой оплата помещается в период (иначе — начало периода)."""
        r = self.rng
        for _ in range(8):
            issued = self.local_dt(r.randint(1, self.last_day))
            t = self.pay_time(issued, 1, 5)
            if t is not None:
                break
        else:
            issued = self.local_dt(r.randint(1, 5))
            t = self.pay_time(issued, 1, 5)
        if t is None:
            raise GenerationError("оплата счёта-цели не помещается в период")
        inv = self.supply_invoice(issued=issued)
        p = self.new_payment(inv["entity"], inv["currency"], t, inv["total"], [(inv["key"], inv["total"])])
        self.payments.append(p)
        self.supplied.append(("pay", p["key"]))
        return inv, p

    # ------------------------------------------------------------ сценарий partial_fx_boundary и его двойник
    def boundary_pair(self, ent: dict) -> tuple:
        """Счёт в первой половине периода на существенную сумму и его частичная оплата в последний
        день периода у смены даты по UTC (``boundary_instant``); смещение печати — общее
        (``display``). Один конструктор для сценария partial_fx_boundary (ошибки на платеже вносит
        injection.py) и для корректного двойника boundary_fx_partial: открытые признаки у них из
        одного распределения, отличает их только проводка."""
        r = self.rng
        inv = self.new_invoice(ent, self.local_dt(r.randint(1, max(1, self.last_day - 12))),
                               lines=self.large_lines(ent["currency"], 2000))  # сумма должна быть существенной
        self.invoices.append(inv)
        self.assigned.add(inv["key"])
        amt = max(1, int(inv["total"] * Decimal(str(r.uniform(0.35, 0.75)))))
        p = self.new_payment(inv["entity"], inv["currency"], self.boundary_instant(), amt, [(inv["key"], amt)])
        self.payments.append(p)
        return inv, p

    def build_boundary_pairs(self) -> None:
        """Документы сценария partial_fx_boundary. Сами документы корректны; ошибки на этом
        платеже вносит injection.py."""
        fents = [c for c in self.customers if c["currency"] != self.base]
        for _ in range(self.cfg["interactions"]["partial_fx_boundary"]):
            inv, p = self.boundary_pair(self.rng.choice(fents))
            self.locked_inv.add(inv["key"])
            self.boundary_pairs.append((inv, p))
