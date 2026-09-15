# Draftmons

A draft-league app for Pokémon Showdown. A commissioner creates a league,
picks a format, prices the Pokémon; players join with a code and take turns
drafting under a points budget and a clock.

Legality comes from Showdown's own data rather than a hand-kept list, so a
pool is exactly what the format allows — 771 Pokémon for Gen 9 OU, 1125 for
National Dex, with the bans already applied.

- **Backend** — FastAPI + SQLite, 39 routes, no ORM
- **Frontend** — plain ES modules, no build step, served by the API
- **Tests** — 256, no network

---

## Running it

Needs Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python scripts/update_formats.py     # build the format tables (once)
.venv/bin/python -m uvicorn main:app --reload
```

Then open **http://localhost:8000/app/** for the board, or
**http://localhost:8000/docs** for the API.

The database is created on first boot. There is nothing else to configure;
`DRAFT_DB` moves it somewhere other than `./draft.db`.

### Checking it works

```bash
.venv/bin/python -m pytest              # 256 tests
.venv/bin/python scripts/smoke_test.py  # end-to-end against the real PokéAPI
```

---

## How a league runs

1. **Create it** — *New draft* in the top bar: name, points per player, roster
   size. Whoever creates it becomes the commissioner.
2. **Build the pool** — pick a Showdown format, or drop in a `.txt`/`.json`
   list of your own.
3. **Price it** — select Pokémon and set them all to a value at once.
4. **Share the code** — players join from the *League* tab and pick a team name.
5. **Set the order** — drag, nudge or shuffle the teams.
6. **Draft** — snake order, a clock per pick, budgets enforced. Everyone
   watches the same board.

Players also get a **planner** for working out a team from what they drafted —
items, abilities, natures, EVs, IVs, moves — which exports a Showdown paste.

---

## Where legality comes from

Three Showdown files in `data/`, checked in so a clone runs:

| File | What it decides |
| --- | --- |
| `formats-data.js` | every Pokémon's tier, per ladder |
| `formats.ts` | the formats, their rulesets and banlists |
| `rulesets.ts` | what a named rule like `Standard` expands to |
| `items.ts` + `items-text.ts` | holdable items, and what each one does |

`scripts/update_formats.py` parses them into the database. None of it is
evaluated as code — they are TypeScript modules full of validator functions,
so the parser walks the object literals by brace depth and lifts out the
declarative fields.

That yields **18 formats** across three ladders — singles, doubles and
National Dex — each with the right `isNonstandard` rules applied, which is why
Mega stones are absent from Gen 9 OU and present in National Dex.

Tiers are most of legality but not all of it: `formats.ts` carries species
bans no tier implies, plus the clauses and move/ability/item bans a draft
board cannot express. Those are stored too and surfaced at
`GET /formats/{key}/rules`.

Pokémon data — stats, types, sprites — comes from
[PokéAPI](https://pokeapi.co) at pool-build time.

```bash
python scripts/update_formats.py --fetch-rules   # re-download from Showdown
python scripts/update_formats.py --check         # report drift, write nothing
```

---

## Layout

```
main.py              entry point — `uvicorn main:app`
draftmons/
  app.py             the FastAPI app, seasons/teams/pool/format routes
  models.py          every request and response shape
  poke_db.py         SQLite access and additive migrations
  pokeapi.py         the PokéAPI client: cache, concurrency cap
  paths.py           where data/ and web/ are
  showdown_rules.py  parses formats.ts + rulesets.ts
  showdown_items.py  parses items.ts + its text file
  routes/            draft, players, results, replay
  services/          the logic each route calls
data/                Showdown's data files, and the schema
web/                 the frontend (see web/README.md)
scripts/             update_formats.py, smoke tests
tests/
```

Routes stay thin: they translate a service exception into a status code and
return a model. Anything with real logic lives in `services/`, which is what
lets the same code run from a script with no HTTP involved.

---

## Notable design decisions

**No accounts.** Joining a league returns one bearer token, and holding it
*is* being that player. There is no password to reset, so a lost token cannot
be reissued — the UI says so where it matters.

**Draft order is derived, not stored.** Who is on the clock is a function of
how many picks have been made, so it cannot drift out of step with the picks.
The two guarantees that matter under concurrency are database constraints
rather than Python checks, because two people clicking at once is the normal
case at a draft:

```sql
UNIQUE (season_id, pick_no)        -- two picks cannot take one slot
UNIQUE (season_id, pool_entry_id)  -- a Pokémon cannot go to two teams
```

**Costs are snapshotted onto each pick.** Repricing the pool mid-season cannot
rewrite what a team has already spent.

**The pick clock is evaluated lazily** — on any read of the draft — because
there is no background worker. A timer therefore expires when somebody next
looks, not on the exact second. Past ten missed turns the clock resets rather
than auto-drafting through them, so a league that closes its laptops overnight
does not come back to a finished draft.

---

## Known gaps

- **The API is unauthenticated** apart from the player and commissioner token
  routes. Anyone who can reach it can rename a team, reprice a pool or change
  the draft order. It is built for a private league on a trusted network; it
  is not ready to be exposed to the internet as-is.
- `GET /seasons` does not exist, so the season picker probes ids 1–30.
- The frontend has no test runner; its logic is covered by hand and by the
  backend tests behind it.

---

## Credits

Format, ruleset and item data from
[Pokémon Showdown](https://github.com/smogon/pokemon-showdown) (MIT).
Pokémon data from [PokéAPI](https://pokeapi.co). Pokémon is a trademark of
Nintendo / Creatures Inc. / GAME FREAK inc.; this is an unofficial fan project
with no affiliation.
