# ESG Commitment Verification — Annotation Platform (English edition)

A small Flask + SQLite web app where annotators check the pipeline's year-by-year
tracking of corporate ESG commitments. One commitment plus all of its yearly AI
verdicts is one item, answered on a single page; the answers become a dataset of
corrected verdicts and year-by-year achievement-probability trajectories.

The pipeline's output stays exactly as `openai_build_summary_json.py` writes it
(its status tokens are Traditional Chinese by design — see the edition README);
the platform maps them to English labels and never shows the raw tokens.

## 1. What you need

- Python 3.9+ with a virtualenv (no root needed), `tmux` and `curl` on the server.
- The pipeline output and the reports, in this layout (paths are configurable):

```
<edition folder>/                 e.g. ESG_research/english/
  pdf/<company>/<year>.pdf        the reports, one file per report year
  result/<company>/summary.json   one per company, from openai_build_summary_json.py
  annotation_platform/            this folder
```

`<company>` is any folder name; it is shown to annotators as-is. `<year>` must
be the same number that appears as `year` in `summary.json` (ROC years by
default — `109` = 2020 — or Gregorian years with `ESG_YEAR_STYLE=ad`). The
platform serves annotators a small PDF excerpt of only the cited pages, so the
originals are never sent whole.

If your data lives elsewhere, set `ESG_RESULT_DIR` and `ESG_PDF_DIR` in
`env.prod.sh` (or `env.test.sh`); nothing else needs to change.

Optional: an `industry.json` in this folder (`{"<company>": "<industry>"}`) puts
the industry next to the company name in the item header; without it nothing is shown.

`ingest.py` refuses to start when the result folder is missing or holds no
`<company>/summary.json`, and says which path it looked at.

## 2. Quick start

```bash
cd annotation_platform
bash run.sh setup        # venv + packages + cloudflared (once)
bash run.sh test         # test profile: ingest, then serve on http://127.0.0.1:8081/?pid=test01
bash run.sh wipe         # delete the test DB when done playing
```

`test` uses `env.test.sh` (separate DB, no proxy). Open the printed URLs; on a
remote server forward the port first: `ssh -N -L 8081:localhost:8081 <user>@<server>`.

Defaults — port **8081**, database under `/var/tmp/esg_annotation_en/`, tmux
session `esg-en` — are deliberately distinct from the Chinese edition's, and
`stop` / `restart_web.sh` only signal processes verified to be this instance's
(working directory + port; pid files are never trusted on their own), so both
editions can run on one machine. To use another port or tmux session name,
`export ESG_PORT=<port>` / `export ESG_SESSION=<name>` in your shell before
running any script (they are not read from `env.prod.sh`).

## 3. Configure a real run

```bash
bash run.sh prod-init    # creates env.prod.sh from the example with fresh secrets, ingests the items
```

Then edit `env.prod.sh`. The lines that matter:

| Variable | Meaning |
|---|---|
| `ESG_RESULT_DIR`, `ESG_PDF_DIR` | where `summary.json` files and the PDFs are (defaults: `../result`, `../pdf`) |
| `ESG_DB_PATH` | the SQLite file — **local disk only**, never a network mount |
| `ESG_YEAR_STYLE` | `roc` (109 → shown as “ROC 109 (2020)”) or `ad` (2020) |
| `ESG_PID_ALLOWLIST` | comma-separated participant ids; each id is a personal link and the only credential. `bash add_pids.sh 6` generates random ones |
| `ESG_N_ANNOTATORS` | team size. `>0` splits the items evenly (±1) with balanced difficulty; `0` = first come, first served |
| `ESG_REDUNDANCY` / `ESG_REDUNDANCY_CONTROL` | people per flagged item / per control item (e.g. 3 / 1) |
| `ESG_CALIBRATION_N` | shared calibration items everyone does first (default 5; automatic pick: the top N by anomaly score `priority`) |
| `ESG_GOLD_FILE` | optional curated calibration set instead: a JSON list of `{"company","declared_year","commitment","expect"?}` matching existing items (see `ingest.LoadGold`); `expect` uses the platform's own labels and enables pass/fail scoring |
| `ESG_TEST_PIDS` | test accounts (default `self-check`): see every item, hold no seat, excluded from exports |
| `ESG_VOLUNTEER_MODE`, `ESG_ALLOW_LOCAL_PID`, `ESG_TASKS_PER_SESSION` | volunteer run (`1`, `1`, `0`) or Prolific run (`0`, `0`, `10` + completion code) |
| `ESG_AI_*`, `ESG_PAGE_PATTERN` | only if you changed the pipeline prompts to emit other status tokens / page markers |

Never commit or share `env.prod.sh` (it is git-ignored): it holds the secret key,
the admin key and the participant ids.

## 4. Start, get the links, restart

```bash
bash launch.sh           # start everything (fills in empty secrets first), verify the allowlist, print every personal link
bash run.sh links        # print the links again (only the first ESG_N_ANNOTATORS ids can enter)
bash run.sh status       # is it up?
bash restart_web.sh      # apply a CONFIG change: migrate DB, re-pick calibration, re-allocate, restart
bash reload_web.sh       # apply a CODE/TEMPLATE change: back up the DB, add any new columns, restart
                         #   gunicorn. Existing answers and the allocation are never modified.
.venv/bin/python backup_db.py   # a consistent online snapshot, any time (never `cp` the DB -- WAL mode)
.venv/bin/python page_shift.py measure           # per report file: printed-page vs PDF-page offset (read-only)
.venv/bin/python page_shift.py apply --dry_run   # then `apply`: move every cited page onto PDF numbering
                                                 #   (backups first; answers are never touched -- see DATASET_SCHEMA.md §2b)
bash run.sh tunnel       # restart only the public tunnel
bash run.sh export       # exports/esg_item.csv, esg_year.csv, esg_risk.csv
bash run.sh stop
```

The admin page is `<address>/admin?key=<ESG_ADMIN_KEY>` (progress, coverage,
per-person counts, “latest submitted answers” to confirm that answers really
land, CSV downloads). “Per-annotator statistics →” (`/admin/annotators`) puts
every person's correction rate, source-page verdicts, probability distribution,
minutes per item and evidence look-ups side by side with a team row — the page
for spotting someone clicking “correct” without reading. The figures come from
the same function as each annotator's own “My stats”, so the two cannot drift.
“Results by item” (`/admin/items`) lists every person's answer to each item side by
side (filter to items with disagreement, or to one person's items); “Calibration
items” (`/admin/calibration`) shows the shared items year by year with the majority
label, the agreement share and the quartiles of the 0–100 estimates, plus how closely
each person tracks the majority. “Stars, notes and questions” (`/admin/notes`) collects
what people leave on items: the stars and private notes from the task page, the reasons
given for “not a commitment” / “no target year”, and corrected wordings — newest first.
All four pages take the admin key and are read-only.

### Public address

`run.sh serve` exposes the local port through a free Cloudflare quick tunnel
(`tunnel_loop.sh`: no root, no open inbound port, restarted automatically when it
dies). A quick tunnel gets a **new random address every time it starts**, so for
a multi-week run set up the fixed link page once:

1. Create a public GitHub repository, e.g. `esg-link`, with a README; Settings →
   Pages → deploy from branch `main`, folder `/ (root)`.
2. On the server: `git clone git@github.com:<you>/esg-link.git ~/esg-link` and
   make sure `git push` works without a prompt (SSH key or a repo deploy key
   with write access).
3. In `env.prod.sh`: `export ESG_LINK_REPO_DIR=$HOME/esg-link` and
   `export ESG_LINK_PAGE_URL=https://<you>.github.io/esg-link/`.
4. `bash run.sh tunnel`. From now on `links` prints
   `https://<you>.github.io/esg-link/?pid=…` for everyone; the page forwards to
   the current tunnel address (query string included) and is updated
   automatically whenever the address changes (`logs/publish.log`).

Keep that repository dedicated to the redirect page — `publish_link.sh` refuses
to push if anything else is tracked, staged, or present in unpushed history.

To survive a reboot without root: `crontab -e` and add
`@reboot sleep 60 && cd /path/to/annotation_platform && bash run.sh serve >> logs/reboot.log 2>&1`.

## 5. What annotators see

Consent → instructions → items. Per item: Q0 “is this a commitment?” (yes / not a
commitment / no target year / unsure — “no target year” is for text that reads like
a commitment but names no year to meet it by, so there is nothing to track; it ends
the item like “not a commitment”, with an optional note) plus “is it really on the
page the AI cited?” (no → wrong page reference / commitment not found; “not found”
ends the item too), then for every tracked year: “is the AI's verdict correct?” (if
not: tick the correct basis — quoted evidence / clues / none of these — re-judge the
status with four labels, give the page), and a 0–100 slider for the probability of
meeting the target; finally the year-range check and the overall conclusion (three
labels). Years unlock one at a time; the AI's overview and conclusion appear only
after all years are answered. Everything autosaves; submitted items can be revised.
A corrected wording entered in Q0 replaces the commitment heading for that person
(the original stays underneath). Under the commitment, each person can star an item
(“unsure, come back to this”) and keep a private note; both save on their own, show
in “My answers” (which can list starred items only) and to the admin, and are not
part of the answer (they are exported as `starred` / `note` in `esg_item.csv`).

Labels: yearly **Achieved / Not yet achieved / Moving away from target / Not
mentioned**; overall **Achieved / Not achieved / Not mentioned**. The pipeline's
“partially achieved” is shown as “Not yet achieved” per year and counts as “Not
achieved” overall.

“My stats” in the header (opens in its own tab) is `/mystats`: that annotator's
own summary — progress, how often they corrected the AI, the AI's label vs
theirs, the distribution of their probability slider, evidence look-ups. It is
read-only and scoped to their own worker_id, so it shows nobody else's numbers
and opening it mid-task cannot disturb an answer.

## Known limits

- The pipeline in `..` instructs the model in English but still asks for
  Traditional-Chinese commitment/evidence text and ROC years (`../PROMPTS_EN.md`);
  the platform itself is language-agnostic (`ESG_YEAR_STYLE=ad`, English page
  patterns), so running the whole flow on English reports needs those prompts
  adapted first.
- Cited page numbers are used as page indices into the PDF file (page N of the
  file). The pipeline's numbers are mostly the page numbers *printed* on the
  report pages, which differ from the PDF index by the cover and front matter,
  so run `page_shift.py measure` / `apply` once after ingesting (it moves every
  cited page of a report by that report's offset and records the originals;
  `DATASET_SCHEMA.md` §2b). A citation entirely outside the file gets a plain
  "no such pages" reply instead of an error.

## 6. Output

| File | Granularity | Use |
|---|---|---|
| `esg_item.csv` | one row per (item, annotator) | AI vs human overall conclusion, commitment validity, flags, timing |
| `esg_year.csv` | one row per (item, year, annotator) | the correction record + per-year 0–100 probability |
| `esg_risk.csv` | one row per item (majority vote) | features → probability trajectory → human-verified outcome |

Test accounts are excluded from all three. Calibration items are part of the
dataset like any other item (the `is_gold` column tells them apart) — and since
they skip the redundancy cap and everyone answers them, they carry more
annotators per item than a normal one, so their majority vote is the firmest.

`DATASET_SCHEMA.md` documents every column of the three files, the label scheme
(fulfilment / evidence basis / verifiability as three separate axes), and how the
participant id must be replaced by an anonymous annotator label before any copy
leaves the collecting machine.

## 7. Files

```
app.py          Flask app: dispatch, the single-page item, drafts, revisions, PDF excerpts, admin
common.py       label mapping, page-reference parsing, DB access, migrations
config.py       every setting (all overridable by ESG_* environment variables)
ingest.py       summary.json -> DB, anomaly scoring, calibration items
allocate.py     balanced allocation of items to annotator slots
export.py       the three CSVs
reset.py        wipe answers / one annotator / everything
schema.sql      tables
run.sh          setup / test / prod-init / serve / tunnel / links / status / stop / export
launch.sh       one-shot start with checks;  add_pids.sh  more ids
restart_web.sh  apply a config change;       reload_web.sh  apply code/templates only
backup_db.py    consistent online DB snapshot (run it before any deployment)
page_shift.py   printed-page -> PDF-page conversion of the cited pages (measure / set / apply / status)
tunnel_loop.sh  keeps the tunnel alive;      publish_link.sh  updates the fixed link page
templates/, static/style.css
DATASET_SCHEMA.md   every column of the three CSVs, the label scheme, page numbering, de-identification
```
