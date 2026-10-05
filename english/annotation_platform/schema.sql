-- ESG annotation platform schema.
-- One "item" = one commitment + all of its yearly AI verdicts = one task.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS item (
    item_id       INTEGER PRIMARY KEY,
    company       TEXT    NOT NULL,
    industry      TEXT,
    commitment    TEXT    NOT NULL,
    declared_year INTEGER,
    target_year   INTEGER,
    source_page   TEXT,
    final_status  TEXT,               -- the pipeline's raw token
    n_years       INTEGER NOT NULL DEFAULT 0,
    seq           TEXT,               -- "109:Not mentioned 110:Not yet achieved ..."
    letters       TEXT,               -- "N P A A" shorthand
    payload       TEXT    NOT NULL,   -- full promise object from summary.json
    priority      REAL    NOT NULL DEFAULT 0,
    flags         TEXT,               -- json list of anomaly flags
    stratum       TEXT    NOT NULL DEFAULT 'control',  -- anomaly | control
    is_gold       INTEGER NOT NULL DEFAULT 0,
    gold_rank     INTEGER,            -- calibration order
    gold_expect   TEXT,               -- json: {"is_commitment":..,"year_status":{..}}
    active        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (company, commitment, declared_year, target_year)
);

CREATE INDEX IF NOT EXISTS idx_item_pick ON item (active, is_gold, priority DESC);

CREATE TABLE IF NOT EXISTS worker (
    worker_id    INTEGER PRIMARY KEY,
    prolific_pid TEXT    NOT NULL UNIQUE,    -- participant id (Prolific id or ?pid=)
    study_id     TEXT,
    session_id   TEXT,
    first_seen   TEXT,
    consented_at TEXT,
    finished_at  TEXT,
    n_done       INTEGER NOT NULL DEFAULT 0,
    gold_pass    INTEGER NOT NULL DEFAULT 0,
    gold_fail    INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'active', -- active | done | screened_out
    is_test      INTEGER NOT NULL DEFAULT 0,        -- test account: no cap, not in the dataset
    slot         INTEGER                            -- annotator slot in balanced mode (see alloc)
);

-- Balanced pre-allocation, used only when ESG_N_ANNOTATORS > 0: the annotator
-- slots (0..N-1) each non-calibration item goes to. A worker is mapped to a
-- slot on first entry and only ever receives that slot's items.
CREATE TABLE IF NOT EXISTS alloc (
    item_id INTEGER NOT NULL REFERENCES item (item_id),
    slot    INTEGER NOT NULL,
    PRIMARY KEY (item_id, slot)
);

CREATE INDEX IF NOT EXISTS idx_alloc_slot ON alloc (slot);

CREATE TABLE IF NOT EXISTS assignment (
    assign_id    INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES item (item_id),
    worker_id    INTEGER NOT NULL REFERENCES worker (worker_id),
    -- 2 = open, 3 = submitted (1 is a legacy value, treated as open)
    stage        INTEGER NOT NULL DEFAULT 2,
    assigned_at  TEXT,
    stage1_at    TEXT,
    submitted_at TEXT,
    edited_at    TEXT,               -- last revision after submission
    n_edits      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (item_id, worker_id)
);

-- Unsent answers, autosaved from the task page, so leaving it (to revise an
-- earlier item, or by accident) never loses what was already filled in.
CREATE TABLE IF NOT EXISTS draft (
    assign_id INTEGER PRIMARY KEY REFERENCES assignment (assign_id),
    form      TEXT NOT NULL,         -- json object of the form fields; '{}' = tombstone
                                     -- (cleared or submitted: nothing to restore)
    saved_at  TEXT,
    seq       INTEGER NOT NULL DEFAULT 0, -- per-page sequence: a save arriving late never
                                          -- overwrites a newer one
    gen       INTEGER NOT NULL DEFAULT 0  -- generation of the latest render of the task page;
                                          -- saves from an older render (a stale tab) are refused
);

CREATE INDEX IF NOT EXISTS idx_assign_item ON assignment (item_id);
CREATE INDEX IF NOT EXISTS idx_assign_open ON assignment (worker_id, stage);

-- Commitment validity screen, answered at the top of the task page.
CREATE TABLE IF NOT EXISTS ante (
    assign_id      INTEGER PRIMARY KEY REFERENCES assignment (assign_id),
    is_commitment  TEXT,        -- valid | not_commitment | unsure
    not_reason     TEXT,
    commitment_fix TEXT,        -- annotator-corrected commitment wording (optional)
    target_year_fix INTEGER,    -- annotator-corrected target year (optional)
    seconds        INTEGER,     -- wall-clock: first opening of the page -> submit
    active_seconds INTEGER      -- client-measured time actually spent in the tab
                                -- (hidden, unfocused and idle stretches left out)
);

-- One row per report year. achieve_prob is the forward-looking estimate made
-- from the clues so far: "given the achievement status this year's report
-- shows, how likely is the target-year record to be met?" It may drift year by
-- year -- one row per year makes it a risk trajectory.
CREATE TABLE IF NOT EXISTS verdict (
    assign_id        INTEGER NOT NULL REFERENCES assignment (assign_id),
    year             INTEGER NOT NULL,
    ai_status        TEXT,      -- the AI's yearly status as shown (four-label name)
    evidence_quality TEXT,      -- ok | current | clue | mixed | other | none (see config.py)
    human_status     TEXT,      -- Achieved | Not yet achieved | Moving away from target | Not mentioned
    achieve_prob     TEXT,      -- "0".."100" slider value
    correct_page     TEXT,      -- page the annotator says the real evidence is on
    alt_clue         TEXT,      -- bases ticked: comma list of 'current' and 1-based clue
                                -- indices; or 'none' (none of the listed items)
    custom_basis     TEXT,      -- optional: the annotator's own evidence text
    PRIMARY KEY (assign_id, year)
);

-- Task wrap-up.
CREATE TABLE IF NOT EXISTS final (
    assign_id           INTEGER PRIMARY KEY REFERENCES assignment (assign_id),
    ai_final_agree      INTEGER,
    human_final_status  TEXT,   -- Achieved | Not achieved | Not mentioned
    comment             TEXT,
    -- Did the pipeline even track the right span of years? This is where a
    -- misread target year becomes reportable.
    window_ok           TEXT,
    correct_target_year INTEGER,
    n_page_views        INTEGER NOT NULL DEFAULT 0,
    seconds             INTEGER
);
