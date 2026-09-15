#!/usr/bin/env python3
"""Cron entry point: refresh the format tables from Showdown.

    # build the format tables from the formats-data.js in this folder
    python update_formats.py

    # check the folder's copy against live Showdown — exit 1 if Smogon moved
    python update_formats.py --check --source upstream

    # refresh the local copy, then rebuild
    python update_formats.py --source upstream

Exit codes are the interesting part, because cron needs them:

    0   ran, nothing changed (or applied successfully)
    1   --check found drift, nothing written
    2   could not fetch or parse

Suggested crontab — Monday morning, mail on drift, write nothing:

    0 9 * * 1 cd /path/to/draft && /usr/bin/python3 update_formats.py \
        --check --source upstream >> formats.log 2>&1 \
        || mail -s "Smogon tiers moved" you@example.com

Applying is deliberately a separate, manual run. A tier shift landing in the
middle of a season would move a Pokemon out of a format someone already
drafted from, so the decision to apply belongs to the commissioner.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draftmons.services import format_service as fmt          # noqa: E402
from draftmons import showdown_items as items_data  # noqa: E402
from draftmons import showdown_rules as rules       # noqa: E402
from draftmons.poke_db import init_db, transaction   # noqa: E402


def fetch_rules() -> None:
    """Re-download the TypeScript data files Showdown publishes.

    Four of them: the formats and their rulesets, which decide legality, and
    the item list with its descriptions, which the planner's item picker reads
    instead of PokeAPI's catalogue of bicycles.
    """
    for url, path in (
        (rules.UPSTREAM_FORMATS_TS, rules.FORMATS_TS),
        (rules.UPSTREAM_RULESETS_TS, rules.RULESETS_TS),
        (items_data.UPSTREAM_ITEMS_TS, items_data.ITEMS_TS),
        (items_data.UPSTREAM_ITEMS_TEXT_TS, items_data.ITEMS_TEXT_TS),
    ):
        text = fmt.read_source(url)
        pathlib.Path(path).write_text(text, encoding="utf-8")
        print(f"   fetched {pathlib.Path(path).name} ({len(text):,} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", default=fmt.DEFAULT_SOURCE,
        help="URL or local path; 'upstream' is shorthand for Showdown's live file "
             f"(default: {fmt.DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="report drift and exit 1; writes nothing to the format tables",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="rebuild even when the upstream hash is unchanged",
    )
    parser.add_argument(
        "--fetch-rules", action="store_true",
        help="re-download config/formats.ts and data/rulesets.ts first",
    )
    args = parser.parse_args()
    if args.source == "upstream":
        args.source = fmt.UPSTREAM_SOURCE

    init_db()

    if args.fetch_rules:
        print("Fetching rules:")
        try:
            fetch_rules()
        except fmt.FormatSourceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # The tier file may not have moved, but the rules did, so the format
        # tables have to be rewritten regardless of the hash check below.
        args.force = True

    try:
        if args.check:
            with transaction() as conn:
                raw = fmt.read_source(args.source)
                import hashlib
                digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
                current = fmt.current_snapshot(conn)

                print(f"local  {current or '(none built yet)'}")
                print(f"remote {digest}")
                if digest == current:
                    print("\nUp to date.")
                    return 0

                _, species = fmt.parse_tiers(raw)
                moves = fmt.diff_tiers(
                    fmt.stored_tiers(conn), fmt.species_for(species, "AG")
                )
                print(f"\nUpstream has changed. {len(moves)} species moved tier.")
                for move in moves[:40]:
                    print(f"   {move['species']:24} {move['from']:9} -> {move['to']}")
                if len(moves) > 40:
                    print(f"   ... and {len(moves) - 40} more")
                print("\nNothing written. To apply:")
                print(f"   python {Path(__file__).name} --source {args.source}")
                if args.source == fmt.UPSTREAM_SOURCE:
                    print("   (then save that file over formats-data.js to keep the "
                          "folder's copy in step)")
                return 1

        with transaction() as conn:
            report = fmt.refresh(conn, args.source, force=args.force)

        if not report["changed"]:
            print(f"Up to date ({report['source_sha256']}). Nothing written.")
            return 0

        print(f"Updated {report['formats']} formats to {report['source_sha256']}"
              f" (was {report['previous_sha256'] or 'nothing'}).")
        if report["rules_applied"]:
            print(f"Applied config/formats.ts: {report['species_banned']} species "
                  "banned beyond their tier.")
        else:
            print("WARNING: config/formats.ts not found — tier data only, so "
                  "format banlists and clauses are NOT applied.")
            print("   Fetch them with: python update_formats.py --fetch-rules")
        if report["moves"]:
            print(f"{len(report['moves'])} species moved tier:")
            for move in report["moves"][:40]:
                print(f"   {move['species']:24} {move['from']:9} -> {move['to']}")
        with transaction() as conn:
            for row in fmt.list_formats(conn):
                banned = row["species_banned"]
                extra = f"  (-{banned} by banlist)" if banned else ""
                print(f"   {row['key']:20} {row['species_count']:5} species{extra}")
        return 0

    except fmt.FormatSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
