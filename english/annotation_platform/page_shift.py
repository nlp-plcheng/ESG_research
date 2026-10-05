"""Bring the dataset's page numbers onto PDF page indices, citation by citation.

The pipeline was asked for the page number printed on the report page, but
also for a number inside the PDF-index range of the chunk it was reading, so
the cited pages are a MIX: most are printed numbers (one or two short of the
PDF page on a report with a cover leaf), some are already PDF indices. A
uniform shift per report would therefore break the ones that are right. This
tool decides per citation:

  1. measure  -- for every report file (company + year) find the printed-vs-PDF
     offset: the mode of "where the quoted sentence really is minus the page
     cited", with the numbers printed on the pages as a cross-check.
  2. apply    -- move the cited pages of every report by its offset Z.
     --mode uniform (default): EVERY cited page of company X / year Y --
       the commitment's source page, every year's evidence page, every page
       reference inside the clue texts -- is shifted by Z (kind uniform).
       This is the rule the dataset owner set: the numbers the LLM wrote are
       the report's printed page numbers, so the whole report moves together.
     --mode verify: per citation, the quoted text is looked up in the PDF
       first -- the WHOLE quote on the cited page and nowhere else -> already
       a PDF page, kept (pdf); the WHOLE quote at cited+Z -> shifted
       (printed); only a fragment found, or the whole quote on both pages ->
       no proof, shifted like the rest and marked for a human (unsure);
       elsewhere / not found ->
       shifted like the rest (wrong / assumed). --keep_unverified keeps the
       unsure / wrong / not-found citations where they are instead.
     In both modes a shift that would run past the end of the file is never
     made (kind out_of_range), every field keeps its original value and the
     decision beside it, and the run aborts (rolled back) if any answer table
     would change in anything but the seen_* provenance columns.

    cd <platform folder> && source env.prod.sh
    .venv/bin/python page_shift.py measure            # -> page_offsets.json + a table (read-only)
    .venv/bin/python page_shift.py set TSMC 98 +1     # hand-checked offset for one report (optional)
    .venv/bin/python page_shift.py apply --dry_run    # per-report decision counts, nothing written
    .venv/bin/python page_shift.py apply              # backups first, then summary.json + DB items
    .venv/bin/python page_shift.py status             # what the DB currently carries

What "apply" touches, and what it never touches
  * result/<company>/summary.json: source_page, each year's evidence_page and
    the page references inside clue texts; originals kept beside them
    (source_page_orig, evidence_page_orig, clues_orig) and every decision
    recorded under "page_shift". A copy of each file goes to
    result/_page_shift_backup_<UTC>/ first.
  * the platform DB, table item only: source_page and payload (the same
    promise object), plus item.source_page_orig and item.page_offset. The DB
    is backed up with backup_db.py first. No restart needed.
  * NOT touched: worker, assignment, ante, verdict, final, draft, alloc --
    every answer, draft, allocation and everyone's progress stay as they are.
    Pages annotators typed (correct_page, source_page_fix) are theirs and are
    left alone. The export tells, per answer, which page the annotator SAW
    (page_shift.at vs the answer time) next to the standardised page.
  * Re-running is safe: decisions are recomputed from the original values,
    so the same offsets give the same result and a changed offset moves a
    page by the change only.

Flagged reports (unknown / mixed / labels disagree / no pdf) are NOT applied
unless the entry in page_offsets.json carries "manual": true or
--include_flagged is given. `set <company> <year> <offset>` records such a
hand-checked offset (a scanned report with no text layer is invisible to the
measurement: open it, find one cited passage, count the difference); every
later measure keeps it, and `set ... --clear` drops it again.
"""

import argparse
import collections
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import common
import config

HERE = os.path.dirname(os.path.abspath(__file__))
# The edition's own page-reference pattern (第N頁; the English edition also
# accepts "(p. 12)" / "page 12"): one capturing group per alternative.
PAGE_REF = getattr(common, "_PAGE_REF", None) or getattr(common, "_PAGE_IN_TEXT")
_WS = re.compile(r"\s+")
_PLAIN_PAGES = re.compile(r"^\s*\d{1,4}(\s*[,，、]\s*\d{1,4})*\s*$")
MAX_OFFSET = 12        # cover + front matter; a report needing more is flagged and done by hand
NEAR = 3               # a quote this close but not AT cited+offset is a wrong citation, not an offset
BLOCKING = ("unknown", "mixed", "labels say", "no pdf", "cannot open", "far")


def Now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def Norm(text):
    return _WS.sub("", text or "")


def Needles(text, size):
    flat = Norm(text)
    if len(flat) < size:
        return [flat] if len(flat) >= 8 else []
    starts = sorted({0, (len(flat) - size) // 2, len(flat) - size})
    return [flat[s:s + size] for s in starts]


def FindIn(texts, text):
    """1-based pages of `texts` holding a FRAGMENT of the quote (18-char probes
    at its start / middle / end, then 12-char ones). A hit says the passage
    is probably there; it does not prove the whole sentence is."""
    for size in (18, 12):
        needles = Needles(text, size)
        if not needles:
            return []
        found = [i + 1 for i, t in enumerate(texts) if any(nd in t for nd in needles)]
        if found:
            return found
    return []


def FindFull(texts, text):
    """1-based pages holding the WHOLE quote in one piece (whitespace ignored);
    [] when it is too short to be distinctive or is nowhere in one piece."""
    flat = Norm(text)
    if len(flat) < 12:
        return []
    return [i + 1 for i, t in enumerate(texts) if flat in t]


# A list that continues an explicit reference -- "(p.12,13)", "第12、13頁",
# "p. 12; 13" -- is pages too, but only when the WHOLE list reads as a page
# list: separators and 1-4 digit numbers, ending at a closing bracket, 頁,
# the end of the text, or a full stop / semicolon / comma not followed by a
# digit. Anything else means it was not a page list and nothing after the
# reference is taken: "(p.12,30.5% less)" and "(p.12,30,000 tonnes)" stay as
# they are. A bare comma followed by exactly three digits is a thousands
# separator, never a page. A range "p.12-14" yields its two ends, it is not
# expanded.
_REF_LIST = re.compile(r"(?:\s*(?!,\d{3}(?!\d))[,;，；、/\-–—~]\s*\d{1,4})+")
_REF_LIST_END = re.compile(r"\s*(?:[)\]）】頁]|$|[.。;；,，](?!\s*\d))")
_REF_NUM = re.compile(r"\d{1,4}")


def ListEnd(text, pos):
    """Where the page list continuing a reference that ends at `pos` stops;
    `pos` itself when what follows is not a page list."""
    lst = _REF_LIST.match(text, pos)
    if lst and _REF_LIST_END.match(text, lst.end()):
        return lst.end()
    return pos


def RefSpans(text):
    """(start, end, page) of every page number written as an explicit
    reference, in text order, including the rest of a list that continues one."""
    text = text or ""
    out = []
    for m in PAGE_REF.finditer(text):
        gi = next((i for i in range(1, (m.lastindex or 0) + 1) if m.group(i)), None)
        if gi is None:
            continue
        out.append((m.start(gi), m.end(gi), int(m.group(gi))))
        for num in _REF_NUM.finditer(text, m.end(gi), ListEnd(text, m.end(gi))):
            out.append((num.start(), num.end(), int(num.group())))
    return out


def RefPages(text):
    """Every page number a clue cites explicitly, in order."""
    return [p for _s, _e, p in RefSpans(text)]


def ShiftRefs(text, deltas):
    """Add a delta to every explicit page reference -- one number for all, or
    a list with one entry per reference in order -- keeping each reference's
    own format ("（第12頁）" stays "（第14頁）", "(p.12,13)" stays "(p.14,15)")."""
    spans = RefSpans(text)
    per = deltas if isinstance(deltas, list) else None
    if not spans or (per is None and not deltas):
        return text
    out, last = [], 0
    for k, (s, e, p) in enumerate(spans):
        d = per[k] if per is not None and k < len(per) else (0 if per is not None else deltas)
        out.append(text[last:s])
        out.append(str(p + d))
        last = e
    out.append(text[last:])
    return "".join(out)


_EMPTY_BRACKETS = re.compile(r"[（(\[【]\s*[)）\]】]")


def ClueBody(text):
    """The clue without its page references (the part looked up in the PDF)."""
    text = text or ""
    out, last = [], 0
    for m in PAGE_REF.finditer(text):
        gi = next((i for i in range(1, (m.lastindex or 0) + 1) if m.group(i)), None)
        out.append(text[last:m.start()])
        last = max(m.end(), ListEnd(text, m.end(gi)) if gi else m.end())
    out.append(text[last:])
    return _EMPTY_BRACKETS.sub("", "".join(out))


# ---------------------------------------------------------------- the dataset

def ReportFiles():
    out = []
    if not os.path.isdir(config.PDF_DIR):
        return out
    for company in sorted(os.listdir(config.PDF_DIR)):
        d = os.path.join(config.PDF_DIR, company)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            stem, ext = os.path.splitext(name)
            if ext.lower() == ".pdf" and stem.isdigit():
                out.append((company, int(stem)))
    return out


def SummaryPath(result_dir, company):
    return os.path.join(result_dir, company, "summary.json")


def LoadSummaries(result_dir):
    out = {}
    if not os.path.isdir(result_dir):
        return out
    for company in sorted(os.listdir(result_dir)):
        if company.startswith("_"):
            continue
        path = SummaryPath(result_dir, company)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                out[company] = json.load(f)
    return out


def Orig(p):
    """The promise's page fields as the pipeline wrote them (before any shift):
    (source_page, {year: (evidence_page, [clues])})."""
    src = p.get("source_page_orig", p.get("source_page"))
    years = {}
    for e in p.get("status") or []:
        try:
            y = int(e.get("year"))
        except (TypeError, ValueError):
            continue
        years[y] = (e.get("evidence_page_orig", e.get("evidence_page")),
                    e.get("clues_orig", e.get("clues") or []))
    return src, years


def Citations(company, promises):
    """(cited page, quote text, kind) per report year, from the ORIGINAL page
    fields, deduplicated on the text so a repeated quote votes once."""
    by_year = collections.defaultdict(dict)
    for p in promises:
        src, years = Orig(p)
        dy = p.get("declared_year")
        pages = common.ParsePages(src)
        if isinstance(dy, int) and pages and p.get("commitment"):
            by_year[dy].setdefault(Norm(p["commitment"]), (pages[0], p["commitment"], "commitment"))
        for y, (ev_page, clues) in years.items():
            e = next((s for s in p.get("status") or [] if str(s.get("year")) == str(y)), {})
            pages = common.ParsePages(ev_page)
            if pages and e.get("evidence"):
                by_year[y].setdefault(Norm(e["evidence"]), (pages[0], e["evidence"], "evidence"))
            for c in clues:
                if isinstance(c, str):
                    refs = RefPages(c)
                    if refs:
                        by_year[y].setdefault(Norm(ClueBody(c)), (refs[0], ClueBody(c), "clue"))
    return {y: list(v.values()) for y, v in by_year.items()}


# ------------------------------------------------------------------ the PDFs

class Reports:
    """Page texts (whitespace-stripped) and page counts, a few reports at a
    time. `light` loads page counts only (uniform mode needs nothing else)."""

    def __init__(self, pymupdf, keep=24, light=False):
        self.pymupdf, self.keep, self.light, self.cache = pymupdf, keep, light, {}

    def get(self, company, year):
        key = (company, year)
        if key not in self.cache:
            path = common.PdfPath(company, year)
            entry = None
            if os.path.isfile(path):
                try:
                    with self.pymupdf.open(path) as doc:
                        entry = {"count": doc.page_count}
                        if not self.light:
                            entry["texts"] = [Norm(doc.load_page(i).get_text())
                                              for i in range(doc.page_count)]
                            entry["labels"] = PageLabels(doc)
                except Exception as exc:                                 # noqa: BLE001
                    entry = {"error": f"cannot open: {exc.__class__.__name__}"}
            if len(self.cache) >= self.keep:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = entry
        return self.cache[key]


def PageLabels(doc):
    """printed number per page index (1-based): PDF page labels when the file
    has them, else a bare number at the top or bottom of the page text."""
    out = {}
    for i in range(doc.page_count):
        page = doc.load_page(i)
        printed = None
        try:
            label = (page.get_label() or "").strip()
        except Exception:                                                # noqa: BLE001
            label = ""
        if label.isdigit():
            printed = int(label)
        else:
            try:
                lines = [l.strip() for l in page.get_text().splitlines() if l.strip()]
            except Exception:                                            # noqa: BLE001
                lines = []
            for line in lines[:3] + lines[-3:]:
                if len(line) > 40:
                    continue
                m = (re.fullmatch(r"(\d{1,3})", line) or re.match(r"^(\d{1,3})\s*[|｜/／]", line)
                     or re.search(r"[|｜/／]\s*(\d{1,3})$", line)
                     or re.fullmatch(r"[-–—]?\s*(\d{1,3})\s*[-–—]?", line))
                if m:
                    printed = int(m.group(1))
                    break
        out[i + 1] = printed
    return out


# ------------------------------------------------------------------ measuring

def Mode(counter):
    total = sum(counter.values())
    if not total:
        return None, 0.0, 0
    value, n = counter.most_common(1)[0]
    return value, n / total, total


def MeasureReport(rep, cites):
    info = {"pages": 0, "quotes": {"n": 0, "mode": None, "share": 0.0, "hist": {}},
            "labels": {"n": 0, "mode": None, "share": 0.0}, "far": 0, "missing_quote": 0}
    if rep is None:
        info["flag"] = "no pdf"
        return info
    if "error" in rep:
        info["flag"] = rep["error"]
        return info
    info["pages"] = rep["count"]
    votes = collections.Counter()
    for idx, printed in rep["labels"].items():
        if printed is not None and 0 <= idx - printed <= MAX_OFFSET:
            votes[idx - printed] += 1
    m, share, n = Mode(votes)
    info["labels"] = {"n": n, "mode": m, "share": round(share, 2)}
    votes = collections.Counter()
    for cited, text, _kind in cites:
        if not (1 <= cited <= rep["count"] + MAX_OFFSET):
            continue
        hits = FindIn(rep["texts"], text)
        if not hits:
            info["missing_quote"] += 1
            continue
        delta = min(hits, key=lambda p: abs(p - cited)) - cited
        if -NEAR <= delta <= MAX_OFFSET:
            votes[delta] += 1
        else:
            info["far"] += 1
    m, share, n = Mode(votes)
    info["quotes"] = {"n": n, "mode": m, "share": round(share, 2),
                      "hist": {str(k): v for k, v in sorted(votes.items())}}
    return info


def Decide(info):
    """The report's printed-vs-PDF offset from its votes. Quotes decide when
    there are enough distinct ones; a strong page-label consensus outranks a
    handful of quotes (one wrong citation must not outvote twenty numbered
    pages)."""
    q, l = info["quotes"], info["labels"]
    flags = []
    strong_labels = l["n"] >= 10 and l["share"] >= 0.8
    if q["n"] >= 8 and q["share"] >= 0.6:
        chosen, by = q["mode"], "quotes"
    elif strong_labels:
        chosen, by = l["mode"], "labels"
    elif l["n"] >= 10 and l["share"] >= 0.5:
        chosen, by = l["mode"], "labels"
    elif q["n"] >= 3 and q["share"] >= 0.7:
        chosen, by = q["mode"], "quotes"
    else:
        chosen, by = 0, "none"
        flags.append("unknown")
    # Page labels only count against the quotes when they agree among
    # themselves: a dozen pages with a number found but no two alike is noise.
    if q["mode"] is not None and l["mode"] is not None and q["n"] >= 3 and l["n"] >= 10 \
            and l["share"] >= 0.5 and q["mode"] != l["mode"]:
        flags.append(f"labels say {l['mode']}")
    hist = q.get("hist") or {}
    if q["n"] >= 3:
        second = sorted(hist.values(), reverse=True)[1:2]
        if second and second[0] / q["n"] >= 0.3:
            flags.append("mixed")
    if info.get("flag"):
        flags.append(info["flag"])
    return chosen, by, flags


def CmdMeasure(args):
    pymupdf = common.Pymupdf()
    if pymupdf is None:
        raise SystemExit("PyMuPDF is not installed in this interpreter")
    summaries = LoadSummaries(args.result_dir)
    if not summaries:
        raise SystemExit(f"no <company>/summary.json under {args.result_dir}")
    previous = {}
    if os.path.isfile(args.offsets):
        with open(args.offsets, encoding="utf-8") as f:
            previous = (json.load(f).get("reports") or {})
    reps = Reports(pymupdf)
    files = set(ReportFiles())
    reports = {}
    for company, summary in summaries.items():
        cites = Citations(company, summary.get("promises") or [])
        for year in sorted(set(cites) | {y for c, y in files if c == company}):
            info = MeasureReport(reps.get(company, year), cites.get(year, []))
            chosen, by, flags = Decide(info)
            entry = {"offset": chosen, "decided_by": by, "pages": info["pages"],
                     "quotes": info["quotes"], "labels": info["labels"], "far": info["far"],
                     "missing_quote": info["missing_quote"], "flags": flags,
                     "n_citations": len(cites.get(year, []))}
            prev = (previous.get(company) or {}).get(str(year)) or {}
            if prev.get("manual"):
                entry.update(offset=prev["offset"], manual=True, decided_by="manual",
                             measured={"offset": chosen, "decided_by": by})
            reports.setdefault(company, {})[str(year)] = entry

    # No directory paths in the file: it travels with the dataset.
    out = {"measured_at": Now() + " UTC",
           "note": "offset = PDF page index - printed page number, per report file; "
                   "apply --mode uniform moves every cited page of the report by it, "
                   "--mode verify checks each quote first; manual: true = set by hand (page_shift.py set)",
           "reports": reports}
    with open(args.offsets, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"{'company':<8} {'year':>4} {'pages':>5} {'offset':>6}  by       "
          f"{'quotes n':>8} {'share':>5} {'far':>3} {'miss':>4}  {'labels n':>8} {'share':>5}  flags")
    n_shift = n_flag = 0
    for company in sorted(reports):
        for year in sorted(reports[company], key=int):
            e = reports[company][year]
            q, l = e["quotes"], e["labels"]
            n_shift += bool(e["offset"])
            n_flag += bool(e["flags"]) and not e.get("manual")
            print(f"{company:<8} {year:>4} {e['pages']:>5} {e['offset']:>+6}  {e['decided_by']:<8} "
                  f"{q['n']:>8} {q['share']:>5.2f} {e['far']:>3} {e['missing_quote']:>4}  "
                  f"{l['n']:>8} {l['share']:>5.2f}  {', '.join(e['flags'])}")
    total = sum(len(v) for v in reports.values())
    print(f"\n{total} report files, {n_shift} with a non-zero offset, {n_flag} flagged -> {args.offsets}")
    print("Flagged reports are NOT applied: check them by eye and record the offset with "
          "`page_shift.py set <company> <year> <offset>` (or apply --include_flagged). "
          "Then: page_shift.py apply --dry_run")


# ------------------------------------------------------------------- applying

def LoadOffsets(path, include_flagged):
    """(company, year) -> offset for the reports that may be applied, and the
    list of reports held back."""
    if not os.path.isfile(path):
        raise SystemExit(f"{path} not found -- run `page_shift.py measure` first")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    table, held = {}, []
    for company, years in (data.get("reports") or {}).items():
        for year, e in years.items():
            off = int(e.get("offset") or 0)
            blocked = [fl for fl in e.get("flags") or [] if fl.startswith(BLOCKING)]
            if blocked and not e.get("manual") and not include_flagged:
                held.append((company, int(year), off, blocked))
                table[(company, int(year))] = None   # held back: leave what is stored alone
                continue
            table[(company, int(year))] = off
    return table, held


def Classify(cited, text, off, rep, assume, mode="uniform"):
    """(delta to apply, kind) for one citation. See the module docstring."""
    if not off or rep is None or "error" in rep:
        return 0, "none"
    count = rep["count"]
    if not 1 <= cited + off <= count:      # past the last page, or before the first
        return 0, "out_of_range"
    if mode == "uniform":
        return off, "uniform"
    # Only the WHOLE quote counts as proof: on the cited page and nowhere
    # else -> kept (pdf); at cited+offset and not on the cited page -> moved
    # (printed). A fragment hit (a shared heading, a repeated opening) on
    # either candidate page, or the whole quote on both, proves nothing: the
    # report's rule applies and the citation is marked unsure for a human.
    full = FindFull(rep["texts"], text) if text else []
    if full == [cited]:
        return 0, "pdf"
    if full and cited + off in full and cited not in full:
        return off, "printed"
    hits = full or (FindIn(rep["texts"], text) if text else [])
    if hits and (cited in hits or cited + off in hits):
        return (off if assume else 0), "unsure"
    if not assume:
        return 0, ("wrong" if hits else "unknown")
    return off, ("wrong" if hits else "assumed")


def AnswerDigest(conn):
    """A fingerprint of every answer table -- every row, every column except
    the provenance columns apply itself fills (seen_*) and the draft
    generation it retires -- so an apply can prove it changed nothing else."""
    import hashlib
    h = hashlib.sha256()
    counts = {}
    for table, skip in (("worker", ()), ("assignment", ()), ("ante", ("seen_source_page",)),
                        ("verdict", ("seen_evidence_page",)), ("final", ()), ("draft", ("gen",)),
                        ("alloc", ())):
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})") if r["name"] not in skip]
        n = 0
        for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1, 2"):
            h.update(repr(tuple(row)).encode("utf-8"))
            n += 1
        counts[table] = n
    return h.hexdigest(), counts


def ShiftField(value, d):
    """'84' -> '86', '84,85' -> '86,87'; None when no page number is in it."""
    pages = common.ParsePages(value)
    if not pages:
        return None
    seen, out = set(), []
    for p in pages:
        q = p + d
        if q > 0 and q not in seen:
            seen.add(q)
            out.append(str(q))
    return ",".join(out) if out else None


def Rebuild(pages, rec):
    """The converted pages of a field, from its decision record (one delta per
    page when the field cites several)."""
    per = {x["p"]: x["d"] for x in rec.get("pages") or []}
    return [pg + (per[pg] if pg in per else (rec.get("d") or 0)) for pg in pages]


def Join(pages):
    seen, out = set(), []
    for q in pages:
        if q > 0 and q not in seen:
            seen.add(q)
            out.append(str(q))
    return ",".join(out)


def Note(mode, assume):
    """What the kinds mean for THIS run. In every record `d` is the delta that
    was actually applied to the stored value (0 = kept); a kind never
    overrides it."""
    tail = ("d = delta applied to the stored value (0 = kept); out_of_range = the shift would "
            "leave the file (kept); held = report held back (untouched); none = report offset 0 "
            "(unchanged)")
    if mode == "uniform":
        return f"pages are PDF page indices; uniform = moved by the report offset; {tail}"
    if assume:
        return ("pages are PDF page indices; pdf = whole quote found on the cited page only (kept); "
                "printed = whole quote found at cited+offset (moved); unsure = only a fragment of "
                "the quote found on a candidate page, or the whole quote on both (moved by the "
                "offset -- check by hand); "
                f"wrong = quote elsewhere (moved); assumed = quote not found (moved); {tail}")
    return ("pages are PDF page indices; pdf = whole quote found on the cited page only (kept); "
            "printed = whole quote found at cited+offset (moved); unsure / wrong / unknown = kept "
            f"(--keep_unverified); {tail}")


def ShiftPromise(p, company, table, reps, assume, kinds, log, where, mode="uniform"):
    """Recompute every page of one promise from its original value; returns
    2 when a stored page moved, 1 when only the decision record changed, else
    0. Every page of a field
    that cites several ("2,5") is decided on its own. A report held back
    (offset None) leaves its fields -- and their earlier decisions -- exactly
    as they are."""
    src_orig, years_orig = Orig(p)
    dy = p.get("declared_year")
    prev_all = p.get("page_shift") or {}
    prev_years = prev_all.get("years") or {}
    record = {"src": None, "years": {}}
    before = json.dumps({k: v for k, v in prev_all.items() if k not in ("at", "updated", "note")},
                        sort_keys=True, ensure_ascii=False)
    moved = False

    def Field(orig, text, year, prev):
        """(new value, or None to leave the stored value alone; record)."""
        pages = common.ParsePages(orig)
        if not pages:
            return None, {"orig": orig, "d": 0, "kind": "none"}
        off = table.get((company, year), 0)
        if off is None:
            kinds[(company, year, "held")] += 1
            return None, (prev if prev and prev.get("kind") not in (None, "none")
                          else {"orig": orig, "d": 0, "kind": "held"})
        rep = reps.get(company, year) if off else None
        per = []
        for pg in pages:
            d, kind = Classify(pg, text, off, rep, assume, mode)
            kinds[(company, year, kind)] += 1
            per.append({"p": pg, "d": d, "kind": kind})
        rec = {"orig": orig, "d": per[0]["d"], "kind": per[0]["kind"]}
        if len(per) > 1:
            rec["pages"] = per
        if not any(x["d"] for x in per):
            return (orig if isinstance(orig, str) else str(orig)), rec
        if not _PLAIN_PAGES.match(str(orig)):
            log.append(f"{where} {year}: {orig!r} rewritten as plain page numbers")
        return Join(Rebuild(pages, rec)), rec

    if isinstance(dy, int) and src_orig:
        new, rec = Field(src_orig, p.get("commitment") or "", dy, prev_all.get("src"))
        record["src"] = rec
        if new is not None:
            moved = moved or new != p.get("source_page")
            p["source_page_orig"] = src_orig
            p["source_page"] = new
    for e in p.get("status") or []:
        try:
            y = int(e.get("year"))
        except (TypeError, ValueError):
            continue
        ev_orig, clues_orig = years_orig[y]
        pv = prev_years.get(str(y)) or {}
        yrec = {"evidence": None, "clues": []}
        if ev_orig:
            new, rec = Field(ev_orig, e.get("evidence") or "", y, pv.get("evidence"))
            yrec["evidence"] = rec
            if new is not None:
                moved = moved or new != e.get("evidence_page")
                e["evidence_page_orig"] = ev_orig
                e["evidence_page"] = new
        off = table.get((company, y), 0)
        if off is None:
            # Held back: the clue texts stay as stored, with their earlier decisions.
            prev_clues = pv.get("clues") or []
            for i, c in enumerate(clues_orig):
                refs = RefPages(c) if isinstance(c, str) else []
                if refs:
                    kinds[(company, y, "held")] += 1
                yrec["clues"].append(prev_clues[i] if (refs and i < len(prev_clues))
                                     else {"d": 0, "kind": "held" if refs else "none"})
            record["years"][str(y)] = yrec
            continue
        rep = reps.get(company, y) if off else None
        new_clues = []
        for c in clues_orig:
            refs = RefPages(c) if isinstance(c, str) else []
            if not refs:
                new_clues.append(c)
                yrec["clues"].append({"d": 0, "kind": "none"})
                continue
            per = []
            for pg in refs:
                d, kind = Classify(pg, ClueBody(c), off, rep, assume, mode)
                kinds[(company, y, kind)] += 1
                per.append({"p": pg, "d": d, "kind": kind})
            crec = {"d": per[0]["d"], "kind": per[0]["kind"]}
            if len(per) > 1:
                crec["refs"] = per
            new_clues.append(ShiftRefs(c, [x["d"] for x in per]))
            yrec["clues"].append(crec)
        if new_clues != (e.get("clues") or []):
            moved = True
        if new_clues != clues_orig or "clues_orig" in e:
            e["clues_orig"] = clues_orig
        e["clues"] = new_clues
        record["years"][str(y)] = yrec

    after = json.dumps(record, sort_keys=True, ensure_ascii=False)
    note = Note(mode, assume)
    if after == before and not moved and prev_all.get("note") == note:
        return 0
    # `at` = the first time a stored page of this promise actually changed;
    # it stays unset while only decisions (held, kept) are recorded.
    record["at"] = prev_all.get("at") or (Now() if moved else None)
    record["updated"] = Now()
    record["note"] = note
    p["page_shift"] = record
    return 2 if moved else 1


def CmdApply(args):
    pymupdf = common.Pymupdf()
    if pymupdf is None:
        raise SystemExit("PyMuPDF is not installed in this interpreter")
    table, held = LoadOffsets(args.offsets, args.include_flagged)
    summaries = LoadSummaries(args.result_dir)
    if not summaries:
        raise SystemExit(f"no <company>/summary.json under {args.result_dir}")
    reps = Reports(pymupdf, light=(args.mode == "uniform"))
    kinds = collections.Counter()
    log = []
    assume = not args.keep_unverified

    changed_files, n_promises, n_moved = {}, 0, 0
    for company, summary in summaries.items():
        n = 0
        for i, p in enumerate(summary.get("promises") or []):
            res = ShiftPromise(p, company, table, reps, assume, kinds, log, f"{company}#{i}", args.mode)
            if res:
                n += 1
                n_moved += res == 2
        if n:
            changed_files[company] = summary
            n_promises += n

    conn = common.Connect(args.db)
    updates = []
    db_moved = 0
    db_kinds = collections.Counter()
    for r in conn.execute("SELECT item_id, company, source_page, payload FROM item ORDER BY company, item_id"):
        p = json.loads(r["payload"])
        res = ShiftPromise(p, r["company"], table, reps, assume, db_kinds, [], f"item {r['item_id']}", args.mode)
        if res:
            db_moved += res == 2
            updates.append((json.dumps(p, ensure_ascii=False), str(p.get("source_page") or ""),
                            r["source_page"], json.dumps(p["page_shift"], ensure_ascii=False),
                            r["item_id"]))
    n_items = conn.execute("SELECT COUNT(*) FROM item").fetchone()[0]

    # ---- report
    print(f"mode: {args.mode}")
    print(f"{'company':<8} {'year':>4} {'offset':>6}  {'moved':>6} {'pdf':>5} {'printed':>7} {'unsure':>6} "
          f"{'assumed':>7} {'wrong':>5} {'unknown':>7} {'out':>3}")
    for (company, year), off in sorted(table.items()):
        if not off:
            continue
        c = {k: kinds[(company, year, k)] for k in
             ("uniform", "pdf", "printed", "unsure", "assumed", "wrong", "unknown", "out_of_range")}
        print(f"{company:<8} {year:>4} {off:>+6}  {c['uniform']:>6} {c['pdf']:>5} {c['printed']:>7} "
              f"{c['unsure']:>6} {c['assumed']:>7} {c['wrong']:>5} {c['unknown']:>7} {c['out_of_range']:>3}")
    tot = collections.Counter()
    for (_c, _y, k), n in kinds.items():
        tot[k] += n
    moved_by_rule = tot["unsure"] + tot["assumed"] + tot["wrong"]
    converted = tot["uniform"] + tot["pdf"] + tot["printed"] + (moved_by_rule if assume else 0)
    not_conv = tot["out_of_range"] + tot["held"] + (0 if assume else tot["unknown"] + tot["wrong"] + tot["unsure"])
    # kind none = a citation in a report whose offset is 0: already a PDF page.
    # This counts the numbering convention, not whether each citation is right.
    print(f"\ncitations in summary.json on the reports' PDF numbering after this run "
          f"(a conversion count, not a check of each citation): {converted + tot['none']} "
          f"(in reports with offset 0, unchanged: {tot['none']}; moved by the report offset {tot['uniform']}; "
          f"verify mode: kept, whole quote on the cited page only {tot['pdf']}; moved, whole quote "
          f"at cited+offset {tot['printed']}; moved by the rule without proof: unsure {tot['unsure']} "
          f"(fragment only / whole quote on both pages -- check by hand), quote elsewhere {tot['wrong']}, "
          f"quote not found {tot['assumed']})")
    print(f"NOT converted: {not_conv} (beyond the file {tot['out_of_range']}, "
          f"in held-back reports {tot['held']}"
          f"{'' if assume else ', kept unverified ' + str(tot['unknown'] + tot['wrong'] + tot['unsure'])})")
    if held:
        print(f"\n{len(held)} report file(s) held back (flagged; page_shift.py set <company> <year> <offset>, "
              "or --include_flagged):")
        for company, year, off, fl in held:
            print(f"   {company}/{year} (measured {off:+d}): {', '.join(fl)}")
    if log:
        print(f"\n{len(log)} note(s) on odd page fields:")
        for line in log[:20]:
            print("   " + line)
    print(f"\nsummary.json: a page moves in {n_moved} promise(s); {n_promises} promise(s) in "
          f"{len(changed_files)} file(s) get a page_shift record (decisions are recorded even where nothing moves)")
    print(f"DB items    : a page moves in {db_moved} of {n_items}; {len(updates)} get a record")
    if args.dry_run:
        print("\ndry run: nothing written")
        return
    if not updates and not changed_files:
        print("\nnothing to do")
        return

    # ---- backups, refusing to go on without them
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    print("\nbacking up the DB ...")
    rc = subprocess.call([sys.executable, os.path.join(HERE, "backup_db.py"), "--db", args.db])
    if rc != 0:
        raise SystemExit("✗ backup_db.py failed -- nothing changed")
    bdir = os.path.join(args.result_dir, f"_page_shift_backup_{stamp}")
    for company in changed_files:
        dst = os.path.join(bdir, company, "summary.json")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(SummaryPath(args.result_dir, company), dst)
    print(f"summary.json copies -> {bdir}")

    # ---- write: DB in one transaction, then the files (atomic rename each).
    # The answer tables are fingerprinted before and after: if anything but
    # the seen_* provenance columns and the draft generation differs, the
    # whole transaction is rolled back and nothing is written.
    conn.execute("BEGIN IMMEDIATE")
    try:
        common.Migrate(conn)
        digest_before, counts = AnswerDigest(conn)
        # Before any page moves: every existing answer that does not yet carry
        # the pages it was judged against gets them now -- the values stored
        # at this moment ARE what those answers saw, since nothing has moved
        # yet. (Only the seen_* provenance columns are written; no answer
        # value changes.) Pages still open in a browser were rendered against
        # the old numbers too: their draft generation is retired so the next
        # save or submit from them is refused and the page re-renders with the
        # new numbers and a note.
        n_ante = conn.execute(
            """UPDATE ante SET seen_source_page = (
                   SELECT i.source_page FROM assignment a JOIN item i ON i.item_id = a.item_id
                    WHERE a.assign_id = ante.assign_id)
                WHERE seen_source_page IS NULL""").rowcount
        fills = []
        for v in conn.execute(
                """SELECT v.assign_id, v.year, i.payload FROM verdict v
                     JOIN assignment a ON a.assign_id = v.assign_id
                     JOIN item i ON i.item_id = a.item_id
                    WHERE v.seen_evidence_page IS NULL"""):
            for entry in json.loads(v["payload"]).get("status") or []:
                if str(entry.get("year")) == str(v["year"]):
                    fills.append((entry.get("evidence_page") or "", v["assign_id"], v["year"]))
                    break
        conn.executemany("UPDATE verdict SET seen_evidence_page = ? WHERE assign_id = ? AND year = ?",
                         fills)
        n_gen = conn.execute(
            """UPDATE draft SET gen = gen + 1 WHERE assign_id IN
               (SELECT assign_id FROM assignment WHERE stage IN (1, 2))""").rowcount
        print(f"recorded the pages {n_ante} answer(s) / {len(fills)} year row(s) were judged against; "
              f"{n_gen} open page(s) will reload")
        conn.executemany(
            """UPDATE item SET payload = ?, source_page = ?,
                               source_page_orig = COALESCE(source_page_orig, ?),
                               page_offset = ? WHERE item_id = ?""", updates)
        digest_after, counts_after = AnswerDigest(conn)
        if digest_after != digest_before or counts_after != counts:
            conn.execute("ROLLBACK")
            raise SystemExit("✗ an answer table would have changed beyond the seen_* columns "
                             "-- rolled back, nothing written")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()
    print("✓ answer tables unchanged (every row, every value): "
          + ", ".join(f"{t} {n}" for t, n in counts.items()))
    for company, summary in changed_files.items():
        path = SummaryPath(args.result_dir, company)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    print(f"✓ applied: pages moved in {db_moved} DB item(s) / {n_moved} promise(s); records written to "
          f"{len(updates)} item(s), {n_promises} promise(s) in {len(changed_files)} file(s)")
    print("  answers, drafts, assignments and progress were not touched.")


def CmdSet(args):
    """Record a hand-checked offset for one report in page_offsets.json
    ("manual": true, kept by every later measure), or drop it with --clear."""
    if not os.path.isfile(args.offsets):
        raise SystemExit(f"{args.offsets} not found -- run `page_shift.py measure` first")
    with open(args.offsets, encoding="utf-8") as f:
        data = json.load(f)
    for key in ("pdf_dir", "result_dir"):        # paths an older measure wrote
        data.pop(key, None)
    e = ((data.get("reports") or {}).get(args.company) or {}).get(str(args.year))
    if e is None:
        raise SystemExit(f"{args.company}/{args.year} is not in {args.offsets}: check the company "
                         "folder name and the year, or run measure first")
    if args.clear:
        if not e.get("manual"):
            raise SystemExit(f"{args.company}/{args.year} has no manual offset")
        m = e.pop("measured", {})
        e.pop("manual", None)
        e["offset"], e["decided_by"] = m.get("offset", 0), m.get("decided_by", "none")
        what = f"back to the measured {e['offset']:+d} ({e['decided_by']})"
    else:
        if not e.get("manual"):
            e["measured"] = {"offset": e.get("offset", 0), "decided_by": e.get("decided_by", "none")}
        e.update(offset=args.offset, manual=True, decided_by="manual")
        what = (f"manual {args.offset:+d} (measured {e['measured']['offset']:+d}; "
                f"flags: {', '.join(e.get('flags') or []) or 'none'})")
    tmp = args.offsets + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.offsets)
    print(f"{args.company}/{args.year}: {what} -> {args.offsets}")
    print("next: page_shift.py apply --dry_run")


def CmdStatus(args):
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(item)")}
    if "page_offset" not in cols:
        print("no page shift has been applied to this DB")
        return
    kinds = collections.Counter()
    n = 0
    for r in conn.execute("SELECT company, page_offset FROM item WHERE page_offset IS NOT NULL"):
        n += 1
        ps = json.loads(r["page_offset"] or "{}")
        if ps.get("src"):
            kinds[(r["company"], ps["src"]["kind"])] += 1
        for y in (ps.get("years") or {}).values():
            if y.get("evidence"):
                kinds[(r["company"], y["evidence"]["kind"])] += 1
            for c in y.get("clues") or []:
                kinds[(r["company"], c["kind"])] += 1
    print(f"{n} item(s) carry a page_shift record")
    for (company, kind), cnt in sorted(kinds.items()):
        print(f"   {company:<8} {kind:<13} {cnt}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--result_dir", default=config.RESULT_DIR)
    ap.add_argument("--offsets", default=os.path.join(HERE, "page_offsets.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("measure", help="measure every report's offset (read-only)")
    a = sub.add_parser("apply", help="decide every citation and shift the printed ones")
    a.add_argument("--dry_run", action="store_true")
    a.add_argument("--mode", choices=("uniform", "verify"), default="uniform",
                   help="uniform (default): every cited page of a report moves by its offset; "
                        "verify: keep citations whose quote is already on the cited PDF page")
    a.add_argument("--keep_unverified", action="store_true",
                   help="verify mode: leave citations without proof (unsure / wrong / not found) "
                        "untouched (default: shift them like the rest of the report)")
    a.add_argument("--include_flagged", action="store_true",
                   help="apply flagged reports too (otherwise only clean or manual ones)")
    s = sub.add_parser("set", help="record a hand-checked offset for one report (manual: true)")
    s.add_argument("company", help="folder name under the pdf / result dirs")
    s.add_argument("year", type=int)
    s.add_argument("offset", type=int, nargs="?", default=0,
                   help="PDF page index minus the number the report prints (+1 = a cover leaf; "
                        "0 = the cited numbers are PDF pages already)")
    s.add_argument("--clear", action="store_true", help="drop the manual offset, back to the measured one")
    sub.add_parser("status", help="decision counts currently stored in the DB")
    args = ap.parse_args()
    {"measure": CmdMeasure, "apply": CmdApply, "set": CmdSet, "status": CmdStatus}[args.cmd](args)


if __name__ == "__main__":
    main()
