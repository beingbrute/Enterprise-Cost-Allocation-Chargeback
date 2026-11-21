"""Recalculate allocations from the raw CSV files with pandas, without using any SQL, and compare
them to what the pipeline posted. This checks the SQL allocation against an independent method."""
from decimal import ROUND_DOWN, Decimal

import pandas as pd
import pytest

from conftest import ROOT

RAW = ROOT / "data" / "raw"
# Months where every pool was allocated from its first file version, with no hierarchy closures in play.
MONTHS = ["2025-01", "2025-07", "2025-12"]
POOLS = ["P1", "P2", "P3", "P4"]


def policy_driver(pool, period):
    pol = pd.read_csv(RAW / "reference" / "allocation_policies.csv", dtype=str)
    day = f"{period}-01"
    row = pol[(pol.pool_code == pool) & (pol.valid_from <= day) & (pol.valid_to >= day)].iloc[0]
    return row.driver, row.excludes_remote_only == "True"


def recalc(period, pool):
    cost = pd.read_csv(RAW / period / f"expenses_{pool}.csv", dtype={"amount": str}).amount.map(Decimal).sum()
    driver, excl_remote = policy_driver(pool, period)
    vol = pd.read_csv(RAW / period / f"volumes_{pool}.csv")
    vol = vol[vol.driver == driver]
    if excl_remote:
        remote = pd.read_csv(RAW / "reference" / "cost_centres.csv")
        vol = vol[~vol.cc_code.isin(remote[remote.remote_only].cc_code)]
    total = Decimal(int(vol.quantity.sum()))
    exact = {cc: cost * Decimal(int(q)) / total for cc, q in zip(vol.cc_code, vol.quantity)}
    floored = {cc: v.quantize(Decimal("0.01"), rounding=ROUND_DOWN) for cc, v in exact.items()}
    cents_left = int((cost - sum(floored.values())) * 100)
    for cc in sorted(exact, key=lambda c: (-(exact[c] - floored[c]), c))[:cents_left]:
        floored[cc] += Decimal("0.01")
    return cost, floored


@pytest.mark.parametrize("period", MONTHS)
@pytest.mark.parametrize("pool", POOLS)
def test_pandas_recalculation_matches_posted_allocation(cur, period, pool):
    cost, expected = recalc(period, pool)
    assert sum(expected.values()) == cost
    cur.execute("""SELECT cc_code, SUM(amount) FROM rpt.fact_chargeback
                   WHERE source_period = %s AND pool_code = %s GROUP BY cc_code""", (period, pool))
    posted = dict(cur.fetchall())
    assert posted == expected
