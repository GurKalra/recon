#!/usr/bin/env python
"""A quick end-to-end check of a RUNNING install, using only the standard library.

    BASE_URL=http://localhost:8080 DEMO_EXEC_PASSWORD=... DEMO_COFOUNDER_PASSWORD=... \
        python scripts/smoke.py

It signs in as both roles and checks what each may and may not do. It changes nothing that
matters: the one claim it creates is a draft that it discards again. The workspace should already
hold data (use "Load demo data" first). Exits 1 if anything fails.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
FAILED: list[str] = []


class Session:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def call(self, method: str, path: str, body: object = None) -> tuple[int, bytes, dict]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if method != "GET" and self.csrf:
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            with self.http.open(req, timeout=120) as res:
                return res.status, res.read(), {k.lower(): v for k, v in res.headers.items()}
        except urllib.error.HTTPError as err:
            return err.code, err.read(), {k.lower(): v for k, v in err.headers.items()}

    def json(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        status, raw, _ = self.call(method, path, body)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def sign_in(self, email: str, password: str) -> bool:
        status, out = self.json("POST", "/api/auth/login", {"email": email, "password": password})
        if status == 200 and isinstance(out, dict):
            self.csrf = out["csrf_token"]
        return status == 200


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


def main() -> int:
    exec_pw = os.environ.get("DEMO_EXEC_PASSWORD", "")
    cofounder_pw = os.environ.get("DEMO_COFOUNDER_PASSWORD", "")
    if not exec_pw or not cofounder_pw:
        print("Set DEMO_EXEC_PASSWORD and DEMO_COFOUNDER_PASSWORD.")
        return 2
    print(f"Checking {BASE}")
    anon = Session()
    status, out = anon.json("GET", "/api/health")
    check("the API answers and the database is reachable",
          status == 200 and out.get("db") is True)
    check("nothing is readable without signing in", anon.json("GET", "/api/orders")[0] == 401)

    ex = Session()
    check("the finance executive can sign in", ex.sign_in("exec@isolabs.demo", exec_pw))
    status, dash = ex.json("GET", "/api/dashboard")
    check("the overview loads", status == 200 and isinstance(dash, dict))
    status, orders = ex.json("GET", "/api/orders?limit=5")
    check("the orders table has data", status == 200 and orders.get("total", 0) > 0,
          "load the demo data first")
    status, raw, headers = ex.call("GET", "/api/orders/export.csv?payment_mode=cod")
    is_csv = "text/csv" in headers.get("content-type", "")
    check("the orders export is a CSV download", status == 200 and is_csv and b"order_ref" in raw)
    check("rate cards load", ex.json("GET", "/api/rate-cards")[0] == 200)
    check("the audit trail is closed to the executive", ex.json("GET", "/api/audit")[0] == 403)
    started = time.perf_counter()
    status, run = ex.json("POST", "/api/recon/run")
    check("the reconciliation runs", status == 200, str(run)[:200])
    print(f"        reconciliation took {time.perf_counter() - started:.1f}s")

    status, cands = ex.json("GET", "/api/claims/candidates?limit=50")
    rows = cands.get("rows", cands) if isinstance(cands, dict) else cands
    if status == 200 and rows:
        pid = rows[0]["partner_id"]
        ids = [r.get("finding_id", r.get("id")) for r in rows if r["partner_id"] == pid][:1]
        status, made = ex.json("POST", "/api/claims", {"partner_id": pid, "finding_ids": ids})
        check("a draft claim can be created", status in (200, 201), str(made)[:200])
        if status in (200, 201):
            gone = ex.json("DELETE", f"/api/claims/{made['id']}")[0]
            check("the draft can be discarded", gone in (200, 204))
    else:
        check("there are findings to claim", False, "load the demo data first")

    co = Session()
    check("the co-founder can sign in", co.sign_in("cofounder@isolabs.demo", cofounder_pw))
    check("the audit trail opens for the co-founder",
          co.json("GET", "/api/audit?limit=5")[0] == 200)
    check("the co-founder cannot run the reconciliation",
          co.json("POST", "/api/recon/run")[0] == 403)
    check("a wrong password is refused",
          not Session().sign_in("exec@isolabs.demo", "definitely-wrong-password"))

    print("\nAll checks passed." if not FAILED else f"\n{len(FAILED)} check(s) failed.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())