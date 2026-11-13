"""The engine is checked against SQLite: same data, same query, same answer."""
import random
import sqlite3

import pytest

from minisql import Database, SqlError

SETUP = [
    "CREATE TABLE users (id INT, name TEXT, city TEXT, age INT)",
    "CREATE TABLE orders (id INT, user_id INT, amount REAL, status TEXT, qty INT)",
]


def make_data(seed=5, users=40, orders=160):
    rng = random.Random(seed)
    cities = ["Delhi", "Pune", "Goa", "Kochi", None]
    names = ["Asha", "Ravi", "Meera", "Zoya", "Kabir", "Anu", "Dev", "Isha"]
    u = [(i, f"{rng.choice(names)}{i}", rng.choice(cities), rng.choice([None] + list(range(18, 60)))) for i in range(1, users + 1)]
    o = [(i, rng.randint(1, users + 5), rng.choice([None, round(rng.uniform(5, 900), 2)]) if rng.random() < 0.15 else round(rng.uniform(5, 900), 2),
          rng.choice(["paid", "refunded", "pending"]), rng.randint(1, 9)) for i in range(1, orders + 1)]
    return u, o


@pytest.fixture(scope="module")
def dbs():
    mine, ref = Database(), sqlite3.connect(":memory:")
    for stmt in SETUP:
        mine.execute(stmt)
        ref.execute(stmt)
    u, o = make_data()
    lit = lambda v: "NULL" if v is None else (f"'{v}'" if isinstance(v, str) else repr(v))
    for table, rows in (("users", u), ("orders", o)):
        for i in range(0, len(rows), 25):
            mine.execute(f"INSERT INTO {table} VALUES " + ", ".join("(" + ", ".join(lit(v) for v in r) + ")" for r in rows[i:i + 25]))
        ref.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)
    return mine, ref


def norm(rows, ordered):
    rows = [tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in rows]
    return rows if ordered else sorted(rows, key=lambda r: tuple((v is not None, str(type(v)), v) for v in r))


def check(dbs, sql, ordered=False):
    mine, ref = dbs
    assert norm(mine.execute(sql), ordered) == norm(ref.execute(sql).fetchall(), ordered), sql


QUERIES = [
    "SELECT * FROM users",
    "SELECT name, age + 1, age * 2 - 3, -age FROM users WHERE age >= 30 AND city != 'Goa'",
    "SELECT id FROM users WHERE city IS NULL OR age IS NULL",
    "SELECT id FROM users WHERE NOT (age > 30)",                       # NULL ages must be excluded, not included
    "SELECT id FROM users WHERE age NOT IN (20, 21, 22, NULL)",        # NOT IN with a NULL matches nothing
    "SELECT id FROM users WHERE city IN ('Delhi', 'Goa') AND age BETWEEN 25 AND 45",
    "SELECT name FROM users WHERE name LIKE 'a%' OR name LIKE '%1_'",
    "SELECT id, qty / 2, qty % 4, amount / qty FROM orders WHERE qty > 0",
    "SELECT id, 7 / (qty - qty) FROM orders WHERE id < 5",              # division by zero is NULL
    "SELECT COUNT(*), COUNT(amount), SUM(amount), AVG(amount), MIN(amount), MAX(amount) FROM orders",
    "SELECT COUNT(*), SUM(qty), MAX(age) FROM orders JOIN users ON users.id = orders.user_id WHERE 1 = 0",
    "SELECT status, COUNT(*), SUM(qty), AVG(amount) FROM orders GROUP BY status",
    "SELECT city, COUNT(*) AS n FROM users GROUP BY city HAVING COUNT(*) > 5",
    "SELECT status, qty, COUNT(*) FROM orders GROUP BY status, qty HAVING SUM(amount) > 500 AND qty < 8",
    "SELECT COUNT(DISTINCT city), COUNT(DISTINCT user_id) FROM users JOIN orders ON orders.user_id = users.id",
    "SELECT DISTINCT city, status FROM users u JOIN orders o ON o.user_id = u.id",
    "SELECT u.name, o.amount FROM users u JOIN orders o ON u.id = o.user_id WHERE o.status = 'paid' AND u.age > 40",
    "SELECT u.id, o.id FROM users u LEFT JOIN orders o ON o.user_id = u.id AND o.status = 'refunded'",
    "SELECT u.id, o.id FROM users u LEFT JOIN orders o ON o.user_id = u.id WHERE o.id IS NULL",       # anti-join
    "SELECT u.id, o.id FROM users u LEFT JOIN orders o ON o.user_id = u.id WHERE o.status = 'paid'",  # WHERE on the nullable side
    "SELECT a.id, b.id FROM users a JOIN users b ON a.city = b.city AND a.id < b.id WHERE a.age > 50",
    "SELECT a.id, b.id FROM users a JOIN users b ON a.age > b.age + 35",                              # no equality: nested loop
    "SELECT u.city, COUNT(o.id), SUM(o.amount) FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.city",
    "SELECT o.status, u.city, AVG(o.amount) FROM orders o JOIN users u ON u.id = o.user_id GROUP BY o.status, u.city HAVING COUNT(*) >= 3",
]


@pytest.mark.parametrize("sql", QUERIES)
def test_matches_sqlite(dbs, sql):
    check(dbs, sql)


ORDERED = [
    "SELECT id, age FROM users ORDER BY age DESC, id",
    "SELECT id, age FROM users ORDER BY age, id DESC LIMIT 7 OFFSET 3",
    "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC, status",
    "SELECT city, SUM(age) FROM users GROUP BY city ORDER BY SUM(age) DESC, city LIMIT 3",
    "SELECT DISTINCT qty FROM orders ORDER BY qty DESC",
    "SELECT u.id, COUNT(o.id) AS n FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.id ORDER BY n DESC, u.id LIMIT 10",
]


@pytest.mark.parametrize("sql", ORDERED)
def test_ordering_matches_sqlite(dbs, sql):
    check(dbs, sql, ordered=True)


def test_two_hundred_random_queries_match_sqlite(dbs):
    rng = random.Random(11)
    preds = ["u.age > {n}", "u.age <= {n}", "u.city = '{c}'", "u.city IS NULL", "u.age IS NOT NULL", "o.qty >= {q}", "o.status != '{s}'",
             "o.amount > {a}", "o.amount IS NULL", "u.city IN ('{c}', 'Goa')", "u.name LIKE '%{d}%'", "o.qty + u.id > {n}",
             "NOT (o.amount < {a})", "u.age BETWEEN {q} * 5 AND {n}"]

    def pred():
        p = rng.choice(preds).format(n=rng.randint(18, 60), c=rng.choice(["Delhi", "Pune", "Kochi"]), q=rng.randint(1, 9),
                                     s=rng.choice(["paid", "pending"]), a=rng.randint(50, 800), d=rng.randint(0, 9))
        return p if rng.random() < 0.6 else f"({p} {rng.choice(['AND', 'OR'])} {pred()})"

    for _ in range(200):
        join = rng.choice(["JOIN", "LEFT JOIN"])
        where = f" WHERE {pred()}" if rng.random() < 0.85 else ""
        if rng.random() < 0.5:
            sql = f"SELECT u.id, o.id, u.city, o.amount FROM users u {join} orders o ON o.user_id = u.id{where}"
        else:
            key = rng.choice(["u.city", "o.status", "u.city, o.status", "o.qty"])
            having = f" HAVING COUNT(*) > {rng.randint(1, 6)}" if rng.random() < 0.4 else ""
            sql = f"SELECT {key}, COUNT(*), COUNT(o.amount), SUM(o.qty), MIN(u.age), AVG(o.amount) FROM users u {join} orders o ON o.user_id = u.id{where} GROUP BY {key}{having}"
        check(dbs, sql)


def test_index_is_chosen_and_reads_fewer_rows(dbs):
    mine, _ = dbs
    sql = "SELECT id, name FROM users WHERE city = 'Pune' AND age > 20"
    before = mine.execute(sql)
    assert mine.stats["rows_scanned"] == 40
    assert "Scan users" in mine.execute("EXPLAIN " + sql)[-1][0]
    mine.execute("CREATE INDEX users_city ON users (city)")
    plan = [line for (line,) in mine.execute("EXPLAIN " + sql)]
    assert plan == ["Project id, name", "  Filter age > 20", "    IndexLookup users.city = 'Pune'"]
    assert sorted(mine.execute(sql)) == sorted(before)
    assert 0 < mine.stats["rows_scanned"] < 20
    mine.execute("INSERT INTO users VALUES (999, 'New', 'Pune', 33)")       # the index is maintained on insert
    _.execute("INSERT INTO users VALUES (999, 'New', 'Pune', 33)")
    assert (999, "New") in mine.execute(sql)
    check(dbs, sql)


def test_plan_pushes_filters_below_joins_but_not_into_the_nullable_side():
    db = Database()
    db.execute("CREATE TABLE a (id, x)")
    db.execute("CREATE TABLE b (id, a_id, y)")
    inner = [l for (l,) in db.execute("EXPLAIN SELECT * FROM a JOIN b ON b.a_id = a.id WHERE a.x = 1 AND b.y = 2 AND a.x < b.y")]
    assert inner == ["Project id, x, id, a_id, y", "  Filter a.x < b.y", "    HashJoin (inner) on b.a_id = a.id",
                     "      Filter a.x = 1", "        Scan a", "      Filter b.y = 2", "        Scan b"]
    left = [l for (l,) in db.execute("EXPLAIN SELECT * FROM a LEFT JOIN b ON b.a_id = a.id WHERE b.y = 2")]
    assert left[1] == "  Filter b.y = 2" and left[-1] == "      Scan b"      # stays above the join
    loop = [l for (l,) in db.execute("EXPLAIN SELECT * FROM a JOIN b ON a.x > b.y")]
    assert "NestedLoopJoin" in loop[1]


def test_limit_stops_reading_early():
    db = Database()
    db.execute("CREATE TABLE t (n)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i})" for i in range(500)))
    assert db.execute("SELECT n FROM t LIMIT 3") == [(0,), (1,), (2,)]
    assert db.stats["rows_scanned"] == 4                 # pulled lazily: three rows plus the one that hit the limit
    db.execute("SELECT n FROM t ORDER BY n DESC LIMIT 3")
    assert db.stats["rows_scanned"] == 500               # a sort has to see everything


def test_column_names_and_aliases():
    db = Database()
    db.execute("CREATE TABLE t (a, b)")
    assert db.columns("SELECT a, b AS bee, a + b, COUNT(*) total FROM t GROUP BY a, b") == ["a", "bee", "a + b", "total"]


@pytest.mark.parametrize("sql, message", [
    ("SELEC * FROM users", "unsupported statement"),
    ("SELECT * FROM missing", "no such table"),
    ("SELECT nope FROM users", "no such column"),
    ("SELECT id FROM users u JOIN orders o ON o.user_id = u.id", "ambiguous column"),
    ("SELECT name, COUNT(*) FROM users GROUP BY city", "must appear in GROUP BY"),
    ("SELECT * FROM users WHERE COUNT(*) > 1", "not allowed here"),
    ("SELECT SUM(COUNT(*)) FROM users", "cannot be nested"),
    ("SELECT * FROM users WHERE", "unexpected"),
    ("SELECT * FROM users LIMIT 5 garbage", "unexpected"),
    ("SELECT * FROM users u JOIN users u ON 1 = 1", "distinct alias"),
    ("SELECT * FROM users WHERE name > 5", "cannot compare"),
    ("INSERT INTO users VALUES (1, 2)", "expected 4 values"),
    ("SELECT id FROM users WHERE id = #", "unexpected character"),
    ("SELECT UPPER(name) FROM users", "unknown function"),
])
def test_errors_are_reported_not_swallowed(dbs, sql, message):
    with pytest.raises(SqlError, match=message):
        dbs[0].execute(sql)
