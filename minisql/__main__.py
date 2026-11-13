"""Demo: python -m minisql   (or: python -m minisql "SELECT ..." to run a query on the demo data)"""
from __future__ import annotations

import random
import sys

from . import Database


def demo_db() -> Database:
    rng = random.Random(1)
    db = Database()
    db.execute("CREATE TABLE customers (id INT, name TEXT, city TEXT)")
    db.execute("CREATE TABLE orders (id INT, customer_id INT, amount REAL, status TEXT)")
    cities = ["Delhi", "Mumbai", "Pune", "Chennai", "Kochi"]
    for start in range(0, 2000, 200):
        db.execute("INSERT INTO customers VALUES " + ", ".join(
            f"({i}, 'customer{i}', '{rng.choice(cities)}')" for i in range(start + 1, start + 201)))
    for start in range(0, 10000, 200):
        db.execute("INSERT INTO orders VALUES " + ", ".join(
            f"({i}, {rng.randint(1, 2000)}, {round(rng.uniform(100, 5000), 2)}, '{rng.choice(['paid', 'paid', 'paid', 'refunded'])}')"
            for i in range(start + 1, start + 201)))
    return db


def show(db: Database, sql: str) -> None:
    print(f"\n> {sql}")
    rows = db.execute(sql)
    if not sql.upper().startswith("EXPLAIN"):
        print("  " + " | ".join(db.columns(sql)))
    for row in rows[:8]:
        print("  " + " | ".join(str(round(v, 2) if isinstance(v, float) else v) for v in row))


def main() -> None:
    db = demo_db()
    if len(sys.argv) > 1:
        return show(db, sys.argv[1])
    print("Demo data: 2,000 customers and 10,000 orders (randomly generated).")
    report = ("SELECT c.city, COUNT(*) AS orders, SUM(o.amount) AS revenue, AVG(o.amount) AS average FROM customers c "
              "JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.city ORDER BY revenue DESC")
    show(db, report)
    show(db, "EXPLAIN " + report)

    lookup = "SELECT id, amount, status FROM orders WHERE customer_id = 1234 AND amount > 1000"
    show(db, lookup)
    print(f"  rows read without an index: {db.stats['rows_scanned']:,}")
    db.execute("CREATE INDEX orders_customer ON orders (customer_id)")
    show(db, "EXPLAIN " + lookup)
    db.execute(lookup)
    print(f"  rows read with the index:   {db.stats['rows_scanned']:,}")


if __name__ == "__main__":
    main()
