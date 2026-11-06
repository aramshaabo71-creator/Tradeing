#!/usr/bin/env python3
"""Whale tracker for trader-whales. Standard library only.

Spots when big holders step into or out of US stocks, from SEC EDGAR:

  1. Schedule 13D / 13G and amendments (SC 13D, SC 13D/A, SC 13G, SC 13G/A and
     the XML forms SCHEDULE 13D, 13D/A, 13G, 13G/A used since December 2024):
     who crossed 5 %, who added, who cut, who fell under 5 %.
  2. Form 4 insider trades: open market buys (code P) and sales (code S),
     with option exercises, 10b5-1 plan trades and small trades filtered out,
     and cluster buys (two or more insiders buying one company within 14 days).
  3. 13F-HR: the two latest quarters of the funds in
     ../trader-copycat/filers.json compared: new positions, positions sold
     out, and changes of more than 50 %.

    python3 whales.py scan                        # the last 7 days, whole market
    python3 whales.py scan --days 3 --holdings    # only the stocks in trader/targets.json
    python3 whales.py scan --tickers NVDA TSLA --min 250000
    python3 whales.py ticker AAPL                 # everything about one company (90 days)
    python3 whales.py filer ackman                # one investor: 13D/G, Form 4, 13F diff
    python3 whales.py watch --holdings            # only what is new since the last run

Nothing here is real time. Every section prints how late its data is:
Form 4 is filed up to 2 business days after the trade, 13D up to 5 business
days after crossing 5 %, 13G up to 45 days after the quarter ends, 13F up to
45 days after the quarter ends. The scan reads EDGAR's daily form index,
which is published after each business day closes, so filings made today
show up tomorrow (the ticker and filer commands read the live submissions
list and include today's filings).

Unusual options activity and dark pool prints are NOT covered: there is no
free source. They need a paid connector (for example Quant Data in Claude).
This tool never scrapes them.

SEC wants a User-Agent with your name and email on every request
(https://www.sec.gov/os/accessing-edgar-data). It is read from --user-agent,
then SEC_USER_AGENT, then "sec_user_agent" in trader/config.json. Requests
are rate limited to at most 10 a second (default 8).

State: trader/state/whales.json (what watch has shown, recent buys for
clusters, last run) and trader/state/whales_cache.json (parsed filings, so a
second run does not download them again). Offline: --offline DIR reads every
URL from DIR/<url path with / replaced by _>. The tests use it.

Education and tooling, not advice. This tool never places an order.
"""

import argparse
import concurrent.futures
import datetime as _dt
import gzip
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
COPYCAT = os.path.join(os.path.dirname(HERE), "trader-copycat")
FILERS = os.path.join(COPYCAT, "filers.json")

DAILY_DIR = "https://www.sec.gov/Archives/edgar/daily-index/%d/QTR%d/index.json"
DAILY_FORM = "https://www.sec.gov/Archives/edgar/daily-index/%d/QTR%d/form.%s.idx"
FULL_TXT = "https://www.sec.gov/Archives/edgar/data/%d/%s.txt"
FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/%d/%s/"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK%s.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
PLACEHOLDER = "you@example.com"

FORMS_13D = {"SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A"}
FORMS_13G = {"SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}
FORMS_OWN = FORMS_13D | FORMS_13G
FORMS_F4 = {"4"}
EXERCISE_CODES = {"M", "X", "C", "O"}
CLUSTER_DAYS = 14
KEEP_DAYS = 45
MAX_RATE = 10.0

DELAYS = {
    "13dg": ("Delay: 13D is due 5 business days after crossing 5 % and 13D/A 2 business "
             "days after a material change; 13G is due 5 business days (passive "
             "investors) to 45 days after the quarter ends (institutions); 13G/A 45 days "
             "after the quarter ends. The scan sees a filing the day after it is filed."),
    "form4": ("Delay: Form 4 is due 2 business days after the trade (the lag column "
              "shows it). The scan sees a filing the day after it is filed."),
    "13f": ("Delay: 13F holdings are a snapshot of the last day of the quarter, filed up "
            "to 45 days later, so they can be 4.5 months old. Long US positions only: "
            "no shorts, no trades inside the quarter."),
    "flow": ("Unusual options and dark pool: not covered. There is no free source; it "
             "needs a paid connector (for example Quant Data in Claude). Skipped."),
}
NEXT_STEP = ("Nothing here is an order. To act on an idea: /trader-budget sizes it, "
             "/trader-risk checks it, /trader-broker places it only after your explicit yes.")

UA_HELP = (
    "SEC requires a User-Agent with your name and email on every request "
    "(https://www.sec.gov/os/accessing-edgar-data). Set it once, any of these:\n"
    "  1. add  \"sec_user_agent\": \"Your Name you@example.com\"  to trader/config.json\n"
    "  2. export SEC_USER_AGENT=\"Your Name you@example.com\"\n"
    "  3. pass --user-agent \"Your Name you@example.com\"")


class WhaleError(Exception):
    pass


class NotFound(WhaleError):
    pass


# ---------------------------------------------------------------- inputs

def load_json(path, default=None):
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default
    return default


def save_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def sec_user_agent(cli=None, config="trader/config.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua:
        cfg = load_json(config, {}) or {}
        ua = str(cfg.get("sec_user_agent") or "").strip()
    if not ua or "@" not in ua or PLACEHOLDER in ua:
        return None
    return ua


def norm_ticker(t):
    return (t or "").strip().upper().replace("-", ".").replace("/", ".")


def tickers_from_files(targets="trader/targets.json", portfolio="trader/portfolio.json"):
    """Your holdings: trader/portfolio.json positions plus trader/targets.json weights."""
    out = []
    for p in (load_json(portfolio, {}) or {}).get("positions", []):
        if p.get("ticker"):
            out.append(norm_ticker(p["ticker"]))
    for w in (load_json(targets, {}) or {}).get("weights", []):
        if w.get("ticker"):
            out.append(norm_ticker(w["ticker"]))
        for c in w.get("classes", []) or []:
            out.append(norm_ticker(c))
    return sorted(set(out))


def tickers_from_watchlist(path):
    """A watchlist file: JSON list, {"tickers": [...]}, targets-shaped JSON, or text."""
    if not os.path.isfile(path):
        raise WhaleError("watchlist %s not found" % path)
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    out = []
    if isinstance(data, list):
        out = [x if isinstance(x, str) else x.get("ticker", "") for x in data]
    elif isinstance(data, dict):
        if "tickers" in data:
            out = list(data["tickers"])
        for key in ("weights", "positions"):
            out += [w.get("ticker", "") for w in data.get(key, [])]
    else:
        for line in raw.splitlines():
            line = line.split("#", 1)[0]
            out += re.split(r"[\s,;]+", line)
    return sorted({norm_ticker(t) for t in out if t and t.strip()})


# ---------------------------------------------------------------- fetching

class RateLimiter(object):
    """At most `rate` calls a second across every thread."""

    def __init__(self, rate):
        self.interval = 1.0 / min(max(rate, 0.1), MAX_RATE)
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class Fetcher(object):
    """GETs SEC URLs with the User-Agent, gzip, a rate limit and retries.

    Offline mode reads DIR/<url path with "/" replaced by "_">."""

    def __init__(self, ua=None, offline=None, rate=8.0):
        self.ua = ua
        self.offline = offline
        self.limiter = RateLimiter(rate)
        self.calls = 0
        self._lock = threading.Lock()
        self._memo = {}

    @staticmethod
    def offline_name(url):
        path = urllib.parse.urlparse(url).path.strip("/")
        return path.replace("/", "_") or "index"

    def get(self, url, sec=True):
        with self._lock:
            self.calls += 1
        if self.offline:
            path = os.path.join(self.offline, self.offline_name(url))
            if not os.path.isfile(path):
                raise NotFound("offline: no file %s for %s" % (path, url))
            with open(path, "rb") as fh:
                return fh.read()
        if not self.ua:
            raise WhaleError(UA_HELP)
        req = urllib.request.Request(url, headers={"User-Agent": self.ua,
                                                   "Accept-Encoding": "gzip"})
        for attempt in range(4):
            self.limiter.wait()
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise NotFound("404 for %s" % url)
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                if exc.code == 403:
                    raise WhaleError("SEC said 403 Forbidden for %s. Usually a missing or "
                                     "generic User-Agent, or too many requests.\n%s"
                                     % (url, UA_HELP))
                raise WhaleError("HTTP %s for %s" % (exc.code, url))
            except (urllib.error.URLError, OSError) as exc:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise WhaleError("could not reach %s: %s" % (url, getattr(exc, "reason", exc)))
        raise WhaleError("gave up on %s" % url)

    def get_json(self, url, sec=True):
        memo = "/submissions/" in url or url.endswith("company_tickers.json")
        if memo and url in self._memo:
            return self._memo[url]
        try:
            data = json.loads(self.get(url, sec=sec).decode("utf-8"))
        except ValueError as exc:
            raise WhaleError("%s did not return JSON: %s" % (url, exc))
        if memo:
            self._memo[url] = data
        return data


def cik10(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(10) if digits else ""


class Tickers(object):
    """SEC's company_tickers.json both ways."""

    def __init__(self, data=None):
        self.by_ticker, self.by_cik, self.title, self.raw = {}, {}, {}, []
        items = data.values() if isinstance(data, dict) else (data or [])
        for v in items:
            c, t = cik10(v["cik_str"]), norm_ticker(v["ticker"])
            self.raw.append((c, v["ticker"], v.get("title", "")))
            self.by_ticker.setdefault(t, c)
            self.by_cik.setdefault(c, t)
            self.title.setdefault(c, v.get("title", ""))

    @classmethod
    def load(cls, fetcher):
        try:
            return cls(fetcher.get_json(TICKERS_URL))
        except WhaleError:
            return cls({})

    def cik(self, ticker):
        return self.by_ticker.get(norm_ticker(ticker))

    def ticker(self, cik):
        return self.by_cik.get(cik10(cik))


# ---------------------------------------------------------------- dates

def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return _dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def iso(d):
    return d.isoformat() if d else ""


def business_days(a, b):
    """Weekdays from a to b (a excluded), ignoring holidays. None if unknown."""
    if not a or not b:
        return None
    if b < a:
        return -business_days(b, a)
    n, d = 0, a
    while d < b:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def quarter(d):
    return (d.month - 1) // 3 + 1


# ---------------------------------------------------------------- the daily index

IDX_LINE = re.compile(r"^(?P<form>\S.*?)\s{2,}(?P<name>.*?)\s+(?P<cik>\d{1,10})\s+"
                      r"(?P<date>\d{8})\s+(?P<file>edgar/data/\S+)\s*$")


def parse_form_index(text, forms):
    """form.YYYYMMDD.idx -> {accession: {form, filed, ciks, names, path_cik}}."""
    out = {}
    for line in text.splitlines():
        m = IDX_LINE.match(line.rstrip())
        if not m:
            continue
        form = re.sub(r"\s+", " ", m.group("form").strip())
        if form not in forms:
            continue
        acc = os.path.basename(m.group("file")).replace(".txt", "")
        rec = out.setdefault(acc, {"accession": acc, "form": form, "ciks": [], "names": [],
                                   "filed": iso(parse_date(m.group("date"))),
                                   "path_cik": int(m.group("cik"))})
        c = cik10(m.group("cik"))
        if c not in rec["ciks"]:
            rec["ciks"].append(c)
            rec["names"].append(m.group("name").strip())
    return out


def daily_index(fetcher, start, end, forms, notes):
    """Every filing of `forms` filed from start to end, from the daily form indexes."""
    found = {}
    qs = sorted({(d.year, quarter(d)) for d in
                 (start + _dt.timedelta(days=i) for i in range((end - start).days + 1))})
    have = set()
    for y, q in qs:
        try:
            listing = fetcher.get_json(DAILY_DIR % (y, q))
            for it in listing.get("directory", {}).get("item", []):
                m = re.match(r"form\.(\d{8})\.idx$", it.get("name", ""))
                if m:
                    have.add(m.group(1))
        except NotFound:
            continue
    days = []
    d = start
    while d <= end:
        key = d.strftime("%Y%m%d")
        if key in have:
            days.append(d)
        d += _dt.timedelta(days=1)
    for d in days:
        key = d.strftime("%Y%m%d")
        try:
            text = fetcher.get(DAILY_FORM % (d.year, quarter(d), key)).decode("latin-1")
        except NotFound:
            continue
        found.update(parse_form_index(text, forms))
    last = max(days) if days else None
    if not days:
        notes.append("No EDGAR daily index between %s and %s yet." % (iso(start), iso(end)))
    elif last < end:
        notes.append("Newest EDGAR daily index is for %s; filings made after that are "
                     "not in this scan yet (use the ticker command for today's)." % iso(last))
    return found, [iso(x) for x in days]


# ---------------------------------------------------------------- full submission parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _first(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return c
    return None


def _text(el, name, default=""):
    c = _first(el, name) if el is not None else None
    if c is None:
        return default
    v = c.find("./{*}value")
    if v is None:
        v = next((x for x in c if _local(x.tag) == "value"), None)
    t = (v.text if v is not None else c.text) or ""
    return t.strip() or default


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def parse_header(txt):
    """The SGML header of a full submission .txt."""
    head = txt.split("</SEC-HEADER>", 1)[0]
    out = {"form": "", "filed": "", "subject": [], "filer": [], "issuer": [], "owner": []}
    m = re.search(r"CONFORMED SUBMISSION TYPE:\s*(.+)", head)
    if m:
        out["form"] = m.group(1).strip()
    m = re.search(r"FILED AS OF DATE:\s*(\d{8})", head)
    if m:
        out["filed"] = iso(parse_date(m.group(1)))
    m = re.search(r"ACCESSION NUMBER:\s*(\S+)", head)
    if m:
        out["accession"] = m.group(1).strip()
    blocks = re.split(r"\n(SUBJECT COMPANY|FILED BY|ISSUER|REPORTING-OWNER):", head)
    roles = {"SUBJECT COMPANY": "subject", "FILED BY": "filer", "ISSUER": "issuer",
             "REPORTING-OWNER": "owner"}
    for i in range(1, len(blocks) - 1, 2):
        body = blocks[i + 1]
        name = re.search(r"COMPANY CONFORMED NAME:\s*(.+)", body)
        cik = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", body)
        if cik:
            out[roles[blocks[i]]].append({"cik": cik10(cik.group(1)),
                                          "name": name.group(1).strip() if name else ""})
    return out


def xml_block(txt):
    m = re.search(r"<XML>\s*(.*?)\s*</XML>", txt, re.S)
    if not m:
        return None
    try:
        return ET.fromstring(m.group(1).strip().encode("utf-8"))
    except ET.ParseError:
        return None


def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s))


PCT_TEXT = re.compile(r"PERCENT\s+OF\s+CLASS\s+REPRESENTED\s+BY\s+AMOUNT\s+IN\s+ROW\s*"
                      r"\(?\s*(?:9|11)\s*\)?\s*:?\s*(?:\(\d+\)\s*)?([0-9]{1,3}(?:\.[0-9]+)?)\s*%",
                      re.I)
AMOUNT_TEXT = re.compile(r"AGGREGATE\s+AMOUNT\s+BENEFICIALLY\s+OWNED\s+BY\s+EACH\s+REPORTING"
                         r"\s+PERSON\s*:?\s*([0-9][0-9,]*)", re.I)


def parse_ownership(txt, url=""):
    """A Schedule 13D or 13G (XML since Dec 2024, or the old text form)."""
    h = parse_header(txt)
    form = h["form"]
    rec = {"type": "13dg", "form": form, "accession": h.get("accession", ""),
           "filed": h["filed"], "url": url, "amendment": form.endswith("/A"),
           "kind": "13D" if "13D" in form else "13G",
           "subject_cik": h["subject"][0]["cik"] if h["subject"] else "",
           "subject": h["subject"][0]["name"] if h["subject"] else "",
           "filer_ciks": [f["cik"] for f in h["filer"]],
           "filers": [f["name"] for f in h["filer"]],
           "percent": None, "shares": None, "event_date": "", "five_or_less": False,
           "cusip": "", "amendment_no": None, "class": "", "purpose": ""}
    root = xml_block(txt)
    if root is not None:
        issuer_cik = _text(root, "issuerCIK") or _text(root, "issuerCik")
        if issuer_cik:
            rec["subject_cik"] = cik10(issuer_cik)
        rec["subject"] = _text(root, "issuerName", rec["subject"])
        rec["cusip"] = _text(root, "issuerCusipNumber")
        rec["class"] = _text(root, "securitiesClassTitle")
        ev = _text(root, "dateOfEvent") or _text(root, "eventDateRequiresFilingThisStatement")
        rec["event_date"] = iso(parse_date(ev))
        rec["amendment_no"] = _text(root, "amendmentNo") or None
        pcts, amts, names = [], [], []
        for el in root.iter():
            n = _local(el.tag)
            if n in ("reportingPersonInfo", "coverPageHeaderReportingPersonDetails"):
                p = _num(_text(el, "percentOfClass") or _text(el, "classPercent"))
                a = _num(_text(el, "aggregateAmountOwned") or
                         _text(el, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares"))
                nm = _text(el, "reportingPersonName")
                if p is not None:
                    pcts.append(p)
                if a is not None:
                    amts.append(a)
                if nm and nm not in names:
                    names.append(nm)
        if pcts:
            rec["percent"] = max(pcts)
        if amts:
            rec["shares"] = max(amts)
        if names:
            rec["filers"] = names
        if _text(root, "classOwnership5PercentOrLess").upper() == "Y":
            rec["five_or_less"] = True
        purpose = _text(root, "transactionPurpose")
        if purpose:
            rec["purpose"] = re.sub(r"\s+", " ", purpose)[:400]
    else:
        body = strip_html(txt.split("</SEC-HEADER>", 1)[-1])
        pcts = [_num(x) for x in PCT_TEXT.findall(body)]
        pcts = [p for p in pcts if p is not None and p <= 100]
        if pcts:
            rec["percent"] = max(pcts)
        amts = [_num(x) for x in AMOUNT_TEXT.findall(body)]
        amts = [a for a in amts if a is not None]
        if amts:
            rec["shares"] = max(amts)
        if re.search(r"ownership of (?:5|five) percent or less of a class[^.]{0,80}\[\s*[xX]\s*\]",
                     body):
            rec["five_or_less"] = True
    if rec["percent"] is not None and rec["percent"] < 5:
        rec["five_or_less"] = True
    return rec


def parse_form4(txt, url=""):
    """A Form 4: the issuer, the owners and every non-derivative trade."""
    h = parse_header(txt)
    rec = {"type": "form4", "form": h["form"], "accession": h.get("accession", ""),
           "filed": h["filed"], "url": url, "issuer_cik": "", "issuer": "", "ticker": "",
           "owners": [], "owner_ciks": [], "roles": [], "plan": False, "trades": []}
    root = xml_block(txt)
    if root is None:
        rec["error"] = "no XML in this Form 4"
        return rec
    rec["issuer_cik"] = cik10(_text(root, "issuerCik"))
    rec["issuer"] = _text(root, "issuerName")
    sym = _text(root, "issuerTradingSymbol")
    rec["ticker"] = "" if sym.upper() in ("", "N/A", "NONE", "NA") else norm_ticker(sym)
    roles = []
    for ow in root.iter():
        if _local(ow.tag) != "reportingOwner":
            continue
        rec["owners"].append(_text(ow, "rptOwnerName"))
        rec["owner_ciks"].append(cik10(_text(ow, "rptOwnerCik")))
        rel = _first(ow, "reportingOwnerRelationship")
        if rel is None:
            continue
        flag = lambda n: (_text(rel, n).lower() in ("1", "true"))
        title = _text(rel, "officerTitle")
        if flag("isOfficer"):
            roles.append(title or "Officer")
        if flag("isDirector"):
            roles.append("Director")
        if flag("isTenPercentOwner"):
            roles.append("10% owner")
        if flag("isOther"):
            roles.append(_text(rel, "otherText") or "Other")
    seen = []
    for r in roles:
        if r not in seen:
            seen.append(r)
    rec["roles"] = seen
    rec["plan"] = _text(root, "aff10b5One").lower() in ("1", "true")
    notes = {}
    for fn in root.iter():
        if _local(fn.tag) == "footnote":
            notes[fn.get("id")] = re.sub(r"\s+", " ", "".join(fn.itertext()))
    for tx in root.iter():
        if _local(tx.tag) != "nonDerivativeTransaction":
            continue
        ids = [f.get("id") for f in tx.iter() if _local(f.tag) == "footnoteId"]
        shares = _num(_text(_first(tx, "transactionShares"), "transactionShares"))
        price = _num(_text(_first(tx, "transactionPricePerShare"), "transactionPricePerShare"))
        trade = {
            "date": iso(parse_date(_text(tx, "transactionDate"))),
            "code": _text(tx, "transactionCode").upper(),
            "security": _text(_first(tx, "securityTitle"), "securityTitle"),
            "shares": shares, "price": price,
            "value": round(shares * price, 2) if shares and price else None,
            "ad": _text(tx, "transactionAcquiredDisposedCode").upper(),
            "after": _num(_text(_first(tx, "sharesOwnedFollowingTransaction"),
                                "sharesOwnedFollowingTransaction")),
            "direct": _text(tx, "directOrIndirectOwnership").upper(),
            "plan": any("10b5-1" in notes.get(i, "") for i in ids),
        }
        rec["trades"].append(trade)
    return rec


# ---------------------------------------------------------------- loading filings

class Store(object):
    """Fetch-and-parse with a cache of parsed filings."""

    def __init__(self, fetcher, cache_path=None, workers=4, max_fetch=0, progress=True):
        self.fetcher = fetcher
        self.cache_path = cache_path
        self.cache = (load_json(cache_path, {}) or {}) if cache_path else {}
        self.workers = workers
        self.max_fetch = max_fetch
        self.progress = progress
        self.skipped = 0
        self.errors = []

    def save(self, today):
        if not self.cache_path:
            return
        cutoff = iso(today - _dt.timedelta(days=KEEP_DAYS + 400))
        self.cache = {k: v for k, v in self.cache.items() if v.get("filed", "9") >= cutoff}
        save_json(self.cache_path, self.cache)

    def _one(self, item):
        acc, path_cik, kind = item
        url = FULL_TXT % (path_cik, acc)
        txt = self.fetcher.get(url).decode("utf-8", "replace")
        link = FILING_INDEX % (path_cik, acc.replace("-", ""))
        return parse_ownership(txt, link) if kind == "13dg" else parse_form4(txt, link)

    def load(self, items):
        """items: [(accession, cik for the path, "13dg" or "form4")] -> parsed records."""
        out, todo = [], []
        for it in items:
            hit = self.cache.get(it[0])
            if hit and hit.get("type") == it[2]:
                out.append(hit)
            else:
                todo.append(it)
        if self.max_fetch and len(todo) > self.max_fetch:
            self.skipped += len(todo) - self.max_fetch
            todo = todo[:self.max_fetch]
        if len(todo) > 20 and self.progress:
            print("whales: reading %d filings from SEC (about %d s at the rate limit)..."
                  % (len(todo), int(len(todo) * self.fetcher.limiter.interval) + 1),
                  file=sys.stderr)
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
            futs = {ex.submit(self._one, it): it for it in todo}
            for fut in concurrent.futures.as_completed(futs):
                it = futs[fut]
                done += 1
                try:
                    rec = fut.result()
                except WhaleError as exc:
                    self.errors.append("%s: %s" % (it[0], exc))
                    continue
                rec["accession"] = rec.get("accession") or it[0]
                self.cache[it[0]] = rec
                out.append(rec)
                if self.progress and done % 250 == 0:
                    print("whales: %d/%d" % (done, len(todo)), file=sys.stderr)
        order = {it[0]: i for i, it in enumerate(items)}
        out.sort(key=lambda r: order.get(r.get("accession"), len(order)))
        return out


# ---------------------------------------------------------------- 13D / 13G signals

def subject_key(rec):
    return rec.get("subject_cik") or rec.get("subject", "").upper()


def same_filer(a, b):
    sa = set(a.get("filer_ciks") or []) | {n.upper() for n in a.get("filers") or []}
    sb = set(b.get("filer_ciks") or []) | {n.upper() for n in b.get("filers") or []}
    return bool(sa & sb)


def find_prior(store, rec, subs_cache, max_tries=4):
    """The same filer's previous 13D/13G on the same company, from the company's
    submissions list. Returns a parsed record or None."""
    cik = rec.get("subject_cik")
    if not cik:
        return None
    if cik not in subs_cache:
        try:
            subs_cache[cik] = store.fetcher.get_json(SUBMISSIONS % cik)
        except WhaleError:
            subs_cache[cik] = {}
    r = (subs_cache[cik] or {}).get("filings", {}).get("recent", {})
    cands = []
    for i, form in enumerate(r.get("form", [])):
        if form in FORMS_OWN and r["accessionNumber"][i] != rec["accession"] \
                and r["filingDate"][i] <= rec["filed"]:
            cands.append((r["filingDate"][i], r["accessionNumber"][i]))
    cands.sort(reverse=True)
    family = "13D" if rec["kind"] == "13D" else None
    tried = 0
    for _, acc in cands:
        if tried >= max_tries:
            break
        got = store.load([(acc, int(cik), "13dg")])
        tried += 1
        if got and same_filer(got[0], rec) and (family is None or got[0]["kind"] in ("13D", "13G")):
            return got[0]
    return None


def classify_ownership(rec, prior=None):
    """NEW_5PCT, INCREASED, DECREASED, BELOW_5, UNCHANGED or AMENDED."""
    pct = rec.get("percent")
    if rec.get("five_or_less") or (pct is not None and pct < 5):
        sig = "BELOW_5" if rec.get("amendment") else "UNDER_5"
    elif not rec.get("amendment"):
        sig = "NEW_5PCT"
    elif prior is not None and prior.get("percent") is not None and pct is not None:
        d = round(pct - prior["percent"], 2)
        sig = "INCREASED" if d > 0 else "DECREASED" if d < 0 else "UNCHANGED"
    else:
        sig = "AMENDED"
    rec["signal"] = sig
    if prior is not None:
        rec["prior_percent"] = prior.get("percent")
        rec["prior_filed"] = prior.get("filed")
        rec["prior_shares"] = prior.get("shares")
    return rec


SIGNAL_TEXT = {
    "NEW_5PCT": "crossed 5 %", "INCREASED": "increased", "DECREASED": "decreased",
    "BELOW_5": "fell to 5 % or less", "UNDER_5": "reports under 5 %",
    "UNCHANGED": "unchanged", "AMENDED": "amended",
}
OWNERSHIP_GROUPS = [
    ("Crossed 5 %: new 13D (activist) or 13G (passive)", {"NEW_5PCT"}),
    ("Increased", {"INCREASED"}),
    ("Decreased", {"DECREASED"}),
    ("Fell to 5 % or less (exit)", {"BELOW_5", "UNDER_5"}),
    ("Other 13D amendments (earlier filing not found, open it to see the change)",
     {"AMENDED", "UNCHANGED"}),
]
SIGNAL_RANK = {"NEW_5PCT": 0, "BELOW_5": 1, "INCREASED": 2, "DECREASED": 3,
               "UNDER_5": 4, "AMENDED": 5, "UNCHANGED": 6}


def notable_ownership(rec):
    """What a scan shows by default: every 13D, every new 13G, and 13G/A only when it
    crosses under 5 % or the change is known."""
    if rec["kind"] == "13D":
        return True
    return rec["signal"] in ("NEW_5PCT", "BELOW_5", "INCREASED", "DECREASED", "UNDER_5")


# ---------------------------------------------------------------- Form 4 signals

def insider_trades(rec, min_value=1_000_000, include_plans=False):
    """The open market buys and sales in one Form 4 that pass the filters.

    Returns {"buys": [...], "sells": [...], "dropped": {reason: n}}."""
    out = {"buys": [], "sells": [], "dropped": {}}

    def drop(reason):
        out["dropped"][reason] = out["dropped"].get(reason, 0) + 1

    trades = rec.get("trades", [])
    exercise_days = {t["date"] for t in trades if t["code"] in EXERCISE_CODES}
    for t in trades:
        if t["code"] not in ("P", "S"):
            if t["code"]:
                drop("code %s (not an open market trade)" % t["code"])
            continue
        if t["code"] == "S" and t["date"] in exercise_days:
            drop("sale on an option exercise day")
            continue
        if (t.get("plan") or rec.get("plan")) and not include_plans:
            drop("10b5-1 plan")
            continue
        (out["buys"] if t["code"] == "P" else out["sells"]).append(t)
    for side in ("buys", "sells"):
        total = sum(t["value"] or 0 for t in out[side])
        if out[side] and total < min_value:
            if not any(t["value"] for t in out[side]):
                drop("no price given")
            else:
                drop("under the minimum")
            out[side] = []
    return out


def summarize_form4(rec, min_value, include_plans):
    got = insider_trades(rec, min_value, include_plans)
    rows = []
    for side, label in (("buys", "BUY"), ("sells", "SELL")):
        ts = got[side]
        if not ts:
            continue
        shares = sum(t["shares"] or 0 for t in ts)
        value = sum(t["value"] or 0 for t in ts)
        dates = sorted(t["date"] for t in ts if t["date"])
        last = ts[-1]
        held_before = None
        if last.get("after") is not None:
            held_before = last["after"] + (-shares if side == "buys" else shares)
        rows.append({
            "side": label, "accession": rec["accession"], "filed": rec["filed"],
            "issuer": rec["issuer"], "issuer_cik": rec["issuer_cik"],
            "ticker": rec.get("ticker"), "owner": owner_label(rec),
            "owner_key": "|".join(sorted(c for c in rec["owner_ciks"] if c)) or owner_label(rec),
            "roles": rec.get("roles", []), "shares": shares, "value": round(value, 2),
            "avg_price": round(value / shares, 4) if shares else None,
            "first_trade": dates[0] if dates else "", "last_trade": dates[-1] if dates else "",
            "after": last.get("after"),
            "pct_of_holding": (round(100.0 * shares / held_before, 1)
                               if held_before and held_before > 0 else None),
            "lag_bdays": business_days(parse_date(dates[-1]) if dates else None,
                                       parse_date(rec["filed"])),
            "url": rec.get("url", ""),
        })
    return rows, got["dropped"]


SUSPECT_PRICE = 5000.0


def _trade_key(issuer_cik, side, shares, price, first, last):
    return (issuer_cik, side, round(shares or 0), round(price or 0, 2), first, last)


def merge_joint(rows):
    """One row per trade. Funds and the people who control them often file the same
    trade on separate Form 4s (joint filers); count it once and list every filer."""
    out, seen = [], {}
    for r in rows:
        k = _trade_key(r["issuer_cik"], r["side"], r["shares"], r["avg_price"],
                       r["first_trade"], r["last_trade"])
        if k in seen:
            m = seen[k]
            if r["owner"] not in m["owner"]:
                m["owner"] += " / " + r["owner"]
            m["roles"] = m["roles"] + [x for x in r["roles"] if x not in m["roles"]]
            m["accessions"].append(r["accession"])
            continue
        r = dict(r, accessions=[r["accession"]])
        if r.get("avg_price") and r["avg_price"] > SUSPECT_PRICE:
            r["check"] = ("price $%s a share looks unusual; check the filing before trusting "
                          "the dollar value" % "{:,.2f}".format(r["avg_price"]))
        seen[k] = r
        out.append(r)
    return out


def merge_joint_buys(buys):
    """The same, for the open market buys used to find clusters."""
    out, seen = [], {}
    for b in buys:
        k = (b["issuer_cik"], b["date"], round(b["shares"] or 0), round(b["value"] or 0))
        if k in seen:
            m = seen[k]
            if b["owner"] not in m["owner"]:
                m["owner"] += " / " + b["owner"]
            continue
        seen[k] = dict(b)
        out.append(seen[k])
    return out


def owner_label(rec):
    owners = [o for o in rec.get("owners", []) if o]
    if not owners:
        return "?"
    # Prefer a person (names filed "LAST FIRST") over the entities in a group filing.
    people = [o for o in owners if not re.search(
        r"\b(LLC|L\.?P\.?|INC\.?|LTD\.?|FUND|TRUST|CORP|HOLDINGS|PARTNERS|MANAGEMENT|CAPITAL)\b",
        o, re.I)]
    head = people[0] if people else owners[0]
    return head + (" (+%d)" % (len(owners) - 1) if len(owners) > 1 else "")


def open_market_buys(rec, include_plans=False):
    """Every open market buy in a Form 4 (no minimum), for cluster detection."""
    out = []
    got = insider_trades(rec, 0, include_plans)
    for t in got["buys"]:
        out.append({"issuer_cik": rec["issuer_cik"], "issuer": rec["issuer"],
                    "ticker": rec.get("ticker"), "owner": owner_label(rec),
                    "owner_key": "|".join(sorted(c for c in rec["owner_ciks"] if c))
                    or owner_label(rec),
                    "date": t["date"], "value": t["value"] or 0.0, "shares": t["shares"] or 0,
                    "accession": rec["accession"]})
    return out


def find_clusters(buys, days=CLUSTER_DAYS, min_insiders=2, min_total=0.0):
    """Companies where at least `min_insiders` different insiders bought on the open
    market within `days` days of each other."""
    by_issuer = {}
    for b in buys:
        if b.get("date"):
            by_issuer.setdefault(b["issuer_cik"] or b["issuer"], []).append(b)
    out = []
    for key, rows in by_issuer.items():
        rows.sort(key=lambda r: r["date"])
        best = None
        for i, first in enumerate(rows):
            d0 = parse_date(first["date"])
            window = [r for r in rows[i:] if (parse_date(r["date"]) - d0).days <= days]
            owners = {r["owner_key"] for r in window}
            if len(owners) >= min_insiders:
                if best is None or len(owners) > len(best[1]) or (
                        len(owners) == len(best[1]) and
                        sum(r["value"] for r in window) > sum(r["value"] for r in best[0])):
                    best = (window, owners)
        if not best:
            continue
        window, owners = best
        total = sum(r["value"] for r in window)
        if total < min_total:
            continue
        names = []
        for r in window:
            if r["owner"] not in names:
                names.append(r["owner"])
        out.append({"issuer": window[0]["issuer"], "issuer_cik": window[0]["issuer_cik"],
                    "ticker": window[0].get("ticker"), "insiders": len(owners),
                    "names": names, "from": window[0]["date"], "to": window[-1]["date"],
                    "value": round(total, 2),
                    "accessions": sorted({r["accession"] for r in window})})
    out.sort(key=lambda c: (-c["insiders"], -c["value"]))
    return out


# ---------------------------------------------------------------- 13F

def _edgar13f():
    if COPYCAT not in sys.path:
        sys.path.insert(0, COPYCAT)
    import edgar13f  # noqa: E402  (trader-copycat's 13F reader)
    return edgar13f


def load_filers(path=FILERS):
    return (load_json(path, {}) or {}).get("filers", [])


def find_filers(query, filers):
    q = query.lower().strip()
    if re.fullmatch(r"\d{1,10}", q):
        hit = [f for f in filers if f["cik"] == cik10(q)]
        return hit or [{"cik": cik10(q), "name": "CIK %s" % cik10(q), "aliases": []}]
    words = q.split()
    out = []
    for f in filers:
        hay = " ".join([f["name"], f.get("person", "")] + f.get("aliases", [])).lower()
        if all(w in hay for w in words):
            out.append(f)
    return out


def latest_13f(fetcher, cik):
    """(newest 13F-HR filing dict, submissions JSON) or (None, sub)."""
    e = _edgar13f()
    sub = fetcher.get_json(SUBMISSIONS % cik10(cik))
    originals = [f for f in e.list_filings(sub) if f["form"] == "13F-HR"]
    originals.sort(key=lambda f: (f["period"], f["filed"]), reverse=True)
    return (originals[0] if originals else None), sub


class _CopycatFetcher(object):
    """Our Fetcher, raising trader-copycat's EdgarError so its 13F code can catch it."""

    def __init__(self, fetcher, error):
        self.fetcher, self.error = fetcher, error

    def get(self, url, sec=True):
        try:
            return self.fetcher.get(url, sec)
        except WhaleError as exc:
            raise self.error(str(exc))

    def get_json(self, url, sec=True):
        try:
            return self.fetcher.get_json(url, sec)
        except WhaleError as exc:
            raise self.error(str(exc))


def diff_13f(fetcher, cik, tickers=None, min_change=50.0, top=None):
    """Compare a fund's two latest 13F-HR quarters: NEW, SOLD OUT, and changes over
    min_change percent (by shares)."""
    e = _edgar13f()
    fetcher = _CopycatFetcher(fetcher, e.EdgarError)
    try:
        meta, rows, _ = e.load_quarter(fetcher, cik)
        pmeta, prows, _ = e.load_quarter(fetcher, cik, before=meta["period"])
    except e.EdgarError as exc:
        raise WhaleError(str(exc))
    index = None
    if tickers is not None and getattr(tickers, "raw", None):
        index = e.TickerIndex(tickers.raw)
    overrides = e.load_overrides([e.CLASSES, "trader/tickers.json"])
    cur, total = e.build(meta, rows, index, overrides)
    prev, ptotal = e.build(pmeta, prows, index, overrides)
    changes = []
    for d in e.diff(cur, prev):
        pct = d.get("change_pct")
        if d["status"] in ("NEW", "SOLD OUT") or (pct is not None and abs(pct) > min_change):
            d["pct_of_fund"] = round(100.0 * d["value_after"] / total, 2) if total else 0.0
            d["pct_of_fund_before"] = (round(100.0 * d["value_before"] / ptotal, 2)
                                       if ptotal else 0.0)
            changes.append(d)
    changes.sort(key=lambda d: -max(d["value_after"], d["value_before"]))
    if top:
        changes = changes[:top]
    return {"filer": meta.get("filer"), "cik": cik10(cik), "period": meta["period"],
            "filed": meta["filed"], "accession": meta["accession"],
            "prior_period": pmeta["period"], "prior_filed": pmeta["filed"],
            "total": total, "prior_total": ptotal, "changes": changes,
            "notes": meta.get("notes", [])}


# ---------------------------------------------------------------- the flows

class Scope(object):
    """Which companies to look at: everything, or a set of CIKs/tickers."""

    def __init__(self, tickers=None, ciks=None, label="whole market"):
        self.tickers = set(tickers or [])
        self.ciks = set(ciks or [])
        self.label = label

    @property
    def all(self):
        return not self.tickers and not self.ciks

    def wants_ciks(self, ciks):
        return self.all or bool(self.ciks & set(ciks))

    def wants_ticker(self, t):
        return self.all or norm_ticker(t) in self.tickers


def make_scope(args, tickers):
    names = []
    label = []
    if getattr(args, "tickers", None):
        names += [norm_ticker(t) for t in args.tickers]
        label.append("tickers")
    if getattr(args, "holdings", False):
        h = tickers_from_files(args.targets, args.portfolio)
        if not h:
            raise WhaleError("--holdings: no tickers in %s or %s" % (args.targets, args.portfolio))
        names += h
        label.append("your holdings")
    if getattr(args, "watchlist", None):
        names += tickers_from_watchlist(args.watchlist)
        label.append(os.path.basename(args.watchlist))
    if not names:
        return Scope(), []
    names = sorted(set(names))
    ciks, missing = set(), []
    for t in names:
        c = tickers.cik(t)
        if c:
            ciks.add(c)
        else:
            missing.append(t)
    notes = ["Not found in SEC's ticker list, skipped: %s" % ", ".join(missing)] if missing else []
    return Scope(names, ciks, "%s: %s" % (" + ".join(label), ", ".join(names))), notes


def collect_index(fetcher, store, scope, start, end, signals, notes):
    forms = set()
    if "13dg" in signals:
        forms |= FORMS_OWN
    if "form4" in signals:
        forms |= FORMS_F4
    if not forms:
        return [], [], []
    found, days = daily_index(fetcher, start, end, forms, notes)
    own, f4 = [], []
    for acc, r in sorted(found.items(), key=lambda kv: kv[1]["filed"], reverse=True):
        if not scope.wants_ciks(r["ciks"]):
            continue
        if r["form"] in FORMS_OWN:
            own.append((acc, r["path_cik"], "13dg"))
        else:
            f4.append((acc, r["path_cik"], "form4"))
    return own, f4, days


def collect_submissions(fetcher, cik, start, end, signals, role="issuer"):
    """Filings in a company's (or an investor's) submissions list within the dates."""
    sub = fetcher.get_json(SUBMISSIONS % cik10(cik))
    r = sub.get("filings", {}).get("recent", {})
    own, f4 = [], []
    for i, form in enumerate(r.get("form", [])):
        d = parse_date(r["filingDate"][i])
        if not d or d < start or d > end:
            continue
        item = r["accessionNumber"][i]
        if form in FORMS_OWN and "13dg" in signals:
            own.append((item, int(cik10(cik)), "13dg"))
        elif form in FORMS_F4 and "form4" in signals:
            f4.append((item, int(cik10(cik)), "form4"))
    return sub, own, f4


def analyse(store, tickers, scope, own_items, f4_items, args, priors="13D", notes=None,
            extra_buys=None):
    """Parse and classify. Returns the report sections."""
    notes = notes if notes is not None else []
    rep = {"ownership": [], "ownership_hidden": 0, "insiders": [], "clusters": [],
           "dropped": {}, "buys_seen": []}
    if own_items:
        recs = store.load(own_items)
        recs = [r for r in recs if scope.all or r.get("subject_cik") in scope.ciks]
        subs_cache = {}
        by_key = {}
        for r in recs:
            by_key.setdefault((subject_key(r), tuple(sorted(r.get("filer_ciks") or []))),
                              []).append(r)
        for r in recs:
            prior = None
            if r.get("amendment"):
                # an earlier filing in this same batch by the same filer first
                same = [x for x in by_key[(subject_key(r), tuple(sorted(r.get("filer_ciks") or [])))]
                        if x["filed"] < r["filed"] or
                        (x["filed"] == r["filed"] and x["accession"] < r["accession"])]
                if same:
                    prior = max(same, key=lambda x: (x["filed"], x["accession"]))
                elif priors == "all" or (priors == "13D" and r["kind"] == "13D"):
                    if not (r.get("five_or_less") or (r.get("percent") or 99) < 5):
                        prior = find_prior(store, r, subs_cache)
            classify_ownership(r, prior)
            r["ticker"] = tickers.ticker(r.get("subject_cik")) or ""
            r["lag_bdays"] = business_days(parse_date(r.get("event_date")),
                                           parse_date(r.get("filed")))
        if scope.all and not args.include_unlisted:
            unlisted = [r for r in recs if not r["ticker"]]
            if unlisted:
                notes.append("13D/13G: %d filings on companies without a US ticker (private "
                             "funds, ETFs, unlisted shares) hidden; --include-unlisted shows them."
                             % len(unlisted))
            recs = [r for r in recs if r["ticker"]]
        shown = [r for r in recs if args.all or not scope.all or notable_ownership(r)]
        rep["ownership_hidden"] = len(recs) - len(shown)
        shown.sort(key=lambda r: (SIGNAL_RANK.get(r["signal"], 9), r["kind"] != "13D",
                                  -(r.get("percent") or 0), r["filed"]))
        rep["ownership"] = shown
    if f4_items:
        recs = store.load(f4_items)
        recs = [r for r in recs if scope.all or r.get("issuer_cik") in scope.ciks]
        recs.sort(key=lambda r: (r.get("filed", ""), r.get("accession", "")))
        rows, buys = [], list(extra_buys or [])
        for r in recs:
            if r.get("error"):
                continue
            if not r.get("ticker"):
                r["ticker"] = tickers.ticker(r.get("issuer_cik")) or ""
            got, dropped = summarize_form4(r, args.min, args.include_plans)
            rows += got
            for k, v in dropped.items():
                rep["dropped"][k] = rep["dropped"].get(k, 0) + v
            for b in open_market_buys(r, args.include_plans):
                if b["accession"] not in {x["accession"] for x in buys}:
                    buys.append(b)
        if scope.all and not args.include_unlisted:
            rows = [x for x in rows if x.get("ticker")]
            buys = [b for b in buys if b.get("ticker")]
        rows.sort(key=lambda x: (x["filed"], x["accession"]))
        rows = merge_joint(rows)
        buys = merge_joint_buys(buys)
        rows.sort(key=lambda x: (x["side"] != "BUY", bool(x.get("check")), -x["value"]))
        rep["insiders"] = rows
        rep["buys_seen"] = buys
        rep["clusters"] = find_clusters(buys, days=CLUSTER_DAYS, min_total=args.cluster_min)
    if store.skipped:
        notes.append("%d filings were not read because of --max-fetch %d. Narrow the scan "
                     "(--holdings, --tickers, --days) or raise the cap."
                     % (store.skipped, store.max_fetch))
    if store.errors:
        notes.append("%d filings could not be read (first: %s)."
                     % (len(store.errors), store.errors[0]))
    return rep


def funds_13f(fetcher, tickers, scope, args, start, end, notes, only_new_since=None,
              names=None):
    """13F diffs for the bundled funds whose newest 13F-HR is in the window (or all)."""
    filers = load_filers(args.filers)
    if names:
        chosen = []
        for n in names:
            hits = find_filers(n, filers)
            if not hits:
                notes.append("13F: no fund matched %r in %s" % (n, args.filers))
            chosen += [h for h in hits if h not in chosen]
        filers = chosen
    out, quiet, newest_seen = [], [], []
    for f in filers:
        try:
            newest, _ = latest_13f(fetcher, f["cik"])
        except WhaleError as exc:
            notes.append("13F %s: %s" % (f["name"], exc))
            continue
        if not newest:
            continue
        newest_seen.append(newest["accession"])
        filed = parse_date(newest["filed"])
        if only_new_since is not None:
            if newest["accession"] in only_new_since:
                continue
        elif not args.all_13f and not (filed and start <= filed <= end):
            quiet.append("%s (%s)" % (f["name"], newest["filed"]))
            continue
        try:
            d = diff_13f(fetcher, f["cik"], tickers, args.min_change, None)
        except Exception as exc:  # one bad filing should not end the scan
            notes.append("13F %s: %s" % (f["name"], exc))
            continue
        d["person"] = f.get("person", "")
        if not scope.all:
            d["changes"] = [c for c in d["changes"] if c.get("ticker") and
                            scope.wants_ticker(c["ticker"])]
        if args.top:
            d["changes"] = d["changes"][:args.top]
        out.append(d)
    if quiet and only_new_since is None:
        notes.append("13F: %d funds have no new 13F-HR in this window (newest filed: %s). "
                     "Use --all-13f to compare them anyway."
                     % (len(quiet), "; ".join(quiet[:6]) + (" ..." if len(quiet) > 6 else "")))
    return out, newest_seen


# ---------------------------------------------------------------- output

def money(v):
    if v is None:
        return "?"
    a = abs(v)
    s = ("$%.2fB" % (a / 1e9) if a >= 1e9 else "$%.2fM" % (a / 1e6) if a >= 1e6
         else "$%.0fk" % (a / 1e3) if a >= 1e3 else "$%.0f" % a)
    return ("-" + s) if v < 0 else s


def num(v):
    if v is None:
        return "?"
    return "{:,.0f}".format(v)


def pct(v):
    return "?" if v is None else ("%.2f" % v).rstrip("0").rstrip(".") + " %"


def _who(r):
    names = r.get("filers") or ["?"]
    return names[0] + (" (+%d)" % (len(names) - 1) if len(names) > 1 else "")


def _co(name, ticker):
    return "%s%s" % (name or "?", " (%s)" % ticker if ticker else "")


def render(report, limit=40):
    out = []
    w = out.append
    w("WHALES  %s  |  %s  |  %s" % (report["title"], report["window"], report["scope"]))
    w("Not real time. Every section says how late its data is.")
    for n in report.get("notes", []):
        w("  note: %s" % n)

    if "13dg" in report["signals"]:
        rows = report["ownership"]
        w("")
        w("== 13D / 13G: 5 %% holders (%d shown%s)" % (
            len(rows), ", %d routine 13G/A amendments hidden (--all lists them)"
            % report["ownership_hidden"] if report["ownership_hidden"] else ""))
        w("   " + DELAYS["13dg"])
        if not rows:
            w("   nothing in this window.")
        for title, sigs in OWNERSHIP_GROUPS:
            group = [r for r in rows if r["signal"] in sigs]
            if not group:
                continue
            w("  -- %s (%d)" % (title, len(group)))
            for r in group[:limit] if limit else group:
                change = ""
                if r.get("prior_percent") is not None:
                    change = " (was %s on %s)" % (pct(r["prior_percent"]), r.get("prior_filed"))
                lag = (" lag %d bd" % r["lag_bdays"]) if r.get("lag_bdays") is not None else ""
                w("  %-6s %s -> %s: %s%s%s" % (
                    r["kind"] + ("/A" if r["amendment"] else ""), _who(r),
                    _co(r["subject"], r.get("ticker")), pct(r.get("percent")),
                    " (%s sh)" % num(r.get("shares")) if r.get("shares") else "", change))
                w("         filed %s, event %s%s  %s" % (r["filed"], r.get("event_date") or "?",
                                                      lag, r.get("url", "")))
            if limit and len(group) > limit:
                w("     ... %d more (--limit 0 or --json for all)" % (len(group) - limit))

    if "form4" in report["signals"]:
        rows = report["insiders"]
        w("")
        w("== Form 4: insider open market trades of %s or more (%d)" % (
            money(report["min"]), len(rows)))
        w("   " + DELAYS["form4"])
        if report["dropped"]:
            w("   filtered out: " + ", ".join("%s %d" % (k, v) for k, v in
                                               sorted(report["dropped"].items(),
                                                      key=lambda kv: -kv[1])))
        if not rows:
            w("   nothing in this window.")
        for r in rows[:limit] if limit else rows:
            lag = (" lag %d bd" % r["lag_bdays"]) if r.get("lag_bdays") is not None else ""
            w("  %-4s %s  %s  %s sh @ %s  by %s [%s]" % (
                r["side"], money(r["value"]).rjust(9), _co(r["issuer"], r.get("ticker")),
                num(r["shares"]), ("$%.2f" % r["avg_price"]) if r["avg_price"] else "?",
                r["owner"], ", ".join(r["roles"]) or "?"))
            hold = (", holding %s%s %%" % ("+" if r["side"] == "BUY" else "-",
                                           r["pct_of_holding"])) if r.get(
                "pct_of_holding") is not None else ""
            w("        traded %s%s, filed %s%s%s  %s" % (
                r["first_trade"], (" to " + r["last_trade"]) if r["last_trade"] != r[
                    "first_trade"] else "", r["filed"], lag, hold, r.get("url", "")))
            if r.get("check"):
                w("        CHECK: %s" % r["check"])
        if limit and len(rows) > limit:
            w("  ... %d more (--limit 0 or --json for all)" % (len(rows) - limit))
        w("")
        w("== Cluster buys: 2+ insiders buying one company within %d days (%d)" % (
            CLUSTER_DAYS, len(report["clusters"])))
        if not report["clusters"]:
            w("   none.")
        for c in report["clusters"][:limit] if limit else report["clusters"]:
            w("  %s  %d insiders, %s, %s to %s: %s" % (
                _co(c["issuer"], c.get("ticker")), c["insiders"], money(c["value"]),
                c["from"], c["to"], "; ".join(c["names"][:5])))

    if "13f" in report["signals"]:
        w("")
        w("== 13F: fund changes, latest quarter vs the one before (%d funds)" % len(
            report["funds"]))
        w("   " + DELAYS["13f"])
        if not report["funds"]:
            w("   no fund in the list filed a new 13F-HR in this window.")
        for f in report["funds"]:
            w("  %s%s: %s (filed %s) vs %s, total %s" % (
                f["filer"], " - " + f["person"] if f.get("person") else "", f["period"],
                f["filed"], f["prior_period"], money(f["total"])))
            if not f["changes"]:
                w("     no new, sold-out or >%g %% changes%s." % (
                    report["min_change"], " in your scope" if report["scoped"] else ""))
            for c in f["changes"]:
                label = c["status"] if c["status"] in ("NEW", "SOLD OUT") else (
                    "UP %+.0f %%" % c["change_pct"] if c["change_pct"] > 0 else
                    "DOWN %+.0f %%" % c["change_pct"])
                w("     %-11s %-28s %s sh -> %s sh, %s (%s of fund)%s" % (
                    label, _co(c["issuer"][:22], c.get("ticker")),
                    num(c["shares_before"]), num(c["shares_after"]),
                    money(c["value_after"] or c["value_before"]),
                    pct(c["pct_of_fund"] if c["value_after"] else c["pct_of_fund_before"]),
                    " " + c["put_call"] if c.get("put_call") else ""))

    w("")
    w("== Unusual options / dark pool")
    w("   " + DELAYS["flow"])
    w("")
    w(NEXT_STEP)
    w("SEC requests this run: %d." % report.get("sec_calls", 0))
    return "\n".join(out) + "\n"


def has_findings(rep):
    return bool(rep.get("ownership") or rep.get("insiders") or rep.get("clusters") or
                any(f.get("changes") for f in rep.get("funds", [])))


# ---------------------------------------------------------------- commands

def _today(args):
    return parse_date(args.today) if args.today else _dt.date.today()


def _signals(args):
    s = [x.strip().lower() for x in (args.signals or "13dg,form4,13f").split(",") if x.strip()]
    bad = [x for x in s if x not in ("13dg", "form4", "13f")]
    if bad:
        raise WhaleError("unknown signal %s (use 13dg, form4, 13f)" % ", ".join(bad))
    return s


def base_report(title, window, scope, signals, args):
    return {"title": title, "window": window, "scope": scope.label, "signals": signals,
            "notes": [], "ownership": [], "ownership_hidden": 0, "insiders": [],
            "clusters": [], "dropped": {}, "funds": [], "min": args.min,
            "min_change": args.min_change, "scoped": not scope.all,
            "delays": DELAYS, "next_step": NEXT_STEP}


def cmd_scan(args, fetcher, store, tickers, state=None):
    today = _today(args)
    signals = _signals(args)
    start = today - _dt.timedelta(days=max(args.days, 1) - 1)
    scope, notes = make_scope(args, tickers)
    rep = base_report("scan", "%s to %s" % (iso(start), iso(today)), scope, signals, args)
    rep["notes"] += notes
    own, f4, days = collect_index(fetcher, store, scope, start, today, signals, rep["notes"])
    rep["index_days"] = days
    priors = "all" if not scope.all else "13D"
    rep.update(analyse(store, tickers, scope, own, f4, args, priors, rep["notes"]))
    if "13f" in signals:
        rep["funds"], _ = funds_13f(fetcher, tickers, scope, args, start, today,
                                    rep["notes"], names=args.funds)
    return rep


def cmd_ticker(args, fetcher, store, tickers):
    today = _today(args)
    signals = [s for s in _signals(args) if s != "13f"]
    t = norm_ticker(args.symbol)
    cik = tickers.cik(t) if not re.fullmatch(r"\d{1,10}", t) else cik10(t)
    if not cik:
        raise WhaleError("%s is not in SEC's ticker list" % t)
    start = today - _dt.timedelta(days=max(args.days, 1) - 1)
    scope = Scope([t], [cik], "%s (CIK %s)" % (t, cik))
    rep = base_report("ticker %s" % t, "%s to %s" % (iso(start), iso(today)), scope,
                      signals + ["13f"] if args.funds_13f else signals, args)
    sub, own, f4 = collect_submissions(fetcher, cik, start, today, signals)
    rep["company"] = sub.get("name")
    rep["title"] = "ticker %s - %s" % (t, sub.get("name", ""))
    rep.update(analyse(store, tickers, scope, own, f4, args, "all", rep["notes"]))
    if args.funds_13f:
        args.all_13f = True
        rep["funds"], _ = funds_13f(fetcher, tickers, scope, args, start, today,
                                    rep["notes"], names=args.funds)
    return rep


def cmd_filer(args, fetcher, store, tickers):
    today = _today(args)
    signals = _signals(args)
    filers = load_filers(args.filers)
    hits = find_filers(" ".join(args.name), filers)
    if not hits:
        raise WhaleError("no investor matched %r in %s. Pass a CIK, or add the investor "
                         "there (find a CIK with trader-copycat: edgar13f.py find NAME --sec)."
                         % (" ".join(args.name), args.filers))
    if len(hits) > 1:
        raise WhaleError("%r matches %d investors: %s. Be more specific or pass the CIK."
                         % (" ".join(args.name), len(hits),
                            "; ".join("%s (%s)" % (h["name"], h["cik"]) for h in hits)))
    f = hits[0]
    start = today - _dt.timedelta(days=max(args.days, 1) - 1)
    scope = Scope()
    scope.label = "%s%s, CIK %s" % (f["name"], " - " + f["person"] if f.get("person") else "",
                                    f["cik"])
    rep = base_report("filer %s" % f["name"], "%s to %s" % (iso(start), iso(today)), scope,
                      signals, args)
    try:
        sub, own, f4 = collect_submissions(fetcher, f["cik"], start, today, signals)
    except NotFound:
        raise WhaleError("SEC has no submissions for CIK %s" % f["cik"])
    args.all = True
    rep.update(analyse(store, tickers, scope, own, f4, args, "all", rep["notes"]))
    for extra in f.get("ciks", []):
        rep["notes"].append("Also files under CIK %s (not read)." % extra)
    if "13f" in signals:
        try:
            d = diff_13f(fetcher, f["cik"], tickers, args.min_change, args.top)
            d["person"] = f.get("person", "")
            rep["funds"] = [d]
        except WhaleError as exc:
            rep["notes"].append("13F: %s" % exc)
    return rep


def cmd_watch(args, fetcher, store, tickers, state_path):
    today = _today(args)
    state = load_json(state_path, {}) or {}
    seen = state.get("seen", {})
    last = parse_date(state.get("last_index_day") or "")
    start = (last - _dt.timedelta(days=1)) if last else today - _dt.timedelta(
        days=max(args.days, 1) - 1)
    signals = _signals(args)
    scope, notes = make_scope(args, tickers)
    rep = base_report("watch", "new since %s" % (state.get("last_run") or "never (first run: %d days)"
                                                 % args.days), scope, signals, args)
    rep["notes"] += notes
    own, f4, days = collect_index(fetcher, store, scope, start, today, signals, rep["notes"])
    own = [i for i in own if i[0] not in seen]
    f4 = [i for i in f4 if i[0] not in seen]
    recent_buys = [b for b in state.get("buys", [])
                   if b.get("date", "") >= iso(today - _dt.timedelta(days=31))]
    priors = "all" if not scope.all else "13D"
    rep.update(analyse(store, tickers, scope, own, f4, args, priors, rep["notes"],
                       extra_buys=recent_buys))
    # Only clusters with at least one buy from this run are news.
    fresh = {i[0] for i in f4}
    rep["clusters"] = [c for c in rep["clusters"] if set(c["accessions"]) & fresh]
    newest_13f = []
    if "13f" in signals:
        # First run: like a scan of the window. Later runs: any 13F-HR not seen before.
        seen_13f = set(state["seen_13f"]) if state.get("seen_13f") else None
        rep["funds"], newest_13f = funds_13f(fetcher, tickers, scope, args, start, today,
                                             rep["notes"], only_new_since=seen_13f,
                                             names=args.funds)
    if not args.dry_run:
        for acc, *_ in own + f4:
            seen[acc] = today.isoformat()
        cutoff = iso(today - _dt.timedelta(days=KEEP_DAYS))
        state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
        keep = {b["accession"]: b for b in recent_buys + rep.get("buys_seen", [])}
        state["buys"] = sorted(keep.values(), key=lambda b: b["date"])
        if newest_13f:
            state["seen_13f"] = sorted(set(state.get("seen_13f", [])) | set(newest_13f))
        if days:
            state["last_index_day"] = max(days)
        state["last_run"] = _dt.datetime.now().replace(microsecond=0).isoformat()
        save_json(state_path, state)
    return rep


# ---------------------------------------------------------------- cli

def _common(p, days):
    p.add_argument("--days", type=int, default=days, help="how many days back (default %d)" % days)
    p.add_argument("--min", type=float, default=1_000_000,
                   help="Form 4 minimum dollar value per insider filing (default 1000000)")
    p.add_argument("--cluster-min", type=float, default=100_000,
                   help="cluster buys: minimum combined value (default 100000)")
    p.add_argument("--include-plans", action="store_true",
                   help="keep 10b5-1 plan trades (filtered out by default)")
    p.add_argument("--min-change", type=float, default=50.0,
                   help="13F: show changes bigger than this %% (default 50)")
    p.add_argument("--signals", help="comma list of 13dg, form4, 13f (default all)")
    p.add_argument("--all", action="store_true", help="list routine 13G/A amendments too")
    p.add_argument("--include-unlisted", action="store_true",
                   help="whole-market scans: keep companies without a US ticker")
    p.add_argument("--limit", type=int, default=40, help="rows per section (0 = all)")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--user-agent", help="SEC User-Agent: \"Your Name you@example.com\"")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="DIR", help="read every URL from files in DIR")
    p.add_argument("--rate", type=float, default=8.0, help="SEC requests a second (max 10)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--max-fetch", type=int, default=12000,
                   help="read at most this many filings per run (0 = no cap)")
    p.add_argument("--cache", default="trader/state/whales_cache.json",
                   help="parsed filings cache ('' to turn off)")
    p.add_argument("--filers", default=FILERS, help="13F fund list (trader-copycat/filers.json)")
    p.add_argument("--funds", nargs="*", help="13F: only these funds (names or CIKs)")
    p.add_argument("--all-13f", action="store_true",
                   help="13F: compare every fund, not only those that filed in the window")
    p.add_argument("--top", type=int, default=15, help="13F: changes per fund (default 15)")
    p.add_argument("--today", help=argparse.SUPPRESS)


def _scope_args(p):
    p.add_argument("--holdings", action="store_true",
                   help="only the stocks in trader/targets.json and trader/portfolio.json")
    p.add_argument("--watchlist", metavar="FILE", help="only the tickers in this file")
    p.add_argument("--tickers", nargs="*", help="only these tickers")
    p.add_argument("--targets", default="trader/targets.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")


def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("scan", help="the last N days (default 7)")
    _common(p, 7)
    _scope_args(p)
    p = sub.add_parser("ticker", help="everything about one company")
    p.add_argument("symbol")
    p.add_argument("--funds-13f", action="store_true",
                   help="also compare the bundled funds' 13Fs for this ticker")
    _common(p, 90)
    p = sub.add_parser("filer", help="one investor: 13D/G, Form 4 and 13F")
    p.add_argument("name", nargs="+")
    _common(p, 365)
    p = sub.add_parser("watch", help="only what is new since the last run")
    _common(p, 7)
    _scope_args(p)
    p.add_argument("--state", default="trader/state/whales.json")
    p.add_argument("--dry-run", action="store_true", help="do not update the state")
    return ap


def main(argv=None):
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        ua = sec_user_agent(args.user_agent, args.config)
        if not ua and not args.offline:
            raise WhaleError(UA_HELP)
        fetcher = Fetcher(ua, offline=args.offline, rate=min(args.rate, MAX_RATE))
        store = Store(fetcher, args.cache or None, workers=args.workers,
                      max_fetch=args.max_fetch, progress=not args.offline)
        tickers = Tickers.load(fetcher)
        if args.cmd == "scan":
            rep = cmd_scan(args, fetcher, store, tickers)
        elif args.cmd == "ticker":
            rep = cmd_ticker(args, fetcher, store, tickers)
        elif args.cmd == "filer":
            rep = cmd_filer(args, fetcher, store, tickers)
        else:
            rep = cmd_watch(args, fetcher, store, tickers, args.state)
        store.save(_today(args))
        rep["sec_calls"] = fetcher.calls
        rep.pop("buys_seen", None)
        if args.json:
            print(json.dumps(rep, indent=2, default=str))
        else:
            sys.stdout.write(render(rep, args.limit))
        return 0 if has_findings(rep) else 1
    except WhaleError as exc:
        print("whales: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
