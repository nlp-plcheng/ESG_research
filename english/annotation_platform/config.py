"""Runtime configuration for the ESG annotation platform (English edition).

Every value can be overridden with an environment variable, so the same tree
runs locally (SQLite on local disk, made-up participant ids) and in production
(see env.prod.sh.example).

Where the data lives -- set these two if your layout differs:

    ESG_RESULT_DIR   pipeline output:  <result_dir>/<company>/summary.json
    ESG_PDF_DIR      the reports:      <pdf_dir>/<company>/<year>.pdf

By default both are the `result/` and `pdf/` folders next to this platform
folder -- i.e. the edition folder the pipeline was run from.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EDITION = os.path.dirname(_HERE)

# SQLite must NOT live on a network mount (NFS / SMB): file locking there is
# unreliable and concurrent annotators hit "database is locked". Local disk only.
# (Its own folder, so this edition never shares a DB with another platform
# instance on the same machine.)
DB_PATH = os.getenv("ESG_DB_PATH", "/var/tmp/esg_annotation_en/annotation.db")

RESULT_DIR = os.getenv("ESG_RESULT_DIR", os.path.join(_EDITION, "result"))
PDF_DIR = os.getenv("ESG_PDF_DIR", os.path.join(_EDITION, "pdf"))
PAGE_IMAGE_DIR = os.getenv("ESG_PAGE_IMAGE_DIR", os.path.join(_HERE, "static", "pages"))
# Optional {"<company>": "<industry>"} map shown in the item header.
INDUSTRY_JSON = os.getenv("ESG_INDUSTRY_JSON", os.path.join(_HERE, "industry.json"))

# How report years are written in summary.json and in the pdf file names, and
# therefore how they are shown to annotators:
#   roc  ROC (Minguo) years, e.g. 109 -> shown as "ROC 109 (2020)"
#   ad   Gregorian years, e.g. 2020 -> shown as "2020"
YEAR_STYLE = os.getenv("ESG_YEAR_STYLE", "roc").lower()
# A corrected year typed by an annotator must fall in this range.
YEAR_RANGE = (90, 140) if YEAR_STYLE == "roc" else (1990, 2100)

# The pipeline's raw status tokens. They are Traditional Chinese by design --
# the pipeline's table parsers key off them -- and annotators never see them
# (common.DisplayStatus maps them to the English labels below). Change these
# only if you changed the pipeline prompts to emit other tokens.
AI_ACHIEVED = os.getenv("ESG_AI_ACHIEVED", "已達成")
AI_PARTIAL = os.getenv("ESG_AI_PARTIAL", "部分達成")
AI_NOT_MENTIONED = os.getenv("ESG_AI_NOT_MENTIONED", "未提及")
AI_NOT_ACHIEVED = os.getenv("ESG_AI_NOT_ACHIEVED", "未達成")  # rare in practice
# How a page reference is written inside clue text. The pipeline writes
# "（第12頁）"; "(p. 12)" and "page 12" are accepted too. One capturing group
# per alternative, holding the FIRST page; a list that continues it
# ("（第12、13頁）", "(p.12,13)") is read whole by common.PageRefs. Only such
# explicit references are trusted as page numbers (bare numbers in a clue
# are usually quantities).
PAGE_PATTERN = os.getenv("ESG_PAGE_PATTERN",
                         r"第\s*(\d+)(?:\s*[、,，;；]\s*\d{1,4})*\s*頁|\bp(?:age|\.)?\s*(\d+)\b")

SECRET_KEY = os.getenv("ESG_SECRET_KEY", "dev-only-change-me")
ADMIN_KEY = os.getenv("ESG_ADMIN_KEY", "")

# How many independent annotators see the same item. An assignment counts
# from the moment it is opened and is never released on a timer: someone who
# leaves mid-item simply resumes it next time, and the item is never handed to
# one person more than REDUNDANCY. Calibration items are exempt from the cap.
REDUNDANCY = int(os.getenv("ESG_REDUNDANCY", "3"))
# Control-stratum (unflagged) items may get fewer people than flagged items
# when annotator time is short -- e.g. flagged x3, control x1. Defaults to
# REDUNDANCY; 0 = control items are not served at all.
REDUNDANCY_CONTROL = int(os.getenv("ESG_REDUNDANCY_CONTROL", str(REDUNDANCY)))
# Share of items drawn from the flagged stratum in first-come mode. The rest
# are sampled uniformly from ordinary commitments, so the dataset keeps a
# realistic base rate.
ANOMALY_RATIO = float(os.getenv("ESG_ANOMALY_RATIO", "0.7"))
# Items per person before the "done" page (drives a paid study's time
# estimate). 0 = no cap: annotators stop whenever they like.
TASKS_PER_SESSION = int(os.getenv("ESG_TASKS_PER_SESSION", "10"))
# Legacy: gold items are now calibration items served to everyone FIRST, in a
# fixed order (see app.Task); this knob no longer affects dispatch.
GOLD_EVERY = int(os.getenv("ESG_GOLD_EVERY", "5"))
# Legacy speed-floor knobs. The submit gate was replaced by client+server
# completeness checks; per-item seconds are still recorded for analysis.
MIN_SECONDS_TASK = int(
    os.getenv("ESG_MIN_SECONDS_TASK", os.getenv("ESG_MIN_SECONDS_STAGE2", "40")))
MIN_SECONDS_PER_YEAR = int(os.getenv("ESG_MIN_SECONDS_PER_YEAR", "10"))

# The per-year PDF excerpt holds exactly the cited pages (evidence source +
# clue-cited pages). PAGE_PAD adds context pages on both sides if ever needed.
PAGE_PAD = int(os.getenv("ESG_PAGE_PAD", "0"))
MAX_PAGES_PER_YEAR = int(os.getenv("ESG_MAX_PAGES_PER_YEAR", "8"))
# Hard ceiling on one excerpt, so a commitment citing many pages cannot turn
# into a download the size of the report. (MAX_PAGES_PER_YEAR already caps the
# cited set well below this; it is a belt-and-braces limit on the route.)
MAX_EXCERPT_PAGES = int(os.getenv("ESG_MAX_EXCERPT_PAGES", "30"))
PAGE_DPI = int(os.getenv("ESG_PAGE_DPI", "110"))
# PyMuPDF's Pixmap.save only writes png/pnm/jpg/...; jpeg keeps page images
# small enough to load over a slow connection.
PAGE_JPEG_QUALITY = int(os.getenv("ESG_PAGE_JPEG_QUALITY", "80"))

PROLIFIC_COMPLETE_URL = os.getenv(
    "ESG_PROLIFIC_COMPLETE_URL", "https://app.prolific.com/submissions/complete?cc="
)
PROLIFIC_CODE = os.getenv("ESG_PROLIFIC_CODE", "CHANGEME")
PROLIFIC_SCREENOUT_CODE = os.getenv("ESG_PROLIFIC_SCREENOUT_CODE", "")

# Allow ?pid=... entry without Prolific parameters (local testing, in-house
# annotators). Turn it OFF for a real Prolific run so nobody can bypass the
# id capture.
ALLOW_LOCAL_PID = os.getenv("ESG_ALLOW_LOCAL_PID", "1") == "1"

# Comma-separated allowlist of accepted ids (applies to both ?pid= and
# PROLIFIC_PID). Empty = accept anyone. Set it for an in-house run, so a
# stranger who gets hold of the URL cannot enter with a made-up id.
_ALLOW = [p.strip() for p in os.getenv("ESG_PID_ALLOWLIST", "").split(",") if p.strip()]

# Test accounts: always admitted (whenever an allowlist is in force), see every
# item with no cap or quota, never count toward an item's REDUNDANCY, and are
# left out of every export and of the admin statistics.
TEST_PIDS = {p.strip() for p in os.getenv("ESG_TEST_PIDS", "self-check").split(",") if p.strip()}

# Total number of annotators. > 0 switches dispatch to a fixed, balanced
# allocation: every non-calibration item is pre-assigned to REDUNDANCY distinct
# people so per-person item counts differ by at most one and priority-score
# totals stay close (allocate.py). 0 = first-come dispatch.
N_ANNOTATORS = int(os.getenv("ESG_N_ANNOTATORS", "0"))

# Real annotators, in allowlist order. With N_ANNOTATORS > 0 only the first N
# are admitted (and `run.sh links` prints only their links), so the balanced
# allocation is never diluted by extra people. Test ids ride along.
ANNOTATOR_PIDS = [p for p in _ALLOW if p not in TEST_PIDS]
if N_ANNOTATORS > 0:
    ANNOTATOR_PIDS = ANNOTATOR_PIDS[:N_ANNOTATORS]
PID_ALLOWLIST = (set(ANNOTATOR_PIDS) | TEST_PIDS) if _ALLOW else set()

# Shared calibration items served first to everyone, in the same order: the
# CALIBRATION_N items with the highest anomaly score (ingest.ScorePromise), i.e.
# the straight top of the priority ranking. Exempt from the redundancy cap and
# from the balanced allocation.
CALIBRATION_N = int(os.getenv("ESG_CALIBRATION_N", "5"))
# Optional: a curated calibration set instead of the automatic pick -- a JSON
# list of {"company", "declared_year", "commitment", "expect"?} (see
# ingest.LoadGold). Used by ingest.py and restart_web.sh whenever set, so the
# curated set survives every restart; leave empty for the automatic pick.
GOLD_FILE = os.getenv("ESG_GOLD_FILE", "")

# Set when served through cloudflared / nginx: trust X-Forwarded-* and mark
# the session cookie secure.
BEHIND_PROXY = os.getenv("ESG_BEHIND_PROXY", "0") == "1"

# Volunteer run (self-recruited annotators, no payment): consent/done pages
# drop the Prolific wording -- payment, completion code, "return to Prolific".
VOLUNTEER_MODE = os.getenv("ESG_VOLUNTEER_MODE", "0") == "1"

# The labels annotators use. Yearly verdicts have four (the pipeline's
# "partially achieved" is shown as "Not yet achieved"); the overall conclusion
# has three (anything short of achieved is "Not achieved").
STATUS_CHOICES = ["Achieved", "Not yet achieved", "Moving away from target", "Not mentioned"]
NOT_MENTIONED = "Not mentioned"
FINAL_CHOICES = ["Achieved", "Not achieved", "Not mentioned"]

# verdict.achieve_prob stores a 0-100 slider value (0 = almost impossible,
# 100 = almost certain). Each year's slider defaults to the previous year's
# value, so an untouched slider means "no change from last year".

# Reveal the yearly blocks one at a time (still one page, later years stay
# locked until the current one is answered). Slower, but keeps early years
# from being coloured by later ones. Off by default.
PROGRESSIVE_REVEAL = os.getenv("ESG_PROGRESSIVE_REVEAL", "0") == "1"

# Values stored in verdict.evidence_quality:
#   ok      -- verdict marked correct as-is
#   current -- verdict wrong; the quoted evidence alone is the right basis
#   clue    -- one or more same-year clues are the basis (indices in alt_clue)
#   mixed   -- the quoted evidence plus clue(s)
#   other   -- none of the listed items, but the annotator found other evidence
#              (human_status != Not mentioned; correct_page says where)
#   none    -- none of the listed items and nothing else either -> Not mentioned

# Per-year triage of the AI's verdict: the fast path (correct) is one click.
# "wrong" opens the follow-ups: tick the correct evidence basis (any mix of the
# quoted evidence / same-year clues, or "none of these"), re-judge the status,
# and give the page unless the status is Not mentioned.
VERDICT_CHOICES = [
    ("correct", "Correct: verdict and evidence both sound"),
    ("wrong", "Wrong: the evidence or the verdict is off"),
]

WINDOW_CHOICES = [
    ("ok", "Correct: every year that should be tracked was tracked"),
    ("target_wrong", "Target year misread (enter the correct year)"),
    ("missing_years", "Not enough: later years should have been checked too"),
    ("extra_years", "Too far: some years should not have been included"),
    ("unsure", "Can't tell"),
]
