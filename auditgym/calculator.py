"""Калькулятор-песочница: только десятичная арифметика (+ − * / // % скобки, унарные ±).

Никаких имён, вызовов, атрибутов, степеней, строк и файлов. Выражение разбирается через
``ast`` и считается на ``Decimal`` с точностью 34 значащих цифры; числовые литералы берутся
из исходного текста, поэтому 0.1 — это ровно Decimal('0.1').
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext

MAX_LEN = 300
MAX_NODES = 200
_ALLOWED = re.compile(r"^[0-9\s.+\-*/%()]*$")
_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)


class CalcError(ValueError):
    pass


def evaluate(expression) -> str:
    if not isinstance(expression, str):
        raise CalcError("выражение должно быть строкой")
    if len(expression) > MAX_LEN:
        raise CalcError("выражение слишком длинное")
    if not _ALLOWED.match(expression) or "**" in expression:
        raise CalcError("разрешены только цифры, десятичная точка, + - * / // % и скобки")
    literals = re.findall(r"\d+\.\d*|\.\d+|\d+", expression)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise CalcError("синтаксическая ошибка")
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise CalcError("выражение слишком сложное")
    consts = sorted((n for n in nodes if isinstance(n, ast.Constant)), key=lambda n: (n.lineno, n.col_offset))
    if len(consts) != len(literals):
        raise CalcError("синтаксическая ошибка")
    exact = {id(n): Decimal(t) for n, t in zip(consts, literals)}

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if id(node) not in exact:
                raise CalcError("недопустимый литерал")
            return exact[id(node)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            v = ev(node.operand)
            return v if isinstance(node.op, ast.UAdd) else -v
        if isinstance(node, ast.BinOp) and isinstance(node.op, _OPS):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if b == 0:
                raise CalcError("деление на ноль")
            if isinstance(node.op, ast.Div):
                return a / b
            if isinstance(node.op, ast.FloorDiv):
                return a // b
            return a % b
        raise CalcError("недопустимый элемент выражения")

    with localcontext() as ctx:
        ctx.prec = 34
        ctx.Emax = 10 ** 6
        ctx.Emin = -(10 ** 6)
        try:
            val = ev(tree)
        except (InvalidOperation, DivisionByZero, OverflowError):
            raise CalcError("арифметическая ошибка")
        if abs(val.adjusted()) > 60:
            raise CalcError("результат вне допустимого диапазона")
        s = format(val, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s
