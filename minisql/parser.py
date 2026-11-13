"""SQL text -> tokens -> abstract syntax tree (plain tuples and dicts)."""
from __future__ import annotations

import re


class SqlError(Exception):
    pass


KEYWORDS = {"SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "LIMIT", "OFFSET", "AS", "AND", "OR", "NOT",
            "NULL", "IS", "IN", "LIKE", "BETWEEN", "JOIN", "INNER", "LEFT", "ON", "ASC", "DESC", "DISTINCT", "CREATE",
            "TABLE", "INDEX", "INSERT", "INTO", "VALUES", "EXPLAIN", "TRUE", "FALSE"}
AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}
TOKEN = re.compile(r"""\s*(?:(?P<num>\d+\.\d+|\d+)|(?P<str>'(?:[^']|'')*')|(?P<id>[A-Za-z_][A-Za-z_0-9]*)|(?P<op><=|>=|<>|!=|[=<>+\-*/%(),.;]))""")


def tokenize(sql: str) -> list[tuple[str, object]]:
    tokens, pos = [], 0
    sql = sql.rstrip()
    while pos < len(sql):
        m = TOKEN.match(sql, pos)
        if not m:
            raise SqlError(f"unexpected character {sql[pos:].lstrip()[:1]!r} at position {pos}")
        pos = m.end()
        if m.group("num"):
            tokens.append(("num", float(m.group("num")) if "." in m.group("num") else int(m.group("num"))))
        elif m.group("str"):
            tokens.append(("str", m.group("str")[1:-1].replace("''", "'")))
        elif m.group("id"):
            word = m.group("id")
            tokens.append(("kw", word.upper()) if word.upper() in KEYWORDS else ("id", word.lower()))
        else:
            tokens.append(("op", m.group("op")))
    tokens.append(("end", None))
    return tokens


class Parser:
    def __init__(self, sql: str) -> None:
        self.tokens, self.i = tokenize(sql), 0

    # ---- token helpers ---------------------------------------------------
    def peek(self):
        return self.tokens[self.i]

    def take(self):
        self.i += 1
        return self.tokens[self.i - 1]

    def accept(self, kind: str, value=None) -> bool:
        k, v = self.peek()
        if k == kind and (value is None or v == value):
            self.i += 1
            return True
        return False

    def expect(self, kind: str, value=None):
        k, v = self.peek()
        if k != kind or (value is not None and v != value):
            raise SqlError(f"expected {value or kind}, found {v if v is not None else 'end of input'!r}")
        return self.take()[1]

    # ---- statements ------------------------------------------------------
    def statement(self) -> dict:
        if self.accept("kw", "EXPLAIN"):
            stmt = {"type": "explain", "select": self.select()}
        elif self.peek() == ("kw", "SELECT"):
            stmt = self.select()
        elif self.accept("kw", "CREATE"):
            if self.accept("kw", "INDEX"):
                name = self.expect("id")
                self.expect("kw", "ON")
                table = self.expect("id")
                self.expect("op", "(")
                column = self.expect("id")
                self.expect("op", ")")
                stmt = {"type": "create_index", "name": name, "table": table, "column": column}
            else:
                self.expect("kw", "TABLE")
                table = self.expect("id")
                self.expect("op", "(")
                columns = []
                while True:
                    columns.append(self.expect("id"))
                    if self.peek()[0] == "id":   # optional type name, accepted and ignored
                        self.take()
                    if not self.accept("op", ","):
                        break
                self.expect("op", ")")
                stmt = {"type": "create_table", "table": table, "columns": columns}
        elif self.accept("kw", "INSERT"):
            self.expect("kw", "INTO")
            table = self.expect("id")
            columns = None
            if self.accept("op", "("):
                columns = [self.expect("id")]
                while self.accept("op", ","):
                    columns.append(self.expect("id"))
                self.expect("op", ")")
            self.expect("kw", "VALUES")
            rows = []
            while True:
                self.expect("op", "(")
                row = [self.expr()]
                while self.accept("op", ","):
                    row.append(self.expr())
                self.expect("op", ")")
                rows.append(row)
                if not self.accept("op", ","):
                    break
            stmt = {"type": "insert", "table": table, "columns": columns, "rows": rows}
        else:
            raise SqlError(f"unsupported statement starting with {self.peek()[1]!r}")
        self.accept("op", ";")
        if self.peek()[0] != "end":
            raise SqlError(f"unexpected {self.peek()[1]!r} after the end of the statement")
        return stmt

    def select(self) -> dict:
        self.expect("kw", "SELECT")
        distinct = self.accept("kw", "DISTINCT")
        items = []
        while True:
            if self.accept("op", "*"):
                items.append(("star", None, None))
            else:
                e = self.expr()
                alias = self.expect("id") if self.accept("kw", "AS") else (self.take()[1] if self.peek()[0] == "id" else None)
                items.append(("expr", e, alias))
            if not self.accept("op", ","):
                break
        self.expect("kw", "FROM")
        tables = [self.table_ref()]
        joins = []
        while True:
            kind = "inner"
            if self.accept("kw", "LEFT"):
                kind = "left"
            elif self.accept("kw", "INNER"):
                pass
            elif self.peek() != ("kw", "JOIN"):
                break
            self.expect("kw", "JOIN")
            ref = self.table_ref()
            self.expect("kw", "ON")
            joins.append((kind, ref, self.expr()))
        where = self.expr() if self.accept("kw", "WHERE") else None
        group = []
        if self.accept("kw", "GROUP"):
            self.expect("kw", "BY")
            group = [self.expr()]
            while self.accept("op", ","):
                group.append(self.expr())
        having = self.expr() if self.accept("kw", "HAVING") else None
        order = []
        if self.accept("kw", "ORDER"):
            self.expect("kw", "BY")
            while True:
                e = self.expr()
                desc = self.accept("kw", "DESC") or (self.accept("kw", "ASC") and False)
                order.append((e, desc))
                if not self.accept("op", ","):
                    break
        limit = offset = None
        if self.accept("kw", "LIMIT"):
            limit = self.expect("num")
            if self.accept("kw", "OFFSET"):
                offset = self.expect("num")
        return {"type": "select", "distinct": distinct, "items": items, "from": tables[0], "joins": joins, "where": where,
                "group": group, "having": having, "order": order, "limit": limit, "offset": offset}

    def table_ref(self) -> tuple[str, str]:
        name = self.expect("id")
        alias = self.expect("id") if self.accept("kw", "AS") else (self.take()[1] if self.peek()[0] == "id" else name)
        return (name, alias)

    # ---- expressions, lowest precedence first ----------------------------
    def expr(self):
        left = self.and_expr()
        while self.accept("kw", "OR"):
            left = ("or", left, self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.accept("kw", "AND"):
            left = ("and", left, self.not_expr())
        return left

    def not_expr(self):
        if self.accept("kw", "NOT"):
            return ("not", self.not_expr())
        return self.comparison()

    def comparison(self):
        left = self.additive()
        k, v = self.peek()
        if k == "op" and v in ("=", "!=", "<>", "<", "<=", ">", ">="):
            self.take()
            return ("cmp", "!=" if v == "<>" else v, left, self.additive())
        if self.accept("kw", "IS"):
            negated = self.accept("kw", "NOT")
            self.expect("kw", "NULL")
            return ("isnull", left, negated)
        negated = self.accept("kw", "NOT")
        if self.accept("kw", "IN"):
            self.expect("op", "(")
            options = [self.expr()]
            while self.accept("op", ","):
                options.append(self.expr())
            self.expect("op", ")")
            node = ("in", left, options)
        elif self.accept("kw", "LIKE"):
            node = ("like", left, self.additive())
        elif self.accept("kw", "BETWEEN"):
            low = self.additive()
            self.expect("kw", "AND")
            node = ("and", ("cmp", ">=", left, low), ("cmp", "<=", left, self.additive()))
        elif negated:
            raise SqlError("expected IN, LIKE or BETWEEN after NOT")
        else:
            return left
        return ("not", node) if negated else node

    def additive(self):
        left = self.multiplicative()
        while self.peek() in (("op", "+"), ("op", "-")):
            left = ("arith", self.take()[1], left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.unary()
        while self.peek() in (("op", "*"), ("op", "/"), ("op", "%")):
            left = ("arith", self.take()[1], left, self.unary())
        return left

    def unary(self):
        if self.accept("op", "-"):
            return ("neg", self.unary())
        return self.primary()

    def primary(self):
        k, v = self.take()
        if k in ("num", "str"):
            return ("lit", v)
        if k == "kw" and v in ("NULL", "TRUE", "FALSE"):
            return ("lit", {"NULL": None, "TRUE": 1, "FALSE": 0}[v])
        if k == "op" and v == "(":
            e = self.expr()
            self.expect("op", ")")
            return e
        if k == "id":
            if self.accept("op", "("):
                fn = v.upper()
                if fn not in AGGREGATES:
                    raise SqlError(f"unknown function {v}")
                if self.accept("op", "*"):
                    if fn != "COUNT":
                        raise SqlError(f"{fn}(*) is not valid")
                    arg, distinct = None, False
                else:
                    distinct = self.accept("kw", "DISTINCT")
                    arg = self.expr()
                self.expect("op", ")")
                return ("agg", fn, arg, distinct)
            if self.accept("op", "."):
                return ("col", v, self.expect("id"))
            return ("col", None, v)
        raise SqlError(f"unexpected {v if v is not None else 'end of input'!r} in expression")


def parse(sql: str) -> dict:
    return Parser(sql).statement()
