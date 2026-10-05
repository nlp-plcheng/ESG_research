"""Flask task-dispatch platform for human verification of ESG commitment tracking.

One commitment plus all of its yearly AI verdicts is one item, answered on a
single page:

  1. Is this a real, verifiable commitment? ("no" ends the item right there.)
  2. Per year: is the AI's verdict correct -- is the evidence/reasoning sound?
     The fast path (correct) is one click; problems open the correction fields.
     Then, from the achievement status the clues show so far, the annotator
     rates 0-100 how likely the target-year record is to be met (each slider
     defaults to the previous year's value). That estimate may drift year by
     year -- the drift IS the risk trajectory the downstream model trains on.
  3. Wrap-up: tracking-window check and the overall conclusion.

Entry points:
    personal link   https://<host>/?pid=<participant id>          (ESG_ALLOW_LOCAL_PID=1)
    Prolific        https://<host>/?PROLIFIC_PID={{%PROLIFIC_PID%}}&STUDY_ID={{%STUDY_ID%}}&SESSION_ID={{%SESSION_ID%}}
"""

import collections
import io
import json
import os
import re
import time

from flask import (Flask, abort, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

import common
import config

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# Tables and columns added after a DB was first created (common.Migrate) are
# put in place at start-up, so a code upgrade needs nothing beyond a reload.
# Only ever adds; existing rows are untouched.
try:
    _conn = common.Connect()
    common.Migrate(_conn)
    _conn.close()
except Exception as _exc:                                           # noqa: BLE001
    app.logger.warning("schema migration at start-up skipped: %s", _exc)

if config.BEHIND_PROXY:
    # Behind cloudflared / nginx the app only sees http on localhost, so url_for
    # and the session cookie both need the forwarded scheme to be honoured.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(SESSION_COOKIE_SECURE=True, PREFERRED_URL_SCHEME="https")


@app.context_processor
def InjectMode():
    # Templates swap the Prolific wording (payment, completion code) for
    # volunteer wording when there is no paid study behind the run.
    return {"volunteer": config.VOLUNTEER_MODE}


# The pipeline's raw tokens shown as the annotators' labels, and the colour
# class a label gets in badges / summary chips.
_STATUS_CLASS = {"Achieved": "a", "Not yet achieved": "p", "Moving away from target": "f",
                 "Not mentioned": "n"}
app.jinja_env.filters["status"] = common.DisplayStatus
app.jinja_env.filters["final"] = common.FinalStatus
app.jinja_env.filters["status_class"] = lambda s: _STATUS_CLASS.get(common.DisplayStatus(s), "n")
app.jinja_env.filters["yr"] = common.YearLabel


# --------------------------------------------------------------------------- db


def Db():
    if "db" not in g:
        g.db = common.Connect()
    return g.db


@app.teardown_appcontext
def CloseDb(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def Now():
    """UTC, so it can be compared against SQLite's datetime('now')."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def CurrentWorker():
    worker_id = session.get("worker_id")
    if not worker_id:
        return None
    return Db().execute("SELECT * FROM worker WHERE worker_id = ?", (worker_id,)).fetchone()


def RequireWorker():
    worker = CurrentWorker()
    if worker is None:
        abort(redirect(url_for("Index")))
    # The allowlist is authoritative even for sessions established before it
    # was configured -- otherwise an old cookie keeps a stranger in forever.
    if config.PID_ALLOWLIST and worker["prolific_pid"] not in config.PID_ALLOWLIST:
        session.clear()
        abort(redirect(url_for("Index")))
    return worker


# ------------------------------------------------------------------ entry / flow


def NextSlot(db):
    """Slot for a new real annotator in balanced mode: the lowest unused one.
    Once every slot is taken, extra people share slots round-robin -- the
    per-item cap still keeps any item from reaching one person too many."""
    if config.N_ANNOTATORS <= 0:
        return None
    used = {r["slot"] for r in db.execute(
        "SELECT slot FROM worker WHERE is_test = 0 AND slot IS NOT NULL")}
    free = [s for s in range(config.N_ANNOTATORS) if s not in used]
    if free:
        return free[0]
    n = db.execute("SELECT COUNT(*) AS n FROM worker WHERE is_test = 0").fetchone()["n"]
    return n % config.N_ANNOTATORS


@app.route("/")
def Index():
    pid = request.args.get("PROLIFIC_PID") or (
        request.args.get("pid") if config.ALLOW_LOCAL_PID else None
    )
    if pid and config.PID_ALLOWLIST and pid not in config.PID_ALLOWLIST:
        pid = None  # unknown id -> same "cannot identify you" page as no id
    if not pid:
        return render_template("consent.html", worker=None, no_pid=True,
                               n_tasks=config.TASKS_PER_SESSION)

    db = Db()
    is_test = 1 if pid in config.TEST_PIDS else 0
    row = db.execute("SELECT * FROM worker WHERE prolific_pid = ?", (pid,)).fetchone()
    if row is None:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT INTO worker (prolific_pid, study_id, session_id, first_seen, is_test, slot) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pid, request.args.get("STUDY_ID"), request.args.get("SESSION_ID"), Now(),
                 is_test, None if is_test else NextSlot(db)),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    elif row["is_test"] != is_test:
        db.execute("UPDATE worker SET is_test = ? WHERE worker_id = ?", (is_test, row["worker_id"]))
    row = db.execute("SELECT * FROM worker WHERE prolific_pid = ?", (pid,)).fetchone()
    session["worker_id"] = row["worker_id"]

    if row["status"] == "done":
        if config.TASKS_PER_SESSION > 0:
            return redirect(url_for("Done"))
        # Uncapped run: "done" only meant the pool was empty at the time.
        db.execute("UPDATE worker SET status = 'active' WHERE worker_id = ?",
                   (row["worker_id"],))
    if row["consented_at"]:
        return redirect(url_for("Instructions"))
    return render_template("consent.html", worker=row, no_pid=False,
                           n_tasks=config.TASKS_PER_SESSION)


@app.route("/consent", methods=["POST"])
def Consent():
    worker = RequireWorker()
    if request.form.get("agree") != "yes":
        Db().execute("UPDATE worker SET status = 'screened_out' WHERE worker_id = ?",
                     (worker["worker_id"],))
        return render_template("done.html", worker=worker, screened_out=True,
                               code=config.PROLIFIC_SCREENOUT_CODE or config.PROLIFIC_CODE,
                               complete_url=config.PROLIFIC_COMPLETE_URL)
    Db().execute("UPDATE worker SET consented_at = ? WHERE worker_id = ?",
                 (Now(), worker["worker_id"]))
    return redirect(url_for("Instructions"))


@app.route("/instructions")
def Instructions():
    worker = RequireWorker()
    return render_template("instructions.html", worker=worker,
                           n_tasks=config.TASKS_PER_SESSION)


def PickItem(db, worker, want_gold, stratum=None):
    """Claim the next item this worker has not seen.

    Every assignment by a real annotator -- still open or already submitted --
    holds one of the item's REDUNDANCY slots for good, so an item never reaches
    more than REDUNDANCY people (nothing is released on a timer). Test accounts
    neither count toward that cap nor are bound by it. Calibration (gold) items
    are exempt from the cap: everyone answers them, in gold_rank order.

    In balanced mode (N_ANNOTATORS > 0) a real annotator only receives the items
    pre-allocated to their slot. Otherwise the flagged stratum is served by
    priority (worst-looking first) and the control stratum in a fixed
    pseudo-random order, both preferring partially-annotated items so coverage
    completes instead of spreading thin.
    """
    test = bool(worker["is_test"])
    if want_gold or test:
        cap_anomaly = cap_control = 10 ** 6
    else:
        cap_anomaly, cap_control = config.REDUNDANCY, config.REDUNDANCY_CONTROL
    joins, params = "", []
    if not want_gold and not test and config.N_ANNOTATORS > 0:
        joins = "JOIN alloc al ON al.item_id = i.item_id AND al.slot = ?"
        params.append(worker["slot"])
    where = "i.active = 1 AND i.is_gold = ?"
    params.append(1 if want_gold else 0)
    if stratum:
        where += " AND i.stratum = ?"
        params.append(stratum)
    real = "COALESCE(SUM(CASE WHEN aw.is_test = 0 THEN 1 ELSE 0 END), 0)"
    if want_gold:
        order = "i.gold_rank, i.item_id"  # identical fixed sequence for every worker
    elif stratum == "control":
        order = f"{real} DESC, (i.item_id * 2654435761) % 1000003"
    else:
        order = f"i.priority DESC, {real} DESC, i.item_id"
    params.extend([cap_anomaly, cap_control, worker["worker_id"]])
    row = db.execute(
        f"""SELECT i.item_id
              FROM item i {joins}
              LEFT JOIN assignment a ON a.item_id = i.item_id
              LEFT JOIN worker aw ON aw.worker_id = a.worker_id
             WHERE {where}
          GROUP BY i.item_id
            HAVING {real} < CASE WHEN i.stratum = 'anomaly' THEN ? ELSE ? END
               AND COALESCE(SUM(CASE WHEN a.worker_id = ? THEN 1 ELSE 0 END), 0) = 0
          ORDER BY {order}
             LIMIT 1""",
        params,
    ).fetchone()
    return row["item_id"] if row else None


def NextStratum(n_done):
    """Deterministic interleave, e.g. ratio 0.7 -> 7 flagged then 3 control."""
    slot = n_done % 10
    return "anomaly" if slot < round(config.ANOMALY_RATIO * 10) else "control"


def TakeBackSkipped(db, worker):
    """Hand back the longest-parked skipped task, un-skipping it so it stays
    the open task until it is submitted (or skipped again). None when nothing
    is parked. Skipped items come back before "done" is ever shown: the
    per-session cap counts submitted items, and a skipped one is not."""
    back = db.execute(
        "SELECT assign_id FROM assignment WHERE worker_id = ? AND stage IN (1, 2) "
        "AND skipped_at IS NOT NULL ORDER BY skipped_at, assign_id LIMIT 1",
        (worker["worker_id"],)).fetchone()
    if not back:
        return None
    db.execute("UPDATE assignment SET skipped_at = NULL WHERE assign_id = ?", (back["assign_id"],))
    session["notice"] = "Every other item is done: back to an item you skipped earlier."
    return redirect(url_for("Post", assign_id=back["assign_id"]))


@app.route("/task")
def Task():
    worker = RequireWorker()
    db = Db()

    if not worker["consented_at"]:
        return redirect(url_for("Index"))

    # The open task comes first -- but not one the person chose to skip: those
    # wait at the back of the queue until nothing new is left (or until they
    # come back to one deliberately, see SkippedList / Resume). Checked before
    # the per-session cap: an open task, skipped or handed back, is always
    # finished before "done" is shown.
    open_row = db.execute(
        "SELECT * FROM assignment WHERE worker_id = ? AND stage IN (1, 2) "
        "AND skipped_at IS NULL ORDER BY assign_id LIMIT 1", (worker["worker_id"],)).fetchone()
    if open_row:
        return redirect(url_for("Post", assign_id=open_row["assign_id"]))
    if (not worker["is_test"] and config.TASKS_PER_SESSION > 0
            and worker["n_done"] >= config.TASKS_PER_SESSION):
        back = TakeBackSkipped(db, worker)
        if back:
            return back
        db.execute("UPDATE worker SET status = 'done', finished_at = ? WHERE worker_id = ?",
                   (Now(), worker["worker_id"]))
        return redirect(url_for("Done"))

    db.execute("BEGIN IMMEDIATE")
    try:
        if (config.N_ANNOTATORS > 0 and not worker["is_test"] and worker["slot"] is None):
            # Worker created before balanced mode was switched on.
            db.execute("UPDATE worker SET slot = ? WHERE worker_id = ?",
                       (NextSlot(db), worker["worker_id"]))
            worker = db.execute("SELECT * FROM worker WHERE worker_id = ?",
                                (worker["worker_id"],)).fetchone()
        # Calibration first: every worker answers every is_gold item, in the
        # same fixed order, before anything else -- identical items expose each
        # person's rating standard so the 0-100 sliders can be compared.
        item_id = PickItem(db, worker, want_gold=True)
        if item_id is None:
            stratum = NextStratum(worker["n_done"])
            # Stratum exhausted -- fall back to the other one rather than ending
            # the session early.
            other = "control" if stratum == "anomaly" else "anomaly"
            item_id = (PickItem(db, worker, False, stratum)
                       or PickItem(db, worker, False, other))
        if item_id is None:
            db.execute("COMMIT")
            # Nothing new is left: the skipped items come back now, in the order
            # they were skipped.
            back = TakeBackSkipped(db, worker)
            if back:
                return back
            db.execute("UPDATE worker SET status = 'done', finished_at = ? WHERE worker_id = ?",
                       (Now(), worker["worker_id"]))
            return redirect(url_for("Done", exhausted=1))
        cur = db.execute(
            "INSERT INTO assignment (item_id, worker_id, stage, assigned_at) VALUES (?, ?, 2, ?)",
            (item_id, worker["worker_id"], Now()))
        assign_id = cur.lastrowid
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return redirect(url_for("Post", assign_id=assign_id))


def NormalizePages(text):
    """'87, 88; 90' -> '87,88,90'; None when it is not a list of page numbers."""
    t = re.sub(r"[,，、;\s]+", ",", (text or "").strip()).strip(",")
    return t if re.fullmatch(r"\d{1,4}(,\d{1,4})*", t) else None


def ValidYear(raw):
    """A corrected year typed by an annotator, or None when it is not a year
    in the configured range."""
    lo, hi = config.YEAR_RANGE
    return int(raw) if raw.isdigit() and lo <= int(raw) <= hi else None


def Elapsed(key, pop=False):
    """Seconds since the task page was rendered (recorded for analysis only;
    there is no minimum-time gate)."""
    started = session.get(key)
    if started is None:
        return None
    if pop:
        session.pop(key, None)
    return int(time.time() - started)


def SourcePages(row):
    """Declared-year report pages where the commitment itself appears."""
    return common.CitedPages({"evidence_page": row["source_page"]})


def LoadAssignment(assign_id, worker, allow_submitted=False):
    """This worker's assignment, or 404. Submitted ones (stage 3) are reachable
    only where revising an earlier answer is intended."""
    row = Db().execute(
        "SELECT a.*, i.* FROM assignment a JOIN item i ON i.item_id = a.item_id "
        "WHERE a.assign_id = ?", (assign_id,)).fetchone()
    if row is None or row["worker_id"] != worker["worker_id"]:
        abort(404)
    if row["stage"] not in ((1, 2, 3) if allow_submitted else (1, 2)):
        abort(redirect(url_for("Task")))
    return row


def SubmittedIndex(db, worker_id, assign_id):
    """1-based position of a submitted assignment in this worker's answer order
    (assignments are opened one at a time, so assign_id order is answer order)."""
    return db.execute(
        "SELECT COUNT(*) AS n FROM assignment WHERE worker_id = ? AND stage = 3 AND assign_id <= ?",
        (worker_id, assign_id)).fetchone()["n"]


def AssignIndex(db, worker_id, assign_id):
    """1-based position of an assignment in the order this person received
    items -- the number shown as "Item X". Fixed once given: skipping,
    revising or submitting out of order never renumbers anything."""
    return db.execute(
        "SELECT COUNT(*) AS n FROM assignment WHERE worker_id = ? AND assign_id <= ?",
        (worker_id, assign_id)).fetchone()["n"]


def Neighbour(db, worker_id, assign_id, direction):
    """The submitted assignment before (-1) / after (+1) assign_id in answer
    order, or None. assign_id None means "the current open task", whose
    previous is the latest submitted one."""
    if direction < 0:
        sql = "SELECT assign_id FROM assignment WHERE worker_id = ? AND stage = 3"
        params = [worker_id]
        if assign_id is not None:
            sql += " AND assign_id < ?"
            params.append(assign_id)
        sql += " ORDER BY assign_id DESC LIMIT 1"
    else:
        sql = ("SELECT assign_id FROM assignment WHERE worker_id = ? AND stage = 3 "
               "AND assign_id > ? ORDER BY assign_id LIMIT 1")
        params = [worker_id, assign_id]
    row = db.execute(sql, params).fetchone()
    return row["assign_id"] if row else None


_ANSWER_KEYS = ("is_commitment", "not_reason", "commitment_fix", "target_year_fix", "vq_", "ev_",
                "st_", "pg_", "cb_", "window_ok", "correct_target_year", "human_final_status",
                "source_page")


def HasAnswers(data):
    """True when a draft holds something the person typed or clicked -- not
    just the page clock, the untouched sliders and the counters."""
    for key, value in data.items():
        if key.startswith(_ANSWER_KEYS) and (value if isinstance(value, str) else any(value)):
            return True
    return False


class FormView:
    """Uniform read access to a stored or drafted form (a dict whose values are
    strings or lists) mirroring request.form: .get() gives the first value,
    .getlist() every value -- the evidence basis is multi-select."""

    def __init__(self, data):
        self._d = {k: (v if isinstance(v, list) else [v]) for k, v in (data or {}).items()}

    def get(self, key, default=None):
        values = self._d.get(key)
        return values[0] if values else default

    def getlist(self, key):
        return list(self._d.get(key) or [])


def StoredForm(db, assign_id):
    """Rebuild the task form (field name -> value, or list of values for the
    multi-select basis) from a submitted answer, so the same template can show
    it for editing."""
    form = {}
    ante = db.execute("SELECT * FROM ante WHERE assign_id = ?", (assign_id,)).fetchone()
    if ante:
        form["is_commitment"] = ante["is_commitment"] or ""
        form["not_reason"] = ante["not_reason"] or ""
        form["commitment_fix"] = ante["commitment_fix"] or ""
        form["target_year_fix"] = "" if ante["target_year_fix"] is None else str(ante["target_year_fix"])
        form["active_seconds"] = str(ante["active_seconds"] or 0)
        # One stored value, two radio groups on the page (ok/no, then why).
        stored_src = ante["source_page_ok"] or ""
        form["source_page_ok"] = "ok" if stored_src == "ok" else ("no" if stored_src else "")
        form["source_page_bad"] = stored_src if stored_src in ("wrong_page", "not_found") else ""
        form["source_page_fix"] = ante["source_page_fix"] or ""
    for v in db.execute("SELECT * FROM verdict WHERE assign_id = ?", (assign_id,)):
        y, q = v["year"], v["evidence_quality"]
        form[f"vq_{y}"] = "correct" if q == "ok" else "wrong"
        if q and q != "ok":
            # alt_clue lists the ticked bases ("current,1,3", or "none").
            picked = [b for b in (v["alt_clue"] or "").split(",") if b]
            form[f"ev_{y}"] = picked or (["current"] if q == "current" else [])
            form[f"st_{y}"] = common.DisplayStatus(v["human_status"] or "")
        form[f"cb_{y}"] = v["custom_basis"] or ""
        form[f"pb_{y}"] = v["achieve_prob"] or ""
        form[f"pg_{y}"] = v["correct_page"] or ""
    fin = db.execute("SELECT * FROM final WHERE assign_id = ?", (assign_id,)).fetchone()
    if fin:
        form["human_final_status"] = common.FinalStatus(fin["human_final_status"] or "")
        form["window_ok"] = fin["window_ok"] or ""
        form["correct_target_year"] = ("" if fin["correct_target_year"] is None
                                       else str(fin["correct_target_year"]))
        form["comment"] = fin["comment"] or ""
        form["n_page_views"] = str(fin["n_page_views"] or 0)
    return form


# ------------------------------------------------------------------ the one page


def YearsFor(row):
    return common.YearRows(json.loads(row["payload"]))


def ProgressTotal(db, worker):
    """Denominator of "Item X / Y": one commitment (with all its years) = 1.
    Balanced mode: this person's calibration + allocated items; otherwise the
    per-person cap, or every active item when there is none."""
    if not worker["is_test"] and config.N_ANNOTATORS > 0 and worker["slot"] is not None:
        return db.execute(
            """SELECT (SELECT COUNT(*) FROM item WHERE active = 1 AND is_gold = 1)
                    + (SELECT COUNT(*) FROM alloc al JOIN item i ON i.item_id = al.item_id
                        WHERE al.slot = ? AND i.active = 1) AS n""",
            (worker["slot"],)).fetchone()["n"]
    total = db.execute("SELECT COUNT(*) AS n FROM item WHERE active = 1").fetchone()["n"]
    if worker["is_test"]:
        return total
    return config.TASKS_PER_SESSION or total


def RegisterRender(db, assign_id, worker_id, require=None):
    """One atomic step for a render of the task page: give it a fresh
    generation number and take a snapshot of everything the page is built
    from as of that same moment -- the draft (sequence so far, generation),
    the assignment's state and, when the draft is empty and the answer was
    submitted, the stored answer itself. Every autosave and the final submit
    must carry the generation; a page rendered earlier (a stale tab, a cached
    page) is refused from then on. Taking it all under one write lock means a
    submit or a save landing concurrently can never leave the page showing a
    blank form, or content that disagrees with the sequence it carries.

    `require` (a submitted form) makes the registration conditional: the new
    generation is issued only if that form's own generation is still the
    current one; otherwise nothing changes and None is returned. This keeps a
    re-render with validation errors from handing stale content a fresh
    generation after another page has already moved on."""
    db.execute("BEGIN IMMEDIATE")
    try:
        if require is not None:
            cur = db.execute("SELECT gen FROM draft WHERE assign_id = ?", (assign_id,)).fetchone()
            if IsStale(cur["gen"] if cur else None, require):
                db.execute("ROLLBACK")
                return None
        db.execute(
            """INSERT INTO draft (assign_id, form, saved_at, seq, gen) VALUES (?, '{}', ?, 0, ?)
               ON CONFLICT (assign_id) DO UPDATE SET gen = MAX(draft.gen + 1, excluded.gen)""",
            (assign_id, Now(), int(time.time() * 1000)))
        d = db.execute("SELECT form, seq, gen FROM draft WHERE assign_id = ?",
                       (assign_id,)).fetchone()
        a = db.execute("SELECT stage, submitted_at FROM assignment WHERE assign_id = ?",
                       (assign_id,)).fetchone()
        w = db.execute("SELECT n_done FROM worker WHERE worker_id = ?", (worker_id,)).fetchone()
        data = json.loads(d["form"])
        stored = StoredForm(db, assign_id) if not data and a["stage"] == 3 else None
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"data": data, "stored": stored, "seq": d["seq"], "gen": d["gen"],
            "stage": a["stage"], "submitted_at": a["submitted_at"], "n_done": w["n_done"]}


def IsStale(stored_gen, form):
    """True when a request comes from a page older than the latest render of
    the item (stored_gen None = no draft row yet: nothing to be stale
    against). A request without the protocol fields (a page loaded before
    this version) passes only while no new-protocol render or submit has
    happened since (stored generation still 0)."""
    if stored_gen is None:
        return False
    if "_gen" not in form and "page_gen" not in form:
        return stored_gen > 0
    return ClientSeq(form.get("_gen", form.get("page_gen"))) != stored_gen


def StalePage(db, assign_id, form):
    cur = db.execute("SELECT gen FROM draft WHERE assign_id = ?", (assign_id,)).fetchone()
    return IsStale(cur["gen"] if cur else None, form)


def StaleRedirect(assign_id):
    """A submit from a page older than the latest render of the item: nothing
    is written; the person is shown the item's current state instead."""
    session["notice"] = ("This item has newer content in another, more recent tab; the latest "
                         "version has been loaded here. The submission from that other tab was "
                         "not accepted -- please review here and submit again.")
    return redirect(url_for("Post", assign_id=assign_id))


def RenderTask(row, worker, assign_id, **kw):
    """The task page. `require=<submitted form>` (error re-render) makes it
    conditional -- None comes back when that form's page is no longer the
    current render, and the caller redirects instead."""
    db = Db()
    wid = worker["worker_id"]
    # Everything state-dependent comes from one snapshot (see RegisterRender):
    # `row` is only used for the item itself from here on.
    snap = RegisterRender(db, assign_id, wid, require=kw.pop("require", None))
    if snap is None:
        return None
    editing = snap["stage"] == 3
    nav = {
        "editing": editing,
        "edit_index": AssignIndex(db, wid, assign_id) if editing else None,
        "prev_id": Neighbour(db, wid, assign_id if editing else None, -1),
        "next_id": Neighbour(db, wid, assign_id, +1) if editing else None,
        "submitted_at": snap["submitted_at"],
    }
    # "Item X": this assignment's position in the order the person received
    # items (skipping does not renumber), next to how many are submitted.
    current = AssignIndex(db, wid, assign_id)
    # Skipped items: how many are waiting (besides this one), and whether this
    # one is itself parked -- the page says so and offers the way back.
    skipped = db.execute(
        "SELECT assign_id FROM assignment WHERE worker_id = ? AND stage IN (1, 2) "
        "AND skipped_at IS NOT NULL", (wid,)).fetchall()
    skip = {"n_other": sum(1 for r in skipped if r["assign_id"] != assign_id),
            "this": any(r["assign_id"] == assign_id for r in skipped)}
    if "form" not in kw:
        # Prefill: the draft as of this render (the newest state); a submitted
        # answer being revised comes from the stored records. '{}' is a
        # tombstone (cleared or submitted).
        if snap["data"]:
            kw["form"] = FormView(common.NormalizeForm(snap["data"]))
            # A draft holding only the clock is not "your unsent answers".
            kw["restored"] = HasAnswers(snap["data"])
        elif snap["stored"] is not None:
            kw["form"] = FormView(snap["stored"])
    # The cited pages were converted (page_shift.py) after this answer or
    # draft was written against the old numbers: say so, so the old choices
    # are not read against the new pages -- and are re-checked if need be.
    page_note = None
    shift = (json.loads(row["payload"]).get("page_shift") or {})
    if shift.get("at"):
        fv = kw.get("form")
        # What the saved work was made against: the stored answer's own
        # record first, else the hidden field the draft carried.
        against = None
        if editing:
            stored = db.execute("SELECT seen_source_page FROM ante WHERE assign_id = ?",
                                (assign_id,)).fetchone()
            against = stored["seen_source_page"] if stored else None
        if against is None and fv is not None:
            against = fv.get("seen_src")
        older = editing and (snap["submitted_at"] or "") < shift["at"]
        if (against is not None and str(against) != str(row["source_page"] or "")) or \
                (against is None and older):
            src = shift.get("src") or {}
            moved = (f"the commitment's source page was {src['orig']}, now {row['source_page']}"
                     if src.get("d") else "the yearly evidence pages")
            page_note = (f"Note: this item's cited pages were converted to PDF page numbers after "
                         f"you filled it in ({moved}). Your earlier choices were about the old "
                         "numbers; if the page-related answers (source page, correct page) should "
                         "be re-checked against the new ones, please adjust them -- nothing else "
                         "is affected.")
    lo, hi = config.YEAR_RANGE
    mark = db.execute("SELECT starred, note FROM mark WHERE assign_id = ?", (assign_id,)).fetchone()
    return render_template(
        "post.html", worker=worker, item=row, assign_id=assign_id,
        mark=mark, mark_url=url_for("Mark", assign_id=assign_id),
        years=YearsFor(row), draft_seq=snap["seq"], page_gen=snap["gen"],
        verdict_choices=config.VERDICT_CHOICES, status_choices=config.STATUS_CHOICES,
        final_choices=config.FINAL_CHOICES, window_choices=config.WINDOW_CHOICES,
        progressive=config.PROGRESSIVE_REVEAL, source_pages=SourcePages(row),
        ai_final=common.FinalStatus(row["final_status"]),
        year_lo=lo, year_hi=hi, year_style=config.YEAR_STYLE,
        progress=(current, ProgressTotal(db, worker), snap["n_done"]),
        nav=nav, skip=skip, page_note=page_note, notice=session.pop("notice", None),
        **kw)


@app.route("/task/<int:assign_id>/post")
def Post(assign_id):
    worker = RequireWorker()
    row = LoadAssignment(assign_id, worker, allow_submitted=True)
    # One open assignment per worker, so timers left by abandoned tasks are
    # dead weight in the cookie; drop them before it can outgrow the 4 KB limit.
    for stale in [k for k in session if k.startswith("t2_") and k != f"t2_{assign_id}"]:
        session.pop(stale, None)
    if row["stage"] != 3:
        session.setdefault(f"t2_{assign_id}", time.time())
    return RenderTask(row, worker, assign_id)


@app.route("/task/<int:assign_id>/skip", methods=["POST"])
def Skip(assign_id):
    """Park the open task and move on. Nothing is discarded: the page saves
    its draft first, the assignment stays open (stage unchanged, so the item
    is not handed to anyone else), and it comes back once nothing new is
    left -- or earlier, from the list of skipped items."""
    worker = RequireWorker()
    LoadAssignment(assign_id, worker)  # open tasks only; a revision cannot be skipped
    db = Db()
    # The request carries the whole form with its generation and next sequence
    # number: the draft and the skip are one transaction, under the same rules
    # as an autosave (a stale page is refused, a late sequence dropped), so
    # nothing typed can fall between "saved" and "skipped".
    if StalePage(db, assign_id, request.form):
        return StaleRedirect(assign_id)
    seq = ClientSeq(request.form.get("draft_seq"))
    gen = ClientSeq(request.form.get("page_gen"))
    data = request.form.to_dict(flat=False)
    for key in ("draft_seq", "page_gen", "goto"):
        data.pop(key, None)
    payload = json.dumps(data, ensure_ascii=False)
    if len(payload) > 40000:
        abort(413)
    db.execute("BEGIN IMMEDIATE")
    try:
        if StalePage(db, assign_id, request.form):
            db.execute("ROLLBACK")
            return StaleRedirect(assign_id)
        db.execute(
            """INSERT INTO draft (assign_id, form, saved_at, seq, gen) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (assign_id) DO UPDATE SET form = excluded.form,
                 saved_at = excluded.saved_at, seq = excluded.seq
               WHERE excluded.seq > draft.seq AND excluded.gen = draft.gen""",
            (assign_id, payload, Now(), seq, gen))
        db.execute("UPDATE assignment SET skipped_at = ? WHERE assign_id = ? AND stage IN (1, 2)",
                   (Now(), assign_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    session["notice"] = ("That item was skipped (what you had filled in is kept). It comes back "
                         "once the other items are done; to return earlier, use “Skipped items” above.")
    return redirect(url_for("Task"))


@app.route("/task/<int:assign_id>/resume", methods=["POST"])
def Resume(assign_id):
    """Come back to a skipped item now, ahead of the queue."""
    worker = RequireWorker()
    LoadAssignment(assign_id, worker)
    Db().execute("UPDATE assignment SET skipped_at = NULL WHERE assign_id = ? AND stage IN (1, 2)",
                 (assign_id,))
    return redirect(url_for("Post", assign_id=assign_id))


@app.route("/skipped")
def SkippedList():
    worker = RequireWorker()
    rows = Db().execute(
        """SELECT a.assign_id, a.skipped_at, i.company, i.commitment, i.declared_year,
                  i.target_year, i.is_gold
             FROM assignment a JOIN item i ON i.item_id = a.item_id
            WHERE a.worker_id = ? AND a.stage IN (1, 2) AND a.skipped_at IS NOT NULL
            ORDER BY a.skipped_at, a.assign_id""", (worker["worker_id"],)).fetchall()
    return render_template("skipped.html", worker=worker, rows=rows,
                           notice=session.pop("notice", None))


@app.route("/task/<int:assign_id>/draft", methods=["POST"])
def Draft(assign_id):
    """Autosave of the task form (called by the page on every change and when
    it is left), so nothing typed is ever lost. Every write carries the page
    generation it was rendered with (RegisterRender) and a per-page sequence
    number: a stale render is refused (409), a late arrival is dropped. The
    check and the write share one write lock, so a render landing in between
    cannot lend its generation to an older request."""
    worker = RequireWorker()
    LoadAssignment(assign_id, worker, allow_submitted=True)
    db = Db()
    legacy = "_gen" not in request.form  # a page loaded before this version
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute("SELECT seq, gen FROM draft WHERE assign_id = ?", (assign_id,)).fetchone()
        if legacy:
            # No sequence or generation: allowed only while no new-protocol
            # render or submit has happened (generation still 0), ordered
            # after whatever is stored instead of dropped as "late".
            if cur and cur["gen"] > 0:
                db.execute("ROLLBACK")
                return "", 409
            seq, gen = (cur["seq"] if cur else 0) + 1, 0
        else:
            seq = ClientSeq(request.form.get("_seq"))
            gen = ClientSeq(request.form.get("_gen"))
            if cur and gen != cur["gen"]:
                db.execute("ROLLBACK")
                return "", 409  # a newer render of this page exists: this tab is stale
            if cur and seq <= cur["seq"]:
                db.execute("ROLLBACK")
                return "", 204  # arrived late; a newer save from this page is already stored
        if request.form.get("_clear") == "1":
            # "Clear this item": the page blanked its fields. Keep a tombstone
            # (an empty form) rather than deleting the row, so an older save
            # still in flight cannot bring the old answers back.
            payload = "{}"
        else:
            # Every value of every field: the evidence basis is multi-select.
            data = request.form.to_dict(flat=False)
            data.pop("_seq", None)
            data.pop("_gen", None)
            payload = json.dumps(data, ensure_ascii=False)
            if len(payload) > 40000:
                abort(413)  # rolled back below
        # The same rules once more inside the statement; the row count says
        # whether the write actually happened.
        cur = db.execute(
            """INSERT INTO draft (assign_id, form, saved_at, seq, gen) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (assign_id) DO UPDATE SET form = excluded.form,
                 saved_at = excluded.saved_at, seq = excluded.seq
               WHERE excluded.seq > draft.seq AND excluded.gen = draft.gen""",
            (assign_id, payload, Now(), seq, gen))
        if cur.rowcount == 0:
            db.execute("ROLLBACK")
            return "", 409
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return "", 204


def ClientSeq(raw):
    """A sequence / generation number sent by the page; 0 when absent or
    malformed."""
    try:
        return max(0, int(float(raw or 0)))
    except ValueError:
        return 0


@app.route("/history")
def History():
    """This person's submitted items. ?starred=1 lists only the ones they
    starred. The commitment shows in the wording they corrected, if any."""
    worker = RequireWorker()
    starred = request.args.get("starred") == "1"
    rows = Db().execute(
        f"""SELECT a.assign_id, a.submitted_at, a.edited_at, a.n_edits,
                   i.company, i.commitment, i.final_status, i.is_gold,
                   an.is_commitment, an.commitment_fix, f.human_final_status,
                   COALESCE(m.starred, 0) AS starred, COALESCE(m.note, '') AS note
              FROM assignment a
              JOIN item i ON i.item_id = a.item_id
              LEFT JOIN ante an ON an.assign_id = a.assign_id
              LEFT JOIN final f ON f.assign_id = a.assign_id
              LEFT JOIN mark m ON m.assign_id = a.assign_id
             WHERE a.worker_id = ? AND a.stage = 3{' AND m.starred = 1' if starred else ''}
             ORDER BY a.assign_id""", (worker["worker_id"],)).fetchall()
    # Numbered as "Item X" on the task page: position among ALL of this
    # person's items, so a skipped item keeps its number.
    order = {r["assign_id"]: i for i, r in enumerate(Db().execute(
        "SELECT assign_id FROM assignment WHERE worker_id = ? ORDER BY assign_id",
        (worker["worker_id"],)), start=1)}
    rows = [dict(r, idx=order.get(r["assign_id"], "")) for r in rows]
    return render_template("history.html", worker=worker, rows=rows, starred=starred,
                           notice=session.pop("notice", None))


@app.route("/task/<int:assign_id>/mark", methods=["POST"])
def Mark(assign_id):
    """The person's own star and note on an item -- a working aid, not part of
    the answer. Saved by the task page as soon as either changes, for open and
    submitted items alike; one row per assignment, owner-only. Nothing else is
    touched, so it is safe at any point of a task.

    Ordering. Every save carries the generation the server gave that render
    of the page (page_gen, from RegisterRender: strictly increasing with
    every render) and the page's own running sequence number; the stored row
    keeps the highest (gen, seq) pair and a save is applied only when its
    pair is higher. So a slow earlier save from the same page cannot
    overwrite a later one, and -- whatever its sequence number -- a save from
    an older render of the page cannot overwrite what a newer render stored.
    Only the mark row's own pair is compared, never the draft generation:
    submitting the answer retires the page for drafts but not for its note.
    A rejected save answers 409 with the stored state, and the page lets the
    person choose; `_force` then re-applies this page's content on top of the
    stored lineage (the stored gen, next seq), and the response carries the
    pair the page continues from. Separately from that lineage, every save
    names the page it comes from (`_src`: the render's generation, which the
    page never changes) and that page's own running number (`_n`); the row
    remembers, per page, the highest number applied and the pair it was
    applied as (`srcs`), so one page's saves stay in order whatever lineage
    they claim and whether or not they are forced: a save overtaken by a
    later one from the same page is refused for good, even after other pages
    have written in between, and a save sent again (its answer lost) is
    answered as it was the first time instead of being applied twice. A page
    that sends no `_src` (rendered before this protocol) gets the lineage
    rule alone."""
    worker = RequireWorker()
    LoadAssignment(assign_id, worker, allow_submitted=True)
    starred = 1 if request.form.get("starred") == "1" else 0
    note = (request.form.get("note") or "").strip()[:1000]
    src = ClientSeq(request.form.get("_src"))
    n = ClientSeq(request.form.get("_n"))
    gen = ClientSeq(request.form.get("_gen"))
    seq = ClientSeq(request.form.get("_seq"))
    force = request.form.get("_force") == "1"
    db = Db()
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute("SELECT starred, note, gen, seq, srcs FROM mark WHERE assign_id = ?",
                         (assign_id,)).fetchone()
        try:
            srcs = json.loads(cur["srcs"] or "{}") if cur is not None else {}
        except ValueError:
            srcs = {}
        applied = (gen, seq)
        if cur is not None:
            # A page rendered before this protocol sends no _src: it gets the
            # lineage rule alone and leaves no record (it keeps working until
            # it is closed, instead of being refused from its second save on).
            rec = srcs.get(str(src)) if src else None
            if isinstance(rec, int):                   # written by an earlier build
                rec = [rec, cur["gen"], cur["seq"]]
            if rec and n < rec[0]:
                # Stale whatever its force flag: a later save from this same
                # page has already been applied (requests can overtake).
                db.execute("ROLLBACK")
                return jsonify(ok=False, starred=cur["starred"], note=cur["note"],
                               gen=cur["gen"], seq=cur["seq"]), 409
            if rec and n == rec[0]:
                # The same save once more (the page retries a save whose
                # answer it never got): it was applied then, so it is done --
                # answered as it was answered then, never carried out twice.
                db.execute("ROLLBACK")
                return jsonify(ok=True, gen=rec[1], seq=rec[2], repeat=True)
            newer_stored = (cur["gen"], cur["seq"]) >= (gen, seq)
            if newer_stored and not force:
                db.execute("ROLLBACK")
                return jsonify(ok=False, starred=cur["starred"], note=cur["note"],
                               gen=cur["gen"], seq=cur["seq"]), 409
            if newer_stored:
                applied = (cur["gen"], cur["seq"] + 1)  # forced: onto the stored lineage
        if src:
            srcs[str(src)] = [n, applied[0], applied[1]]
        # Bounded by age, not by count: a page's key is the time of its
        # render, and only once the map is large are the records of pages
        # older than a month dropped -- a save from one of those would be a
        # month late, and that is the only kind the map then cannot refuse.
        if len(srcs) > 200:
            cutoff = int(time.time() * 1000) - 30 * 86400 * 1000
            for key in [k for k in srcs if k.isdigit() and int(k) < cutoff]:
                del srcs[key]
        db.execute(
            """INSERT INTO mark (assign_id, starred, note, updated_at, gen, seq, srcs)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (assign_id) DO UPDATE SET starred = excluded.starred,
                 note = excluded.note, updated_at = excluded.updated_at,
                 gen = excluded.gen, seq = excluded.seq, srcs = excluded.srcs""",
            (assign_id, starred, note, Now(), applied[0], applied[1], json.dumps(srcs)))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(ok=True, gen=applied[0], seq=applied[1])


@app.route("/task/<int:assign_id>/post", methods=["POST"])
def PostSubmit(assign_id):
    worker = RequireWorker()
    row = LoadAssignment(assign_id, worker, allow_submitted=True)
    editing = row["stage"] == 3  # revising an already-submitted answer in place
    db = Db()
    # A page older than the latest render of this item never writes -- and is
    # not re-rendered with errors either, which would make it current again.
    if StalePage(db, assign_id, request.form):
        return StaleRedirect(assign_id)
    years = YearsFor(row)
    errors = []
    lo, hi = config.YEAR_RANGE

    # -- commitment validity screen ------------------------------------------
    is_commitment = request.form.get("is_commitment")
    not_reason = (request.form.get("not_reason") or "").strip()
    commitment_fix = (request.form.get("commitment_fix") or "").strip()[:300]
    # no_target: it reads like a commitment but names no target year, so there
    # is nothing to track it against -- ends the item like not_commitment,
    # with the reason optional.
    if is_commitment not in ("valid", "not_commitment", "unsure", "no_target"):
        errors.append("Please decide first whether this is a verifiable commitment.")
    if is_commitment == "not_commitment" and len(not_reason) < 5:
        errors.append("When choosing “Not a commitment”, give a short reason (at least 5 characters).")
    # -- is the commitment actually on the page the pipeline cited? -----------
    # Asked wherever there is a page to check and the item was not dismissed as
    # a non-commitment. A wrong citation is a property of the pipeline, not of
    # the company, so it is recorded on its own instead of leaking into the
    # commitment judgement or into a year's "not mentioned".
    src_ok = src_fix = None
    if is_commitment not in ("not_commitment", "no_target") and SourcePages(row):
        choice = request.form.get("source_page_ok")
        if choice == "ok":
            src_ok = "ok"
        elif choice == "no":
            bad = request.form.get("source_page_bad")
            if bad in ("wrong_page", "not_found"):
                src_ok = bad
            else:
                errors.append("Say which it is: the page reference is wrong, or the commitment "
                              "is not in the report at all.")
        else:
            errors.append(f"Check the cited source page (p. {row['source_page']}): is the "
                          "commitment really on it?")
        if src_ok == "wrong_page":
            raw_fix = (request.form.get("source_page_fix") or "").strip()
            if raw_fix:
                src_fix = NormalizePages(raw_fix)
                if src_fix is None:
                    errors.append("The correct page must be digits only (e.g. 85 or 85,86); "
                                  "leave it blank if you do not know.")

    # Either answer ends the item here: there is nothing to verify year by year
    # when the text is not a commitment, or when the commitment cannot be found
    # in the report at all. A merely mis-cited page is not a reason to stop --
    # the commitment is there, just elsewhere.
    skip_years = is_commitment in ("not_commitment", "no_target") or src_ok == "not_found"
    ty_fix_raw = (request.form.get("target_year_fix") or "").strip()
    ty_fix = None
    if not skip_years and ty_fix_raw:
        if ValidYear(ty_fix_raw) is None:
            errors.append(f"The corrected target year must be a year between {lo} and {hi}.")
        elif isinstance(row["declared_year"], int) and int(ty_fix_raw) <= row["declared_year"]:
            errors.append("The corrected target year must be later than the declaration year "
                          f"({common.YearLabel(row['declared_year'])}).")
        else:
            ty_fix = int(ty_fix_raw)
    if skip_years:
        commitment_fix = ""  # nothing to fix on text we cannot check

    # -- per-year triage ------------------------------------------------------
    answers = []
    human_final = window_ok = correct_ty = None
    page_views = 0
    comment = ""  # free-text justification was dropped from the form; column kept

    if not skip_years:
        for y in years:
            yr = y["year"]
            yl = common.YearLabel(yr)
            if ty_fix and yr > ty_fix:
                continue  # past the corrected target year: nothing to track
            vq = request.form.get(f"vq_{yr}")
            pb = request.form.get(f"pb_{yr}")
            ev, st, alt, page, custom = "ok", y["ai_label"], "", "", ""
            if vq == "wrong":
                # What supports the correct verdict? Any mix of the quoted
                # evidence and the same-year clues -- or "none of these", in
                # which case the person either found other evidence (status +
                # page; the text box is optional) or nothing at all.
                clue_idx = {str(i) for i in range(1, len(y["clues"]) + 1)}
                allowed = {"current", "none", "notfound"} | clue_idx
                basis = [b for b in dict.fromkeys(request.form.getlist(f"ev_{yr}")) if b in allowed]
                custom = (request.form.get(f"cb_{yr}") or "").strip()[:500]
                st = request.form.get(f"st_{yr}")
                if not basis:
                    errors.append(f"{yl}: tick the correct basis (any that apply).")
                elif "none" in basis and len(basis) > 1:
                    errors.append(f"{yl}: “None of these” cannot be combined with other bases.")
                elif "notfound" in basis and "current" in basis:
                    errors.append(f"{yl}: “I cannot find this text in the report” and “the quoted "
                                  "evidence holds” contradict each other — pick one.")
                elif st not in config.STATUS_CHOICES:
                    errors.append(f"{yl}: re-judge this year's status.")
                else:
                    kinds = set(basis)
                    if "notfound" in kinds:
                        # The quoted sentence is not in the report at all. That
                        # is a fact about the AI, not about the company, and it
                        # outranks whatever else was ticked -- the clues that
                        # ARE the real basis stay recorded in alt_clue.
                        ev = "not_in_report"
                    elif kinds == {"none"}:
                        ev = "none" if st == config.NOT_MENTIONED else "other"
                    elif kinds == {"current"}:
                        ev = "current"
                    elif kinds <= clue_idx:
                        ev = "clue"
                    else:
                        ev = "mixed"
                    alt = ",".join(basis)
                    if st != config.NOT_MENTIONED:
                        # A positive re-judgement must point at where the
                        # report supports it.
                        page = NormalizePages(request.form.get(f"pg_{yr}"))
                        if not page:
                            errors.append(f"{yl}: a status of “{st}” needs the report page(s) "
                                          "that support it (e.g. 87 or 87,88).")
            elif vq != "correct":
                errors.append(f"{yl}: decide first whether the AI's verdict is correct.")
            if pb is None or not pb.isdigit() or not 0 <= int(pb) <= 100:
                errors.append(f"{yl}: use the slider to estimate the probability of meeting "
                              "the target (0–100).")
            # The evidence page this render showed for the year (hidden field):
            # what the judgement was about, whatever the item says later.
            seen_pg = (request.form.get(f"seen_pg_{yr}") or "").strip() or None
            answers.append((yr, y["ai_label"], ev, st, pb, page, vq, alt, custom, seen_pg))

        human_final = request.form.get("human_final_status")
        if human_final not in config.FINAL_CHOICES:
            errors.append("Give your overall conclusion.")

        if ty_fix:
            # The Q0 correction already answers the window question.
            window_ok, correct_ty = "target_wrong", ty_fix
        else:
            window_ok = request.form.get("window_ok")
            if window_ok not in {k for k, _ in config.WINDOW_CHOICES}:
                errors.append("Decide whether the AI tracked the right range of years.")
            raw_ty = (request.form.get("correct_target_year") or "").strip()
            if window_ok == "target_wrong" and ValidYear(raw_ty) is None:
                errors.append(f"With “target year misread”, enter the correct year ({lo}–{hi}).")
            correct_ty = int(raw_ty) if raw_ty.isdigit() else None

        try:
            page_views = max(0, int(request.form.get("n_page_views") or 0))
        except ValueError:
            page_views = 0

    # Two clocks: `seconds` is wall-clock from first opening the page to this
    # submit; `active_seconds` (measured by the page) counts only the time
    # actually spent in that tab -- hidden, unfocused and idle stretches do
    # not count.
    try:
        active_seconds = max(0, int(float(request.form.get("active_seconds") or 0)))
    except ValueError:
        active_seconds = 0
    if editing:
        # Keep the original answering times; a revision is not a new answer.
        prev = db.execute("SELECT seconds, active_seconds FROM ante WHERE assign_id = ?",
                          (assign_id,)).fetchone()
        seconds = (prev["seconds"] if prev and prev["seconds"] is not None else 0)
        if prev and prev["active_seconds"] is not None:
            active_seconds = prev["active_seconds"]
    else:
        seconds = Elapsed(f"t2_{assign_id}") or 0

    if errors:
        # Re-render with the errors only if this page is still the current
        # render, checked under the registration lock: otherwise its old
        # content would be handed a fresh generation and could overwrite the
        # answer another page has submitted meanwhile.
        page = RenderTask(row, worker, assign_id, errors=errors, form=request.form,
                          require=request.form)
        if page is None:
            return StaleRedirect(assign_id)
        return page, 400

    Elapsed(f"t2_{assign_id}", pop=True)
    db.execute("BEGIN IMMEDIATE")
    try:
        # Checked again under the write lock: another tab may have rendered
        # this item between the check above and now.
        if StalePage(db, assign_id, request.form):
            db.execute("ROLLBACK")
            return StaleRedirect(assign_id)
        # The cited pages as this render showed them travel with the answer
        # (seen_*). A revision keeps the pages recorded with the judgement it
        # revises -- unless that judgement itself changed, in which case the
        # person re-judged against what the page shows now.
        seen_src = (request.form.get("seen_src") or "").strip() or None
        prev_ante = db.execute(
            "SELECT source_page_ok, source_page_fix, seen_source_page FROM ante WHERE assign_id = ?",
            (assign_id,)).fetchone()
        if (prev_ante and prev_ante["seen_source_page"] is not None
                and (prev_ante["source_page_ok"] or None) == src_ok
                and (prev_ante["source_page_fix"] or None) == src_fix):
            seen_src = prev_ante["seen_source_page"]
        db.execute(
            """INSERT INTO ante (assign_id, is_commitment, not_reason, commitment_fix,
                                 target_year_fix, seconds, active_seconds,
                                 source_page_ok, source_page_fix, seen_source_page)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (assign_id) DO UPDATE SET
                 is_commitment = excluded.is_commitment,
                 not_reason = excluded.not_reason,
                 commitment_fix = excluded.commitment_fix,
                 target_year_fix = excluded.target_year_fix, seconds = excluded.seconds,
                 active_seconds = excluded.active_seconds,
                 source_page_ok = excluded.source_page_ok,
                 source_page_fix = excluded.source_page_fix,
                 seen_source_page = excluded.seen_source_page""",
            (assign_id, is_commitment, not_reason, commitment_fix, ty_fix, seconds,
             active_seconds, src_ok, src_fix, seen_src),
        )
        prev_rows = {r["year"]: r for r in db.execute(
            "SELECT year, evidence_quality, human_status, correct_page, alt_clue, "
            "seen_evidence_page FROM verdict WHERE assign_id = ?", (assign_id,))}

        def SeenFor(y, ev, st, pg, alt, shown):
            prev = prev_rows.get(y)
            if (prev and prev["seen_evidence_page"] is not None
                    and (prev["evidence_quality"], prev["human_status"], prev["correct_page"] or "",
                         prev["alt_clue"] or "") == (ev, st, pg or "", alt or "")):
                return prev["seen_evidence_page"]
            return shown

        db.execute("DELETE FROM verdict WHERE assign_id = ?", (assign_id,))
        if skip_years:
            # A revision from "valid" to "not a commitment" must not leave the
            # old wrap-up behind.
            db.execute("DELETE FROM final WHERE assign_id = ?", (assign_id,))
        if not skip_years:
            db.executemany(
                "INSERT INTO verdict (assign_id, year, ai_status, evidence_quality, "
                "human_status, achieve_prob, correct_page, alt_clue, custom_basis, "
                "seen_evidence_page) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(assign_id, y, ai, ev, st, pb, pg, alt, custom, SeenFor(y, ev, st, pg, alt, seen))
                 for y, ai, ev, st, pb, pg, _vq, alt, custom, seen in answers],
            )
            db.execute(
                """INSERT INTO final (assign_id, ai_final_agree, human_final_status, comment,
                                      window_ok, correct_target_year, n_page_views, seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (assign_id) DO UPDATE SET
                     ai_final_agree = excluded.ai_final_agree,
                     human_final_status = excluded.human_final_status,
                     comment = excluded.comment, window_ok = excluded.window_ok,
                     correct_target_year = excluded.correct_target_year,
                     n_page_views = excluded.n_page_views, seconds = excluded.seconds""",
                (assign_id, 1 if human_final == common.FinalStatus(row["final_status"]) else 0,
                 human_final, comment, window_ok, correct_ty, page_views, seconds),
            )
        # Retire the draft with a tombstone at the page's latest sequence, and
        # retire the generation too: nothing rendered before this submit --
        # this page, another tab, a page from before this version (gen 0) --
        # may write again; the redirect below renders a fresh page.
        db.execute(
            """INSERT INTO draft (assign_id, form, saved_at, seq, gen) VALUES (?, '{}', ?, ?, ?)
               ON CONFLICT (assign_id) DO UPDATE SET form = '{}',
                 saved_at = excluded.saved_at, seq = MAX(draft.seq, excluded.seq),
                 gen = MAX(draft.gen + 1, excluded.gen)""",
            (assign_id, Now(), ClientSeq(request.form.get("draft_seq")),
             int(time.time() * 1000)))
        if editing:
            db.execute("UPDATE assignment SET edited_at = ?, n_edits = n_edits + 1 "
                       "WHERE assign_id = ?", (Now(), assign_id))
        else:
            # Close the assignment in the same transaction as the answer rows:
            # a crash in between must never leave answers the export (stage 3)
            # cannot see while the person is handed an empty form again.
            FinishAssignment(db, assign_id, row, worker)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    if editing:
        # The page may ask to continue elsewhere after saving (the previous /
        # next item, the history list); the default is the open task.
        idx = AssignIndex(db, worker["worker_id"], assign_id)
        goto = request.form.get("goto") or ""
        if goto.isdigit():
            session["notice"] = f"Changes to item {idx} saved."
            return redirect(url_for("Post", assign_id=int(goto)))
        if goto == "history":
            session["notice"] = f"Changes to item {idx} saved."
            return redirect(url_for("History"))
        session["notice"] = f"Changes to item {idx} saved; back to the current item."
        return redirect(url_for("Task"))
    return redirect(url_for("Task"))


def FinishAssignment(db, assign_id, item_row, worker):
    """Close the assignment, bump the worker counter, score gold items. Runs
    inside the caller's transaction."""
    cur = db.execute(
        "UPDATE assignment SET stage = 3, submitted_at = ? "
        "WHERE assign_id = ? AND stage IN (1, 2)", (Now(), assign_id))
    if cur.rowcount == 0:
        return  # double submit -- a concurrent request already closed it
    db.execute("UPDATE worker SET n_done = n_done + 1 WHERE worker_id = ?",
               (worker["worker_id"],))
    if not item_row["is_gold"] or not item_row["gold_expect"]:
        return

    expect = json.loads(item_row["gold_expect"])
    ante = db.execute("SELECT * FROM ante WHERE assign_id = ?", (assign_id,)).fetchone()
    ok = True
    if expect.get("is_commitment") and ante and ante["is_commitment"] != expect["is_commitment"]:
        ok = False
    for year, want in (expect.get("year_status") or {}).items():
        got = db.execute("SELECT human_status FROM verdict WHERE assign_id = ? AND year = ?",
                         (assign_id, int(year))).fetchone()
        if got is None or got["human_status"] != want:
            ok = False
    column = "gold_pass" if ok else "gold_fail"
    db.execute(f"UPDATE worker SET {column} = {column} + 1 WHERE worker_id = ?",
               (worker["worker_id"],))


@app.route("/done")
def Done():
    worker = RequireWorker()
    # The completion code is payment; do not hand it out just because someone
    # typed /done into the address bar.
    if worker["status"] != "done" and (
            config.TASKS_PER_SESSION <= 0 or worker["n_done"] < config.TASKS_PER_SESSION):
        return redirect(url_for("Task"))
    return render_template("done.html", worker=worker, screened_out=False,
                           exhausted=bool(request.args.get("exhausted")),
                           code=config.PROLIFIC_CODE,
                           complete_url=config.PROLIFIC_COMPLETE_URL)


# --------------------------------------------------------------- own statistics


def Median(values):
    """Middle value of a list of numbers; None when there is nothing to show."""
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def Pct(part, whole):
    """`part` of `whole` as a percentage with one decimal; None when whole = 0."""
    return round(100.0 * part / whole, 1) if whole else None


# As a global rather than a render_template argument: a Jinja macro resolves
# globals, which is what the admin table's share() macro needs.
app.jinja_env.globals["Pct"] = Pct


def Quartiles(values, scale=1.0, digits=1):
    """n / min / Q1 / median / Q3 / max / mean of a list of numbers, each
    multiplied by `scale` (linear interpolation between ranks, like a
    spreadsheet's QUARTILE); None when the list is empty."""
    vals = sorted(values)
    if not vals:
        return None

    def At(p):
        pos = (len(vals) - 1) * p
        lo = int(pos)
        hi = min(lo + 1, len(vals) - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)

    def R(x):
        return round(x * scale, digits) if digits else int(round(x * scale))

    return {"n": len(vals), "min": R(vals[0]), "q1": R(At(0.25)), "median": R(At(0.5)),
            "q3": R(At(0.75)), "max": R(vals[-1]), "mean": R(sum(vals) / len(vals))}


def Majority(values, order=None):
    """The most frequent value and its share in percent; a tie goes to the
    value earliest in `order`. (None, None) when there is nothing."""
    vals = [v for v in values if v]
    if not vals:
        return None, None
    counts = collections.Counter(vals)

    def Rank(v):
        return -(order.index(v) if order and v in order else 10 ** 6)

    best = max(counts, key=lambda v: (counts[v], Rank(v)))
    return best, Pct(counts[best], len(vals))


# Buckets of the 0-100 achievement-probability slider, coloured like the task
# page's low / mid / high.
_PROB_BINS = [(0, 9), (10, 19), (20, 29), (30, 39), (40, 49),
              (50, 59), (60, 69), (70, 79), (80, 89), (90, 100)]

# Value of verdict.evidence_quality -> what the annotator said the correct
# basis was, in the wording of the task page.
_BASIS_LABELS = [
    ("current", "The quoted evidence"),
    ("clue", "Same-year clues"),
    ("mixed", "The quoted evidence plus clues"),
    ("other", "None of those, but you found other evidence"),
    ("none", "None of those — the year says nothing"),
    ("not_in_report", "The AI's quoted sentence is not in the report at all"),
]


def OwnStats(db, worker):
    """Everything the personal statistics page shows.

    Read-only by construction: no statement here writes anything, and every
    query is scoped by the session's worker_id -- so opening the page can
    neither disturb an answer in progress nor show another annotator's numbers.
    """
    wid = worker["worker_id"]
    counts = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN stage = 3 THEN 1 ELSE 0 END), 0) AS done,
                  COALESCE(SUM(CASE WHEN stage IN (1, 2) THEN 1 ELSE 0 END), 0) AS open,
                  COALESCE(SUM(n_edits), 0) AS edits
             FROM assignment WHERE worker_id = ?""", (wid,)).fetchone()
    items = db.execute(
        """SELECT a.assign_id, a.submitted_at, a.edited_at, a.n_edits,
                  i.item_id, i.company, i.commitment, i.declared_year, i.target_year,
                  i.is_gold, i.gold_rank, i.final_status,
                  an.is_commitment, an.active_seconds, an.source_page_ok, an.target_year_fix,
                  an.commitment_fix,
                  f.assign_id AS has_final, f.ai_final_agree, f.human_final_status,
                  f.window_ok, f.n_page_views,
                  COALESCE(m.starred, 0) AS starred, COALESCE(m.note, '') AS note
             FROM assignment a
             JOIN item i ON i.item_id = a.item_id
             LEFT JOIN ante an ON an.assign_id = a.assign_id
             LEFT JOIN final f ON f.assign_id = a.assign_id
             LEFT JOIN mark m ON m.assign_id = a.assign_id
            WHERE a.worker_id = ? AND a.stage = 3
            ORDER BY a.assign_id""", (wid,)).fetchall()
    years = db.execute(
        """SELECT v.assign_id, v.year, v.ai_status, v.human_status, v.evidence_quality,
                  v.achieve_prob, v.correct_page
             FROM verdict v JOIN assignment a ON a.assign_id = v.assign_id
            WHERE a.worker_id = ? AND a.stage = 3
            ORDER BY v.assign_id, v.year""", (wid,)).fetchall()
    # "Item X": the position among ALL of this person's items, as on the task
    # page (a skipped item keeps its number).
    order = {r["assign_id"]: i for i, r in enumerate(db.execute(
        "SELECT assign_id FROM assignment WHERE worker_id = ? ORDER BY assign_id", (wid,)),
        start=1)}

    labels = config.STATUS_CHOICES
    matrix = {ai: dict.fromkeys(labels, 0) for ai in labels}
    basis = dict.fromkeys([k for k, _ in _BASIS_LABELS], 0)
    probs, by_status = [], {}
    flagged = flips = page_fixes = 0
    per_item = {}  # assign_id -> this item's own counters, for the per-item table
    for v in years:
        ai = common.DisplayStatus(v["ai_status"] or "")
        human = common.DisplayStatus(v["human_status"] or "")
        c = per_item.setdefault(v["assign_id"],
                                {"years": 0, "flagged": 0, "flips": 0, "probs": []})
        c["years"] += 1
        if ai in matrix and human in matrix[ai]:
            matrix[ai][human] += 1
        quality = v["evidence_quality"] or "ok"
        if quality != "ok":
            flagged += 1  # "the AI got this year wrong"
            c["flagged"] += 1
            if quality in basis:
                basis[quality] += 1
        if ai != human:
            flips += 1  # ... and the label itself was changed
            c["flips"] += 1
        if (v["correct_page"] or "").strip():
            page_fixes += 1
        prob = v["achieve_prob"]
        if prob is not None and str(prob).isdigit():
            probs.append(int(prob))
            c["probs"].append(int(prob))
            by_status.setdefault(human, []).append(int(prob))

    hist = [0] * len(_PROB_BINS)
    for p in probs:
        hist[min(len(_PROB_BINS) - 1, p // 10)] += 1
    bins = [{"label": f"{lo}–{hi}", "n": hist[i],
             "cls": "low" if hi < 30 else "mid" if hi < 70 else "high"}
            for i, (lo, hi) in enumerate(_PROB_BINS)]

    finals = [r for r in items if r["has_final"] is not None]
    views = [r["n_page_views"] or 0 for r in finals]
    active = [r["active_seconds"] for r in items if r["active_seconds"]]
    # One line per submitted item, in the order they were answered.
    item_rows = []
    for r in items:
        c = per_item.get(r["assign_id"], {"years": 0, "flagged": 0, "flips": 0, "probs": []})
        item_rows.append({
            "idx": order.get(r["assign_id"], ""), "assign_id": r["assign_id"],
            "item_id": r["item_id"], "company": r["company"], "commitment": r["commitment"],
            # The wording as this person corrected it, when they did.
            "commitment_fix": r["commitment_fix"] or "",
            "starred": r["starred"], "note": r["note"],
            "declared_year": r["declared_year"], "target_year": r["target_year"],
            "is_gold": r["is_gold"], "gold_rank": r["gold_rank"],
            "is_commitment": r["is_commitment"], "source_page_ok": r["source_page_ok"],
            "target_year_fix": r["target_year_fix"],
            "ai_final": common.FinalStatus(r["final_status"] or ""),
            "human_final": (common.FinalStatus(r["human_final_status"])
                            if r["human_final_status"] else None),
            "final_agree": r["ai_final_agree"], "window_ok": r["window_ok"],
            "years": c["years"], "flagged": c["flagged"], "flips": c["flips"],
            "prob_last": c["probs"][-1] if c["probs"] else None,
            "minutes": round(r["active_seconds"] / 60.0, 1) if r["active_seconds"] else None,
            "views": r["n_page_views"] if r["has_final"] is not None else None,
            "n_edits": r["n_edits"], "submitted_at": r["submitted_at"],
            "edited_at": r["edited_at"],
        })
    return {
        # progress
        "done": counts["done"], "open": counts["open"], "edits": counts["edits"],
        "total": ProgressTotal(db, worker),
        "gold_done": sum(1 for r in items if r["is_gold"]),
        "gold_total": db.execute(
            "SELECT COUNT(*) AS n FROM item WHERE active = 1 AND is_gold = 1").fetchone()["n"],
        "gold_pass": worker["gold_pass"], "gold_fail": worker["gold_fail"],
        "first_seen": worker["first_seen"],
        # what was corrected, per year and per item
        "years": len(years), "flagged": flagged, "flips": flips, "page_fixes": page_fixes,
        "basis": [(label, basis[key]) for key, label in _BASIS_LABELS if basis[key]],
        "matrix": matrix, "labels": labels,
        "n_items": len(items), "finals": len(finals),
        "final_flips": sum(1 for r in finals if r["ai_final_agree"] == 0),
        "window_bad": sum(1 for r in finals
                          if (r["window_ok"] or "ok") not in ("ok", "unsure")),
        "not_commit": sum(1 for r in items if r["is_commitment"] == "not_commitment"),
        "unsure": sum(1 for r in items if r["is_commitment"] == "unsure"),
        "no_target": sum(1 for r in items if r["is_commitment"] == "no_target"),
        "starred": sum(1 for r in items if r["starred"]),
        "noted": sum(1 for r in items if r["note"]),
        # The source-page check (Q0). Items answered before it existed, and
        # items with no cited page, are simply not counted in src_asked.
        "src_asked": sum(1 for r in items if r["source_page_ok"]),
        "src_ok": sum(1 for r in items if r["source_page_ok"] == "ok"),
        "src_wrong": sum(1 for r in items if r["source_page_ok"] == "wrong_page"),
        "src_notfound": sum(1 for r in items if r["source_page_ok"] == "not_found"),
        # the 0-100 slider
        "probs": len(probs), "bins": bins, "hist_max": max(hist) if probs else 1,
        "prob_mean": round(sum(probs) / len(probs), 1) if probs else None,
        "prob_median": Median(probs),
        "by_status": [(s, len(by_status[s]), round(sum(by_status[s]) / len(by_status[s]), 1),
                       Median(by_status[s]))
                      for s in labels if by_status.get(s)],
        # evidence look-ups and time on task
        "views": sum(views), "views_avg": round(sum(views) / len(finals), 1) if finals else None,
        "no_views": sum(1 for v in views if v == 0),
        "minutes_median": round(Median(active) / 60.0, 1) if active else None,
        "minutes_total": round(sum(active) / 60.0) if active else None,
        # quartiles (Q1 / median / Q3) of the same three distributions
        "prob_q": Quartiles(probs), "minutes_q": Quartiles(active, scale=1 / 60.0),
        "views_q": Quartiles(views),
        # item by item (this person's own answers only)
        "item_rows": item_rows,
        "gold_rows": [r for r in item_rows if r["is_gold"]],
        # raw values, so the admin page can pool everyone's for team quartiles;
        # never rendered on the personal page
        "_probs": probs, "_minutes": [a / 60.0 for a in active], "_views": views,
    }


@app.route("/mystats")
def MyStats():
    """The annotator's own answer statistics -- their rows only, and nothing
    but SELECTs, so it is safe to open at any point of a task."""
    worker = RequireWorker()
    if not worker["consented_at"]:
        return redirect(url_for("Index"))
    return render_template("mystats.html", worker=worker, stats=OwnStats(Db(), worker),
                           pct=Pct, stats_page=True)


# ------------------------------------------------------------------ page serving


@app.route("/page/<company>/<int:year>/<int:page>.jpg")
def PageImage(company, year, page):
    """Serve a cached page image, rendering it on demand the first time.

    `page` is the page's index in the file, the same convention as the excerpt.
    This is the fallback when the excerpt cannot be built, so a failure here is
    a plain 404 rather than an exception.
    """
    pdf = ReportPath(company, year)
    if pdf is None:
        abort(404)
    path = common.PageImagePath(company, year, page)
    if not os.path.isfile(path):
        pymupdf = common.Pymupdf()
        if pymupdf is None:
            abort(404)
        try:
            with pymupdf.open(pdf) as doc:
                in_range = 1 <= page <= doc.page_count
                if in_range:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    doc.load_page(page - 1).get_pixmap(dpi=config.PAGE_DPI).save(
                        path, jpg_quality=config.PAGE_JPEG_QUALITY)
        except Exception as exc:                                    # noqa: BLE001
            app.logger.warning("page image failed: %s/%s p%s: %s: %s",
                               company, year, page, exc.__class__.__name__, exc)
            abort(404)
        if not in_range:
            abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=86400)


def ReportPath(company, year):
    """The report file for this company/year, or None.

    The name comes out of the URL, so the result is checked to be a file the
    configured PDF folder actually contains -- never an arbitrary path.
    """
    if "/" in company or "\\" in company or ".." in company:
        return None
    root = os.path.abspath(config.PDF_DIR)
    path = os.path.abspath(common.PdfPath(company, year))
    if not (path == root or path.startswith(root + os.sep)) or not os.path.isfile(path):
        return None
    return path


def PdfProblem(company, year, pages, reason, page_count=None):
    """Shown instead of a 500 or an empty PDF when the excerpt cannot be built.

    The evidence page is the only thing the annotator can check the AI against,
    so a failure here has to leave a way through -- the page as an image, the
    whole report -- rather than a dead tab. It never says anything about what
    the answer should be: a report we cannot open is not evidence of "not
    mentioned".
    """
    app.logger.warning("pdf excerpt unavailable (%s): %s/%s pages=%s count=%s",
                       reason, company, year, pages, page_count)
    return render_template("pdfproblem.html", company=company, year=year,
                           pages=pages, reason=reason, page_count=page_count,
                           has_report=ReportPath(company, year) is not None), 200


@app.route("/pdf/<company>/<int:year>.pdf")
def PdfExcerpt(company, year):
    """Serve just the cited pages as a small PDF, so annotators can Ctrl-F the quote.

    Sending the 20-40 MB original over the internet per item is not viable.

    Exactly the cited pages, nothing around them: the excerpt has to show what
    the AI actually pointed at, so that "the commitment is not on this page" is
    something the annotator can see and record (the source-page check in Q0)
    rather than something the platform quietly papers over. The full report is
    one link away when the citation turns out to be wrong.
    """
    cited = sorted({int(p) for p in request.args.getlist("p") if p.isdigit()})
    if not cited:
        abort(404)
    pdf = ReportPath(company, year)
    pymupdf = common.Pymupdf()
    if pymupdf is None or pdf is None:
        return PdfProblem(company, year, cited, "missing")
    try:
        with pymupdf.open(pdf) as src:
            count = src.page_count
            inside = [p for p in cited if 1 <= p <= count]
            if not inside:
                # Every cited page lies outside this file (the AI misread the
                # numbering): say so instead of failing on an empty PDF.
                return PdfProblem(company, year, cited, "out_of_range", count)
            if len(inside) < len(cited):
                app.logger.warning("pdf excerpt partial: %s/%s cited=%s outside=%s count=%s",
                                   company, year, cited,
                                   [p for p in cited if p not in inside], count)
            out = pymupdf.open()
            try:
                for p in inside[:config.MAX_EXCERPT_PAGES]:
                    # widgets=False: copying a page's interactive form fields can
                    # recurse until MuPDF gives up (FzErrorLimit: exception stack
                    # overflow) on reports whose AcroForm is deeply nested -- and
                    # a form field is not something a reader of an excerpt needs.
                    out.insert_pdf(src, from_page=p - 1, to_page=p - 1, widgets=False)
                blob = out.tobytes()
            finally:
                out.close()
    except Exception as exc:                                        # noqa: BLE001
        # Any PyMuPDF failure (a damaged file, an unsupported structure) lands
        # here: the annotator gets the alternatives, not a 500.
        app.logger.warning("pdf excerpt failed: %s/%s pages=%s: %s: %s",
                           company, year, cited, exc.__class__.__name__, exc)
        return PdfProblem(company, year, cited, "broken")
    buf = io.BytesIO(blob)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf",
                     download_name=f"{company}_{year}_pages.pdf")


@app.route("/report/<company>/<int:year>.pdf")
def ReportPdf(company, year):
    """The whole report. The excerpt is what annotators normally use; this is
    for the cases it cannot serve -- a cited page that is not where the AI said
    it was, or a file the excerpt builder chokes on. Range requests are allowed
    so the browser's viewer can page through it instead of pulling 20-40 MB."""
    path = ReportPath(company, year)
    if path is None:
        abort(404)
    return send_file(path, mimetype="application/pdf", max_age=3600, conditional=True)


# ------------------------------------------------------- finding a quote again

# The context pages around a citation absorb the usual one- or two-page offset
# between a report's printed numbering and the page's index in the file. They
# do nothing for a page reference that is simply wrong -- the AI read a figure
# number as a page number, or attributed another year's sentence to this one.
# For those, search the report for the sentence instead of trusting the number.

_WS = re.compile(r"\s+")
_TEXT_CACHE = {}          # (path, mtime) -> whitespace-stripped text, one per page
_TEXT_CACHE_MAX = 6       # a handful of reports per worker; ~1 MB each


def PageTexts(path):
    """Every page's text, whitespace removed, cached per file version.

    PDF text extraction sprinkles spaces and line breaks through CJK text, so a
    quote never matches literally; stripping whitespace on both sides makes the
    comparison meaningful.
    """
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return []
    hit = _TEXT_CACHE.get(key)
    if hit is None:
        pymupdf = common.Pymupdf()
        if pymupdf is None:
            return []
        try:
            with pymupdf.open(path) as doc:
                hit = [_WS.sub("", doc.load_page(i).get_text())
                       for i in range(doc.page_count)]
        except Exception as exc:                                    # noqa: BLE001
            app.logger.warning("page text failed: %s: %s: %s",
                               path, exc.__class__.__name__, exc)
            return []
        if len(_TEXT_CACHE) >= _TEXT_CACHE_MAX:
            _TEXT_CACHE.pop(next(iter(_TEXT_CACHE)))
        _TEXT_CACHE[key] = hit
    return hit


def Needles(text, size):
    """Distinctive slices of the quote to look for: its start, middle and end.

    A quote can be cut differently in the report than in the AI's copy of it
    (a line break inside a number, a footnote marker), so three short probes
    find it where one long one would not.
    """
    flat = _WS.sub("", text or "")
    if len(flat) < size:
        return [flat] if len(flat) >= 8 else []
    starts = sorted({0, (len(flat) - size) // 2, len(flat) - size})
    return [flat[s:s + size] for s in starts]


def FindPages(path, text, limit=12):
    """1-based pages of `path` holding the quote. Long probes first: a shorter
    one matches more reports but also more boilerplate."""
    texts = PageTexts(path)
    if not texts:
        return []
    for size in (18, 12):
        needles = Needles(text, size)
        if not needles:
            return []
        found = [i + 1 for i, page in enumerate(texts)
                 if any(n in page for n in needles)]
        if found:
            return found[:limit]
    return []


@app.route("/locate/<int:item_id>")
def Locate(item_id):
    """Where in the report does this sentence actually appear?

    The way out when a page reference is not merely offset but wrong. Read-only
    -- it looks at the reports, never at anyone's answers, and writes nothing --
    so it is safe to open mid-task, and it deliberately reports only where the
    text is: what that means for the verdict stays the annotator's call.
    """
    worker = RequireWorker()
    if not worker["consented_at"]:
        return redirect(url_for("Index"))
    row = Db().execute("SELECT * FROM item WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        abort(404)

    years = common.YearRows(json.loads(row["payload"]))
    raw = request.args.get("year")
    asked = int(raw) if (raw or "").isdigit() else None
    if asked is None:
        target, needle, cited = row["declared_year"], row["commitment"], row["source_page"]
    else:
        entry = next((y for y in years if y["year"] == asked), None)
        if entry is None or not entry["evidence"]:
            abort(404)
        target, needle = asked, entry["evidence"]
        cited = entry["evidence_page"]

    # Default: the report the citation names. ?all=1 also looks at this
    # company's other report years -- a sentence attributed to the wrong year
    # is a different mistake from a wrong page, and worth telling apart.
    #
    # Only years up to the one being asked about. The task reveals years in
    # order and the 0-100 slider is explicitly "judged from what is known up to
    # this year"; letting the search reach a later report would hand the
    # annotator the outcome before they have rated the trajectory. Carrying an
    # older report's sentence forward is the misattribution that actually
    # happens, and that stays visible.
    wanted = [target]
    if request.args.get("all") == "1":
        wanted = sorted({y for y in {row["declared_year"], *(y["year"] for y in years)}
                         if isinstance(y, int) and isinstance(target, int) and y <= target},
                        key=lambda y: (y != target, y))
        wanted = wanted or [target]

    results = []
    for y in wanted:
        path = ReportPath(row["company"], y)
        results.append({
            "year": y, "is_cited_year": y == target,
            "missing": path is None,
            "pages": FindPages(path, needle) if path else [],
        })
    return render_template(
        "locate.html", worker=worker, item=row, year=asked, needle=needle,
        cited=common.ParsePages(cited), target=target, results=results,
        searched_all=request.args.get("all") == "1", stats_page=True)


# ------------------------------------------------------------------------- admin


def RequireAdmin():
    if not config.ADMIN_KEY or request.args.get("key") != config.ADMIN_KEY:
        abort(403)


@app.route("/admin")
def Admin():
    RequireAdmin()
    db = Db()
    # Test accounts are left out of every figure here, as they are in exports.
    stats = db.execute(
        """WITH ra AS (SELECT a.* FROM assignment a JOIN worker w ON w.worker_id = a.worker_id
                        WHERE w.is_test = 0)
           SELECT (SELECT COUNT(*) FROM item WHERE active = 1)                    AS items,
                  (SELECT COUNT(*) FROM item WHERE active = 1 AND stratum='anomaly') AS anomaly,
                  (SELECT COUNT(*) FROM item WHERE active = 1 AND is_gold = 1)    AS gold,
                  (SELECT COUNT(*) FROM worker WHERE is_test = 0)                 AS workers,
                  (SELECT COUNT(*) FROM ra WHERE stage = 3)                       AS submitted,
                  (SELECT COUNT(*) FROM ra WHERE stage IN (1,2))                  AS open,
                  (SELECT COUNT(*) FROM final f JOIN ra ON ra.assign_id = f.assign_id
                    WHERE f.ai_final_agree = 0)                                   AS final_flips,
                  (SELECT COUNT(*) FROM verdict v JOIN ra ON ra.assign_id = v.assign_id
                    WHERE v.ai_status <> v.human_status)                          AS year_flips,
                  (SELECT COUNT(*) FROM final f JOIN ra ON ra.assign_id = f.assign_id
                    WHERE f.window_ok NOT IN ('ok','unsure'))                     AS window_bad,
                  (SELECT COUNT(*) FROM verdict v JOIN ra ON ra.assign_id = v.assign_id
                    WHERE v.correct_page <> '')                                   AS page_fixes,
                  (SELECT COUNT(*) FROM final f JOIN ra ON ra.assign_id = f.assign_id
                    WHERE f.n_page_views = 0)                                     AS no_page_views,
                  (SELECT COUNT(*) FROM ante an JOIN ra ON ra.assign_id = an.assign_id
                    WHERE an.is_commitment='not_commitment')                      AS not_commit"""
    ).fetchone()
    coverage = db.execute(
        """SELECT stratum, n, COUNT(*) AS items FROM (
               SELECT i.item_id, i.stratum, COUNT(a.assign_id) AS n
                 FROM item i LEFT JOIN assignment a
                   ON a.item_id = i.item_id AND a.stage = 3
                  AND a.worker_id IN (SELECT worker_id FROM worker WHERE is_test = 0)
                WHERE i.active = 1 AND i.is_gold = 0 GROUP BY i.item_id)
           GROUP BY stratum, n ORDER BY stratum, n"""
    ).fetchall()
    workers = db.execute(
        "SELECT * FROM worker ORDER BY is_test, n_done DESC LIMIT 200").fetchall()
    alloc_rows = []
    if config.N_ANNOTATORS > 0:
        import allocate
        pids = {}
        for w in db.execute("SELECT slot, prolific_pid FROM worker "
                            "WHERE is_test = 0 AND slot IS NOT NULL ORDER BY worker_id"):
            pids.setdefault(w["slot"], []).append(w["prolific_pid"])
        alloc_rows = [dict(slot=r["slot"], n=r["n"], score=r["score"],
                           pids=", ".join(pids.get(r["slot"], [])))
                      for r in allocate.Summary(db)]
    return render_template("admin.html", stats=stats, coverage=coverage, workers=workers,
                           alloc=alloc_rows, n_annotators=config.N_ANNOTATORS,
                           key=config.ADMIN_KEY, redundancy=config.REDUNDANCY,
                           redundancy_control=config.REDUNDANCY_CONTROL)


@app.route("/admin/answers")
def AdminAnswers():
    """The latest submitted answers in full, straight from the tables and with
    test accounts included (flagged), so "did what I just clicked actually get
    stored?" can be checked live without an export. ?limit=N, ?pid=<one person>."""
    RequireAdmin()
    db = Db()
    try:
        limit = min(max(int(request.args.get("limit") or 50), 1), 500)
    except ValueError:
        limit = 50
    pid = (request.args.get("pid") or "").strip()
    where, params = "a.stage = 3", []
    if pid:
        where += " AND w.prolific_pid = ?"
        params.append(pid)
    params.append(limit)
    rows = db.execute(
        f"""SELECT a.assign_id, a.submitted_at, a.edited_at, a.n_edits,
                   w.prolific_pid, w.is_test,
                   i.company, i.commitment, i.final_status, i.is_gold, i.gold_rank,
                   an.is_commitment, an.not_reason, an.commitment_fix, an.target_year_fix,
                   an.seconds, an.active_seconds,
                   f.human_final_status, f.window_ok, f.correct_target_year, f.n_page_views
              FROM assignment a
              JOIN worker w ON w.worker_id = a.worker_id
              JOIN item i ON i.item_id = a.item_id
              LEFT JOIN ante an ON an.assign_id = a.assign_id
              LEFT JOIN final f ON f.assign_id = a.assign_id
             WHERE {where}
             ORDER BY COALESCE(a.edited_at, a.submitted_at) DESC, a.assign_id DESC
             LIMIT ?""", params).fetchall()
    verdicts = {}
    if rows:
        ids = [r["assign_id"] for r in rows]
        marks = ",".join("?" * len(ids))
        for v in db.execute(f"SELECT * FROM verdict WHERE assign_id IN ({marks}) "
                            "ORDER BY assign_id, year", ids):
            verdicts.setdefault(v["assign_id"], []).append(v)
    return render_template("admin_answers.html", rows=rows, verdicts=verdicts,
                           window_labels=dict(config.WINDOW_CHOICES),
                           key=config.ADMIN_KEY, limit=limit, pid=pid)


@app.route("/admin/notes")
def AdminNotes():
    """Everyone's stars and notes (the `mark` table) plus the wrap-up comments
    and the reasons given for "not a commitment" / "no target year", newest
    first -- the questions and remarks annotators leave behind, in one place.
    ?pid=<id> one person; ?test=1 includes test accounts; ?limit=N (default
    300). Read-only."""
    RequireAdmin()
    db = Db()
    pid = (request.args.get("pid") or "").strip()
    with_test = request.args.get("test") == "1"
    try:
        limit = min(max(int(request.args.get("limit") or 300), 1), 5000)
    except ValueError:
        limit = 300
    where, params = "1 = 1", []
    if not with_test:
        where += " AND w.is_test = 0"
    if pid:
        where += " AND w.prolific_pid = ?"
        params.append(pid)
    params.append(limit)
    rows = db.execute(
        f"""SELECT a.assign_id, a.item_id, a.stage, a.submitted_at, w.prolific_pid, w.is_test,
                   i.company, i.commitment, i.is_gold, i.gold_rank,
                   COALESCE(m.starred, 0) AS starred, COALESCE(m.note, '') AS note, m.updated_at,
                   an.is_commitment, an.not_reason, an.commitment_fix, f.comment
              FROM assignment a
              JOIN worker w ON w.worker_id = a.worker_id
              JOIN item i ON i.item_id = a.item_id
              LEFT JOIN mark m ON m.assign_id = a.assign_id
              LEFT JOIN ante an ON an.assign_id = a.assign_id
              LEFT JOIN final f ON f.assign_id = a.assign_id
             WHERE {where}
               AND (m.starred = 1 OR COALESCE(m.note, '') <> ''
                    OR COALESCE(f.comment, '') <> '' OR COALESCE(an.not_reason, '') <> ''
                    OR COALESCE(an.commitment_fix, '') <> '')
             ORDER BY COALESCE(m.updated_at, a.submitted_at, '') DESC, a.assign_id DESC
             LIMIT ?""", params).fetchall()
    n_marks = sum(1 for r in rows if r["starred"] or r["note"])
    return render_template("admin_notes.html", rows=rows, n_marks=n_marks, pid=pid,
                           with_test=with_test, limit=limit, key=config.ADMIN_KEY)


# Counters that add up across people; the rest (medians, averages) do not, so
# the team row is built only from these.
_SUM_KEYS = ("done", "open", "edits", "years", "flagged", "flips", "page_fixes",
             "n_items", "finals", "final_flips", "window_bad", "not_commit", "unsure",
             "probs", "views", "no_views", "gold_pass", "gold_fail",
             "src_asked", "src_ok", "src_wrong", "src_notfound")


@app.route("/admin/annotators")
def AdminAnnotators():
    """Every annotator's own statistics, side by side.

    Exactly the figures each person sees on their own /mystats -- same
    function, so the two can never drift apart -- plus a team row to compare
    against. Read-only: OwnStats is SELECTs only.

    This is the page for spotting someone who is clicking through without
    reading: no evidence pages opened, minutes per item far below the group,
    the AI agreed with on every single year.
    """
    RequireAdmin()
    db = Db()
    rows = []
    # Fetched up front: OwnStats runs its own queries on this connection.
    for w in db.execute("SELECT * FROM worker ORDER BY is_test, worker_id").fetchall():
        stats = OwnStats(db, w)
        if not stats["done"] and not stats["open"]:
            continue  # entered but never started: nothing to compare
        rows.append({"w": w, "s": stats})
    real = [r["s"] for r in rows if not r["w"]["is_test"]]
    total = {k: sum(s[k] for s in real) for k in _SUM_KEYS}
    total["n"] = len(real)
    # Team quartiles are computed over everyone's raw values pooled together
    # (every rated year, every item), not over the per-person medians.
    total["prob_q"] = Quartiles([p for s in real for p in s["_probs"]])
    total["minutes_q"] = Quartiles([m for s in real for m in s["_minutes"]])
    total["views_q"] = Quartiles([v for s in real for v in s["_views"]])
    return render_template("admin_annotators.html", rows=rows, total=total,
                           pct=Pct, key=config.ADMIN_KEY)


def _YearCounts(db, real_only):
    """Per submitted assignment: years answered, years where the AI was judged
    wrong, years whose label was changed, and the estimate given for the last
    year."""
    where = " AND w.is_test = 0" if real_only else ""
    out = {}
    for r in db.execute(
            f"""SELECT v.assign_id, v.ai_status, v.human_status, v.evidence_quality, v.achieve_prob
                  FROM verdict v
                  JOIN assignment a ON a.assign_id = v.assign_id
                  JOIN worker w ON w.worker_id = a.worker_id
                 WHERE a.stage = 3{where}
                 ORDER BY v.assign_id, v.year"""):
        c = out.setdefault(r["assign_id"], {"years": 0, "flagged": 0, "flips": 0, "prob_last": None})
        c["years"] += 1
        if (r["evidence_quality"] or "ok") != "ok":
            c["flagged"] += 1
        if common.DisplayStatus(r["ai_status"] or "") != common.DisplayStatus(r["human_status"] or ""):
            c["flips"] += 1
        if r["achieve_prob"] is not None and str(r["achieve_prob"]).isdigit():
            c["prob_last"] = int(r["achieve_prob"])
    return out


@app.route("/admin/items")
def AdminItems():
    """Every item with at least one submitted answer, each person's result
    side by side: Q0, conclusion vs the AI, years flagged, minutes, look-ups.
    ?pid=<id> lists only the items that person answered (the other people's
    answers to the same items stay visible, for comparison); ?gold=1
    calibration items only; ?disagree=1 only items whose annotators differ on
    Q0 or on the conclusion; ?test=1 includes test accounts; ?limit=N
    (default 300). Read-only."""
    RequireAdmin()
    db = Db()
    pid = (request.args.get("pid") or "").strip()
    only_gold = request.args.get("gold") == "1"
    only_dis = request.args.get("disagree") == "1"
    with_test = request.args.get("test") == "1"
    try:
        limit = min(max(int(request.args.get("limit") or 300), 1), 5000)
    except ValueError:
        limit = 300
    # ?item=<id>: just that one item, with its full commitment text -- the
    # target of links from reports that cite items by number.
    try:
        item_id = int(request.args.get("item") or 0)
    except ValueError:
        item_id = 0
    where, params = "a.stage = 3", []
    if not with_test:
        where += " AND w.is_test = 0"
    if only_gold:
        where += " AND i.is_gold = 1"
    if item_id:
        where += " AND i.item_id = ?"
        params.append(item_id)
    rows = db.execute(
        f"""SELECT a.assign_id, a.item_id, a.submitted_at, a.edited_at, a.n_edits,
                   w.prolific_pid, w.is_test,
                   i.company, i.commitment, i.declared_year, i.target_year, i.is_gold,
                   i.gold_rank, i.final_status, i.stratum,
                   an.is_commitment, an.source_page_ok, an.active_seconds, an.target_year_fix,
                   f.human_final_status, f.ai_final_agree, f.window_ok, f.n_page_views
              FROM assignment a
              JOIN worker w ON w.worker_id = a.worker_id
              JOIN item i ON i.item_id = a.item_id
              LEFT JOIN ante an ON an.assign_id = a.assign_id
              LEFT JOIN final f ON f.assign_id = a.assign_id
             WHERE {where}
             ORDER BY i.is_gold DESC, i.gold_rank, i.item_id, a.assign_id""", params).fetchall()
    counts = _YearCounts(db, real_only=not with_test)
    items = {}
    for r in rows:
        it = items.setdefault(r["item_id"], {
            "item_id": r["item_id"], "company": r["company"], "commitment": r["commitment"],
            "declared_year": r["declared_year"], "target_year": r["target_year"],
            "is_gold": r["is_gold"], "gold_rank": r["gold_rank"], "stratum": r["stratum"],
            "ai_final": common.FinalStatus(r["final_status"] or ""), "answers": []})
        c = counts.get(r["assign_id"], {"years": 0, "flagged": 0, "flips": 0, "prob_last": None})
        it["answers"].append({
            "pid": r["prolific_pid"], "is_test": r["is_test"], "assign_id": r["assign_id"],
            "is_commitment": r["is_commitment"], "source_page_ok": r["source_page_ok"],
            "target_year_fix": r["target_year_fix"],
            "human_final": (common.FinalStatus(r["human_final_status"])
                            if r["human_final_status"] else None),
            "final_agree": r["ai_final_agree"], "window_ok": r["window_ok"],
            "years": c["years"], "flagged": c["flagged"], "flips": c["flips"],
            "prob_last": c["prob_last"],
            "minutes": round(r["active_seconds"] / 60.0, 1) if r["active_seconds"] else None,
            "views": r["n_page_views"], "n_edits": r["n_edits"],
            "submitted_at": r["submitted_at"], "edited_at": r["edited_at"]})
    out = []
    for it in items.values():
        ans = it["answers"]
        q0 = {a["is_commitment"] or "" for a in ans}
        fin = {a["human_final"] for a in ans if a["human_final"]}
        it["n"] = len(ans)
        it["disagree"] = len(ans) > 1 and (len(q0) > 1 or len(fin) > 1)
        it["mine"] = bool(pid) and any(a["pid"] == pid for a in ans)
        if pid and not it["mine"]:
            continue
        if only_dis and not it["disagree"]:
            continue
        out.append(it)
    # Calibration items first (in their fixed order), then the items people
    # disagree on, then the most-annotated ones.
    out.sort(key=lambda it: (not it["is_gold"], it["gold_rank"] or 0, not it["disagree"],
                             -it["n"], it["item_id"]))
    return render_template("admin_items.html", items=out[:limit], n_all=len(out), limit=limit,
                           pid=pid, item_id=item_id, only_gold=only_gold, only_dis=only_dis,
                           with_test=with_test,
                           window_labels=dict(config.WINDOW_CHOICES), key=config.ADMIN_KEY)


@app.route("/admin/calibration")
def AdminCalibration():
    """The shared calibration items, one block each: every annotator's answer
    for every year side by side with the majority label, the share of people
    behind it and the spread of the 0-100 estimates -- inter-annotator
    agreement on identical material. Then one row per person: how often they
    sat with the majority, and how far their estimates sat from the group's
    median. ?test=1 includes test accounts. Read-only."""
    RequireAdmin()
    db = Db()
    with_test = request.args.get("test") == "1"
    test_sql = "" if with_test else " AND w.is_test = 0"
    gold = db.execute("SELECT * FROM item WHERE active = 1 AND is_gold = 1 "
                      "ORDER BY gold_rank, item_id").fetchall()
    people = db.execute(
        f"""SELECT DISTINCT w.worker_id, w.prolific_pid, w.is_test
              FROM worker w
              JOIN assignment a ON a.worker_id = w.worker_id
              JOIN item i ON i.item_id = a.item_id
             WHERE a.stage = 3 AND i.is_gold = 1{test_sql}
             ORDER BY w.is_test, w.worker_id""").fetchall()
    answers = {}  # (item_id, worker_id) -> the answer, with its per-year verdicts
    for r in db.execute(
            f"""SELECT a.assign_id, a.item_id, a.worker_id, an.is_commitment, an.source_page_ok,
                       an.active_seconds, an.target_year_fix, f.human_final_status, f.window_ok,
                       f.n_page_views
                  FROM assignment a
                  JOIN worker w ON w.worker_id = a.worker_id
                  JOIN item i ON i.item_id = a.item_id
                  LEFT JOIN ante an ON an.assign_id = a.assign_id
                  LEFT JOIN final f ON f.assign_id = a.assign_id
                 WHERE a.stage = 3 AND i.is_gold = 1{test_sql}"""):
        answers[(r["item_id"], r["worker_id"])] = dict(r, years={})
    by_assign = {a["assign_id"]: a for a in answers.values()}
    for v in db.execute(
            f"""SELECT v.* FROM verdict v
                  JOIN assignment a ON a.assign_id = v.assign_id
                  JOIN worker w ON w.worker_id = a.worker_id
                  JOIN item i ON i.item_id = a.item_id
                 WHERE a.stage = 3 AND i.is_gold = 1{test_sql}"""):
        a = by_assign.get(v["assign_id"])
        if a is not None:
            a["years"][v["year"]] = v

    labels = config.STATUS_CHOICES
    person = {p["worker_id"]: {"items": 0, "years": 0, "agree": 0, "final_n": 0, "final_agree": 0,
                               "q0_n": 0, "q0_agree": 0, "dev": [], "minutes": []}
              for p in people}
    blocks = []
    for it in gold:
        cells = {p["worker_id"]: answers.get((it["item_id"], p["worker_id"])) for p in people}
        present = [p for p in people if cells[p["worker_id"]] is not None]
        q0_major, q0_share = Majority([cells[p["worker_id"]]["is_commitment"] for p in present],
                                      order=["valid", "not_commitment", "unsure"])
        yrows = []
        for y in common.YearRows(json.loads(it["payload"])):
            labs, probs = {}, {}
            for p in present:
                v = cells[p["worker_id"]]["years"].get(y["year"])
                if v is None:
                    continue
                labs[p["worker_id"]] = common.DisplayStatus(v["human_status"] or "")
                if v["achieve_prob"] is not None and str(v["achieve_prob"]).isdigit():
                    probs[p["worker_id"]] = int(v["achieve_prob"])
            major, share = Majority(list(labs.values()), order=labels)
            pq = Quartiles(list(probs.values()), digits=0)
            for w, lab in labs.items():
                s = person[w]
                s["years"] += 1
                s["agree"] += lab == major
                if w in probs and pq:
                    s["dev"].append(abs(probs[w] - pq["median"]))
            yrows.append({"year": y["year"], "ai": y["ai_label"], "labels": labs, "probs": probs,
                          "major": major, "share": share, "n": len(labs), "pq": pq})
        finals = {p["worker_id"]: common.FinalStatus(cells[p["worker_id"]]["human_final_status"])
                  for p in present if cells[p["worker_id"]]["human_final_status"]}
        f_major, f_share = Majority(list(finals.values()), order=config.FINAL_CHOICES)
        for w, f in finals.items():
            person[w]["final_n"] += 1
            person[w]["final_agree"] += f == f_major
        for p in present:
            s, a = person[p["worker_id"]], cells[p["worker_id"]]
            s["items"] += 1
            s["q0_n"] += 1
            s["q0_agree"] += a["is_commitment"] == q0_major
            if a["active_seconds"]:
                s["minutes"].append(a["active_seconds"] / 60.0)
        blocks.append({"item": it, "ai_final": common.FinalStatus(it["final_status"] or ""),
                       "years": yrows, "present": present, "cells": cells,
                       "q0_major": q0_major, "q0_share": q0_share,
                       "finals": finals, "f_major": f_major, "f_share": f_share})
    summary = []
    for p in people:
        s = person[p["worker_id"]]
        # "done", not "items": in a template `r.items` would resolve to dict.items.
        summary.append({
            "pid": p["prolific_pid"], "is_test": p["is_test"], "done": s["items"],
            "years": s["years"], "agree": s["agree"],
            "final_n": s["final_n"], "final_agree": s["final_agree"],
            "q0_n": s["q0_n"], "q0_agree": s["q0_agree"],
            "dev": round(sum(s["dev"]) / len(s["dev"]), 1) if s["dev"] else None,
            "minutes": round(Median(s["minutes"]), 1) if s["minutes"] else None})
    return render_template("admin_calibration.html", blocks=blocks, summary=summary,
                           with_test=with_test, key=config.ADMIN_KEY)


@app.route("/admin/export/<kind>.csv")
def AdminExport(kind):
    RequireAdmin()
    import export
    builders = {"item": export.ItemCsv, "year": export.YearCsv, "risk": export.RiskCsv}
    if kind not in builders:
        abort(404)
    text = builders[kind](Db())
    buf = io.BytesIO(text.encode("utf-8-sig"))
    return send_file(buf, mimetype="text/csv", as_attachment=True,
                     download_name=f"esg_{kind}.csv")


@app.route("/healthz")
def Healthz():
    return {"ok": True, "items": Db().execute("SELECT COUNT(*) AS n FROM item").fetchone()["n"]}


if __name__ == "__main__":
    common.InitDb()
    app.run(host=os.getenv("ESG_HOST", "127.0.0.1"),
            port=int(os.getenv("ESG_PORT", "8081")), debug=False)
