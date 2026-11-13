"""Planner and executor.

A query is turned into a tree of operators. Each operator is an iterator that
pulls rows from its children (the "Volcano" model), so rows flow one at a time
and LIMIT can stop early without computing everything below it.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterator

from .parser import SqlError, parse

Row = tuple
Schema = list[tuple[str, str]]   # (table alias, column name) for each position in a row


# ---- three-valued logic and scalar helpers ---------------------------------
def _and(a, b):
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def _or(a, b):
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


def _truth(v):
    return None if v is None else bool(v)


def _compare(op: str, a, b):
    if a is None or b is None:
        return None      # any comparison with NULL is unknown
    try:
        return {"=": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
    except TypeError:
        raise SqlError(f"cannot compare {type(a).__name__} with {type(b).__name__}") from None


def _arith(op: str, a, b):
    if a is None or b is None:
        return None
    if op in ("/", "%"):
        if b == 0:
            return None  # SQLite's choice; many engines raise instead
        if isinstance(a, int) and isinstance(b, int):
            q = abs(a) // abs(b) * (1 if (a >= 0) == (b >= 0) else -1)   # truncate toward zero
            return q if op == "/" else a - q * b
        return a / b if op == "/" else a - b * int(a / b)
    return {"+": a + b, "-": a - b, "*": a * b}[op]


def _like(value, pattern):
    if value is None or pattern is None:
        return None
    regex = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in str(pattern))
    return re.fullmatch(regex, str(value), flags=re.I | re.S) is not None


def _sort_key(v):
    return (0,) if v is None else (1, v)   # NULLs sort first, as in SQLite


# ---- expressions ------------------------------------------------------------
def resolve(schema: Schema, table: str | None, name: str) -> int:
    matches = [i for i, (t, c) in enumerate(schema) if c == name and (table is None or t == table)]
    if not matches:
        raise SqlError(f"no such column: {table + '.' if table else ''}{name}")
    if len(matches) > 1:
        raise SqlError(f"ambiguous column name: {name}")
    return matches[0]


def compile_expr(expr, schema: Schema, slots: dict | None = None) -> Callable[[Row], Any]:
    """Turn an AST node into a function of a row. Column positions are resolved once, here.

    ``slots`` maps expressions already computed by an Aggregate (group keys and
    aggregate calls) to their position in its output rows.
    """
    if slots is not None and expr in slots:
        i = slots[expr]
        return lambda row: row[i]
    kind = expr[0]
    if kind == "lit":
        v = expr[1]
        return lambda row: v
    if kind == "col":
        if slots is not None:
            raise SqlError(f"column {expr[2]} must appear in GROUP BY or inside an aggregate")
        i = resolve(schema, expr[1], expr[2])
        return lambda row: row[i]
    if kind == "agg":
        raise SqlError("aggregate functions are not allowed here")
    c = lambda e: compile_expr(e, schema, slots)
    if kind == "and":
        l, r = c(expr[1]), c(expr[2])
        return lambda row: _and(_truth(l(row)), _truth(r(row)))
    if kind == "or":
        l, r = c(expr[1]), c(expr[2])
        return lambda row: _or(_truth(l(row)), _truth(r(row)))
    if kind == "not":
        f = c(expr[1])
        return lambda row: (lambda v: None if v is None else not v)(_truth(f(row)))
    if kind == "cmp":
        op, l, r = expr[1], c(expr[2]), c(expr[3])
        return lambda row: _compare(op, l(row), r(row))
    if kind == "arith":
        op, l, r = expr[1], c(expr[2]), c(expr[3])
        return lambda row: _arith(op, l(row), r(row))
    if kind == "neg":
        f = c(expr[1])
        return lambda row: (lambda v: None if v is None else -v)(f(row))
    if kind == "isnull":
        f, negated = c(expr[1]), expr[2]
        return lambda row: (f(row) is None) != negated
    if kind == "like":
        l, r = c(expr[1]), c(expr[2])
        return lambda row: _like(l(row), r(row))
    if kind == "in":
        f, options = c(expr[1]), [c(o) for o in expr[2]]

        def evaluate(row):
            v = f(row)
            values = [o(row) for o in options]
            if v is None:
                return None
            if v in [x for x in values if x is not None]:
                return True
            return None if any(x is None for x in values) else False
        return evaluate
    raise SqlError(f"unsupported expression {kind}")


def walk(expr):
    """Yield every node in an expression tree."""
    if isinstance(expr, tuple):
        yield expr
        for part in expr[1:]:
            if isinstance(part, tuple):
                yield from walk(part)
            elif isinstance(part, list):
                for item in part:
                    yield from walk(item)


def conjuncts(expr) -> list:
    """Split `a AND b AND c` into [a, b, c] so each part can be placed independently."""
    if expr is None:
        return []
    return conjuncts(expr[1]) + conjuncts(expr[2]) if expr[0] == "and" else [expr]


def aliases_used(expr, schema: Schema) -> set[str]:
    return {schema[resolve(schema, n[1], n[2])][0] for n in walk(expr) if n[0] == "col"}


# ---- operators --------------------------------------------------------------
class Op:
    schema: Schema
    children: tuple = ()

    def rows(self) -> Iterator[Row]:
        raise NotImplementedError

    def describe(self) -> str:
        return type(self).__name__

    def explain(self, depth: int = 0) -> list[str]:
        lines = ["  " * depth + self.describe()]
        for child in self.children:
            lines.extend(child.explain(depth + 1))
        return lines


class Scan(Op):
    def __init__(self, table, alias, stats):
        self.table, self.alias, self.stats = table, alias, stats
        self.schema = [(alias, c) for c in table.columns]

    def rows(self):
        for row in self.table.rows:
            self.stats["rows_scanned"] += 1
            yield row

    def describe(self):
        return f"Scan {self.table.name}" + (f" AS {self.alias}" if self.alias != self.table.name else "")


class IndexLookup(Op):
    def __init__(self, table, alias, column, value, stats):
        self.table, self.alias, self.column, self.value, self.stats = table, alias, column, value, stats
        self.schema = [(alias, c) for c in table.columns]

    def rows(self):
        for position in self.table.indexes[self.column].get(self.value, ()):
            self.stats["rows_scanned"] += 1
            yield self.table.rows[position]

    def describe(self):
        return f"IndexLookup {self.table.name}.{self.column} = {self.value!r}"


class Filter(Op):
    def __init__(self, child, predicate, text):
        self.children, self.schema, self.predicate, self.text = (child,), child.schema, predicate, text

    def rows(self):
        return (row for row in self.children[0].rows() if self.predicate(row) is True)

    def describe(self):
        return f"Filter {self.text}"


class HashJoin(Op):
    """Build a hash table on the right input, then probe it with each left row: O(n + m)."""

    def __init__(self, left, right, left_keys, right_keys, residual, kind, text):
        self.children, self.schema = (left, right), left.schema + right.schema
        self.left_keys, self.right_keys, self.residual, self.kind, self.text = left_keys, right_keys, residual, kind, text

    def rows(self):
        left, right = self.children
        table: dict[tuple, list[Row]] = {}
        for row in right.rows():
            key = tuple(k(row) for k in self.right_keys)
            if None not in key:              # NULL never equals anything, so it can never match
                table.setdefault(key, []).append(row)
        padding = (None,) * len(right.schema)
        for row in left.rows():
            matched = False
            for other in table.get(tuple(k(row) for k in self.left_keys), ()):
                joined = row + other
                if self.residual is None or self.residual(joined) is True:
                    matched = True
                    yield joined
            if not matched and self.kind == "left":
                yield row + padding

    def describe(self):
        return f"HashJoin ({self.kind}) on {self.text}"


class NestedLoopJoin(Op):
    """Fallback for joins with no equality to hash on: O(n * m)."""

    def __init__(self, left, right, predicate, kind, text):
        self.children, self.schema = (left, right), left.schema + right.schema
        self.predicate, self.kind, self.text = predicate, kind, text

    def rows(self):
        left, right = self.children
        inner = list(right.rows())
        padding = (None,) * len(right.schema)
        for row in left.rows():
            matched = False
            for other in inner:
                joined = row + other
                if self.predicate(joined) is True:
                    matched = True
                    yield joined
            if not matched and self.kind == "left":
                yield row + padding

    def describe(self):
        return f"NestedLoopJoin ({self.kind}) on {self.text}"


class Aggregate(Op):
    def __init__(self, child, group_fns, aggs, text):
        self.children, self.group_fns, self.aggs, self.text = (child,), group_fns, aggs, text
        self.schema = [("", f"#{i}") for i in range(len(group_fns) + len(aggs))]

    def rows(self):
        groups: dict[tuple, list] = {}
        for row in self.children[0].rows():
            key = tuple(f(row) for f in self.group_fns)
            state = groups.get(key)
            if state is None:
                state = groups[key] = [[] for _ in self.aggs]
            for acc, (fn, arg, distinct) in zip(state, self.aggs):
                acc.append(1 if arg is None else arg(row))
        if not groups and not self.group_fns:
            groups[()] = [[] for _ in self.aggs]      # an aggregate over zero rows still returns one row
        for key, state in groups.items():
            yield key + tuple(self._finish(fn, arg, distinct, acc) for acc, (fn, arg, distinct) in zip(state, self.aggs))

    @staticmethod
    def _finish(fn, arg, distinct, values):
        if arg is not None:
            values = [v for v in values if v is not None]    # aggregates skip NULLs
            if distinct:
                values = list(dict.fromkeys(values))
        if fn == "COUNT":
            return len(values)
        if not values:
            return None
        return {"SUM": sum, "MIN": min, "MAX": max, "AVG": lambda v: sum(v) / len(v)}[fn](values)

    def describe(self):
        return f"Aggregate {self.text}"


class Project(Op):
    def __init__(self, child, fns, names, order_fns, order_desc, distinct, limit, offset):
        self.children, self.fns, self.schema = (child,), fns, [("", n) for n in names]
        self.order_fns, self.order_desc, self.distinct, self.limit, self.offset = order_fns, order_desc, distinct, limit, offset

    def rows(self):
        source = self.children[0].rows()
        if not self.order_fns and not self.distinct:
            # Streaming path: with no sort, LIMIT stops pulling rows from below as soon as it has enough.
            start, stop = self.offset or 0, None if self.limit is None else (self.offset or 0) + self.limit
            for i, row in enumerate(source):
                if stop is not None and i >= stop:
                    return
                if i >= start:
                    yield tuple(f(row) for f in self.fns)
            return
        out = [(tuple(f(row) for f in self.fns), [f(row) for f in self.order_fns]) for row in source]
        if self.distinct:
            seen, unique = set(), []
            for projected, keys in out:
                if projected not in seen:
                    seen.add(projected)
                    unique.append((projected, keys))
            out = unique
        for i in reversed(range(len(self.order_fns))):       # stable sort, last key first
            out.sort(key=lambda item: _sort_key(item[1][i]), reverse=self.order_desc[i])
        rows = [projected for projected, _ in out]
        start = self.offset or 0
        yield from rows[start: None if self.limit is None else start + self.limit]

    def describe(self):
        parts = ["Project " + ", ".join(n for _, n in self.schema)]
        if self.distinct:
            parts.append("distinct")
        if self.order_fns:
            parts.append(f"sort by {len(self.order_fns)} key(s)")
        if self.limit is not None:
            parts.append(f"limit {self.limit}" + (f" offset {self.offset}" if self.offset else ""))
        return ", ".join(parts)


# ---- storage and planning -----------------------------------------------------
class Table:
    def __init__(self, name, columns):
        self.name, self.columns, self.rows, self.indexes = name, columns, [], {}

    def insert(self, row: Row):
        self.rows.append(row)
        for column, index in self.indexes.items():
            index.setdefault(row[self.columns.index(column)], []).append(len(self.rows) - 1)


def show(expr) -> str:
    """Render an expression back to SQL-ish text for EXPLAIN."""
    k = expr[0]
    if k == "lit":
        return "NULL" if expr[1] is None else repr(expr[1])
    if k == "col":
        return (expr[1] + "." if expr[1] else "") + expr[2]
    if k in ("and", "or"):
        return f"({show(expr[1])} {k.upper()} {show(expr[2])})"
    if k in ("cmp", "arith"):
        return f"{show(expr[2])} {expr[1]} {show(expr[3])}"
    if k == "not":
        return f"NOT {show(expr[1])}"
    if k == "neg":
        return f"-{show(expr[1])}"
    if k == "isnull":
        return f"{show(expr[1])} IS {'NOT ' if expr[2] else ''}NULL"
    if k == "like":
        return f"{show(expr[1])} LIKE {show(expr[2])}"
    if k == "in":
        return f"{show(expr[1])} IN ({', '.join(show(o) for o in expr[2])})"
    if k == "agg":
        return f"{expr[1]}({'*' if expr[2] is None else ('DISTINCT ' if expr[3] else '') + show(expr[2])})"
    return str(expr)


class Database:
    def __init__(self) -> None:
        self.tables: dict[str, Table] = {}
        self.stats = {"rows_scanned": 0}

    def execute(self, sql: str) -> list[Row]:
        stmt = parse(sql)
        kind = stmt["type"]
        if kind == "create_table":
            if stmt["table"] in self.tables:
                raise SqlError(f"table {stmt['table']} already exists")
            if len(set(stmt["columns"])) != len(stmt["columns"]):
                raise SqlError("duplicate column name")
            self.tables[stmt["table"]] = Table(stmt["table"], stmt["columns"])
            return []
        if kind == "create_index":
            table = self._table(stmt["table"])
            if stmt["column"] not in table.columns:
                raise SqlError(f"no such column: {stmt['column']}")
            position = table.columns.index(stmt["column"])
            index: dict = {}
            for i, row in enumerate(table.rows):
                index.setdefault(row[position], []).append(i)
            table.indexes[stmt["column"]] = index
            return []
        if kind == "insert":
            table = self._table(stmt["table"])
            columns = stmt["columns"] or table.columns
            for row in stmt["rows"]:
                if len(row) != len(columns):
                    raise SqlError(f"expected {len(columns)} values, got {len(row)}")
                values = dict(zip(columns, (compile_expr(e, [])(()) for e in row)))
                unknown = set(values) - set(table.columns)
                if unknown:
                    raise SqlError(f"no such column: {sorted(unknown)[0]}")
                table.insert(tuple(values.get(c) for c in table.columns))
            return []
        if kind == "explain":
            return [(line,) for line in self.plan(stmt["select"]).explain()]
        self.stats["rows_scanned"] = 0
        return list(self.plan(stmt).rows())

    def columns(self, sql: str) -> list[str]:
        return [name for _, name in self.plan(parse(sql)).schema]

    def _table(self, name: str) -> Table:
        if name not in self.tables:
            raise SqlError(f"no such table: {name}")
        return self.tables[name]

    def plan(self, q: dict) -> Op:
        sources = [(q["from"], "inner", None)] + [(ref, kind, on) for kind, ref, on in q["joins"]]
        aliases = [alias for (_, alias), _, _ in sources]
        if len(set(aliases)) != len(aliases):
            raise SqlError("each table in FROM needs a distinct alias")
        full_schema = [(alias, c) for (name, alias), _, _ in sources for c in self._table(name).columns]
        nullable = {alias for (_, alias), kind, _ in sources if kind == "left"}   # right side of a LEFT JOIN

        # Predicate pushdown: a WHERE condition that mentions one table is applied at
        # that table's scan, before any join multiplies the rows. It must NOT be pushed
        # below the nullable side of a LEFT JOIN, where it would change the result.
        pushed: dict[str, list] = {a: [] for a in aliases}
        remaining = []
        for cond in conjuncts(q["where"]):
            used = aliases_used(cond, full_schema)
            if len(used) == 1 and not (used & nullable):
                pushed[next(iter(used))].append(cond)
            else:
                remaining.append(cond)

        def access(name: str, alias: str) -> Op:
            table = self._table(name)
            conds = pushed[alias]
            op: Op | None = None
            for cond in conds:   # an equality against a constant on an indexed column beats a full scan
                if cond[0] == "cmp" and cond[1] == "=":
                    for col, lit in ((cond[2], cond[3]), (cond[3], cond[2])):
                        if col[0] == "col" and lit[0] == "lit" and col[2] in table.indexes and lit[1] is not None:
                            op = IndexLookup(table, alias, col[2], lit[1], self.stats)
                            conds = [c for c in conds if c is not cond]
                            break
                if op:
                    break
            op = op or Scan(table, alias, self.stats)
            for cond in conds:
                op = Filter(op, compile_expr(cond, op.schema), show(cond))
            return op

        plan = access(*sources[0][0])
        for (name, alias), kind, on in sources[1:]:
            right = access(name, alias)
            joined = plan.schema + right.schema
            left_keys, right_keys, residual, texts = [], [], [], []
            for cond in conjuncts(on):
                if cond[0] == "cmp" and cond[1] == "=":
                    a, b = aliases_used(cond[2], joined), aliases_used(cond[3], joined)
                    left_aliases = {t for t, _ in plan.schema}
                    if a and b and a <= left_aliases and b == {alias}:
                        left_keys.append(compile_expr(cond[2], plan.schema)); right_keys.append(compile_expr(cond[3], right.schema)); texts.append(show(cond)); continue
                    if a and b and b <= left_aliases and a == {alias}:
                        left_keys.append(compile_expr(cond[3], plan.schema)); right_keys.append(compile_expr(cond[2], right.schema)); texts.append(show(cond)); continue
                residual.append(cond)
            if left_keys:
                extra = None
                if residual:
                    fns = [compile_expr(c, joined) for c in residual]
                    extra = lambda row, fns=fns: True if all(f(row) is True for f in fns) else False
                plan = HashJoin(plan, right, left_keys, right_keys, extra, kind, " AND ".join(texts + [show(c) for c in residual]))
            else:
                plan = NestedLoopJoin(plan, right, compile_expr(on, joined), kind, show(on))
        for cond in remaining:
            plan = Filter(plan, compile_expr(cond, plan.schema), show(cond))

        # SELECT list, with * expanded.
        items = []
        for kind, expr, alias in q["items"]:
            if kind == "star":
                items.extend((("col", t, c), c) for t, c in plan.schema)
            else:
                items.append((expr, alias or (expr[2] if expr[0] == "col" else show(expr))))
        by_alias = {name: expr for expr, name in items}
        order = [(by_alias.get(e[2], e) if e[0] == "col" and e[1] is None else e, desc) for e, desc in q["order"]]

        slots = None
        outputs = [e for e, _ in items] + [e for e, _ in order] + ([q["having"]] if q["having"] else [])
        agg_nodes = list(dict.fromkeys(n for e in outputs for n in walk(e) if n[0] == "agg"))
        if agg_nodes or q["group"]:
            for node in agg_nodes:
                if node[2] is not None and any(n[0] == "agg" for n in walk(node[2])):
                    raise SqlError("aggregate functions cannot be nested")
            group_fns = [compile_expr(e, plan.schema) for e in q["group"]]
            aggs = [(n[1], None if n[2] is None else compile_expr(n[2], plan.schema), n[3]) for n in agg_nodes]
            slots = {e: i for i, e in enumerate(q["group"])}
            slots.update({n: len(q["group"]) + i for i, n in enumerate(agg_nodes)})
            text = ", ".join(show(n) for n in agg_nodes) + (f" group by {', '.join(show(e) for e in q['group'])}" if q["group"] else "")
            plan = Aggregate(plan, group_fns, aggs, text)
            if q["having"]:
                plan = Filter(plan, compile_expr(q["having"], plan.schema, slots), show(q["having"]))
        elif q["having"]:
            raise SqlError("HAVING needs GROUP BY or an aggregate")

        return Project(plan, [compile_expr(e, plan.schema, slots) for e, _ in items], [n for _, n in items],
                       [compile_expr(e, plan.schema, slots) for e, _ in order], [d for _, d in order],
                       q["distinct"], q["limit"], q["offset"])
