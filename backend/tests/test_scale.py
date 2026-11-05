"""The brief's speed target: a 1-lakh-row import in under a minute. Skipped unless asked for, since
it generates 100,000 orders and loads about 380,000 payment lines:

    RUN_SCALE=1 pytest tests/test_scale.py -s
"""

from __future__ import annotations

import os
from datetime import date

import gen_data as g
import pytest

from app import demo
from app.ingest.catalog import SOURCES
from app.ingest.pipeline import import_file
from app.recon.engine import run_reconciliation
from app.seed import seed

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_SCALE"), reason="set RUN_SCALE=1")

ORDERS = 100_000
IMPORT_LIMIT_S = 60
RECON_LIMIT_S = 60


def test_a_lakh_of_orders_imports_and_reconciles_inside_a_minute(dict_conn, tmp_path):
    g.Generator(tmp_path, ORDERS, seed=42, today=date(2026, 11, 2), use_xlsx=True).run()
    source_ids = seed(dict_conn, tmp_path)

    orders = next(s for s in SOURCES if s.filename == "orders.csv")
    result = import_file(dict_conn, source_ids[orders.filename], tmp_path / orders.filename)
    print(f"\norders.csv: {result.rows_loaded} rows, stages {result.timings}")
    assert result.status == "succeeded" and result.rows_loaded == ORDERS
    assert result.timings["total"] < IMPORT_LIMIT_S

    demo.load_dataset(dict_conn, tmp_path, echo=False)   # the rest, for a realistic recon
    run = run_reconciliation(dict_conn, date(2026, 11, 2))
    print(f"reconciliation: {run.orders} orders, stages {run.timings}")
    assert run.orders == ORDERS
    assert run.timings["total"] < RECON_LIMIT_S