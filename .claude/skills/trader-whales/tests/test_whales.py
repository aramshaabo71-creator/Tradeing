#!/usr/bin/env python3
"""Offline tests for whales.py. No network: every SEC file comes from tests/fixtures
(made by tests/make_fixtures.py).

    python3 -m unittest discover -s .claude/skills/trader-whales/tests -v
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, SKILL)

import whales as W  # noqa: E402

TODAY = "2026-11-05"


def read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def txt(cik, acc):
    return read("Archives_edgar_data_%d_%s.txt" % (int(cik), acc))


def run(*argv):
    """Run the CLI offline. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    base = ["--offline", FIX, "--today", TODAY, "--filers", os.path.join(FIX, "filers.json"),
            "--config", os.path.join(FIX, "no-config.json"), "--cache", ""]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = W.main(list(argv) + base)
    return code, out.getvalue(), err.getvalue()


class ParseIndex(unittest.TestCase):
    def test_groups_lines_by_accession_and_keeps_wanted_forms(self):
        got = W.parse_form_index(read("Archives_edgar_daily-index_2026_QTR4_form.20261103.idx"),
                                 W.FORMS_OWN | W.FORMS_F4)
        self.assertNotIn("0002222222-26-000099", got)            # the 8-K is ignored
        r = got["0001111111-26-000010"]
        self.assertEqual(r["form"], "SCHEDULE 13D")
        self.assertEqual(r["filed"], "2026-11-03")
        self.assertEqual(sorted(r["ciks"]), ["0001111111", "0002222222"])
        self.assertEqual(got["0007777777-26-000030"]["form"], "SC 13G")
        self.assertEqual(got["0009000001-26-000001"]["form"], "4")


class ParseOwnership(unittest.TestCase):
    def test_xml_13d(self):
        r = W.parse_ownership(txt(2222222, "0001111111-26-000010"))
        self.assertEqual((r["kind"], r["amendment"]), ("13D", False))
        self.assertEqual(r["subject_cik"], "0002222222")
        self.assertEqual(r["subject"], "Acme Robotics Inc")
        self.assertEqual(r["percent"], 7.5)
        self.assertEqual(r["shares"], 3750000)
        self.assertEqual(r["event_date"], "2026-10-28")
        self.assertEqual(r["filers"], ["Activist Partners LP"])
        self.assertIn("engage with the board", r["purpose"])
        self.assertEqual(W.classify_ownership(r)["signal"], "NEW_5PCT")

    def test_xml_13g_amendment_under_five(self):
        r = W.parse_ownership(txt(2222222, "0003333333-26-000020"))
        self.assertEqual((r["kind"], r["amendment"], r["percent"]), ("13G", True, 4.2))
        self.assertTrue(r["five_or_less"])
        self.assertEqual(W.classify_ownership(r)["signal"], "BELOW_5")

    def test_old_text_13g(self):
        r = W.parse_ownership(txt(2222222, "0007777777-26-000030"))
        self.assertEqual(r["form"], "SC 13G")
        self.assertEqual(r["percent"], 6.1)
        self.assertEqual(r["shares"], 3050000)
        self.assertEqual(W.classify_ownership(r)["signal"], "NEW_5PCT")

    def test_amendment_against_prior(self):
        now = W.parse_ownership(txt(4444444, "0001111111-26-000011"))
        before = W.parse_ownership(txt(4444444, "0001111111-26-000005"))
        self.assertEqual(W.classify_ownership(now, before)["signal"], "INCREASED")
        self.assertEqual(now["prior_percent"], 9.0)
        before["percent"] = 15.0
        self.assertEqual(W.classify_ownership(now, before)["signal"], "DECREASED")
        self.assertEqual(W.classify_ownership(now, None)["signal"], "AMENDED")


class ParseForm4(unittest.TestCase):
    def test_open_market_buy(self):
        r = W.parse_form4(txt(2222222, "0009000001-26-000001"))
        self.assertEqual((r["issuer_cik"], r["ticker"]), ("0002222222", "ACME"))
        self.assertEqual(r["owners"], ["Ray Lena"])
        self.assertIn("Chief Executive Officer", r["roles"])
        self.assertIn("Director", r["roles"])
        t = r["trades"][0]
        self.assertEqual((t["code"], t["shares"], t["price"], t["value"]), ("P", 100000, 15.0, 1500000))
        rows, dropped = W.summarize_form4(r, 1_000_000, False)
        self.assertEqual(rows[0]["side"], "BUY")
        self.assertEqual(rows[0]["pct_of_holding"], 33.3)   # 100k on top of 300k
        self.assertEqual(rows[0]["lag_bdays"], 1)

    def test_minimum_amount(self):
        r = W.parse_form4(txt(2222222, "0009000002-26-000001"))   # $151k
        rows, dropped = W.summarize_form4(r, 1_000_000, False)
        self.assertEqual(rows, [])
        self.assertEqual(dropped, {"under the minimum": 1})
        rows, _ = W.summarize_form4(r, 100_000, False)
        self.assertEqual(len(rows), 1)

    def test_10b5_1_footnote_and_flag_are_filtered(self):
        foot = W.parse_form4(txt(4444444, "0009000004-26-000001"))
        self.assertTrue(foot["trades"][0]["plan"])
        flag = W.parse_form4(txt(6666666, "0009000009-26-000001"))
        self.assertTrue(flag["plan"])
        for r in (foot, flag):
            rows, dropped = W.summarize_form4(r, 0, False)
            self.assertEqual(rows, [])
            self.assertEqual(dropped, {"10b5-1 plan": 1})
            rows, _ = W.summarize_form4(r, 0, True)               # --include-plans
            self.assertEqual(rows[0]["side"], "SELL")

    def test_sale_on_exercise_day_is_filtered(self):
        r = W.parse_form4(txt(4444444, "0009000005-26-000001"))
        rows, dropped = W.summarize_form4(r, 0, False)
        self.assertEqual(rows, [])
        self.assertEqual(dropped["sale on an option exercise day"], 1)
        self.assertEqual(dropped["code M (not an open market trade)"], 1)
        self.assertEqual(dropped["code F (not an open market trade)"], 1)


class Clusters(unittest.TestCase):
    def buy(self, issuer, owner, date, value=100000.0, shares=1000):
        return {"issuer_cik": issuer, "issuer": issuer, "ticker": issuer, "owner": owner,
                "owner_key": owner, "date": date, "value": value, "shares": shares,
                "accession": "%s-%s-%s" % (issuer, owner, date)}

    def test_two_insiders_within_14_days(self):
        got = W.find_clusters([self.buy("X", "a", "2026-10-01"), self.buy("X", "b", "2026-10-15")])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["insiders"], 2)
        self.assertEqual((got[0]["from"], got[0]["to"]), ("2026-10-01", "2026-10-15"))

    def test_not_a_cluster(self):
        self.assertEqual(W.find_clusters([self.buy("X", "a", "2026-10-01"),
                                          self.buy("X", "b", "2026-10-16")]), [])  # 15 days
        self.assertEqual(W.find_clusters([self.buy("X", "a", "2026-10-01"),
                                          self.buy("X", "a", "2026-10-02")]), [])  # one person
        self.assertEqual(W.find_clusters([self.buy("X", "a", "2026-10-01"),
                                          self.buy("Y", "b", "2026-10-02")]), [])  # two companies
        self.assertEqual(W.find_clusters([self.buy("X", "a", "2026-10-01", 10),
                                          self.buy("X", "b", "2026-10-02", 10)],
                                         min_total=1000), [])

    def test_joint_filers_count_once(self):
        a = self.buy("X", "fund", "2026-10-01", 5e6, 1000)
        b = self.buy("X", "manager", "2026-10-01", 5e6, 1000)   # same trade, other filer
        merged = W.merge_joint_buys([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["owner"], "fund / manager")
        self.assertEqual(W.find_clusters(merged), [])


class Helpers(unittest.TestCase):
    def test_rate_limit_never_above_ten_a_second(self):
        self.assertAlmostEqual(W.RateLimiter(50).interval, 0.1)
        self.assertAlmostEqual(W.RateLimiter(8).interval, 0.125)

    def test_user_agent_rules(self):
        old = os.environ.pop("SEC_USER_AGENT", None)
        try:
            self.assertIsNone(W.sec_user_agent(None, "/nonexistent.json"))
            self.assertIsNone(W.sec_user_agent("Name you@example.com"))
            self.assertIsNone(W.sec_user_agent("no email here"))
            self.assertEqual(W.sec_user_agent("Ann Lee ann@lee.dev"), "Ann Lee ann@lee.dev")
            d = tempfile.mkdtemp()
            p = os.path.join(d, "config.json")
            with open(p, "w") as fh:
                json.dump({"sec_user_agent": "Bo Ek bo@ek.se"}, fh)
            self.assertEqual(W.sec_user_agent(None, p), "Bo Ek bo@ek.se")
            os.environ["SEC_USER_AGENT"] = "Env Var env@var.io"
            self.assertEqual(W.sec_user_agent(None, p), "Env Var env@var.io")
            shutil.rmtree(d)
        finally:
            os.environ.pop("SEC_USER_AGENT", None)
            if old is not None:
                os.environ["SEC_USER_AGENT"] = old

    def test_watchlist_formats(self):
        d = tempfile.mkdtemp()
        try:
            for name, body in (("a.json", '["aapl", "brk-b"]'), ("b.json", '{"tickers": ["AAPL"]}'),
                               ("c.txt", "AAPL  # apple\nBRK.B, msft\n")):
                with open(os.path.join(d, name), "w") as fh:
                    fh.write(body)
            self.assertEqual(W.tickers_from_watchlist(os.path.join(d, "a.json")), ["AAPL", "BRK.B"])
            self.assertEqual(W.tickers_from_watchlist(os.path.join(d, "b.json")), ["AAPL"])
            self.assertEqual(W.tickers_from_watchlist(os.path.join(d, "c.txt")),
                             ["AAPL", "BRK.B", "MSFT"])
        finally:
            shutil.rmtree(d)

    def test_business_days(self):
        d = W.parse_date
        self.assertEqual(W.business_days(d("2026-10-30"), d("2026-11-03")), 2)   # Fri -> Tue
        self.assertIsNone(W.business_days(None, d("2026-11-03")))

    def test_no_user_agent_refuses_to_call_sec(self):
        old = os.environ.pop("SEC_USER_AGENT", None)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = W.main(["scan", "--config", "/nonexistent.json"])
        finally:
            if old is not None:
                os.environ["SEC_USER_AGENT"] = old
        self.assertEqual(code, 2)
        self.assertIn("User-Agent", err.getvalue())


class Commands(unittest.TestCase):
    def test_scan_whole_market(self):
        code, out, _ = run("scan", "--json")
        self.assertEqual(code, 0)
        rep = json.loads(out)
        sig = {(r["subject"], r["form"]): r["signal"] for r in rep["ownership"]}
        self.assertEqual(sig[("Acme Robotics Inc", "SCHEDULE 13D")], "NEW_5PCT")
        self.assertEqual(sig[("Acme Robotics Inc", "SCHEDULE 13G/A")], "BELOW_5")
        self.assertEqual(sig[("Acme Robotics Inc", "SC 13G")], "NEW_5PCT")
        self.assertEqual(sig[("Beta Foods Corp", "SCHEDULE 13D/A")], "INCREASED")
        self.assertEqual(rep["ownership_hidden"], 1)          # Big Index's routine Beta 13G/A
        ins = [(r["side"], r["ticker"], r["value"]) for r in rep["insiders"]]
        self.assertEqual(ins, [("BUY", "ACME", 1500000.0), ("SELL", "BETA", 4000000.0)])
        self.assertEqual(len(rep["insiders"][0]["accessions"]), 2)   # joint filers merged
        self.assertEqual([c["ticker"] for c in rep["clusters"]], ["ACME"])
        self.assertEqual(rep["clusters"][0]["insiders"], 2)
        f = rep["funds"][0]
        self.assertEqual((f["filer"], f["period"], f["prior_period"]),
                         ("Whale Capital LP", "2026-09-30", "2026-06-30"))
        st = {c["issuer"]: c["status"] for c in f["changes"]}
        self.assertEqual(st, {"ACME ROBOTICS INC": "NEW", "BETA FOODS CORP": "ADDED",
                              "DELTA SHIPPING INC": "SOLD OUT"})   # Gamma +3 % is left out
        self.assertIn("Newest EDGAR daily index is for 2026-11-04", " ".join(rep["notes"]))

    def test_scan_text_says_how_late_and_never_orders(self):
        code, out, _ = run("scan")
        self.assertEqual(code, 0)
        self.assertIn("Not real time", out)
        self.assertIn("Form 4 is due 2 business days after the trade", out)
        self.assertIn("13F holdings are a snapshot", out)
        self.assertIn("13D is due 5 business days", out)
        self.assertIn("Unusual options and dark pool: not covered", out)
        self.assertIn("paid connector", out)
        self.assertIn("/trader-budget", out)
        self.assertIn("/trader-risk", out)
        self.assertIn("/trader-broker", out)
        self.assertIn("lag 1 bd", out)

    def test_scan_min_amount_and_scope(self):
        code, out, _ = run("scan", "--json", "--min", "100000", "--tickers", "ACME")
        rep = json.loads(out)
        self.assertEqual({r["ticker"] for r in rep["insiders"]}, {"ACME"})
        self.assertEqual(len(rep["insiders"]), 2)              # the $151k buy now counts
        self.assertEqual({r["subject"] for r in rep["ownership"]}, {"Acme Robotics Inc"})
        self.assertEqual([c["issuer"] for c in rep["funds"][0]["changes"]], ["ACME ROBOTICS INC"])

    def test_scan_holdings_from_targets(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "targets.json")
            with open(p, "w") as fh:
                json.dump({"weights": [{"ticker": "BETA", "weight": 100}]}, fh)
            code, out, _ = run("scan", "--json", "--holdings", "--targets", p,
                               "--portfolio", os.path.join(d, "none.json"), "--signals", "13dg,form4")
            rep = json.loads(out)
            self.assertIn("your holdings: BETA", rep["scope"])
            self.assertEqual({r["subject"] for r in rep["ownership"]}, {"Beta Foods Corp"})
            # in a narrow scope every amendment is shown, routine or not
            self.assertEqual(len(rep["ownership"]), 2)
            self.assertEqual([r["ticker"] for r in rep["insiders"]], ["BETA"])
            self.assertEqual(rep["funds"], [])
        finally:
            shutil.rmtree(d)

    def test_ticker(self):
        code, out, _ = run("ticker", "BETA", "--json", "--days", "120")
        self.assertEqual(code, 0)
        rep = json.loads(out)
        self.assertIn("Beta Foods Corp", rep["title"])
        sig = sorted((r["form"], r["signal"]) for r in rep["ownership"])
        # the old 13D (2026-09-01) is in the window, so the amendment is compared to it
        self.assertIn(("SCHEDULE 13D/A", "INCREASED"), sig)
        self.assertIn(("SCHEDULE 13D", "NEW_5PCT"), sig)
        self.assertEqual([r["side"] for r in rep["insiders"]], ["SELL"])

    def test_ticker_unknown(self):
        code, _, err = run("ticker", "ZZZZ")
        self.assertEqual(code, 2)
        self.assertIn("not in SEC's ticker list", err)

    def test_filer(self):
        code, out, _ = run("filer", "jane", "whale", "--json")
        self.assertEqual(code, 0)
        rep = json.loads(out)
        self.assertEqual(rep["funds"][0]["filer"], "Whale Capital LP")
        self.assertEqual(len(rep["funds"][0]["changes"]), 3)

    def test_filer_activist_13d_history(self):
        code, out, _ = run("filer", "activist", "--json", "--days", "120")
        rep = json.loads(out)
        forms = sorted(r["form"] for r in rep["ownership"])
        self.assertEqual(forms, ["SCHEDULE 13D", "SCHEDULE 13D", "SCHEDULE 13D/A"])
        self.assertTrue(any(n.startswith("13F") for n in rep["notes"]))   # only one 13F-HR

    def test_filer_unknown(self):
        code, _, err = run("filer", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("no investor matched", err)

    def test_watch_only_shows_new(self):
        d = tempfile.mkdtemp()
        try:
            state = os.path.join(d, "whales.json")
            code, out, _ = run("watch", "--json", "--state", state)
            self.assertEqual(code, 0)
            first = json.loads(out)
            self.assertEqual(len(first["insiders"]), 2)
            self.assertEqual(len(first["funds"]), 1)
            with open(state) as fh:
                st = json.load(fh)
            self.assertEqual(st["last_index_day"], "2026-11-04")
            self.assertIn("0009000001-26-000001", st["seen"])
            self.assertTrue(st["buys"])
            self.assertIn("0005555555-26-000002", st["seen_13f"])
            code, out, _ = run("watch", "--json", "--state", state)
            self.assertEqual(code, 1)                           # nothing new
            again = json.loads(out)
            self.assertEqual(again["ownership"] + again["insiders"] + again["clusters"], [])
            self.assertEqual(again["funds"], [])
        finally:
            shutil.rmtree(d)

    def test_watch_dry_run_leaves_state_alone(self):
        d = tempfile.mkdtemp()
        try:
            state = os.path.join(d, "whales.json")
            run("watch", "--state", state, "--dry-run")
            self.assertFalse(os.path.exists(state))
        finally:
            shutil.rmtree(d)

    def test_cache_skips_second_download(self):
        d = tempfile.mkdtemp()
        try:
            cache = os.path.join(d, "cache.json")
            argv = ["scan", "--offline", FIX, "--today", TODAY, "--cache", cache,
                    "--filers", os.path.join(FIX, "filers.json"), "--signals", "13dg,form4",
                    "--config", "/nonexistent.json", "--json"]
            out1 = io.StringIO()
            with contextlib.redirect_stdout(out1):
                W.main(argv)
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                W.main(argv)
            a, b = json.loads(out1.getvalue()), json.loads(out2.getvalue())
            self.assertLess(b["sec_calls"], a["sec_calls"])
            self.assertEqual(a["insiders"], b["insiders"])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
