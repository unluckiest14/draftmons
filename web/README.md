# The draft board

Static frontend for the draft league API. No build step and no dependencies —
plain ES modules, served as-is. See **Files** at the bottom for the layout.

## Running it

FastAPI serves this directory itself, so start the API and open the board:

    .venv-linux/bin/python -m uvicorn main:app --reload
    # -> http://localhost:8000/app/   ("/" redirects here)

Use that interpreter explicitly, or `source .venv-linux/bin/activate` first.
Two traps on this machine: there is no bare `python` on PATH (only `python3`),
and `.venv` is a broken leftover — a Windows venv with its packages in
`Lib/site-packages` that a later `python3 -m venv` overwrote, so its
`bin/python` reads an empty `lib/python3.14/site-packages` and cannot import
fastapi. `.venv-linux` is the working one.

These files are served with `Cache-Control: no-cache`, which means "ask before
using the copy you have" rather than "do not cache" — the browser keeps them
and gets a 304. Without it Starlette sends no cache directive at all, the
browser guesses a freshness window from the file's age, and you end up running
half of one version: a new `index.html` with a tab button, an `app.js` from
cache that has never heard of that tab, and a click that falls through to the
pool. That happened during a live session; see `RevalidatingFiles` in main.py.

Serving it from the API on purpose: a separate dev server would be a different
origin, which would mean enabling CORS on an API that has no authentication.
Same origin means the board fetches `/seasons/1/pool` with no base URL.

If you do want to serve the files separately, pass the API's address:

    http://localhost:5173/?api=http://localhost:8000


...and add `CORSMiddleware` to `main.py`, which is currently not there.

## Pool and Draft

The first live session found these two confusing, and the names were half of
it: "Board" and "Draft" both read as *the draft board*. The browsing tab is now
called **Pool**, which is what it shows — the season's draftable Pokémon, with
the filters and the detail panel.

The other half was that the Pool could not act. Everybody browsing during the
draft clicked a Pokémon expecting to take it, and had to be told the picking
happens on another tab. Now the Pool says when it is your turn, and any
Pokémon you open there has a **Draft** button that goes through the draft's own
confirmation panel — one place spends a pick, one set of rules about it. The
Draft tab still owns the order, the clock, the picker and everybody's picks.

For that to work the draft is polled for as long as one is running, whatever
tab is on screen, so the banner can appear while you are reading a movepool.

The same poll drives the **your-turn alert**: a full-screen card the moment the
turn becomes yours, with the round, your points and the clock, and a button
straight to the draft. It fires on the edge — when the draft goes from
not-your-turn to your-turn — and once per pick, so a five-second poll cannot
make it flash. Dismissing it dismisses the card, not the turn: the banner and
the clock stay.

Both the Planner and the Roster prime the plan cache themselves
(`plans.load`) the first time they are opened for a season. Nothing else does,
and without it a player in server mode sees an empty tab: their sheets are on
the server while the cache falls back to a localStorage that holds nothing.
The lineup migration waits on the same load — claiming against an empty cache
would find nothing, mark itself done, and strand the sheets it exists to move.

## The two tabs

**Board** is read-only: the season's pool, every Pokémon with its cost, and a
detail panel per Pokémon.

**Planner** is who you *want*, before the draft decides. It draws from the
season's pool — that league's Pokémon at that league's prices, and nothing
else — tracks the budget and roster size, and the question it answers is "do
these fit together". Nothing on it is yours yet, and it does not change after
the draft; looking back at what you wanted is worth keeping.

Its picker searches by name or type, says how much of the pool you are looking
at, and marks anything already drafted in that league (with a filter to hide
them), because a plan built mid-draft should not quietly fill up with Pokémon
somebody else owns. Marked, not withheld: a Pokémon you might trade for is
still worth planning around.

**Roster** is what you *got*. Having picks on the draft board is what fills it:
it reads `GET /draft/board`, finds the team this browser's token belongs to,
and every Pokémon you drafted is already a slot, priced at what it went for
rather than what the pool now lists. There is no add button, because there is
nothing else you may bring, and no budget bar, because the points are spent.
What is left is the weekly work: build the sets, then tick which six you are
bringing. Only those reach the paste.

A Roster sheet is therefore a *lineup*, and keeping several per season is how a
league week works — one per opponent. Plans and lineups live in the same store,
told apart by a `kind` field; a plan written before the split has no kind and
is a plan, which is what every plan was.

There was a version of this app where the Planner did both jobs, and the sheets
built then are lineups by every measure except the label. The Roster tab claims
them, once per season, by two fingerprints only that version produced: the
sheet holds every Pokémon you drafted, or some slot is explicitly benched. It
records that it ran, so a plan you write tomorrow that happens to match your
roster stays where you put it.

The two tabs are separate because they are separate jobs done at different
times, and answering both on one screen confused the first people to use it.
What they share — the slot card, the set editor, the notes, the paste — lives
in `teamsheet.js`, which takes a `host` from whichever tab is drawing: where a
sheet is stored, and which tab to repaint. The set editor alone is three
hundred lines, and a second copy of it is how the two tabs would drift apart
within a week.

Whose roster is answered by the token this browser holds: a player gets their
own team and is never asked. A commissioner who ran the draft for the table
holds an admin token and no team, and has two ways to fix that. **Play as one
of these teams**, in the commissioner panel on the League tab, issues them a
player token for a team that already exists — this browser then drafts, plans
and shows up as that team, exactly like a player who joined. It is also the
only route back for a player who lost their token, since nothing here can
reverse a hash; it rotates, so whoever held the old one is signed out, and the
confirmation says so.

Failing that, the Roster tab asks rather than guessing, and the chosen team
becomes a picker in the banner, switchable week to week and remembered per
league. That one is a tab-local choice, not an identity: the draft board is
public, so it shows nothing that was not already on it.

Both tabs hold a set per Pokémon (nickname, item, ability, tera type, nature,
EVs, IVs, four moves) and export a Showdown paste.

## The draft queue

A plan is a decision you already made, and the first live draft showed what it
costs not to carry that decision forward: digging a Pokémon out of eight
hundred while a clock runs, once per turn, for an hour.

So every card on the Planner has **queue for draft**. Queued Pokémon appear in
order in a panel above the plan — reorderable, because order is the whole point
— and again as chips at the top of the draft picker when it is your turn, with
their rank marked on the grid card too. Clicking one opens the same
confirmation panel as any other pick.

It offers; it never picks. A draft moves, and the Pokémon you queued three
rounds ago may be the wrong call now — what the queue removes is the search,
not the choice. Anything drafted by anyone leaves every queue on the next board
refresh, so what it offers is always something you can still take.

The queue lives in this browser (`draftmons.queue.v1`), not on the server: it
is a scratchpad for the hour the draft takes, on the device you are drafting
from.

**Leagues** is not a tab but a modal, behind the button next to *New draft*. A
league here is a season — one pool, one set of teams, one draft — so running a
second one means creating a second season, which has always worked; finding
your way back to the first is what did not. The library lists every league on
the server with the counts that tell two of them apart (teams, pool size,
unpriced Pokémon, how far the draft got, weeks played), marks the one you are
in and the ones you are commissioner or a player of, and switches, renames or
deletes any of them.

Which league you were last in is remembered in `localStorage`, so the app
reopens it rather than the newest one. A `?season=` link still wins over that,
because a link a commissioner posts to a chat has to open the league it names.

Renaming and deleting need the league's commissioner token, unless nobody has
claimed the league yet — an unclaimed league can be claimed by whoever opens
it, so guarding its name would be theatre. Deleting is not recoverable: the
pool, the teams, the players' saved plans, the draft and every result go with
it, which is why the confirmation counts them out.

**Stats** is the season after the draft: the standings, with a column per week
so a record reads as *when* it was won rather than just how much, and the five
Pokémon with the most eliminations. Both are public — a league's table is the
thing it links people to — and both come straight off the API with no
client-side arithmetic, so the ordering cannot disagree with another client's.

Recording a result lives on the same tab, folded away and shown only to a
browser holding the season's commissioner token. Results have to be entered
somewhere, and a leaderboard whose only input is `/docs` is one that stays
empty. Eliminations are pasted per team, one `Great Tusk, 2` per line — the
same shape as the cost list the pool is built from.

## What it needs from the backend

| It calls | For |
| --- | --- |
| `GET /formats` | the format picker |
| `GET /seasons` | the league picker and the library, with each league's counts |
| `GET /seasons/{id}` | one league, re-read after its pool is built |
| `PATCH /seasons/{id}` | library: renaming one. Commissioner token |
| `DELETE /seasons/{id}` | library: deleting one. Commissioner token |
| `GET /seasons/{id}/pool` | every card on the board |
| `POST /seasons/{id}/pool/from-format/{key}` | the "build pool" button only |
| `GET /formats/{key}/rules` | planner: flagging banned items, moves, abilities |
| `GET /items?format={key}` | planner: the held-item picker and its descriptions |
| `GET /seasons/{id}/teams` | planner: author suggestions on the paste |
| `GET /seasons/{id}/draft/board` | planner: which Pokémon you drafted. Also the draft tab |
| `GET /seasons/{id}/standings` | stats: the table, week by week |
| `GET /seasons/{id}/leaderboard/pokemon?limit=5` | stats: the top five by eliminations |
| `GET /seasons/{id}/matches` | stats: the results list |
| `POST /seasons/{id}/matches` | stats: recording one. Commissioner token |
| `POST /seasons/{id}/teams/{id}/token` | league: taking a team as yours. Commissioner token |
| `DELETE /seasons/{id}/matches/{id}` | stats: correcting one. Commissioner token |

The rules route is what lets the planner say "King's Rock is banned in this
format" rather than just listing a set. It is optional — if it 404s or its
tables are empty, the planner works and simply cannot flag bans.

`GET /formats` returning 503 means the cron job has not run yet; the board says
so and names the command. Run `python update_formats.py` and reload.

`GET /seasons` is what the league picker and the library are both built on:
one request, every league, each with its team, pool and draft counts. It
replaced a `loadSeasons()` that probed ids 1 to 30 on every load and could not
see a league past the thirtieth.

## Where the data comes from

The board renders entirely from `pool_entry`: name, cost, types, base stats,
sprite. One request, then all filtering and sorting happens client-side, because
a pool is at most a couple of thousand rows.

Held **items** do not come from PokeAPI. Its `/item` index is every object in
the games — bicycles, TMs, key items, mail — around 2200 entries of which a
Pokémon can hold perhaps a tenth, so the picker used to make a player scroll
past the Yellow Bike to reach Leftovers. `GET /items` reads Showdown's own
`data/items.ts` instead, so everything offered is holdable, and pairs each with
the one-line description Showdown ships in `data/text/items.ts`.

Each item carries a `spritenum`, which is its cell in Showdown's
`itemicons-sheet.png` — 24x24 cells, sixteen per row. The picker is a real
dropdown rather than a `<datalist>` for exactly this reason: a datalist cannot
show an image, and an item list is far quicker to scan by picture than by name.
One sheet covers the whole catalogue, so there is no second source of item art
to keep in step with the item list.

Passing `?format=` narrows it the rest of the way: Mega stones and Z-crystals
are `isNonstandard: "Past"`, so Gen 9 OU sees 224 items and National Dex sees
508. Items the format bans come back with `banned: true` rather than missing —
a player needs to see that King's Rock exists and is not allowed, not wonder
where it went.

Abilities and movepool are **not** in `pool_entry`, so the detail panel fetches
them from PokeAPI directly, per Pokémon, on click. The movepool is filtered to
the selected format's generation — `gen9-ou` reads the Scarlet/Violet learnset —
so an old-gen league sees the movepool it actually drafts with.

Move type, category and power need one PokeAPI request each, and a full movepool
is 100+ moves, so the table renders names first and fills those columns in as
they arrive. Closing the panel aborts the rest.

## The set editor

Four of its fields are more than plain inputs.

**Natures** list what each one does — `Jolly  +Spe / -SpA` — because the name
alone says nothing. The five that cancel out (Hardy, Docile, Bashful, Quirky,
Serious) are marked `(neutral)` rather than hidden: picking one on purpose is a
real thing to do.

**EV presets** are derived, not decreed. `recommendSpread` reads the Pokémon's
own base stats to decide whether it hits harder physically or specially,
whether it is fast enough to sweep, and whether it is built to take hits, then
stars the matching preset. The "hits softly" test is the one that earns its
place — without it a bulky attacker like Kingambit reads as a wall on bulk
alone and gets told to dump 252 into Defence rather than the stat it wins with.

**IVs** default to 31 and are only stored when they are not, because 31 is what
Showdown assumes for anything unstated. Only the departures reach the paste,
which is why the two that matter in practice — `0 Atk` on a special attacker,
`0 Spe` under Trick Room — are the ones that show up.

**Nicknames** serialise as Showdown's `Nickname (Species)`, with the species
kept in the parentheses so the set still imports as the right Pokémon. A
nickname equal to the species name is dropped rather than repeated.

## Leaving the device

Exactly one action in the planner sends data anywhere: **Open in PokePaste**
form-POSTs the team to `pokepast.es/create` and opens the result in a new tab.
It asks for confirmation first and says that the result is public. Copy and
Download stay local. Nothing is ever sent automatically.

The paste itself is Showdown's export format and imports into the teambuilder
unchanged. `Level` is deliberately omitted so the format decides it — LC is
level 5, most singles is 100 — rather than baking in a guess.

## URL parameters

The URL is the state, so a board is linkable:

    /app/?season=2&format=gen9-lc&view=table&mon=pawniard
    /app/?season=2&format=gen9-lc&tab=planner

`season`, `format`, `tab` (`board` — the Pool tab's old name, kept so links
keep working — `draft`/`planner`/`roster`/`stats`/`league`),
`view` (`grid`/`table`) and
`mon` (a PokeAPI name or Showdown id, which opens the detail panel). `api`
overrides the API base.

## Files

- `index.html` — the shell; every element the app touches is declared here
- `app.js` — season/format/pool state, the board, the detail panel, the tabs
- `planner.js` — the planner tab: the pre-draft wishlist, its budget and pool picker
- `roster.js` — the roster tab: your drafted team, its lineups, who you are bringing
- `teamsheet.js` — what both share: the slot card, the set editor, notes, the paste
- `queue.js` — the draft queue: queued in the Planner, offered in the Draft
- `standings.js` — the stats tab: the table, the top five, and recording a result
- `leagues.js` — the league library: every saved league, and switching between them
- `setup.js` — the new-league wizard: league, pool, prices, join code
- `plans.js` — plan storage: the player's account when joined, localStorage otherwise
- `pokepaste.js` — Showdown paste serialization, the PokePaste POST, legality
- `pokeapi.js` — PokeAPI client, generation → version-group map, type chart
- `api.js` — the draft league API client
- `dom.js` — element builder and the helpers both tabs share
- `styles.css` — one stylesheet, CSS variables for the light/dark themes
