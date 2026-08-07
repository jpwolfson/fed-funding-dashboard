#!/usr/bin/env python3
"""Pull all NSF DMS (Division of Mathematical Sciences) new awards from the
NSF Award Search API and write aggregated data for the dashboard.

The NSF API has a known pagination fault: when a single query spans many
pages it returns duplicate records across pages, and each duplicate silently
displaces a record that is then never returned. The fix is to keep every
query's result set small: pull month-by-month, and recursively bisect any
window whose result count exceeds SAFE_WINDOW or that shows cross-page
duplicates, unioning results at every level. Single-day windows that are
still too large partition by transaction type and awardee state.

Outputs:
  data/dashboard.json  - aggregated series the dashboard reads
  data/awards.csv      - one row per award, for transparency / reanalysis
"""

import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

API = "https://api.nsf.gov/services/v1/awards.json"
DMS_DIV_CODE = "03040000"  # org_code_div for the Division of Mathematical Sciences
RPP = 25          # API maximum results per page
SAFE_WINDOW = 60  # bisect windows larger than this (~2.5 pages) to dodge the pagination fault
RECENT_MONTHS = 4  # incremental runs re-pull this trailing window (NSF backfill period)
PRINT_FIELDS = "id,date,estimatedTotalAmt,transType,title,awardeeName"
SERIES_START = date(2014, 10, 1)  # FY2015 onward
# The API's date filter may not operate on exactly the "date" field it
# returns (residual undercounts suggest e.g. start-date filtering), so query
# a wider horizon than the series and attribute records by their own date.
QUERY_BACK = date(2013, 10, 1)
QUERY_AHEAD = timedelta(days=550)

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = json.loads((Path(__file__).parent / "verified_baseline.json").read_text())["months"]

warnings = []
USE_ZERO_OFFSET = False  # set by main() after probing the API


def warn(msg):
    warnings.append(msg)
    print(f"WARNING: {msg}", file=sys.stderr)


def api_get(params, retries=5):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                body = json.load(resp)
            return body.get("response", {}).get("award", []) or []
        except Exception as e:
            if attempt == retries - 1:
                raise RuntimeError(f"API request failed after {retries} tries: {url}") from e
            time.sleep(2 ** attempt)


def fetch_pages(extra_params):
    """Paginate one query to exhaustion. Returns (awards_by_id, saw_duplicates)."""
    by_id, dups = {}, False
    offset = 1
    while True:
        page = api_get({
            "org_code_div": DMS_DIV_CODE,
            "printFields": PRINT_FIELDS,
            "rpp": RPP,
            "offset": offset,
            **extra_params,
        })
        for a in page:
            if a["id"] in by_id:
                dups = True
            by_id[a["id"]] = a
        if len(page) < RPP:
            break
        offset += RPP
    if USE_ZERO_OFFSET:
        # Every window came back exactly one award short with 1-based
        # pagination - the API's offset is evidently 0-based, so offset=1
        # skips each query's first record. Union in the offset=0 page.
        for a in api_get({
            "org_code_div": DMS_DIV_CODE,
            "printFields": PRINT_FIELDS,
            "rpp": RPP,
            "offset": 0,
            **extra_params,
        }):
            by_id.setdefault(a["id"], a)
    return by_id, dups


def date_params(start, end):
    # Pad one day each side: the API's date-range boundaries have off-by-one
    # behavior (observed: each unpadded month returned exactly one award short).
    # Awards are keyed by id and attributed to months by their own date field,
    # so the overlap between adjacent windows is harmless.
    return {"dateStart": (start - timedelta(days=1)).strftime("%m/%d/%Y"),
            "dateEnd": (end + timedelta(days=1)).strftime("%m/%d/%Y")}


# The pagination fault displaces records at random, so no single query is
# trusted to be complete. Every level below UNIONS its own results with its
# sub-queries' results - a record survives if ANY query returns it, and an
# ineffective partition can never lose data, only waste a few requests.

TRANS_TYPES = [
    "Standard Grant", "Continuing Grant", "Continuing grant", "Fellowship",
    "Cooperative Agreement", "Interagency Agreement", "Contract",
    "Fixed Price Award", "BOA/Task Order", "GAA",
]
STATE_CODES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "VI", "GU", "AS", "MP",
]


def fetch_day_partitions(day):
    """Partition a heavy day by transaction type (documented API param), then
    by awardee state for any type that is still heavy. All results are
    unioned by the caller, so unmatched or ignored filter values cost only
    requests, never records."""
    out = {}
    dp = date_params(day, day)
    for t in TRANS_TYPES:
        sub, dups = fetch_pages({**dp, "transType": t})
        out.update(sub)
        if len(sub) > SAFE_WINDOW or dups:
            for s in STATE_CODES:
                sub2, _ = fetch_pages({**dp, "transType": t, "awardeeStateCode": s})
                out.update(sub2)
    return out


def fetch_window(start, end):
    """Fetch all awards dated within [start, end], bisecting until safe.
    Returns the union of this window's own page results and all sub-queries."""
    by_id, dups = fetch_pages(date_params(start, end))
    if len(by_id) <= SAFE_WINDOW and not dups:
        return by_id
    if start == end:
        return {**by_id, **fetch_day_partitions(start)}
    mid = start + (end - start) // 2
    return {**by_id, **fetch_window(start, mid), **fetch_window(mid + timedelta(days=1), end)}


def month_windows(first, last):
    cur = first.replace(day=1)
    while cur <= last:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield cur, min(nxt - timedelta(days=1), last)
        cur = nxt


def norm_type(t):
    t = (t or "").lower()
    if t.startswith("standard"):
        return "std"
    if t.startswith("continuing"):
        return "cont"
    if t.startswith("fellowship"):
        return "fell"
    return "other"


def fiscal_year(d):
    return d.year + 1 if d.month >= 10 else d.year


def month_floor(d):
    return d.replace(day=1)


def months_back(d, n):
    y, m = d.year, d.month - n
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def load_store():
    """data/awards.csv is the committed store of record from prior runs."""
    path = REPO_ROOT / "data" / "awards.csv"
    if not path.exists():
        return {}
    store = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            date.fromisoformat(row["date"])  # validate before trusting the row
            store[row["id"]] = {
                "id": row["id"],
                "date": row["date"],
                "month": row["date"][:7],
                "amount": int(row["estimatedTotalAmt"]),
                "type": norm_type(row["transType"]),
                "transType": row["transType"],
                "title": row["title"],
                "awardee": row["awardeeName"],
            }
    return store


def main():
    global USE_ZERO_OFFSET
    full = "--full" in sys.argv
    today = date.today()
    try:
        probe = api_get({"org_code_div": DMS_DIV_CODE, "printFields": "id", "rpp": RPP,
                         "offset": 0, "dateStart": "01/01/2015", "dateEnd": "01/31/2015"},
                        retries=2)
        USE_ZERO_OFFSET = len(probe) > 0
    except RuntimeError:
        USE_ZERO_OFFSET = False
    print(f"offset=0 supported: {USE_ZERO_OFFSET}")
    stored = load_store()
    # Recent months are re-pulled every run: NSF backfills awards for weeks
    # after their award date, so this window is always taken from the API.
    window_start = months_back(today, RECENT_MONTHS - 1)
    if stored:
        last_stored = max(date.fromisoformat(a["date"]) for a in stored.values())
        window_start = min(window_start, month_floor(last_stored))
    if full or not stored:
        pull_start, mode = QUERY_BACK, "full"
    else:
        pull_start, mode = window_start, "incremental"
    query_end = today + QUERY_AHEAD
    print(f"{mode} pull: {pull_start} .. {query_end} ({len(stored)} awards in store)")

    collected = {}
    for mstart, mend in month_windows(pull_start, query_end):
        month_key = mstart.strftime("%Y-%m")
        for a in fetch_window(mstart, mend).values():
            d = time.strptime(a["date"], "%m/%d/%Y")
            award_date = date(d.tm_year, d.tm_mon, d.tm_mday)
            if not SERIES_START <= award_date <= today:
                continue  # padding can catch a day outside the series
            collected[a["id"]] = {
                "id": a["id"],
                "date": award_date.isoformat(),
                "month": f"{d.tm_year:04d}-{d.tm_mon:02d}",
                "amount": int(float(a.get("estimatedTotalAmt") or 0)),
                "type": norm_type(a.get("transType")),
                "transType": a.get("transType") or "",
                "title": a.get("title") or "",
                "awardee": a.get("awardeeName") or "",
            }
        n = sum(1 for a in collected.values() if a["month"] == month_key)
        print(f"  {month_key}: {n} awards")
        if n > 600:
            sys.exit(f"FATAL: {month_key} returned {n} awards - far above any plausible "
                     f"DMS volume. The division filter was probably ignored; aborting.")

    # Merge pull into the store: update or add only, never delete. Because
    # the API's filter semantics are not fully trustworthy, a stored award
    # missing from this pull is RETAINED and flagged - API downtime, filter
    # quirks, or lost history can never erase our copy. (The cost: a record
    # NSF genuinely retracts persists here until manually removed.)
    merged = dict(stored)
    merged.update(collected)
    missing = sum(1 for aid in stored if aid not in collected)
    if mode == "full" and missing > 0:
        warn(f"{missing} stored awards not returned by this full re-pull; "
             "retained from the store")

    awards = list(merged.values())
    total = len(awards)
    print(f"Total unique awards: {total}")
    if not 9_000 <= total <= 30_000:
        sys.exit(f"FATAL: implausible total {total} (verified 2026-08 figure was 11,508). "
                 "Refusing to publish.")

    # Drift check against the independently verified 2026-08-07 baseline.
    monthly_counts = {}
    for a in awards:
        monthly_counts[a["month"]] = monthly_counts.get(a["month"], 0) + 1
    for month, (base_n, _) in BASELINE.items():
        if month >= "2026-07":
            continue  # trailing months legitimately grow as NSF backfills
        got = monthly_counts.get(month, 0)
        if abs(got - base_n) > max(3, base_n * 0.05):
            warn(f"{month}: pulled {got} awards vs verified baseline {base_n}")

    # ---- Aggregate ----
    months = {}
    for a in awards:
        m = months.setdefault(a["month"], {"awards": 0, "dollars": 0,
                                           "std": 0, "cont": 0, "fell": 0, "other": 0})
        m["awards"] += 1
        m["dollars"] += a["amount"]
        m[a["type"]] += 1
    # Explicit zero rows so gaps read as 0, not missing data.
    for mstart, _ in month_windows(SERIES_START, today):
        months.setdefault(mstart.strftime("%Y-%m"),
                          {"awards": 0, "dollars": 0, "std": 0, "cont": 0, "fell": 0, "other": 0})
    monthly = [{"month": k, **v} for k, v in sorted(months.items())]

    fys = {}
    for a in awards:
        d = date.fromisoformat(a["date"])
        fy = fys.setdefault(fiscal_year(d), {
            "awards": 0, "dollars": 0, "amounts": [],
            "oj": {"awards": 0, "dollars": 0, "std": 0, "cont": 0, "fell": 0, "other": 0},
        })
        fy["awards"] += 1
        fy["dollars"] += a["amount"]
        fy["amounts"].append(a["amount"])
        if d.month not in (8, 9):  # Oct-Jul basis for partial-year comparison
            fy["oj"]["awards"] += 1
            fy["oj"]["dollars"] += a["amount"]
            fy["oj"][a["type"]] += 1

    def median(xs):
        xs = sorted(xs)
        n = len(xs)
        return 0 if n == 0 else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2)

    current_fy = fiscal_year(today)
    fy_rows = []
    for fy in sorted(fys):
        f = fys[fy]
        top3 = sorted((a for a in awards if fiscal_year(date.fromisoformat(a["date"])) == fy),
                      key=lambda a: -a["amount"])[:3]
        fy_rows.append({
            "fy": fy,
            "partial": fy == current_fy,
            "awards": f["awards"],
            "dollars": f["dollars"],
            "median": median(f["amounts"]),
            "exTop3Dollars": f["dollars"] - sum(a["amount"] for a in top3),
            "octJul": f["oj"],
            "top3": [{"id": a["id"], "title": a["title"], "awardee": a["awardee"],
                      "amount": a["amount"]} for a in top3],
        })

    out = {
        "generated": today.isoformat(),
        "source": f"{API}?org_code_div={DMS_DIV_CODE} (NSF Award Search API), "
                  "month-by-month on Original Award Date",
        "totalAwards": total,
        "currentFY": current_fy,
        "warnings": warnings,
        "monthly": monthly,
        "fiscalYears": fy_rows,
    }
    data_dir = REPO_ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "dashboard.json").write_text(json.dumps(out, indent=1))

    with open(data_dir / "awards.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "date", "estimatedTotalAmt", "transType", "title", "awardeeName"])
        for a in sorted(awards, key=lambda a: (a["date"], a["id"])):
            w.writerow([a["id"], a["date"], a["amount"], a["transType"], a["title"], a["awardee"]])

    print(f"Wrote data/dashboard.json and data/awards.csv ({total} awards, "
          f"{len(warnings)} warnings)")


if __name__ == "__main__":
    main()
