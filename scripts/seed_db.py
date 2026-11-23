"""Create data/enterprise_ops.db with a small, realistic logistics dataset.

Run:  uv run python scripts/seed_db.py
The data is fictional. Re-running rebuilds the file from scratch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "enterprise_ops.db"

SCHEMA = """
CREATE TABLE customers (
    customer_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    city          TEXT NOT NULL,
    tier          TEXT NOT NULL CHECK (tier IN ('standard', 'gold', 'platinum'))
);
CREATE TABLE products (
    sku           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,
    unit_price    REAL NOT NULL
);
CREATE TABLE inventory (
    sku           TEXT NOT NULL REFERENCES products(sku),
    warehouse     TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    reorder_level INTEGER NOT NULL,
    PRIMARY KEY (sku, warehouse)
);
CREATE TABLE orders (
    order_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    sku           TEXT NOT NULL REFERENCES products(sku),
    quantity      INTEGER NOT NULL,
    order_value   REAL NOT NULL,
    ship_from     TEXT NOT NULL,
    status        TEXT NOT NULL,
    order_date    TEXT NOT NULL
);
"""

CUSTOMERS = [
    (101, "Cascade Outfitters", "Seattle", "gold"),
    (102, "Rainier Medical Supply", "Tacoma", "platinum"),
    (103, "Puget Sound Grocers", "Seattle", "standard"),
    (104, "Columbia Hardware Co.", "Portland", "gold"),
    (105, "Emerald City Electronics", "Seattle", "standard"),
    (106, "Bay Area Robotics", "San Francisco", "platinum"),
    (107, "Desert Bloom Florists", "Phoenix", "standard"),
    (108, "Lone Star Freight", "Dallas", "gold"),
    (109, "Windy City Books", "Chicago", "standard"),
    (110, "Big Apple Bakeware", "New York", "gold"),
]

PRODUCTS = [
    ("SKU-90210", "Industrial Drone Battery Pack", "electronics", 189.50),
    ("SKU-10001", "Thermal Shipping Container", "packaging", 74.00),
    ("SKU-10002", "Pallet Wrap Roll (20 pack)", "packaging", 42.25),
    ("SKU-20450", "Handheld Barcode Scanner", "electronics", 129.99),
    ("SKU-31337", "Insulated Medical Cooler", "medical", 215.00),
    ("SKU-40400", "Heavy-Duty Hand Truck", "equipment", 98.75),
    ("SKU-55555", "LED Warehouse Light", "equipment", 64.40),
    ("SKU-77001", "Forklift Safety Vest (10 pack)", "safety", 39.90),
]

INVENTORY = [
    ("SKU-90210", "Seattle", 140, 50), ("SKU-90210", "Portland", 35, 50), ("SKU-90210", "Dallas", 220, 60),
    ("SKU-10001", "Seattle", 60, 20), ("SKU-10001", "Chicago", 85, 20),
    ("SKU-10002", "Seattle", 300, 100), ("SKU-10002", "New York", 410, 100),
    ("SKU-20450", "Seattle", 12, 25), ("SKU-20450", "San Francisco", 48, 25),
    ("SKU-31337", "Seattle", 18, 10), ("SKU-31337", "Phoenix", 4, 10),
    ("SKU-40400", "Dallas", 75, 15), ("SKU-40400", "Chicago", 22, 15),
    ("SKU-55555", "Portland", 190, 40), ("SKU-55555", "New York", 66, 40),
    ("SKU-77001", "Seattle", 95, 30), ("SKU-77001", "Dallas", 140, 30),
]

ORDERS = [
    (4492, 101, "SKU-90210", 120, 22740.00, "Seattle", "pending", "2026-11-14"),
    (4493, 103, "SKU-10002", 40, 1690.00, "Seattle", "shipped", "2026-11-10"),
    (4494, 105, "SKU-20450", 20, 2599.80, "Seattle", "backordered", "2026-11-12"),
    (4495, 102, "SKU-31337", 15, 3225.00, "Seattle", "pending", "2026-11-13"),
    (4496, 104, "SKU-90210", 50, 9475.00, "Portland", "pending", "2026-11-13"),
    (4497, 106, "SKU-20450", 30, 3899.70, "San Francisco", "shipped", "2026-11-08"),
    (4498, 107, "SKU-31337", 6, 1290.00, "Phoenix", "backordered", "2026-11-11"),
    (4499, 108, "SKU-40400", 25, 2468.75, "Dallas", "delivered", "2026-11-02"),
    (4500, 109, "SKU-10001", 10, 740.00, "Chicago", "delivered", "2026-11-01"),
    (4501, 110, "SKU-10002", 120, 5070.00, "New York", "shipped", "2026-11-09"),
    (4502, 101, "SKU-77001", 30, 1197.00, "Seattle", "delivered", "2026-11-03"),
    (4503, 108, "SKU-90210", 80, 15160.00, "Dallas", "pending", "2026-11-15"),
    (4504, 103, "SKU-10001", 25, 1850.00, "Seattle", "pending", "2026-11-15"),
    (4505, 104, "SKU-55555", 60, 3864.00, "Portland", "shipped", "2026-11-07"),
    (4506, 105, "SKU-77001", 12, 478.80, "Seattle", "pending", "2026-11-16"),
    (4507, 106, "SKU-10001", 40, 2960.00, "Seattle", "cancelled", "2026-11-05"),
    (4508, 109, "SKU-40400", 10, 987.50, "Chicago", "pending", "2026-11-16"),
    (4509, 110, "SKU-55555", 30, 1932.00, "New York", "delivered", "2026-10-30"),
    (4510, 102, "SKU-90210", 10, 1895.00, "Seattle", "shipped", "2026-11-11"),
    (4511, 107, "SKU-10002", 15, 633.75, "Seattle", "pending", "2026-11-17"),
    (4512, 101, "SKU-20450", 8, 1039.92, "San Francisco", "pending", "2026-11-17"),
    (4513, 108, "SKU-77001", 50, 1995.00, "Dallas", "shipped", "2026-11-10"),
]


def build(path: Path = DB_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    with con:
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO customers VALUES (?,?,?,?)", CUSTOMERS)
        con.executemany("INSERT INTO products VALUES (?,?,?,?)", PRODUCTS)
        con.executemany("INSERT INTO inventory VALUES (?,?,?,?)", INVENTORY)
        con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", ORDERS)
    con.close()
    return path


if __name__ == "__main__":
    p = build()
    total = len(CUSTOMERS) + len(PRODUCTS) + len(INVENTORY) + len(ORDERS)
    print(f"Built {p.name}: {total} rows ({len(ORDERS)} orders, {len(INVENTORY)} inventory rows)")
