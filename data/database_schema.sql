-- Draft league schema.
--
-- Formats live here rather than in a JSON file so the cron job has somewhere
-- to write and pool building can join against them. `format_species` is the
-- big one: one row per (format, Pokemon), which is what makes "is this mon
-- legal in this season's format" a query instead of a set operation in Python.

CREATE TABLE IF NOT EXISTS season (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    budget      INTEGER NOT NULL DEFAULT 100,
    roster_size INTEGER NOT NULL DEFAULT 8,
    format_key  TEXT,               -- which format this season drafts from
    -- The source_sha256 of the format data in force when the pool was built.
    -- Without this, a season drafted before a Smogon tier shift and one after
    -- are silently incomparable.
    tier_snapshot TEXT,
    -- What to call a custom pool on the board. NULL for a format-built pool,
    -- which is named by its format instead.
    pool_label  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS team (
    id             INTEGER PRIMARY KEY,
    season_id      INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
    name           TEXT    NOT NULL,
    owner          TEXT,             -- the person; team name is the identity
    logo_url       TEXT,
    draft_position INTEGER,          -- NULL until the order is set
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (season_id, name),
    UNIQUE (season_id, draft_position)
);
CREATE INDEX IF NOT EXISTS idx_team_season ON team(season_id);

CREATE TABLE IF NOT EXISTS pool_entry (
    id           INTEGER PRIMARY KEY,
    season_id    INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
    api_name     TEXT    NOT NULL,   -- PokeAPI name: "great-tusk"
    showdown_id  TEXT    NOT NULL,   -- Showdown id: "greattusk"
    display_name TEXT    NOT NULL,
    cost         INTEGER NOT NULL DEFAULT 0,
    types        TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    stats        TEXT    NOT NULL DEFAULT '{}',   -- JSON object, the six base stats
    bst          INTEGER,
    sprite_url   TEXT,               -- small, for the grid
    artwork_url  TEXT,               -- large, for the detail view only
    tier         TEXT,               -- Showdown tier at build time, for display
    banned       INTEGER NOT NULL DEFAULT 0,   -- commissioner house rule
    UNIQUE (season_id, api_name)
);
CREATE INDEX IF NOT EXISTS idx_pool_season ON pool_entry(season_id);

-- One row per format the cron job knows about.
CREATE TABLE IF NOT EXISTS format (
    key           TEXT PRIMARY KEY,
    label         TEXT    NOT NULL,
    -- Which tier column in formats-data.js this format reads: singles reads
    -- `tier`, doubles reads `doublesTier`, natdex reads `natDexTier`. Two
    -- formats can share a ceiling and mean different things without it.
    ladder        TEXT    NOT NULL DEFAULT 'singles',
    tier_ceiling  TEXT    NOT NULL,
    species_count INTEGER NOT NULL,
    -- How many Pokemon config/formats.ts removed on top of the tier ceiling.
    -- Non-zero for LC and NFE, whose banlists are pure species bans.
    species_banned INTEGER NOT NULL DEFAULT 0,
    -- The Showdown format this mirrors, e.g. "[Gen 9] National Dex".
    showdown_name TEXT,
    source_sha256 TEXT    NOT NULL,
    built_at      TEXT    NOT NULL
);

-- The rules from config/formats.ts that a species list cannot express: the
-- clauses in force, and every ban that is not simply a tier. Species bans are
-- applied to format_species at refresh time and also recorded here, so the
-- board can say *why* a Pokemon is missing rather than just omitting it.
CREATE TABLE IF NOT EXISTS format_rule (
    format_key TEXT NOT NULL REFERENCES format(key) ON DELETE CASCADE,
    -- clause | ban_species | ban_tier | ban_other | ban_complex | unban
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    PRIMARY KEY (format_key, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_format_rule ON format_rule(format_key);

CREATE TABLE IF NOT EXISTS format_species (
    format_key  TEXT NOT NULL REFERENCES format(key) ON DELETE CASCADE,
    showdown_id TEXT NOT NULL,
    tier        TEXT NOT NULL,
    PRIMARY KEY (format_key, showdown_id)
);
CREATE INDEX IF NOT EXISTS idx_format_species ON format_species(format_key);

-- Audit trail for the cron job: what it saw, and what moved.
CREATE TABLE IF NOT EXISTS format_update (
    id            INTEGER PRIMARY KEY,
    ran_at        TEXT NOT NULL DEFAULT (datetime('now')),
    source        TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    changed       INTEGER NOT NULL DEFAULT 0,   -- 0 = no drift, nothing written
    moves         TEXT NOT NULL DEFAULT '[]'    -- JSON: [{species, from, to}]
);

-- ---------------------------------------------------------------- players
--
-- How a person gets into a season without making an account.
--
-- The design doc rules out passwords, so identity here is a bearer token
-- handed out once at join time. Only its SHA-256 is stored: a leaked database
-- then cannot be used to act as a player. A single unsalted hash is the right
-- choice for exactly this case and no other — these tokens are 32 bytes of
-- `secrets` output, so there is no dictionary to run against them and nothing
-- for a salt or a slow KDF to protect. Never store a human-chosen password
-- this way.
--
-- The season is the "server": players join one, and everything private is
-- scoped to (player, season).

CREATE TABLE IF NOT EXISTS season_invite (
    season_id  INTEGER PRIMARY KEY REFERENCES season(id) ON DELETE CASCADE,
    join_code  TEXT    NOT NULL UNIQUE,   -- short, readable out loud in a chat
    -- Claim-on-first-use: whoever opens the season gets the admin token, and
    -- from then on rotating or closing the code needs it. The rest of this API
    -- has no auth at all, so this is a lock on the door of one room, not a
    -- security model for the building.
    admin_hash TEXT    NOT NULL,
    is_open    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS player (
    id           INTEGER PRIMARY KEY,
    season_id    INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
    -- The team is the player's public face; the player row is the private
    -- half. One team per player, deleted together.
    team_id      INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    token_hash   TEXT    NOT NULL UNIQUE,
    display_name TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_player_season ON player(season_id);

-- Private per-player storage: the planner's draft plans, server-side.
--
-- `body` is the plan as the planner already writes it — opaque JSON, not
-- columns. The set editor's shape (items, tera, EVs, four moves) is a frontend
-- concern that changes without a migration, and the server never needs to
-- query inside a plan. What the server owes this table is that nobody but its
-- owner can read it, which is `player_id` plus a scoped WHERE on every query.
CREATE TABLE IF NOT EXISTS player_plan (
    id         INTEGER PRIMARY KEY,
    player_id  INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_player_plan ON player_plan(player_id);


-- The draft itself. One row per season; `status` is the lifecycle.
CREATE TABLE IF NOT EXISTS draft (
    season_id    INTEGER PRIMARY KEY REFERENCES season(id) ON DELETE CASCADE,
    status       TEXT    NOT NULL DEFAULT 'setup',   -- setup | live | complete
    -- Copied from season.roster_size when the draft starts, so changing the
    -- roster size mid-draft cannot move the finish line under a draft in
    -- progress.
    rounds       INTEGER NOT NULL,
    -- Seconds a team gets on the clock. 0 disables the timer entirely, which
    -- is what an in-person draft round a table wants.
    pick_seconds INTEGER NOT NULL DEFAULT 120,
    -- When the team currently on the clock got there. The deadline is this
    -- plus pick_seconds; nothing stores the deadline itself, so changing
    -- pick_seconds mid-draft applies from the current pick rather than
    -- retroactively expiring it.
    clock_started_at TEXT,
    started_at   TEXT,
    completed_at TEXT
);

-- A team that ran out of time, and the round it happened in.
--
-- The rule: a timed-out team goes to the back of that round's queue and picks
-- last instead of losing the pick. The deferral is scoped to one round, so the
-- next round starts from the plain snake order again.
--
-- This is the one thing that cannot be derived from the picks. Everything else
-- about whose turn it is falls out of COUNT(picks); a reordered round does
-- not, so it is recorded.
CREATE TABLE IF NOT EXISTS draft_defer (
    season_id   INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
    round_no    INTEGER NOT NULL,
    team_id     INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    deferred_at TEXT    NOT NULL DEFAULT (datetime('now')),
    -- One deferral per team per round: being sent to the back twice in one
    -- round would be a loop with nobody left to go behind.
    PRIMARY KEY (season_id, round_no, team_id)
);

-- One row per pick, in the order they were made.
--
-- `pick_no` is the slot in the snake and is contiguous from 0, which is what
-- lets the team on the clock be derived from COUNT(*) instead of stored. The
-- two UNIQUE constraints are the real concurrency guarantees: at a live draft
-- two people click at the same moment routinely, and a check-then-insert in
-- Python loses that race. The checks in draft_service exist for the error
-- message; these exist for correctness.
CREATE TABLE IF NOT EXISTS draft_pick (
    id            INTEGER PRIMARY KEY,
    season_id     INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
    pick_no       INTEGER NOT NULL,
    round_no      INTEGER NOT NULL,
    team_id       INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    -- NULL means the slot was forfeited: the team was out of time and could
    -- not afford anything left in the pool, so the pick passed. Recording it
    -- as a row rather than a gap keeps every round exactly one pick per team,
    -- which is what lets the round be derived from the pick count.
    pool_entry_id INTEGER REFERENCES pool_entry(id) ON DELETE CASCADE,
    -- What it cost at the moment it was picked. Repricing the pool afterwards
    -- must not rewrite what a team has already spent.
    cost_paid     INTEGER NOT NULL,
    picked_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (season_id, pick_no),
    UNIQUE (season_id, pool_entry_id)
);
CREATE INDEX IF NOT EXISTS idx_draft_pick_season ON draft_pick(season_id);
CREATE INDEX IF NOT EXISTS idx_draft_pick_team ON draft_pick(team_id);

-- ---------------------------------------------------------------- results
--
-- What happened after the draft. The draft tables answer "who owns what";
-- these answer "who won", which is the other half of a league and the only
-- thing the season is actually played for.
--
-- Two tables rather than one, because a result and a stat line are recorded
-- together but read apart: the standings never look at a Pokemon, and the
-- elimination leaderboard never looks at a score.

CREATE TABLE IF NOT EXISTS match_result (
    id             INTEGER PRIMARY KEY,
    season_id      INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
    -- The week is entered, not derived from the date. A match played late is
    -- still that week's match, and the standings are read by week.
    week_no        INTEGER NOT NULL,
    home_team_id   INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    away_team_id   INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    -- NULL is a draw. Rare, but a Showdown battle can end in one, and storing
    -- it as "no winner" beats inventing a third team id or a status column.
    winner_team_id INTEGER REFERENCES team(id) ON DELETE CASCADE,
    -- Pokemon left standing on each side, which is how a draft league writes
    -- a score. Nothing is derived from it; it is the margin, for the table.
    home_score     INTEGER NOT NULL DEFAULT 0,
    away_score     INTEGER NOT NULL DEFAULT 0,
    replay_url     TEXT,
    recorded_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    -- A team cannot play itself, and the winner has to be one of the two
    -- sides. Both are the kind of thing a typo produces and a leaderboard
    -- then reports forever as fact.
    CHECK (home_team_id <> away_team_id),
    CHECK (winner_team_id IS NULL OR winner_team_id IN (home_team_id, away_team_id)),
    -- The guard against the same result being submitted twice, which would
    -- silently hand a team an extra win. A genuine rematch in one week is
    -- recorded as the next week's, or the first row is deleted and replaced.
    UNIQUE (season_id, week_no, home_team_id, away_team_id)
);
CREATE INDEX IF NOT EXISTS idx_match_season ON match_result(season_id, week_no);

-- One row per Pokemon that took a KO in a match, with how many it took.
--
-- A count rather than a row per elimination: nothing needs to know the order
-- they happened in, and the leaderboard is then a SUM over an index instead
-- of a count over thousands of rows.
--
-- `team_id` is who was using it. It is stored rather than joined out of
-- draft_pick because a mon can be on the board without having been drafted --
-- a free agent, or a pool built after the fact -- and the credit still has to
-- land on a team.
CREATE TABLE IF NOT EXISTS match_elim (
    id            INTEGER PRIMARY KEY,
    match_id      INTEGER NOT NULL REFERENCES match_result(id) ON DELETE CASCADE,
    team_id       INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    pool_entry_id INTEGER NOT NULL REFERENCES pool_entry(id) ON DELETE CASCADE,
    elims         INTEGER NOT NULL DEFAULT 0,
    CHECK (elims >= 0),
    -- One line per Pokemon per match: two lines for the same mon is a double
    -- entry, not a Pokemon that fought twice.
    UNIQUE (match_id, pool_entry_id)
);
CREATE INDEX IF NOT EXISTS idx_match_elim_match ON match_elim(match_id);
CREATE INDEX IF NOT EXISTS idx_match_elim_entry ON match_elim(pool_entry_id);


-- An uploaded team logo, stored as bytes rather than a file path.
--
-- In the database on purpose: the league is one SQLite file that someone can
-- copy to another machine, and a logo that lived on disk beside it would be
-- the one part that did not travel. A team logo is a few tens of kilobytes
-- after the browser has downscaled it, so the row stays small.
--
-- `team.logo_url` still exists and still works — that is the "paste a link"
-- path. This is the "upload a file" path, and it wins when both are set,
-- because an upload is the more deliberate act.
CREATE TABLE IF NOT EXISTS team_logo (
    team_id      INTEGER PRIMARY KEY REFERENCES team(id) ON DELETE CASCADE,
    content_type TEXT    NOT NULL,
    image        BLOB    NOT NULL,
    -- Bumped on every replacement and used as a cache-busting query string,
    -- so a new logo appears for everyone instead of sitting behind a cached
    -- copy of the old one.
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- The knockout-by-knockout history of a match read from a replay.
--
-- Stored rather than re-derived: a replay can be deleted from Showdown, and
-- re-fetching and re-parsing one every time somebody opens a past week would
-- put a network round trip behind a page view. The rendered sentence is kept
-- alongside the parts so the history reads the same next season as it did the
-- day it was recorded, even if the wording in the code changes.
CREATE TABLE IF NOT EXISTS match_event (
    id          INTEGER PRIMARY KEY,
    match_id    INTEGER NOT NULL REFERENCES match_result(id) ON DELETE CASCADE,
    -- Position in the battle, so the history keeps its order when two
    -- knockouts happen on the same turn.
    ordinal     INTEGER NOT NULL,
    turn_no     INTEGER NOT NULL DEFAULT 0,
    victim_side TEXT,
    victim      TEXT    NOT NULL,
    killer_side TEXT,
    killer      TEXT,
    -- The move, item or hazard responsible. NULL when the log did not say.
    cause       TEXT,
    -- 1 when it was hazards, recoil or an item rather than a direct hit.
    indirect    INTEGER NOT NULL DEFAULT 0,
    text        TEXT    NOT NULL,
    UNIQUE (match_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_match_event ON match_event(match_id);
