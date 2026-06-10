from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
from typing import Any


class FormulaError(ValueError):
    """Raised when a screener formula cannot be parsed or evaluated."""


SUPPORTED_FIELDS = {
    "O": "open",
    "OPEN": "open",
    "H": "high",
    "HIGH": "high",
    "L": "low",
    "LOW": "low",
    "C": "close",
    "CLOSE": "close",
    "V": "volume",
    "VOL": "volume",
    "VOLUME": "volume",
}

SUPPORTED_FUNCTIONS = {
    "MA",
    "EMA",
    "REF",
    "CROSS",
    "RSI",
    "MACD",
    "HHV",
    "LLV",
    "COUNT",
    "EVERY",
    "ABS",
    "MAX",
    "MIN",
}


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


@dataclass(frozen=True)
class Node:
    kind: str
    value: Any = None
    children: tuple["Node", ...] = ()


@dataclass(frozen=True)
class FormulaProgram:
    assignments: tuple[tuple[str, Node], ...]
    expression: Node


_TOKEN_RE = re.compile(
    r"""
    (?P<number>\d+(?:\.\d+)?|\.\d+)
    |(?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    |(?P<op>>=|<=|!=|<>|:=|[+\-*/(),;:><=])
    |(?P<bad>.)
    """,
    re.VERBOSE,
)


def normalize_formula(text: str) -> str:
    raw = str(text or "")
    raw = raw.replace("，", ",").replace("；", ";").replace("（", "(").replace("）", ")")
    raw = raw.replace("&&", " AND ").replace("||", " OR ")
    lines: list[str] = []
    for line in raw.splitlines():
        # 通达信常见注释不进入解析。
        line = line.split("//", 1)[0]
        line = line.split("#", 1)[0]
        if line.strip():
            lines.append(line.strip())
    return ";".join(lines).strip()


def tokenize(text: str) -> list[Token]:
    source = normalize_formula(text)
    tokens: list[Token] = []
    pos = 0
    while pos < len(source):
        ch = source[pos]
        if ch.isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(source, pos)
        if not match:
            raise FormulaError(f"无法解析公式字符: {ch}")
        pos = match.end()
        kind = match.lastgroup or ""
        value = match.group(kind)
        if kind == "bad":
            raise FormulaError(f"不支持的公式字符: {value}")
        if kind == "ident":
            upper = value.upper()
            if upper in {"AND", "OR", "NOT"}:
                tokens.append(Token("op", upper))
            else:
                tokens.append(Token("ident", upper))
        elif kind == "number":
            tokens.append(Token("number", value))
        else:
            op = "<>" if value == "!=" else value
            tokens.append(Token("op", op))
    return tokens


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def parse_program(self) -> FormulaProgram:
        statements = self._split_statements()
        assignments: list[tuple[str, Node]] = []
        expression: Node | None = None
        for stmt in statements:
            if not stmt:
                continue
            stmt = self._strip_output_label(stmt)
            assign_at = self._find_assignment(stmt)
            parser = Parser(stmt)
            if assign_at == 1 and stmt[0].kind == "ident":
                name = stmt[0].value
                if name in SUPPORTED_FIELDS or name in SUPPORTED_FUNCTIONS:
                    raise FormulaError(f"不能覆盖内置名称: {name}")
                parser.pos = 2
                node = parser.parse_expression()
                parser._expect_end()
                assignments.append((name, node))
            else:
                expression = parser.parse_expression()
                parser._expect_end()
        if expression is None:
            raise FormulaError("公式缺少选股条件表达式")
        return FormulaProgram(tuple(assignments), expression)

    def _split_statements(self) -> list[list[Token]]:
        out: list[list[Token]] = []
        cur: list[Token] = []
        depth = 0
        for token in self.tokens:
            if token.value == "(":
                depth += 1
            elif token.value == ")":
                depth -= 1
            if token.value == ";" and depth == 0:
                out.append(cur)
                cur = []
            else:
                cur.append(token)
        out.append(cur)
        return out

    def _strip_output_label(self, stmt: list[Token]) -> list[Token]:
        # TDX allows "XG: condition;". Keep ":=" assignments intact.
        if len(stmt) >= 3 and stmt[0].kind == "ident" and stmt[1].value == ":":
            return stmt[2:]
        return stmt

    def _find_assignment(self, stmt: list[Token]) -> int:
        depth = 0
        for i, token in enumerate(stmt):
            if token.value == "(":
                depth += 1
            elif token.value == ")":
                depth -= 1
            elif token.value == ":=" and depth == 0:
                return i
        return -1

    def parse_expression(self) -> Node:
        return self.parse_or()

    def parse_or(self) -> Node:
        node = self.parse_and()
        while self._accept("OR"):
            node = Node("binary", "OR", (node, self.parse_and()))
        return node

    def parse_and(self) -> Node:
        node = self.parse_not()
        while self._accept("AND"):
            node = Node("binary", "AND", (node, self.parse_not()))
        return node

    def parse_not(self) -> Node:
        if self._accept("NOT"):
            return Node("unary", "NOT", (self.parse_not(),))
        return self.parse_compare()

    def parse_compare(self) -> Node:
        node = self.parse_add()
        while self._peek_value() in {">", ">=", "<", "<=", "=", "<>"}:
            op = self._consume().value
            node = Node("binary", op, (node, self.parse_add()))
        return node

    def parse_add(self) -> Node:
        node = self.parse_mul()
        while self._peek_value() in {"+", "-"}:
            op = self._consume().value
            node = Node("binary", op, (node, self.parse_mul()))
        return node

    def parse_mul(self) -> Node:
        node = self.parse_unary()
        while self._peek_value() in {"*", "/"}:
            op = self._consume().value
            node = Node("binary", op, (node, self.parse_unary()))
        return node

    def parse_unary(self) -> Node:
        if self._accept("+"):
            return self.parse_unary()
        if self._accept("-"):
            return Node("unary", "-", (self.parse_unary(),))
        return self.parse_primary()

    def parse_primary(self) -> Node:
        token = self._consume()
        if token.kind == "number":
            return Node("number", float(token.value))
        if token.kind == "ident":
            name = token.value
            if self._accept("("):
                args: list[Node] = []
                if not self._accept(")"):
                    while True:
                        args.append(self.parse_expression())
                        if self._accept(")"):
                            break
                        self._expect(",")
                if name not in SUPPORTED_FUNCTIONS:
                    raise FormulaError(f"不支持的函数: {name}")
                return Node("call", name, tuple(args))
            return Node("ident", name)
        if token.value == "(":
            node = self.parse_expression()
            self._expect(")")
            return node
        raise FormulaError(f"意外的公式片段: {token.value}")

    def _accept(self, value: str) -> bool:
        if self._peek_value() == value:
            self.pos += 1
            return True
        return False

    def _expect(self, value: str) -> None:
        if not self._accept(value):
            got = self._peek_value() or "结尾"
            raise FormulaError(f"期望 {value}，实际是 {got}")

    def _expect_end(self) -> None:
        if self.pos != len(self.tokens):
            raise FormulaError(f"公式尾部存在无法解析的内容: {self.tokens[self.pos].value}")

    def _consume(self) -> Token:
        if self.pos >= len(self.tokens):
            raise FormulaError("公式意外结束")
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _peek_value(self) -> str:
        if self.pos >= len(self.tokens):
            return ""
        return self.tokens[self.pos].value


def parse_formula(text: str) -> FormulaProgram:
    return Parser(tokenize(text)).parse_program()


def _nan() -> float:
    return float("nan")


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and isfinite(float(value))


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if _is_num(value):
        return float(value) != 0.0
    return False


def _series(value: Any, length: int) -> list[Any]:
    if isinstance(value, list):
        if len(value) == length:
            return value
        if len(value) > length:
            return value[-length:]
        return [None] * (length - len(value)) + value
    return [value for _ in range(length)]


def _num_series(value: Any, length: int) -> list[float | None]:
    out: list[float | None] = []
    for item in _series(value, length):
        try:
            if item is None or isinstance(item, bool):
                out.append(None)
            else:
                num = float(item)
                out.append(num if isfinite(num) else None)
        except Exception:
            out.append(None)
    return out


def _bool_series(value: Any, length: int) -> list[bool]:
    return [_to_bool(x) for x in _series(value, length)]


def _rolling(values: list[float | None], period: int, op: str) -> list[float | None]:
    p = max(1, int(period))
    out: list[float | None] = []
    for i in range(len(values)):
        window = [x for x in values[max(0, i - p + 1): i + 1] if x is not None]
        if len(window) < p:
            out.append(None)
        elif op == "ma":
            out.append(sum(window) / p)
        elif op == "hhv":
            out.append(max(window))
        else:
            out.append(min(window))
    return out


def _ema(values: list[float | None], period: int) -> list[float | None]:
    p = max(1, int(period))
    alpha = 2.0 / (p + 1.0)
    out: list[float | None] = []
    prev: float | None = None
    for v in values:
        if v is None:
            out.append(prev)
            continue
        prev = v if prev is None else (v * alpha + prev * (1 - alpha))
        out.append(prev)
    return out


def _ref(values: list[Any], period: int) -> list[Any]:
    p = max(0, int(period))
    if p == 0:
        return values[:]
    return [None] * p + values[:-p]


def _rsi(values: list[float | None], period: int) -> list[float | None]:
    p = max(1, int(period))
    out: list[float | None] = [None] * len(values)
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        if values[i] is None or values[i - 1] is None:
            gains.append(0.0)
            losses.append(0.0)
        else:
            change = float(values[i]) - float(values[i - 1])
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        if i >= p:
            gain = sum(gains[i - p:i]) / p
            loss = sum(losses[i - p:i]) / p
            out[i] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def _macd(values: list[float | None], fast: int, slow: int, signal: int) -> tuple[list[float | None], list[float | None], list[float | None]]:
    ema_fast = _ema(values, fast)
    ema_slow = _ema(values, slow)
    dif = [
        None if a is None or b is None else a - b
        for a, b in zip(ema_fast, ema_slow)
    ]
    dea = _ema(dif, signal)
    hist = [
        None if a is None or b is None else (a - b) * 2
        for a, b in zip(dif, dea)
    ]
    return dif, dea, hist


class FormulaEvaluator:
    def __init__(self, klines: list[Any]):
        self.klines = sorted(klines, key=lambda k: getattr(k, "date", ""))
        self.length = len(self.klines)
        self.env: dict[str, Any] = {
            "OPEN": [float(getattr(k, "open", 0) or 0) for k in self.klines],
            "HIGH": [float(getattr(k, "high", 0) or 0) for k in self.klines],
            "LOW": [float(getattr(k, "low", 0) or 0) for k in self.klines],
            "CLOSE": [float(getattr(k, "close", 0) or 0) for k in self.klines],
            "VOL": [float(getattr(k, "volume", 0) or 0) for k in self.klines],
        }
        self.env["O"] = self.env["OPEN"]
        self.env["H"] = self.env["HIGH"]
        self.env["L"] = self.env["LOW"]
        self.env["C"] = self.env["CLOSE"]
        self.env["V"] = self.env["VOL"]
        self.env["VOLUME"] = self.env["VOL"]

    def run(self, program: FormulaProgram) -> dict[str, Any]:
        if self.length < 2:
            raise FormulaError("K线数据不足")
        for name, node in program.assignments:
            self.env[name] = self.eval(node)
        result = self.eval(program.expression)
        series = _bool_series(result, self.length)
        matched = bool(series[-1]) if series else False
        return {
            "matched": matched,
            "latest": series[-1] if series else False,
            "series": series,
            "indicators": self.snapshot(),
        }

    def snapshot(self) -> dict[str, Any]:
        closes = _num_series(self.env["C"], self.length)
        volumes = _num_series(self.env["V"], self.length)
        ma5 = _rolling(closes, 5, "ma")
        ma10 = _rolling(closes, 10, "ma")
        ma20 = _rolling(closes, 20, "ma")
        rsi6 = _rsi(closes, 6)
        dif, dea, hist = _macd(closes, 12, 26, 9)
        vol_ma5 = _rolling(volumes, 5, "ma")

        def last(values: list[Any]) -> Any:
            return values[-1] if values else None

        vol_ratio = None
        if last(vol_ma5) not in (None, 0) and last(volumes) is not None:
            vol_ratio = float(last(volumes)) / float(last(vol_ma5))
        return {
            "asof": getattr(self.klines[-1], "date", ""),
            "close": last(closes),
            "ma5": last(ma5),
            "ma10": last(ma10),
            "ma20": last(ma20),
            "rsi6": last(rsi6),
            "macd_dif": last(dif),
            "macd_dea": last(dea),
            "macd_hist": last(hist),
            "volume_ratio": vol_ratio,
        }

    def eval(self, node: Node) -> Any:
        if node.kind == "number":
            return node.value
        if node.kind == "ident":
            name = str(node.value).upper()
            if name not in self.env:
                raise FormulaError(f"未知字段或变量: {name}")
            return self.env[name]
        if node.kind == "unary":
            value = self.eval(node.children[0])
            if node.value == "-":
                return [None if v is None else -float(v) for v in _num_series(value, self.length)]
            if node.value == "NOT":
                return [not v for v in _bool_series(value, self.length)]
        if node.kind == "binary":
            return self._binary(str(node.value), self.eval(node.children[0]), self.eval(node.children[1]))
        if node.kind == "call":
            return self._call(str(node.value).upper(), [self.eval(x) for x in node.children])
        raise FormulaError("无法执行公式节点")

    def _binary(self, op: str, left: Any, right: Any) -> list[Any]:
        if op in {"AND", "OR"}:
            a = _bool_series(left, self.length)
            b = _bool_series(right, self.length)
            return [(x and y) if op == "AND" else (x or y) for x, y in zip(a, b)]
        if op in {">", ">=", "<", "<=", "=", "<>"}:
            a = _series(left, self.length)
            b = _series(right, self.length)
            out: list[bool] = []
            for x, y in zip(a, b):
                if x is None or y is None:
                    out.append(False)
                    continue
                try:
                    xf = float(x)
                    yf = float(y)
                    if op == ">":
                        out.append(xf > yf)
                    elif op == ">=":
                        out.append(xf >= yf)
                    elif op == "<":
                        out.append(xf < yf)
                    elif op == "<=":
                        out.append(xf <= yf)
                    elif op == "=":
                        out.append(xf == yf)
                    else:
                        out.append(xf != yf)
                except Exception:
                    out.append(False)
            return out
        a = _num_series(left, self.length)
        b = _num_series(right, self.length)
        out: list[float | None] = []
        for x, y in zip(a, b):
            if x is None or y is None:
                out.append(None)
            elif op == "+":
                out.append(x + y)
            elif op == "-":
                out.append(x - y)
            elif op == "*":
                out.append(x * y)
            elif op == "/":
                out.append(None if y == 0 else x / y)
            else:
                raise FormulaError(f"不支持的运算符: {op}")
        return out

    def _call(self, name: str, args: list[Any]) -> Any:
        if name in {"MA", "EMA", "REF", "HHV", "LLV"}:
            if len(args) != 2:
                raise FormulaError(f"{name} 需要 2 个参数")
            period = int(float(_series(args[1], self.length)[-1]))
            vals = _num_series(args[0], self.length)
            if name == "MA":
                return _rolling(vals, period, "ma")
            if name == "EMA":
                return _ema(vals, period)
            if name == "REF":
                return _ref(_series(args[0], self.length), period)
            if name == "HHV":
                return _rolling(vals, period, "hhv")
            return _rolling(vals, period, "llv")
        if name == "RSI":
            if len(args) == 1:
                vals = _num_series(self.env["C"], self.length)
                period = int(float(_series(args[0], self.length)[-1]))
            elif len(args) == 2:
                vals = _num_series(args[0], self.length)
                period = int(float(_series(args[1], self.length)[-1]))
            else:
                raise FormulaError("RSI 需要 1 或 2 个参数")
            return _rsi(vals, period)
        if name == "MACD":
            if len(args) == 0:
                vals, fast, slow, signal = _num_series(self.env["C"], self.length), 12, 26, 9
            elif len(args) == 1:
                vals, fast, slow, signal = _num_series(args[0], self.length), 12, 26, 9
            elif len(args) == 4:
                vals = _num_series(args[0], self.length)
                fast = int(float(_series(args[1], self.length)[-1]))
                slow = int(float(_series(args[2], self.length)[-1]))
                signal = int(float(_series(args[3], self.length)[-1]))
            else:
                raise FormulaError("MACD 需要 0、1 或 4 个参数")
            return _macd(vals, fast, slow, signal)[2]
        if name == "CROSS":
            if len(args) != 2:
                raise FormulaError("CROSS 需要 2 个参数")
            a = _num_series(args[0], self.length)
            b = _num_series(args[1], self.length)
            out = [False] * self.length
            for i in range(1, self.length):
                if a[i] is None or b[i] is None or a[i - 1] is None or b[i - 1] is None:
                    continue
                out[i] = a[i - 1] <= b[i - 1] and a[i] > b[i]
            return out
        if name in {"COUNT", "EVERY"}:
            if len(args) != 2:
                raise FormulaError(f"{name} 需要 2 个参数")
            cond = _bool_series(args[0], self.length)
            period = max(1, int(float(_series(args[1], self.length)[-1])))
            out: list[Any] = []
            for i in range(self.length):
                window = cond[max(0, i - period + 1): i + 1]
                if len(window) < period:
                    out.append(False if name == "EVERY" else 0)
                elif name == "EVERY":
                    out.append(all(window))
                else:
                    out.append(sum(1 for x in window if x))
            return out
        if name == "ABS":
            if len(args) != 1:
                raise FormulaError("ABS 需要 1 个参数")
            return [None if x is None else abs(x) for x in _num_series(args[0], self.length)]
        if name in {"MAX", "MIN"}:
            if len(args) != 2:
                raise FormulaError(f"{name} 需要 2 个参数")
            a = _num_series(args[0], self.length)
            b = _num_series(args[1], self.length)
            out: list[float | None] = []
            for x, y in zip(a, b):
                if x is None:
                    out.append(y)
                elif y is None:
                    out.append(x)
                else:
                    out.append(max(x, y) if name == "MAX" else min(x, y))
            return out
        raise FormulaError(f"不支持的函数: {name}")


def evaluate_formula(text: str, klines: list[Any]) -> dict[str, Any]:
    return FormulaEvaluator(klines).run(parse_formula(text))


def function_catalog() -> dict[str, Any]:
    return {
        "fields": [
            {"name": "C / CLOSE", "description": "收盘价"},
            {"name": "O / OPEN", "description": "开盘价"},
            {"name": "H / HIGH", "description": "最高价"},
            {"name": "L / LOW", "description": "最低价"},
            {"name": "V / VOL", "description": "成交量"},
        ],
        "functions": [
            {"name": "MA(X,N)", "description": "N日简单均线"},
            {"name": "EMA(X,N)", "description": "N日指数均线"},
            {"name": "REF(X,N)", "description": "N日前的值"},
            {"name": "CROSS(A,B)", "description": "A 上穿 B"},
            {"name": "RSI(C,N)", "description": "相对强弱指标"},
            {"name": "MACD(C,12,26,9)", "description": "MACD柱体，参数可省略"},
            {"name": "HHV(X,N) / LLV(X,N)", "description": "N日最高/最低"},
            {"name": "COUNT(COND,N)", "description": "N日内条件成立次数"},
            {"name": "EVERY(COND,N)", "description": "N日内条件每天成立"},
            {"name": "ABS / MAX / MIN", "description": "基础数值函数"},
        ],
        "examples": [
            {
                "name": "均线金叉且 RSI 未过热",
                "formula": "CROSS(MA(C,5), MA(C,20)) AND RSI(C,6) < 70",
            },
            {
                "name": "放量突破20日新高",
                "formula": "C > REF(HHV(H,20),1) AND V > MA(V,5) * 1.5",
            },
            {
                "name": "回踩20日线后转强",
                "formula": "C > MA(C,20) AND REF(C,1) < REF(MA(C,20),1) AND C > O",
            },
        ],
    }
