"""Generate a 10,000-row customer.csv with known, countable defects.

Injected: 340 NULL emails, 120 duplicate customer IDs, 53 invalid dates,
25 invalid state codes. Handy because you can assert the agent finds them.
"""
import random
from pathlib import Path
import pandas as pd

random.seed(42)
N = 10_000
STATES = ["TX", "CA", "NY", "FL", "WA", "IL", "OH", "GA", "NC", "CO"]
FIRST = ["Ava", "Liam", "Noah", "Mia", "Zoe", "Ethan", "Ivy", "Leo", "Maya", "Omar"]
LAST = ["Nguyen", "Patel", "Garcia", "Smith", "Khan", "Lopez", "Chen", "Brown", "Singh", "Kim"]

rows = []
for i in range(1, N + 1):
    f, l = random.choice(FIRST), random.choice(LAST)
    rows.append({
        "customer_id": i,
        "first_name": f,
        "last_name": l,
        "email": f"{f}.{l}{i}@example.com".lower(),
        "signup_date": f"20{random.randint(15, 25)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
        "state": random.choice(STATES),
    })
df = pd.DataFrame(rows)

idx = random.sample(range(N), 340 + 120 + 53 + 25)
null_email, dup, bad_date, bad_state = idx[:340], idx[340:460], idx[460:513], idx[513:]

df.loc[null_email, "email"] = None
df.loc[bad_date, "signup_date"] = [random.choice(["2023-13-45", "31/02/2022", "not a date", "0000-00-00"]) for _ in bad_date]
df.loc[bad_state, "state"] = [random.choice(["XX", "Texas", "T", "ZZ", "99"]) for _ in bad_state]
# duplicate IDs: copy another row's customer_id onto these rows
df.loc[dup, "customer_id"] = [random.randint(1, N) for _ in dup]

out = Path(__file__).resolve().parents[1] / "data" / "customer.csv"
df.to_csv(out, index=False)
print(f"Wrote {out} ({len(df)} rows)")
