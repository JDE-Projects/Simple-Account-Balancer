#!/usr/bin/env python3
"""What the README screenshot shows: an invented checking account with a
month or so of realistic register activity.

None of this is real data. The version shown in the image always comes from
simple_account_balancer.py, never from here, so this fixture holds no version
number.
"""

ACCOUNT = {"id": 1, "name": "Everyday Checking"}

# One row is left estimated (a variable autopay whose amount is a guess until
# the real charge posts) so the "!" flag and the estimate notice both show.
ESTIMATED_DUE_COUNT = 1

TX = [
    {"id": 1, "date": "2026-07-08", "payee": "Paycheck - Acme Corp",
     "category": "Income", "notes": "Biweekly", "amount_cents": 214500,
     "cleared": True, "estimated": False, "balance_cents": 361280},
    {"id": 2, "date": "2026-07-09", "payee": "Corner Grocery",
     "category": "Groceries", "notes": "", "amount_cents": -8734,
     "cleared": True, "estimated": False, "balance_cents": 352546},
    {"id": 3, "date": "2026-07-10", "payee": "City Water",
     "category": "Utilities", "notes": "", "amount_cents": -6120,
     "cleared": True, "estimated": False, "balance_cents": 346426},
    {"id": 4, "date": "2026-07-12", "payee": "Northwind Insurance",
     "category": "Insurance", "notes": "Auto policy, amount varies",
     "amount_cents": -11200, "cleared": False, "estimated": True,
     "balance_cents": 335226},
    {"id": 5, "date": "2026-07-14", "payee": "Riverside Apartments",
     "category": "Rent", "notes": "", "amount_cents": -142000,
     "cleared": True, "estimated": False, "balance_cents": 193226},
    {"id": 6, "date": "2026-07-15", "payee": "Green Line Transit",
     "category": "Transportation", "notes": "Monthly pass",
     "amount_cents": -7500, "cleared": True, "estimated": False,
     "balance_cents": 185726},
    {"id": 7, "date": "2026-07-17", "payee": "Corner Grocery",
     "category": "Groceries", "notes": "", "amount_cents": -6218,
     "cleared": True, "estimated": False, "balance_cents": 179508},
    {"id": 8, "date": "2026-07-18", "payee": "Maple Street Diner",
     "category": "Dining", "notes": "Lunch with Sam", "amount_cents": -2340,
     "cleared": False, "estimated": False, "balance_cents": 177168},
    {"id": 9, "date": "2026-07-19", "payee": "Transfer to Savings",
     "category": "Transfer", "notes": "Monthly set-aside",
     "amount_cents": -30000, "cleared": True, "estimated": False,
     "balance_cents": 147168},
    {"id": 10, "date": "2026-07-21", "payee": "Sunrise Wireless",
     "category": "Utilities", "notes": "Cell phone", "amount_cents": -5800,
     "cleared": True, "estimated": False, "balance_cents": 141368},
    {"id": 11, "date": "2026-07-22", "payee": "Paycheck - Acme Corp",
     "category": "Income", "notes": "Biweekly", "amount_cents": 214500,
     "cleared": True, "estimated": False, "balance_cents": 355868},
    {"id": 12, "date": "2026-07-24", "payee": "Corner Grocery",
     "category": "Groceries", "notes": "", "amount_cents": -9142,
     "cleared": True, "estimated": False, "balance_cents": 346726},
]

CURRENT_BALANCE_CENTS = TX[-1]["balance_cents"]
