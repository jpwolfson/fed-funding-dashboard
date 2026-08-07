# fed-funding-dashboard

A self-updating dashboard of new awards made by the **NSF Division of Mathematical
Sciences (DMS)**, from October 2014 to the present.

**Live dashboard:** https://jpwolfson.github.io/fed-funding-dashboard/

## How it works

- `scripts/pull_nsf_dms.py` pulls every DMS award from the public
  [NSF Award Search API](https://www.research.gov/common/webapi/awardapisearch-v1.htm)
  (`org_code_div=03040000`), month by month on Original Award Date, and writes
  `data/dashboard.json` (aggregates the page reads) and `data/awards.csv`
  (one row per award).
- `.github/workflows/update.yml` runs the pull **every Monday**, commits refreshed
  data, and deploys the site to GitHub Pages. It also runs on every push to `main`
  and can be triggered manually from the Actions tab.
- `index.html` is the dashboard — a single static page, no build step, no
  dependencies.

## The pagination fault

The NSF API returns duplicate records across pages when a single query spans many
pages, and each duplicate silently displaces a record that is never returned —
a naive paginated pull loses ~5% of awards. The pull script therefore keeps every
query window small: it pulls month by month and recursively bisects any window
returning more than 60 records or showing cross-page duplicates (falling back to
partitioning a single heavy day by award amount). Every run is also cross-checked
against `scripts/verified_baseline.json`, a monthly series independently verified
against the API on 2026-08-07; deviations beyond backfill tolerance surface as
warnings on the dashboard, and implausible totals abort the run rather than
publish bad data.

## Data caveats

- **Dollars are intended totals** (`estimatedTotalAmt`), not obligations or
  outlays, read as currently stated — i.e. including later amendments. Older
  cohorts have had longer to accumulate upward amendments, biasing recent years
  low by an unmeasured amount.
- **The dollar series is outlier-driven.** Institute-scale awards can be 15–20%
  of a year's dollars in the top three awards alone; the dashboard splits the
  top 3 out of the stacked dollars chart for this reason.
- **Oct–Jul basis.** Fiscal years run Oct–Sep; cross-year comparisons use the
  Oct–Jul window so a year in progress compares like-for-like with completed
  years.
- **Mechanism mix matters.** A shift from continuing to standard grants lowers
  the new-continuing-award count without necessarily cutting funding; interpret
  the mechanism chart before reading a count decline as a cut.

## Local development

```sh
python3 scripts/pull_nsf_dms.py   # needs outbound access to api.nsf.gov; ~5 min
python3 -m http.server            # then open http://localhost:8000/
```
