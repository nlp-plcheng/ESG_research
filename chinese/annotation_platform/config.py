"""Runtime configuration for the ESG annotation platform (Chinese edition).

Every value can be overridden with an environment variable so the same tree can
run locally (SQLite on local disk, fake participant ids) and in production
(see env.prod.sh.example).

Where the data lives -- set these two if your layout differs:

    ESG_RESULT_DIR   pipeline output:  <result_dir>/<company>/summary.json
    ESG_PDF_DIR      the reports:      <pdf_dir>/<company>/<ROC year>.pdf

By default both are the `result/` and `pdf/` folders next to this platform
folder -- i.e. the edition folder the pipeline was run from.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EDITION = os.path.dirname(_HERE)

# SQLite must NOT live on the NFS mount: POSIX locking there is unreliable and
# concurrent annotators will hit "database is locked". Keep it on local disk.
DB_PATH = os.getenv("ESG_DB_PATH", "/var/tmp/esg_annotation/annotation.db")

RESULT_DIR = os.getenv("ESG_RESULT_DIR", os.path.join(_EDITION, "result"))
PDF_DIR = os.getenv("ESG_PDF_DIR", os.path.join(_EDITION, "pdf"))
PAGE_IMAGE_DIR = os.getenv("ESG_PAGE_IMAGE_DIR", os.path.join(_HERE, "static", "pages"))
# Optional {"<company>": "<industry>"} map shown in the item header.
INDUSTRY_JSON = os.getenv("ESG_INDUSTRY_JSON", os.path.join(_HERE, "industry.json"))

SECRET_KEY = os.getenv("ESG_SECRET_KEY", "dev-only-change-me")
ADMIN_KEY = os.getenv("ESG_ADMIN_KEY", "")

# How many independent annotators see the same item. An assignment counts
# from the moment it is opened, and is never released on a timer: someone who
# leaves mid-task simply resumes it next time, and the item is never handed to
# a 4th person meanwhile. Calibration (gold) items are exempt from the cap.
REDUNDANCY = int(os.getenv("ESG_REDUNDANCY", "3"))
# Control-stratum (unflagged) items may get fewer people than anomaly items
# when annotator time is short -- e.g. anomaly x3, control x1. Defaults to
# REDUNDANCY; 0 = control items are not served at all.
REDUNDANCY_CONTROL = int(os.getenv("ESG_REDUNDANCY_CONTROL", str(REDUNDANCY)))
# Share of tasks drawn from the anomaly stratum. The rest are sampled uniformly
# at random from ordinary promises, so the dataset is not 100% weird cases and
# the risk model sees a realistic base rate.
ANOMALY_RATIO = float(os.getenv("ESG_ANOMALY_RATIO", "0.7"))
# Items served per Prolific submission (drives the study's time estimate).
# 0 = no per-person cap (volunteer mode: annotators stop whenever they like).
TASKS_PER_SESSION = int(os.getenv("ESG_TASKS_PER_SESSION", "10"))
# Legacy: gold items are now calibration items served to every worker FIRST in
# a fixed order (see Task()); this knob no longer affects dispatch.
GOLD_EVERY = int(os.getenv("ESG_GOLD_EVERY", "5"))
# Legacy speed-floor knobs. The submit gate was replaced by client+server
# completeness checks; per-task seconds are still recorded for analysis.
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
# PyMuPDF's Pixmap.save only writes png/pnm/jpg/...; jpeg keeps page images small
# enough to load over a slow connection.
PAGE_JPEG_QUALITY = int(os.getenv("ESG_PAGE_JPEG_QUALITY", "80"))

PROLIFIC_COMPLETE_URL = os.getenv(
    "ESG_PROLIFIC_COMPLETE_URL", "https://app.prolific.com/submissions/complete?cc="
)
PROLIFIC_CODE = os.getenv("ESG_PROLIFIC_CODE", "CHANGEME")
PROLIFIC_SCREENOUT_CODE = os.getenv("ESG_PROLIFIC_SCREENOUT_CODE", "")

# Allow ?pid=... entry without Prolific params (local testing / in-lab pilots).
# Turn this OFF before a real Prolific run so nobody can bypass the ID capture.
ALLOW_LOCAL_PID = os.getenv("ESG_ALLOW_LOCAL_PID", "1") == "1"

# Comma-separated whitelist of accepted IDs (applies to both ?pid= and
# PROLIFIC_PID). Empty = accept anyone. Set it in volunteer mode so a stranger
# who gets hold of the URL cannot enter with a made-up pid.
_ALLOW = [p.strip() for p in os.getenv("ESG_PID_ALLOWLIST", "").split(",") if p.strip()]

# Test accounts: always admitted (whenever an allowlist is in force), see every
# item with no cap or quota, never count toward an item's REDUNDANCY, and are
# dropped from every export and from the admin statistics.
TEST_PIDS = {p.strip() for p in os.getenv("ESG_TEST_PIDS", "self-check").split(",") if p.strip()}

# Total number of annotators. > 0 switches dispatch to a fixed, balanced
# allocation: every non-calibration item is pre-assigned to REDUNDANCY distinct
# people so per-person item counts differ by at most one and priority-score
# totals stay close (allocate.py). 0 = first-come dispatch.
N_ANNOTATORS = int(os.getenv("ESG_N_ANNOTATORS", "0"))

# Real annotators, in allowlist order. With N_ANNOTATORS > 0 only the first N
# are admitted (and `run.sh links` prints only their links), so the balanced
# allocation is never diluted by extra people. Test pids ride along.
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

# Set when served through cloudflared / nginx: trust X-Forwarded-* and mark the
# session cookie secure.
BEHIND_PROXY = os.getenv("ESG_BEHIND_PROXY", "0") == "1"

# Volunteer run (self-recruited annotators, no payment): consent/done pages
# drop the Prolific wording -- payment, completion code, "return to Prolific".
VOLUNTEER_MODE = os.getenv("ESG_VOLUNTEER_MODE", "0") == "1"

# Four labels, agreed with the annotation team. The pipeline's own 部分達成
# (and the rare 未達成) are shown as 尚未達成 (common.DisplayStatus); the
# buttons carry only the label, the definitions live on the instructions page.
#   已達成    the target is clearly met
#   尚未達成  moving toward the target, but not at the expected standard yet
#   遠離目標  worse than before -- further from the target
#   未提及    the report says nothing about progress or status
STATUS_CHOICES = ["已達成", "尚未達成", "遠離目標", "未提及"]

# The overall conclusion (到目標年為止) uses three labels only -- no 遠離目標
# there. The pipeline's 部分達成 counts as 未達成 for this comparison
# (common.FinalStatus).
FINAL_CHOICES = ["已達成", "未達成", "未提及"]

# verdict.achieve_prob stores a 0-100 slider value (0 = almost impossible,
# 100 = almost certain). Each year's slider defaults to the previous year's
# value, so an untouched slider means "no change from last year".

# Optional: reveal the yearly blocks one at a time (still one page, later years
# stay locked until the current one is answered). Slower to answer; off by
# default because fast reading of the whole trace matters more.
PROGRESSIVE_REVEAL = os.getenv("ESG_PROGRESSIVE_REVEAL", "0") == "1"

# Values stored in verdict.evidence_quality:
#   ok      -- verdict marked correct as-is
#   current -- verdict wrong; the quoted evidence alone is the right basis
#   clue    -- one or more same-year clues are the basis (indices in alt_clue)
#   mixed   -- the quoted evidence plus clue(s)
#   other   -- none of the listed items, but the annotator found other evidence
#              (human_status != 未提及; correct_page says where, custom_basis optional)
#   none    -- none of the listed items and nothing else either -> 未提及

# Per-year triage of the AI's verdict: the fast path (correct) is one click.
# "wrong" opens the follow-ups: tick the correct evidence basis (any mix of
# the quoted evidence / same-year clues, or "none of these"), re-judge the
# status, and give the page unless the status is 未提及.
VERDICT_CHOICES = [
    ("correct", "正確：判定與證據都合理"),
    ("wrong", "錯誤：證據或判定有問題"),
]

WINDOW_CHOICES = [
    ("ok", "正確：該追的年份都追了"),
    ("target_wrong", "目標年份判讀錯誤（請填正確的民國年）"),
    ("missing_years", "追得不夠：後面還有年份應該要檢查"),
    ("extra_years", "追過頭：有些年份根本不該納入"),
    ("unsure", "看不出來"),
]
