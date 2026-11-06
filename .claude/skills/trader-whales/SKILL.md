---
name: trader-whales
description: >-
  The Whale Tracker. Spots when big holders step into or out of US stocks,
  straight from SEC EDGAR: Schedule 13D and 13G filings and their amendments
  (who crossed 5 %, who added, who cut, who fell under 5 %), Form 4 insider
  open market buys and big sales (option exercises and 10b5-1 plan trades
  filtered out, cluster buys flagged when two or more insiders buy within 14
  days), and the 13F changes of well-known funds (new positions, sold out,
  moves over 50 %). Says how late every signal is, filters by dollar size,
  can stick to your holdings or a watchlist, and remembers what it has shown.
  Use when the user says "what are the whales doing", "who is buying",
  "insider buying", "cluster buys", "who crossed 5 %", "activist stake",
  "13D", "13G", "Form 4", "big money moves", "what did Ackman do", "anything
  on AAPL from insiders", "alert me when a whale moves", or "run the whales".
argument-hint: "scan [--days N] | ticker SYMBOL | filer NAME | watch"
---

# trader-whales

Big holders have to tell the SEC when they move: anyone passing 5 % of a
company files a Schedule 13D (with intent to influence) or 13G (passive),
insiders file a Form 4 within two business days of a trade, and every fund
over $100 million files a 13F once a quarter. This skill reads all three,
keeps the moves that matter, and tells you how old each one is.

Tool in this folder (standard library Python, no keys, no packages):

```bash
python3 "${CLAUDE_SKILL_DIR}/whales.py" --help
```

SEC wants every program to say who is calling. Set it once in
`trader/config.json` (`"sec_user_agent": "Your Name you@example.com"`, with
the user's real name and email) or `export SEC_USER_AGENT=...`. The tool
refuses to call SEC without it and says how to fix it. Ask the user for it;
never invent one. Requests are rate limited to at most 10 a second (default
8, `--rate` cannot go above 10).

## Commands

| Command | What it does |
|---|---|
| `scan` | The last 7 days (`--days N`), whole market or your scope: 13D/13G, Form 4, clusters, and 13F diffs for funds that filed in the window |
| `ticker AAPL` | Everything on one company from its live submissions list (default 90 days, includes today's filings) |
| `filer ackman` | One investor from `../trader-copycat/filers.json` (name, alias or CIK): their 13D/13G and Form 4 filings (default 365 days) and their latest 13F against the quarter before |
| `watch` | Only what is new since the last run. State in `trader/state/whales.json` |

Filters and scope (scan and watch):

- `--min 1000000`: Form 4 minimum dollar value per insider filing (default
  $1 million). Cluster buys count every open market buy, with a combined
  minimum of `--cluster-min 100000`.
- `--holdings`: only the stocks in `trader/targets.json` and
  `trader/portfolio.json`. `--watchlist FILE` (JSON list, `{"tickers": [...]}`
  or one ticker per line) or `--tickers NVDA TSLA` for your own list. They
  combine. A narrow scope is also far faster.
- `--signals 13dg,form4,13f` to pick signals, `--include-plans` to keep 10b5-1
  trades, `--min-change 50` for the 13F threshold, `--funds ackman burry` to
  pick 13F funds, `--all-13f` to diff every fund even without a new filing,
  `--all` to list routine 13G/A amendments, `--include-unlisted` to keep
  companies without a US ticker, `--json` for machine-readable output.

Exit code 0 means there were findings, 1 nothing, 2 an error.

## Where the data comes from

- **Scan and watch**: EDGAR's daily form index
  (`/Archives/edgar/daily-index/YYYY/QTRn/form.YYYYMMDD.idx`), then each
  matching filing's full submission `.txt`, parsed from its XML (the XML
  Schedule 13D/13G in use since December 2024, the old text `SC 13D/G` as a
  fallback, and the Form 4 ownership XML).
- **Ticker and filer**: the company's or investor's submissions JSON on
  `data.sec.gov`.
- **13F**: trader-copycat's `edgar13f.py` (imported, not copied) and its fund
  list `filers.json`.
- Tickers: SEC's `company_tickers.json`.
- Parsed filings are cached in `trader/state/whales_cache.json`, so a second
  run only downloads what is new. A whole-market week is about 6,000 filings
  the first time (around 15 minutes at the rate limit); after that, a daily
  `watch` is a few minutes and a scoped run takes seconds.

## How each signal is read

**13D / 13G** (each row: filer, company, ticker, percent of class, shares,
filing date, event date, lag in business days, link):

- *Crossed 5 %*: a new 13D (activist, may push for change) or 13G (passive).
- *Increased / Decreased*: an amendment compared with the same filer's
  previous filing on that company. Whole-market scans look up the previous
  filing for 13D amendments; a scoped scan, `ticker` and `filer` do it for
  every amendment.
- *Fell to 5 % or less*: the filer says it owns 5 % or less (an exit).
- Routine 13G/A amendments with no known change are counted, not listed.

**Form 4** (each row: buy or sell, dollar value, company, shares, average
price, insider and role, trade dates, filing date, lag, change in holding):

- Only code P (open market buy) and code S (open market sale).
- Dropped and counted: awards, option exercises (M, X, C, O), tax
  withholding (F), gifts (G) and every other code; sales on the same day as an
  exercise in the same filing (exercise and sell); trades marked as 10b5-1 plan
  trades (the `aff10b5One` box or a footnote naming 10b5-1); filings under the
  minimum or with no price.
- The same trade reported by several joint filers (a fund and the people
  controlling it) is shown once with every filer named.
- A price over $5,000 a share is flagged `CHECK`: usually a typo in the
  filing (or Berkshire class A), so open it before trusting the value.
- **Cluster buys**: two or more different insiders buying the same company on
  the open market within 14 days. `watch` remembers 31 days of buys so a
  cluster can build up across runs.

**13F**: the fund's two latest 13F-HR quarters compared by CUSIP: NEW, SOLD
OUT, and share changes over 50 % (UP / DOWN), biggest first, with the share
of the fund. In `scan`, only funds whose newest 13F-HR was filed in the
window are compared (the others are listed in a note).

## Delays: this is not real time

Every section of the output repeats its delay. Say it in every answer.

| Signal | How late it can be |
|---|---|
| Form 4 | Due 2 business days after the trade (the lag is shown per row; late filers exist) |
| 13D | Due 5 business days after crossing 5 %; amendments 2 business days after a material change |
| 13G | 5 business days (passive investors) up to 45 days after the quarter ends (institutions); amendments 45 days after the quarter ends |
| 13F | A snapshot of the last day of the quarter, filed up to 45 days later: up to 4.5 months old. Long US positions only, nothing traded inside the quarter |
| Scan / watch | Read the daily index, published after each business day: today's filings show up tomorrow. `ticker` reads the live list and includes today |

## Unusual options and dark pool

There is no free, reliable source for options flow or dark pool prints, and
this skill does not scrape one. The tool prints that this part is skipped.

When the user asks for it, check whether a paid market-data connector is
connected (for example Quant Data in Claude): search the available tools
with ToolSearch for "options flow", "dark pool" or "quant data". Only if one
is connected, use it, name it as the source and give its timestamp. If none
is connected, say plainly that options flow and dark pool are skipped because
they need a paid connector, and carry on with the SEC signals. Never invent
flow data and never build a scraper for it.

## Steps

1. Run what the user asked for, from the repository root:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/whales.py" scan
   python3 "${CLAUDE_SKILL_DIR}/whales.py" scan --days 3 --holdings
   python3 "${CLAUDE_SKILL_DIR}/whales.py" ticker NVDA
   python3 "${CLAUDE_SKILL_DIR}/whales.py" filer ackman
   python3 "${CLAUDE_SKILL_DIR}/whales.py" watch --holdings
   ```

   A first whole-market scan takes a while: run it in the background, or
   narrow it with `--holdings`, `--tickers` or `--days`.
2. Read the findings that matter first: 13D filings (activists), exits under
   5 %, cluster buys, and the largest open market buys. Open the filing link
   before describing one (the 13D's Item 4 says what the activist wants).
3. Write a short brief: per company, who moved, how much, when they traded
   and when it became public (the lag), and the delay of the signal. Flag
   anything touching the user's holdings (`trader/targets.json`).
4. If an idea comes out of it, propose it only. The order path is always
   `/trader-budget` (size it) -> `/trader-risk` (check it) -> `/trader-broker`
   (place it, only after the user's explicit yes).

To be told when whales move, run `watch` as a scheduled task each weekday
morning (the daily index for the day before is out by then), or with `/loop`
during the day for `ticker` checks.

## What it never does

- Never places, sizes or approves an order, and never says "buy this
  because an insider did". Insiders and funds sell for many reasons (taxes,
  diversification) and their buys can be wrong.
- Never claims a filing is real time or early, never fills a missing price
  or percent with a guess, and never invents options or dark pool data.
- Never exceeds SEC's 10 requests a second and never calls SEC without the
  user's own User-Agent.

## Tests

```bash
python3 -m unittest discover -s .claude/skills/trader-whales/tests -v
```

Offline fixtures in `tests/fixtures` (made up companies, real EDGAR file
shapes) are written by `tests/make_fixtures.py`.

## Safety

Education and tooling, not financial advice. This skill only reads public
filings and reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
