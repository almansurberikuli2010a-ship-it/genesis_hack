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
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from . import money
from .config import GENERATOR_VERSION, MAX_DOCUMENTS, story_part
from .money import convert_minor, sig_round, tz_from_offset
from .util import canonical_json, h, rng


class GenerationError(RuntimeError):
    """Конфигурацию нельзя разместить в выбранной истории (генератор попробует снова)."""


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
        self.story_hash = h(GENERATOR_VERSION, seed, canonical_json(story_part(cfg)), attempt)
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
        if not any(c["currency"] != self.base for c in self.customers):
            self.customers[0]["currency"] = self.foreign[0]
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
        """Время оплаты после ``after`` внутри периода (рабочие часы) или None."""
        r = self.rng
        t = after + timedelta(days=r.randint(dmin, dmax), hours=r.randint(-3, 5), minutes=r.randint(0, 59))
        loc = t.astimezone(self.tzc)
        if loc.hour < 7 or loc.hour > 20:
            t = loc.replace(hour=r.randint(8, 18))
        if t >= self.p_end - timedelta(hours=6) or t <= after:
            return None
        return t

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
        return self.p_end + timedelta(hours=self.rng.randint(2, 96), minutes=self.rng.randint(0, 59))

    def prior_period_instant(self) -> datetime:
        return self.p_start - timedelta(hours=self.rng.randint(2, 60), minutes=self.rng.randint(0, 59))

    # ------------------------------------------------------------ сборка истории
    def build_story(self) -> None:
        r = self.rng
        d, e, it = self.cfg["distractors"], self.cfg["errors"], self.cfg["interactions"]
        n_inv = r.randint(*self.cfg["n_invoices"])
        # держим эпизод в пределах MAX_DOCUMENTS: ~1.8 документа на базовый счёт + прочее
        extras = (2 * sum(d.values()) + 2 * d["recurring_amount"] + 2 * sum(it.values())
                  + 3 * it["masked_duplicate"] + e["duplicate_invoice"] + e["period_cutoff"] + 4)
        n_inv = max(10, min(n_inv, int((MAX_DOCUMENTS - extras) / 1.8)))
        n_inv = max(10, int(n_inv * (0.85 ** self.attempt)))  # повторная попытка — компания меньше
        for _ in range(n_inv):
            self.invoices.append(self.new_invoice(r.choice(self.customers), self.local_dt(r.randint(1, self.last_day))))
        # запас валютных счетов под заказанные ошибки курса
        need = e["fx_rate"] + 2 if e["fx_rate"] else 0
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

    def check_unique_lines(self) -> None:
        seen = set()
        for inv in self.invoices:
            sig = canonical_json([inv["entity"], inv["currency"], inv["lines"]])
            if sig in seen:
                raise GenerationError("случайный дубль счёта в чистой истории")
            seen.add(sig)

    # ------------------------------------------------------------ отвлекающие случаи (все корректны)
    def build_distractors(self) -> None:
        d = self.cfg["distractors"]
        for kind in ("split_payment", "multi_invoice_payment", "partial_open", "refund", "recurring_amount",
                     "tz_boundary", "unremitted_payment", "same_day_repeat", "next_period_doc", "prior_period_doc"):
            self.role = kind
            fn = getattr(self, "d_" + kind)
            for i in range(d[kind]):
                fn(i)
        self.role = None

    def d_split_payment(self, i):
        r = self.rng
        cands = self.free_invoices(latest_day=self.last_day - 8)
        if not cands:
            return
        inv = r.choice(cands)
        t1 = self.pay_time(inv["issued"], 1, 3)
        t2 = self.pay_time(t1, 1, 4) if t1 else None
        if not t2:
            return
        a = max(1, int(inv["total"] * Decimal(str(r.uniform(0.3, 0.7)))))
        self.assigned.add(inv["key"])
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t1, a, [(inv["key"], a)]))
        b = inv["total"] - a
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t2, b, [(inv["key"], b)]))

    def d_multi_invoice_payment(self, i):
        r = self.rng
        groups = {}
        for inv in self.free_invoices(latest_day=self.last_day - 5):
            groups.setdefault((inv["entity"], inv["currency"]), []).append(inv)
        groups = [g for g in groups.values() if len(g) >= 2]
        if not groups:
            cands = self.free_invoices(latest_day=self.last_day - 5)
            if not cands:
                return
            base_inv = r.choice(cands)
            new = self.new_invoice(self.customer(base_inv["entity"]), self.local_dt(r.randint(1, self.last_day - 5)))
            self.invoices.append(new)
            groups = [[base_inv, new]]
        a, b = r.sample(r.choice(groups), 2)
        t = self.pay_time(max(a["issued"], b["issued"]), 1, 3)
        if not t:
            return
        self.assigned.update({a["key"], b["key"]})
        self.payments.append(self.new_payment(a["entity"], a["currency"], t, a["total"] + b["total"],
                                              [(a["key"], a["total"]), (b["key"], b["total"])]))

    def d_partial_open(self, i):
        r = self.rng
        cands = self.free_invoices(latest_day=self.last_day - 3)
        if not cands:
            return
        inv = r.choice(cands)
        t = self.pay_time(inv["issued"], 1, 6)
        if not t:
            return
        a = max(1, int(inv["total"] * Decimal(str(r.uniform(0.2, 0.8)))))
        self.assigned.add(inv["key"])
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t, a, [(inv["key"], a)]))

    def d_refund(self, i):
        r = self.rng
        cands = self.free_invoices(latest_day=self.last_day - 9)
        if not cands:
            return
        inv = r.choice(cands)
        t1 = self.pay_time(inv["issued"], 1, 3)
        t2 = self.pay_time(t1, 1, 4) if t1 else None
        if not t2:
            return
        x = max(1, int(inv["total"] * Decimal(str(r.uniform(0.02, 0.12)))))
        self.assigned.add(inv["key"])
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t1, inv["total"] + x,
                                              [(inv["key"], inv["total"] + x)]))
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t2, -x, [(inv["key"], -x)]))

    def d_recurring_amount(self, i):
        r = self.rng
        ent = r.choice(self.customers)
        prod = r.choice(PRODUCTS)
        q = r.randint(*prod[1])
        start = r.randint(1, max(1, self.last_day - 16))
        template = self.make_lines(ent["currency"], product=prod, qty=q, label="неделя 1")[0]
        for w in range(3):
            line = dict(template, description="%s — неделя %d (заказ PO-%d)" % (prod[0], w + 1, r.randint(10000, 99999)))
            self.invoices.append(self.new_invoice(ent, self.local_dt(min(self.last_day, start + 7 * w)), lines=[line]))

    def d_tz_boundary(self, i):
        """Документ у границы периода: момент внутри, а напечатанная дата — в соседнем месяце."""
        r = self.rng
        as_payment = i % 2 == 1
        end_side = as_payment or r.random() < 0.5
        if end_side:
            instant = self.p_end - timedelta(minutes=r.randint(25, 170))
            want = self.p_end.date()
        else:
            instant = self.p_start + timedelta(minutes=r.randint(25, 170))
            want = self.p_start.date() - timedelta(days=1)
        offs = list(DISPLAY_OFFSETS)
        r.shuffle(offs)
        disp = next((instant.astimezone(tz_from_offset(o)) for o in offs
                     if instant.astimezone(tz_from_offset(o)).date() == want), None)
        if disp is None:
            return
        if as_payment:
            cands = self.free_invoices(latest_day=self.last_day - 2)
            if not cands:
                return
            inv = r.choice(cands)
            self.assigned.add(inv["key"])
            self.payments.append(self.new_payment(inv["entity"], inv["currency"], disp, inv["total"],
                                                  [(inv["key"], inv["total"])], display=False))
            return
        inv = self.new_invoice(r.choice(self.customers), disp, display=False)
        self.invoices.append(inv)
        if not end_side:  # счёт в начале периода успевают оплатить
            self.assigned.add(inv["key"])
            t = self.pay_time(inv["issued"], 2, 10)
            if t:
                self.payments.append(self.new_payment(inv["entity"], inv["currency"], t, inv["total"],
                                                      [(inv["key"], inv["total"])]))

    def d_unremitted_payment(self, i):
        """Платёж без расписки на сумму открытого счёта: связь угадывать нельзя."""
        r = self.rng
        cands = self.free_invoices(latest_day=self.last_day - 4)
        if not cands:
            return
        inv = r.choice(cands)
        t = self.pay_time(inv["issued"], 1, 5)
        if not t:
            return
        self.assigned.add(inv["key"])
        self.unremitted_inv.add(inv["key"])
        if inv["role"] is None:
            inv["role"] = "unremitted_payment"
        self.payments.append(self.new_payment(inv["entity"], inv["currency"], t, inv["total"], None))

    def d_same_day_repeat(self, i):
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

    def d_next_period_doc(self, i):
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
            return
        inv = self.new_invoice(r.choice(self.customers), after)
        inv["posted"] = False
        self.assigned.add(inv["key"])
        self.invoices.append(inv)
        self.locked_inv.add(inv["key"])

    def d_prior_period_doc(self, i):
        """Счёт прошлого периода: проведён в прошлых книгах, в этом периоде НЕ проводится."""
        inv = self.new_invoice(self.rng.choice(self.customers), self.prior_period_instant())
        inv["posted"] = False
        self.assigned.add(inv["key"])
        self.invoices.append(inv)
        self.locked_inv.add(inv["key"])

    # ------------------------------------------------------------ документы сценария partial_fx_boundary
    def build_boundary_pairs(self) -> None:
        """Валютный счёт и частичная оплата в последний локальный день периода в такое время,
        что дата по UTC отличается от даты компании. Сами документы корректны; ошибки на этом
        платеже вносит injection.py."""
        r = self.rng
        fents = [c for c in self.customers if c["currency"] != self.base]
        for _ in range(self.cfg["interactions"]["partial_fx_boundary"]):
            ent = r.choice(fents)
            inv = self.new_invoice(ent, self.local_dt(r.randint(1, max(1, self.last_day - 12))))
            floor_minor = Decimal(2000) / money.REF_USD_VALUE[inv["currency"]] * (Decimal(10) ** money.exponent(inv["currency"]))
            while inv["total"] < floor_minor:  # сумма должна быть существенной
                ln = inv["lines"][0]
                ln["quantity"] += 3
                ln["amount_minor"] = ln["quantity"] * ln["unit_price_minor"]
                inv["total"] = sum(l["amount_minor"] for l in inv["lines"])
            self.invoices.append(inv)
            self.assigned.add(inv["key"])
            off_h = int(self.offset[:3])
            y, m, last = self.p_start.year, self.p_start.month, self.last_day
            if off_h > 0:      # ранние часы: по UTC это ещё предыдущий день
                ts = datetime(y, m, last, r.randint(0, max(0, off_h - 1)), r.randint(5, 55), 0, tzinfo=self.tzc)
            elif off_h < 0:    # поздний вечер: по UTC уже следующий день
                ts = datetime(y, m, last, r.randint(24 + off_h, 23), r.randint(5, 55), 0, tzinfo=self.tzc)
            else:              # UTC+0: дата валютирования банка — следующий день
                ts = datetime(y, m, last, r.randint(16, 22), r.randint(5, 55), 0, tzinfo=self.tzc)
            amt = max(1, int(inv["total"] * Decimal(str(r.uniform(0.35, 0.75)))))
            p = self.new_payment(inv["entity"], inv["currency"], ts.astimezone(tz_from_offset("+00:00")), amt,
                                 [(inv["key"], amt)], display=False)
            self.payments.append(p)
            self.locked_inv.add(inv["key"])
            self.boundary_pairs.append((inv, p))
