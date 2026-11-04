"""Deterministic sample ledgers.

Three fictional databases, generated with a fixed seed so the demo and the
tests see the same shape of story. PesaLedger is not affiliated with
Safaricom or M-Pesa; it only borrows the shape of a Kenyan mobile-money ledger.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from app.clock import LEDGER_TODAY

CATALOG = [
    {
        "id": "harbor",
        "name": "Harbor SaaS",
        "blurb": "B2B subscriptions, product usage, and support tickets. Built for the churn question.",
        "question": "Which customers churned last quarter and why?",
        "currency": "USD",
    },
    {
        "id": "northline",
        "name": "Northline Commerce",
        "blurb": "Orders, line items, and refunds across five categories from 2025 through this ledger clock.",
        "question": "Build me a sales dashboard for this year",
        "currency": "USD",
    },
    {
        "id": "pesaledger",
        "name": "PesaLedger",
        "blurb": "Fictional Kenyan mobile-money ledger: tills, agents, withdrawals, and fees. Not a Safaricom product.",
        "question": "Where are withdrawals failing, and how much fee revenue did we collect last month?",
        "currency": "KES",
    },
]


def ensure_samples(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {item["id"]: directory / f"{item['id']}.sqlite" for item in CATALOG}
    missing = [key for key, path in paths.items() if not path.exists()]
    if not missing:
        return paths
    rng = random.Random(7)
    builders = {"harbor": _build_harbor, "northline": _build_northline, "pesaledger": _build_pesa}
    for key in missing:
        tmp = paths[key].with_suffix(".building")
        if tmp.exists():
            tmp.unlink()
        builders[key](tmp, rng)
        tmp.replace(paths[key])
    return paths


def _finish(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.commit()
    conn.close()


def _build_harbor(path: Path, rng: random.Random) -> None:
    left = [
        "Tumaini", "Savanna", "Harambee", "Acacia", "Rift", "Baobab", "Amber", "Northline",
        "Silver", "Kindred", "Harbor", "Field", "Lumen", "Kestrel", "Marula", "Saffron",
        "Cedar", "Pamoja", "Boma", "Nyati", "Olive", "Copper", "Atlas", "Kinara",
        "Mara", "Lantern", "Uhuru", "Thika", "Prairie", "Zebra",
    ]
    right = ["Freight", "Clinic", "Foods", "Studio", "Labs", "Capital", "Retail", "Health", "Media", "Logistics", "Schools", "Energy", "Market", "Works"]
    countries = ["Kenya", "Uganda", "Nigeria", "United Kingdom", "Germany", "United States"]
    segments = ["startup", "mid_market", "enterprise"]
    names = [f"{a} {b}" for a in left for b in right]
    rng.shuffle(names)
    names = names[:120]
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, segment TEXT, country TEXT, signed_up_at TEXT, employees INTEGER);
        CREATE TABLE users (id INTEGER PRIMARY KEY, account_id INTEGER, email TEXT, role TEXT, created_at TEXT);
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY, account_id INTEGER, plan_code TEXT, status TEXT,
            mrr_cents INTEGER, started_at TEXT, ended_at TEXT, cancel_reason TEXT
        );
        CREATE TABLE product_events (
            id INTEGER PRIMARY KEY, account_id INTEGER, user_id INTEGER, event_name TEXT, occurred_at TEXT
        );
        CREATE TABLE support_tickets (
            id INTEGER PRIMARY KEY, account_id INTEGER, opened_at TEXT, priority TEXT, topic TEXT, satisfaction INTEGER
        );
        """
    )
    plans = {"startup": ("starter", 2900), "mid_market": ("growth", 9900), "enterprise": ("scale", 34900)}
    reasons = ["price"] * 12 + ["missing_features"] * 8 + ["low_usage"] * 7 + ["support"] * 5 + ["competitor"] * 4
    q3_ids = list(range(1, 37))
    other_churn = list(range(37, 49))
    accounts = []
    subscriptions = []
    for index, name in enumerate(names, start=1):
        segment = segments[index % 3]
        if index in q3_ids and segment == "enterprise" and reasons[index - 1] == "price":
            segment = "startup"
        country = countries[index % len(countries)]
        signup = date(2024, 6, 1) + timedelta(days=rng.randrange(0, 640))
        if index in q3_ids and signup > date(2026, 3, 1):
            signup = date(2025, rng.randint(1, 12), rng.randint(1, 27))
        employees = {"startup": rng.randint(3, 40), "mid_market": rng.randint(40, 250), "enterprise": rng.randint(250, 1800)}[segment]
        accounts.append((index, name, segment, country, signup.isoformat(), employees))
        plan, mrr = plans[segment]
        if index in q3_ids:
            ended = date(2026, 7, 1) + timedelta(days=rng.randrange(0, 92))
            reason = reasons[index - 1]
            status = "canceled"
        elif index in other_churn:
            ended = date(2025, 3, 1) + timedelta(days=rng.randrange(0, 400))
            if ended >= date(2026, 7, 1):
                ended = date(2026, 5, 15)
            reason = rng.choice(["price", "competitor", "missing_features"])
            status = "canceled"
        else:
            ended = None
            reason = None
            status = "active"
        subscriptions.append(
            (index, index, plan, status, mrr, signup.isoformat(), ended.isoformat() if ended else None, reason)
        )
    conn.executemany("INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?)", accounts)
    conn.executemany("INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", subscriptions)
    users = []
    user_id = 1
    account_users: dict[int, list[int]] = {}
    roles = ["owner", "admin", "member", "member"]
    for account_id, name, *_rest in accounts:
        slug = "".join(ch.lower() if ch.isalnum() else "" for ch in name)[:22]
        owned = []
        for offset in range(rng.randint(2, 4)):
            users.append(
                (
                    user_id,
                    account_id,
                    f"{roles[offset]}.{offset}@{slug}.example",
                    roles[offset],
                    accounts[account_id - 1][4],
                )
            )
            owned.append(user_id)
            user_id += 1
        account_users[account_id] = owned
    conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?, ?)", users)
    events = []
    event_id = 1
    event_names = ["login", "dashboard_view", "report_export", "invite_sent", "integration_connected", "api_call"]
    sub_by_account = {row[1]: row for row in subscriptions}
    for account_id, _name, _segment, _country, signup, _employees in accounts:
        sub = sub_by_account[account_id]
        status, ended_at, reason = sub[3], sub[6], sub[7]
        start = date.fromisoformat(signup)
        hard_end = date.fromisoformat(ended_at) if ended_at else LEDGER_TODAY
        daily_p = 0.02 if reason == "low_usage" else 0.35
        quiet_after = hard_end - timedelta(days=30) if reason == "low_usage" else None
        day = start
        while day < hard_end and day <= LEDGER_TODAY:
            if quiet_after and day >= quiet_after:
                day += timedelta(days=1)
                continue
            if rng.random() < daily_p:
                stamp = datetime(day.year, day.month, day.day, rng.randint(8, 18), rng.randint(0, 59), 0)
                events.append(
                    (
                        event_id,
                        account_id,
                        rng.choice(account_users[account_id]),
                        rng.choice(event_names),
                        stamp.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                )
                event_id += 1
            day += timedelta(days=1)
    conn.executemany("INSERT INTO product_events VALUES (?, ?, ?, ?, ?)", events)
    tickets = []
    ticket_id = 1
    topics = ["billing", "sso", "exports", "api", "onboarding"]
    for account_id, _name, *_rest in accounts:
        sub = sub_by_account[account_id]
        reason, ended_at = sub[7], sub[6]
        if reason == "support" and ended_at:
            end = date.fromisoformat(ended_at)
            for _ in range(rng.randint(1, 2)):
                opened = end - timedelta(days=rng.randint(3, 50))
                tickets.append((ticket_id, account_id, opened.isoformat(), "high", rng.choice(topics), rng.choice([1, 2])))
                ticket_id += 1
        elif rng.random() < 0.18:
            opened = date.fromisoformat(accounts[account_id - 1][4]) + timedelta(days=rng.randint(20, 200))
            tickets.append((ticket_id, account_id, opened.isoformat(), "normal", rng.choice(topics), rng.choice([4, 5])))
            ticket_id += 1
    conn.executemany("INSERT INTO support_tickets VALUES (?, ?, ?, ?, ?, ?)", tickets)
    conn.execute("CREATE INDEX idx_sub_status ON subscriptions(status, ended_at)")
    conn.execute("CREATE INDEX idx_events_account ON product_events(account_id, occurred_at)")
    _finish(conn)


def _build_northline(path: Path, rng: random.Random) -> None:
    first = ["Amina", "Brian", "Chipo", "David", "Esther", "Farah", "Grace", "Hassan", "Imani", "James", "Kamau", "Lydia", "Noah", "Olu", "Priya", "Quinn"]
    last = ["Otieno", "Wanjiku", "Mensah", "Adeyemi", "Berg", "Singh", "Okeke", "Keller", "Diallo", "Nguyen", "Brown", "Mwangi"]
    cities = [
        ("Nairobi", "Kenya"), ("Mombasa", "Kenya"), ("Lagos", "Nigeria"), ("London", "United Kingdom"),
        ("Berlin", "Germany"), ("Austin", "United States"), ("Kampala", "Uganda"), ("Accra", "Ghana"),
    ]
    products = [
        ("Apparel", "Canvas field jacket", 8900), ("Apparel", "Meridian tee", 2800), ("Apparel", "Kiln wool scarf", 4200),
        ("Home", "Acacia serving board", 3600), ("Home", "Stoneware pourer", 2400), ("Home", "Linen throw", 6400),
        ("Electronics", "Nimbus earbuds", 7900), ("Electronics", "Harbor lamp charger", 4500), ("Electronics", "Field radio", 12800),
        ("Grocery", "Single-origin tin", 1800), ("Grocery", "Chili crunch jar", 900), ("Grocery", "Honeycomb bar", 600),
        ("Beauty", "Marula oil", 3200), ("Beauty", "Cedar balm", 1400), ("Beauty", "Clay mask", 2100),
    ]
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, full_name TEXT, email TEXT, city TEXT, country TEXT, created_at TEXT);
        CREATE TABLE products (id INTEGER PRIMARY KEY, sku TEXT, name TEXT, category TEXT, unit_price_cents INTEGER);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, ordered_at TEXT, status TEXT);
        CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, quantity INTEGER, unit_price_cents INTEGER);
        CREATE TABLE payments (id INTEGER PRIMARY KEY, order_id INTEGER, method TEXT, amount_cents INTEGER, status TEXT, paid_at TEXT);
        """
    )
    customers = []
    for index in range(1, 201):
        name = f"{rng.choice(first)} {rng.choice(last)}"
        city, country = cities[index % len(cities)]
        created = date(2024, 11, 1) + timedelta(days=rng.randrange(0, 500))
        customers.append((index, name, f"buyer{index}@customers.example", city, country, created.isoformat()))
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", customers)
    prod_rows = []
    for index, (category, name, price) in enumerate(products, start=1):
        prod_rows.append((index, f"NL-{index:03d}", name, category, price))
    conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", prod_rows)
    orders = []
    items = []
    payments = []
    item_id = 1
    start = date(2025, 1, 1)
    days = (LEDGER_TODAY - start).days
    methods = ["card", "card", "mpesa", "paypal", "invoice"]
    for order_id in range(1, 2201):
        customer_id = rng.randint(1, 200)
        ordered = start + timedelta(days=rng.randrange(0, days), hours=rng.randint(8, 20))
        category_bias = None
        picks = [rng.choice(prod_rows) for _ in range(rng.randint(1, 4))]
        electronics = any(row[3] == "Electronics" for row in picks)
        if ordered.year == 2026 and ordered.month == 8 and electronics and rng.random() < 0.22:
            status = "refunded"
        else:
            roll = rng.random()
            status = "paid" if roll < 0.9 else "refunded" if roll < 0.95 else "cancelled" if roll < 0.98 else "pending"
        orders.append((order_id, customer_id, ordered.strftime("%Y-%m-%d %H:%M:%S"), status))
        total = 0
        for product in picks:
            qty = rng.randint(1, 3)
            items.append((item_id, order_id, product[0], qty, product[4]))
            item_id += 1
            total += qty * product[4]
        if status in {"paid", "refunded"}:
            payments.append(
                (order_id, order_id, rng.choice(methods), total, "settled" if status == "paid" else "refunded", ordered.strftime("%Y-%m-%d %H:%M:%S"))
            )
        del category_bias
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", orders)
    conn.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", items)
    conn.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?)", payments)
    conn.execute("CREATE INDEX idx_orders_when ON orders(ordered_at, status)")
    conn.execute("CREATE INDEX idx_items_order ON order_items(order_id)")
    _finish(conn)


def _build_pesa(path: Path, rng: random.Random) -> None:
    first = ["Achieng", "Baraka", "Chebet", "Dawit", "Eunice", "Fatma", "Gideon", "Halima", "Irungu", "Joyce", "Kiptoo", "Linet", "Mwende", "Njeri", "Otieno", "Pendo", "Quinter", "Rotich", "Siti", "Wanjala"]
    last = ["Kimani", "Ochieng", "Mutua", "Cheruiyot", "Abdullahi", "Waweru", "Chepkemoi", "Omondi", "Njoka", "Hassan"]
    counties = ["Nairobi", "Mombasa", "Kisumu", "Nakuru", "Eldoret", "Kiambu", "Kakamega", "Nyeri", "Garissa", "Turkana"]
    hard_counties = {"Garissa", "Turkana"}
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, full_name TEXT, phone TEXT, county TEXT, kyc_tier TEXT, registered_at TEXT);
        CREATE TABLE agents (id INTEGER PRIMARY KEY, agent_code TEXT, agent_name TEXT, county TEXT, status TEXT);
        CREATE TABLE merchants (id INTEGER PRIMARY KEY, business_name TEXT, till_number TEXT, category TEXT, county TEXT);
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, txn_ref TEXT, txn_type TEXT, customer_id INTEGER, agent_id INTEGER, merchant_id INTEGER,
            amount_cents INTEGER, fee_cents INTEGER, status TEXT, failure_reason TEXT, channel TEXT, created_at TEXT
        );
        """
    )
    customers = []
    for index in range(1, 351):
        county = counties[index % len(counties)]
        phone = f"+2547{rng.randint(10000000, 99999999)}"
        registered = date(2024, 1, 15) + timedelta(days=rng.randrange(0, 700))
        customers.append(
            (index, f"{rng.choice(first)} {rng.choice(last)}", phone, county, rng.choice(["tier1", "tier2", "tier3"]), registered.isoformat())
        )
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", customers)
    agents = []
    for index in range(1, 31):
        county = counties[index % len(counties)]
        agents.append((index, f"AG{index:04d}", f"{county} Agent {index}", county, "active"))
    conn.executemany("INSERT INTO agents VALUES (?, ?, ?, ?, ?)", agents)
    shops = ["Duka", "Pharmacy", "Hardware", "Salon", "Butcher", "Agrovet", "Cafe", "Bookshop"]
    merchants = []
    for index in range(1, 21):
        county = counties[index % len(counties)]
        merchants.append((index, f"{rng.choice(first)} {shops[index % len(shops)]}", f"{500000 + index}", shops[index % len(shops)], county))
    conn.executemany("INSERT INTO merchants VALUES (?, ?, ?, ?, ?)", merchants)
    types = ["send_money", "withdraw", "deposit", "lipa", "airtime", "paybill"]
    channels = ["app", "ussd", "stk"]
    start = date(2026, 1, 1)
    span = (LEDGER_TODAY - start).days
    rows = []
    for index in range(1, 6001):
        customer = customers[rng.randrange(0, len(customers))]
        county = customer[3]
        txn_type = rng.choice(types)
        created = datetime(start.year, start.month, start.day) + timedelta(days=rng.randrange(0, span), hours=rng.randint(6, 22), minutes=rng.randint(0, 59))
        amount_kes = rng.choice([100, 200, 350, 500, 800, 1000, 1500, 2500, 5000, 10000, 20000])
        amount_cents = amount_kes * 100
        agent_id = None
        merchant_id = None
        if txn_type in {"withdraw", "deposit"}:
            same = [agent for agent in agents if agent[3] == county] or agents
            agent_id = rng.choice(same)[0]
        if txn_type in {"lipa", "paybill"}:
            merchant_id = rng.choice(merchants)[0]
        status = "completed"
        reason = None
        roll = rng.random()
        if txn_type == "withdraw" and county in hard_counties and roll < 0.38:
            status, reason = "failed", "insufficient_float"
        elif txn_type == "lipa" and county == "Nairobi" and roll < 0.12:
            status, reason = "failed", "timeout"
        elif roll < 0.045:
            status, reason = "failed", rng.choice(["wrong_pin", "customer_limit", "timeout"])
        elif roll > 0.99:
            status, reason = "reversed", "customer_reversal"
        fee = 0
        if status == "completed" and txn_type in {"send_money", "withdraw"}:
            fee = max(1000, int(amount_cents * 0.015))
        elif status == "completed" and txn_type in {"lipa", "paybill"}:
            fee = int(amount_cents * 0.005)
        rows.append(
            (
                index,
                f"PL{created.strftime('%y%m%d')}{index:06d}",
                txn_type,
                customer[0],
                agent_id,
                merchant_id,
                amount_cents,
                fee,
                status,
                reason,
                rng.choice(channels),
                created.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    conn.executemany("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.execute("CREATE INDEX idx_txn_when ON transactions(created_at, txn_type, status)")
    conn.execute("CREATE INDEX idx_txn_customer ON transactions(customer_id)")
    _finish(conn)
