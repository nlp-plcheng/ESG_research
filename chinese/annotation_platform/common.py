"""Shared helpers: page-reference parsing, cited-page expansion, DB access."""

import json
import os
import re
import sqlite3

import config

_PAGE_IN_TEXT = re.compile(r"第\s*(\d+)\s*頁")
_ANY_INT = re.compile(r"\d+")

STATUS_LETTER = {"已達成": "A", "部分達成": "P", "尚未達成": "P", "未達成": "F", "遠離目標": "F",
                 "未提及": "N"}

# The pipeline grades with three labels; annotators answer with four. Its
# 部分達成 (and the rare 未達成) are shown as 尚未達成 -- every badge, and every
# comparison against a human answer, goes through this.
_DISPLAY_STATUS = {"部分達成": "尚未達成", "未達成": "尚未達成"}
# The overall conclusion has three labels: anything short of achieved is 未達成.
_FINAL_STATUS = {"部分達成": "未達成", "尚未達成": "未達成", "遠離目標": "未達成"}


def DisplayStatus(status):
    """A yearly status as the annotators see it (four labels)."""
    return _DISPLAY_STATUS.get(status, status)


def FinalStatus(status):
    """An overall conclusion as the annotators see it (three labels)."""
    return _FINAL_STATUS.get(status, status)


def NormalizeForm(data):
    """Bring a stored draft or answer form up to the current label sets: the
    overall conclusion to its three labels, yearly statuses to their four.
    Values may be strings or lists (multi-select fields)."""
    out = {}
    for key, value in (data or {}).items():
        fn = FinalStatus if key == "human_final_status" else DisplayStatus if key.startswith("st_") else None
        if fn is not None:
            value = [fn(v) for v in value] if isinstance(value, list) else fn(value)
        out[key] = value
    return out


def Connect(path=None):
    """Open the platform DB with sane concurrency settings."""
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def InitDb(path=None):
    schema = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(schema, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = Connect(path)
    conn.executescript(sql)
    Migrate(conn)
    return conn


def Migrate(conn):
    """Add columns introduced after a DB was first created, so existing
    collections survive a schema change instead of needing reset.py --all."""
    added = []
    for table, column, decl in (
        ("verdict", "achieve_prob", "TEXT"),
        ("verdict", "correct_page", "TEXT"),
        ("verdict", "alt_clue", "TEXT"),
        ("verdict", "custom_basis", "TEXT"),
        ("ante", "commitment_fix", "TEXT"),
        ("ante", "target_year_fix", "INTEGER"),
        ("ante", "active_seconds", "INTEGER"),
        # Is the commitment really on the page the pipeline cited?
        # ok / wrong_page / not_found; NULL = answered before the check existed.
        ("ante", "source_page_ok", "TEXT"),
        ("ante", "source_page_fix", "TEXT"),
        # The cited page exactly as this annotator saw it when answering (the
        # page carries it in a hidden field), so a later page_shift.py
        # conversion can never be mistaken for what was judged. Kept from the
        # first submission; a revision does not overwrite it.
        ("ante", "seen_source_page", "TEXT"),
        ("verdict", "seen_evidence_page", "TEXT"),
        ("item", "stratum", "TEXT NOT NULL DEFAULT 'control'"),
        ("item", "gold_rank", "INTEGER"),
        # page_shift.py: the source page as the pipeline wrote it (printed page
        # number) and the offsets applied to bring the item onto PDF page indices.
        ("item", "source_page_orig", "TEXT"),
        ("item", "page_offset", "TEXT"),
        ("worker", "is_test", "INTEGER NOT NULL DEFAULT 0"),
        ("worker", "slot", "INTEGER"),
        ("assignment", "edited_at", "TEXT"),
        ("assignment", "n_edits", "INTEGER NOT NULL DEFAULT 0"),
        # Set while an open task is parked by "skip"; NULL = in the normal queue.
        ("assignment", "skipped_at", "TEXT"),
        ("final", "window_ok", "TEXT"),
        ("final", "correct_target_year", "INTEGER"),
        ("final", "n_page_views", "INTEGER NOT NULL DEFAULT 0"),
        ("draft", "seq", "INTEGER NOT NULL DEFAULT 0"),
        ("draft", "gen", "INTEGER NOT NULL DEFAULT 0"),
        # Order of the last star/note save: the generation of the page render
        # that made it (RegisterRender's, strictly increasing with every
        # render) and that page's own sequence number. A save is applied only
        # when its (gen, seq) is higher than the stored pair, so neither a slow
        # earlier save from the same page nor any save from an older render
        # of the page can overwrite what a newer one stored.
        ("mark", "seq", "INTEGER NOT NULL DEFAULT 0"),
        ("mark", "gen", "INTEGER NOT NULL DEFAULT 0"),
        # JSON {page: [highest running number applied from that page, and
        # the gen, seq it was applied as]}, a page being one render of the
        # task page (its generation, which the page never changes). Lets the
        # server keep each page's saves in order whatever lineage they claim
        # and whether or not they are forced -- a save overtaken by a later
        # one from the same page is refused for good, even after other pages
        # have written in between -- and answer a retried save as it was
        # answered the first time instead of applying it twice.
        ("mark", "srcs", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if tables and "mark" not in tables:
        # The annotator's own star + free-text note on an item (one row per
        # assignment). Private working notes: shown back to the person on the
        # task page and in their history, and to the admin; never part of the
        # answer itself. Created before the column loop above runs on it.
        conn.execute(
            "CREATE TABLE mark (assign_id INTEGER PRIMARY KEY REFERENCES assignment (assign_id), "
            "starred INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', updated_at TEXT, "
            "seq INTEGER NOT NULL DEFAULT 0, gen INTEGER NOT NULL DEFAULT 0, "
            "srcs TEXT NOT NULL DEFAULT '{}')")
        added.append("mark")
    if tables and "alloc" not in tables:
        conn.execute(
            "CREATE TABLE alloc (item_id INTEGER NOT NULL REFERENCES item (item_id), "
            "slot INTEGER NOT NULL, PRIMARY KEY (item_id, slot))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alloc_slot ON alloc (slot)")
        added.append("alloc")
    if tables and "draft" not in tables:
        conn.execute(
            "CREATE TABLE draft (assign_id INTEGER PRIMARY KEY REFERENCES assignment (assign_id), "
            "form TEXT NOT NULL, saved_at TEXT, seq INTEGER NOT NULL DEFAULT 0, "
            "gen INTEGER NOT NULL DEFAULT 0)")
        added.append("draft")
    return added


def ParsePages(value):
    """Pull page numbers out of whatever shape evidence_page happens to be.

    Seen in the wild: 12, "12", "第 12 頁", "12,13", "p.12-14", ["12", "第 30 頁"].
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(ParsePages(v))
        return out
    if isinstance(value, int):
        return [value] if value > 0 else []
    text = str(value)
    nums = [int(m) for m in _PAGE_IN_TEXT.findall(text)]
    if not nums:
        nums = [int(m) for m in _ANY_INT.findall(text)]
    return [n for n in nums if 0 < n < 2000]


def CitedPages(entry, pad=None, cap=None):
    """Pages a reviewer needs to see to check one year's verdict.

    The union of the evidence_page reference and every page the same-year clues
    explicitly cite (第 N 頁). Only explicit page references are trusted inside
    clue text -- bare numbers there are usually quantities, not pages.
    """
    pad = config.PAGE_PAD if pad is None else pad
    cap = config.MAX_PAGES_PER_YEAR if cap is None else cap

    core = ParsePages(entry.get("evidence_page"))
    for clue in entry.get("clues") or []:
        text = clue if isinstance(clue, str) else json.dumps(clue, ensure_ascii=False)
        core.extend(int(m) for m in _PAGE_IN_TEXT.findall(text))
    core = [n for n in core if 0 < n < 2000]
    if not core:
        return []

    pages = set()
    for p in core:
        for q in range(p - pad, p + pad + 1):
            if q > 0:
                pages.add(q)
    ordered = sorted(pages)
    if len(ordered) > cap:
        # Keep the pages closest to an actually-cited page.
        ordered.sort(key=lambda q: (min(abs(q - p) for p in core), q))
        ordered = sorted(ordered[:cap])
    return ordered


def CluePages(text):
    """Pages one clue cites explicitly ('（第14頁）'), in order, deduplicated."""
    out = []
    for m in _PAGE_IN_TEXT.findall(text):
        n = int(m)
        if 0 < n < 2000 and n not in out:
            out.append(n)
    return out


def YearRows(promise):
    """Normalise promise['status'] into render-ready rows, oldest year first."""
    rows = []
    for entry in promise.get("status") or []:
        year = entry.get("year")
        try:
            year = int(year)
        except (TypeError, ValueError):
            continue
        clues = entry.get("clues") or []
        clues = [c if isinstance(c, str) else json.dumps(c, ensure_ascii=False) for c in clues]
        rows.append(
            {
                "year": year,
                "ai_status": entry.get("status") or "未提及",
                # What the annotator sees (and what verdict.ai_status stores).
                "ai_label": DisplayStatus(entry.get("status") or "未提及"),
                "evidence": (entry.get("evidence") or "").strip(),
                "evidence_page": entry.get("evidence_page"),
                "evidence_pages": ParsePages(entry.get("evidence_page")),
                "clues": clues,
                # Parallel to clues: the page(s) each one cites, so the task
                # page can prefill the "which page supports this" field.
                "clue_pages": [CluePages(c) for c in clues],
                "pages": CitedPages(entry),
            }
        )
    rows.sort(key=lambda r: r["year"])
    return rows


def SeqString(rows):
    return " ".join(f"{r['year']}:{r['ai_status']}" for r in rows)


def LetterString(rows):
    return " ".join(STATUS_LETTER.get(r["ai_status"], "?") for r in rows)


def Pymupdf():
    """Return the PyMuPDF module, or None if it is not installed.

    PyMuPDF 1.24.3 renamed the import from `fitz` to `pymupdf` and warns on the
    old name; keep working on both.
    """
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        try:
            import fitz

            return fitz
        except ImportError:
            return None


def PdfPath(company, roc_year):
    return os.path.join(config.PDF_DIR, company, f"{roc_year}.pdf")


def PageImagePath(company, roc_year, page):
    return os.path.join(config.PAGE_IMAGE_DIR, company, str(roc_year), f"{page:04d}.jpg")


def LoadIndustryMap():
    if os.path.isfile(config.INDUSTRY_JSON):
        with open(config.INDUSTRY_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}
