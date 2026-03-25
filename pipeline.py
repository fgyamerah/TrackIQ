#!/usr/bin/env python3
"""
DJ Toolkit — main pipeline entry point.

Usage:
    python3 pipeline.py [--dry-run] [--skip-beets] [--skip-analysis]
    python3 pipeline.py label-intel [--label-seeds PATH] [--label-output DIR]

Steps (in order):
    1. Init dirs + DB
    2. Collect inbox files
    3. QC check (ffprobe)
    4. Duplicate detection (rmlint)
    5. Organize (beets → fallback Python)
    6. BPM + key analysis (aubio + keyfinder-cli)
    7. Tag writing (mutagen)
    8. Mark tracks OK in DB
    9. Playlist generation (M3U + Rekordbox XML)
   10. Report

All steps are idempotent. Already-processed tracks (TXXX:PROCESSED=1 in tags
and status='ok' in DB) are skipped on subsequent runs.

Label Intelligence subcommand:
    python3 pipeline.py label-intel
        Scrape Beatport/Traxsource for every label in the seeds file and
        export results to JSON, CSV, TXT, and SQLite under the output dir.
        Seeds default: $DJ_MUSIC_ROOT/data/labels/seeds.txt
        Output default: $DJ_MUSIC_ROOT/data/labels/output/
        Cache default:  $DJ_MUSIC_ROOT/.cache/label_intel/
"""
import argparse
import logging
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: make sure the djtoolkit directory is on the path
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

import config
import db
from modules import qc, dedupe, organizer, sanitizer, analyzer, tagger, playlists, reporter
from modules.textlog import log_action, log_run_separator


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)
    # Also write to file
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(config.LOGS_DIR / "pipeline.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    logging.getLogger().addHandler(fh)


log = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# Directory initialization
# ---------------------------------------------------------------------------
def _init_dirs() -> None:
    for d in [
        config.INBOX,
        config.PROCESSING,
        config.SORTED,
        config.UNSORTED,
        config.COMPILATIONS,
        config.DUPLICATES,
        config.REJECTED,
        config.M3U_DIR,
        config.GENRE_M3U_DIR,
        config.XML_DIR,
        config.LOGS_DIR,
        config.REPORTS_DIR,
    ]:
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------
def _collect_library_for_reanalysis() -> list:
    """Return all audio files in SORTED that are missing BPM or key."""
    files = []
    for ext in config.AUDIO_EXTENSIONS:
        files.extend(config.SORTED.rglob(f"*{ext}"))
        files.extend(config.SORTED.rglob(f"*{ext.upper()}"))
    seen = set()
    result = []
    for f in sorted(files):
        if str(f) in seen:
            continue
        seen.add(str(f))
        row = db.get_track(str(f))
        # Include if missing BPM, key, or not yet processed
        if row is None or row["bpm"] is None or row["key_camelot"] is None:
            if row is None:
                db.upsert_track(str(f), status="pending")
            result.append(f)
    return result


def _collect_inbox() -> list:
    """Return all audio files in INBOX (recursive). Skip already-processed."""
    files = []
    for ext in config.AUDIO_EXTENSIONS:
        files.extend(config.INBOX.rglob(f"*{ext}"))
        files.extend(config.INBOX.rglob(f"*{ext.upper()}"))
    # Deduplicate (rglob can match same file twice on case-insensitive FS)
    seen = set()
    unique = []
    for f in sorted(files):
        if str(f) not in seen:
            seen.add(str(f))
            unique.append(f)
    return unique


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run_pipeline(dry_run: bool, skip_beets: bool, skip_analysis: bool, verbose: bool, reanalyze: bool = False) -> int:
    """
    Execute the full pipeline.
    Returns exit code: 0 = success, 1 = some files failed, 2 = fatal error.
    """
    t_start = time.monotonic()

    _setup_logging(verbose)
    _init_dirs()
    db.init_db()

    run_id = db.start_run(dry_run)
    log.info("Pipeline start (run_id=%d, dry_run=%s)", run_id, dry_run)
    log_run_separator(f"run_id={run_id}" + (" DRY-RUN" if dry_run else ""))

    # --- Step 1: Collect files ---
    if reanalyze:
        # Re-analyze all tracks in sorted library that are missing BPM or key
        inbox_files = _collect_library_for_reanalysis()
        if not inbox_files:
            log.info("No tracks need re-analysis")
            db.finish_run(run_id, inbox_count=0, processed=0, duration_sec=0.0)
            return 0
        log.info("Re-analysis mode: %d tracks to process", len(inbox_files))
    else:
        inbox_files = _collect_inbox()
        if not inbox_files:
            log.info("Inbox is empty — nothing to process")
            db.finish_run(run_id, inbox_count=0, processed=0, duration_sec=0.0)
            return 0

    log.info("Inbox: %d files found", len(inbox_files))
    db.finish_run(run_id, inbox_count=len(inbox_files))

    # Register all inbox files in DB as 'pending' and log each one
    for f in inbox_files:
        if not db.is_processed(str(f)):
            db.upsert_track(str(f), status="pending")
        log_action(f"PROCESS: {f.name}")

    # --- Step 2: QC ---
    log.info("[1/7] Quality control ...")
    files = qc.run(inbox_files, run_id, dry_run)
    rejected_count = len(inbox_files) - len(files)

    # --- Step 3: Deduplicate ---
    log.info("[2/7] Duplicate detection ...")
    files = dedupe.run(files, run_id, dry_run)
    dupe_count = (len(inbox_files) - rejected_count) - len(files)

    # --- Step 4: Organize ---
    log.info("[3/7] Organizing library ...")
    files = organizer.run(files, run_id, dry_run, use_beets=not skip_beets)

    # --- Label enrichment (optional post-pipeline step) ---
    # Run separately:  python pipeline.py --label-enrich-from-library

    # --- Step 5: Sanitize tags ---
    log.info("[4/7] Sanitizing tags ...")
    files = sanitizer.run(files, run_id, dry_run)

    # --- Step 6: BPM + key analysis ---
    if not skip_analysis:
        log.info("[5/8] BPM + key analysis ...")
        files = analyzer.run(files, run_id, dry_run)
    else:
        log.info("[5/8] Skipping analysis (--skip-analysis)")

    # --- Step 7: Write tags ---
    log.info("[6/8] Writing tags ...")
    files = tagger.run(files, run_id, dry_run)

    # --- Step 8: Mark as OK in DB ---
    processed_count = 0
    error_count     = 0
    for f in files:
        row = db.get_track(str(f))
        if row and row["status"] not in ("rejected", "duplicate", "needs_review"):
            db.mark_status(str(f), "ok")
            processed_count += 1
        elif row and row["status"] == "error":
            error_count += 1

    # --- Step 8: Playlist generation ---
    log.info("[7/8] Generating playlists ...")
    playlists.run(files, run_id, dry_run)

    # --- Step 9: Report ---
    t_end       = time.monotonic()
    duration    = t_end - t_start
    unsorted    = db.get_tracks_by_status("needs_review")

    db.finish_run(
        run_id,
        inbox_count=len(inbox_files),
        processed=processed_count,
        rejected=rejected_count,
        duplicates=dupe_count,
        unsorted=len(unsorted),
        errors=error_count,
        duration_sec=duration,
    )

    log.info("[8/8] Writing report ...")
    report_path = reporter.generate(run_id, duration, dry_run)
    reporter.generate_readme(run_id, duration, dry_run)
    reporter.print_summary(run_id, duration)
    log.info("Report: %s", report_path)
    log_action(f"RUN COMPLETE: run_id={run_id}, processed={processed_count}, errors={error_count}, duration={duration:.1f}s")

    return 0 if error_count == 0 else 1


# ---------------------------------------------------------------------------
# Label Intelligence
# ---------------------------------------------------------------------------
def run_label_intel(args) -> int:
    """Scrape label metadata and export to all formats."""
    _setup_logging(getattr(args, "verbose", False))

    seeds_path  = Path(args.label_seeds)
    output_dir  = Path(args.label_output)
    cache_dir   = Path(args.label_cache)
    sources     = args.label_sources
    delay       = float(args.label_delay)
    skip_enrich = args.label_skip_enrich

    if not seeds_path.exists():
        log.error("Seeds file not found: %s", seeds_path)
        log.error(
            "Create it with one label name per line, for example:\n"
            "  MoBlack Records\n"
            "  Defected Records\n"
            "  Drumcode"
        )
        return 2

    try:
        from label_intel.scraper import scrape_labels
        from label_intel import exporters
    except ImportError as exc:
        log.error("label_intel package not found (%s). "
                  "Ensure label_intel/ is at the project root.", exc)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    log_action("LABEL-INTEL START")
    log.info("Seeds:   %s", seeds_path)
    log.info("Output:  %s", output_dir)
    log.info("Cache:   %s", cache_dir)
    log.info("Sources: %s  |  delay: %.1fs  |  skip_enrich: %s",
             sources, delay, skip_enrich)

    store = scrape_labels(
        seed_path=seeds_path,
        cache_dir=cache_dir,
        source_names=sources,
        delay=delay,
        skip_enrich=skip_enrich,
    )

    records = store.values()
    log.info("Scraped %d label record(s)", len(records))

    exporters.export_json(records,   output_dir / "labels.json")
    exporters.export_csv(records,    output_dir / "labels.csv")
    exporters.export_txt(records,    output_dir / "labels.txt")
    exporters.export_sqlite(records, output_dir / "labels.db")

    log.info("Exported to %s:", output_dir)
    log.info("  labels.json  — full metadata")
    log.info("  labels.csv   — spreadsheet-friendly")
    log.info("  labels.txt   — one name per line  "
             "(copy to known_labels.txt to update parser blocklist)")
    log.info("  labels.db    — SQLite for ad-hoc queries")
    log_action(f"LABEL-INTEL DONE: {len(records)} records → {output_dir}")
    return 0


# ---------------------------------------------------------------------------
# Label Enrichment from Library
# ---------------------------------------------------------------------------
def _collect_library_tracks_for_enrichment() -> list:
    """
    Return [{label, bpm, genre}] for every OK track in the library.

    Reads genre + bpm from the pipeline DB (already stored there after the
    analyze/tag steps) and recovers the record-label name from the audio
    file's 'organization' easy-tag (mutagen → TPUB for ID3, ORGANIZATION
    for Vorbis).  No BPM/key re-analysis is performed.
    """
    from mutagen import File as MFile

    rows   = db.get_all_ok_tracks()
    tracks = []
    for row in rows:
        fpath = row["filepath"]
        try:
            audio = MFile(fpath, easy=True)
            if audio is None:
                continue
            label = (audio.get("organization") or [""])[0].strip()
            if not label:
                continue
        except Exception:
            continue

        tracks.append({
            "label": label,
            "bpm":   row["bpm"],
            "genre": row["genre"] or "",
        })
    return tracks


def run_label_enrichment_from_library(verbose: bool = False) -> int:
    """
    Enrich the label database with real BPM/genre data from the local library.

    Loads labels.json (if it exists), merges in library metadata via
    enrich_store_from_tracks(), then overwrites labels.json / labels.csv /
    labels.db.  Only improves bpm_min/max, genres, subgenres, energy_profile
    and creates new label records for labels not seen before.
    """
    _setup_logging(verbose)

    try:
        from label_intel.enrich_from_library import enrich_store_from_tracks
        from label_intel.store import LabelStore
        from label_intel.models import LabelRecord
        from label_intel import exporters
        from label_intel.utils import normalize_label_name
    except ImportError as exc:
        log.error("label_intel package not found (%s). "
                  "Ensure label_intel/ is at the project root.", exc)
        return 2

    import json as _json
    import dataclasses

    db.init_db()
    output_dir = config.LABEL_INTEL_OUTPUT
    json_path  = output_dir / "labels.json"

    # --- Load existing store ---
    store = LabelStore()
    if json_path.exists():
        raw          = _json.loads(json_path.read_text(encoding="utf-8"))
        valid_fields = {f.name for f in dataclasses.fields(LabelRecord)}
        loaded       = 0
        for item in raw:
            try:
                rec = LabelRecord(**{k: v for k, v in item.items() if k in valid_fields})
                store.records[rec.normalized_name] = rec
                loaded += 1
            except Exception as exc:
                log.debug("Skipped malformed label record: %s", exc)
        log.info("Loaded %d existing label record(s) from %s", loaded, json_path)
    else:
        log.info("No labels.json found — starting with an empty store")

    # --- Collect tracks ---
    tracks = _collect_library_tracks_for_enrichment()
    log.info("Collected %d track(s) with label metadata from library", len(tracks))

    if not tracks:
        log.warning(
            "No labelled tracks found in the library database.\n"
            "Tip: run the full pipeline first so tracks are organised and "
            "their tags are stored (status='ok')."
        )
        return 0

    # --- Snapshot keys for summary counts ---
    before_keys  = set(store.records.keys())
    matched_keys = {
        normalize_label_name(t["label"]) for t in tracks if t.get("label")
    }
    n_will_enrich = len(before_keys & matched_keys)

    log_action("LABEL-ENRICH-LIBRARY START")
    enrich_store_from_tracks(store, tracks)

    after_keys = set(store.records.keys())
    n_new      = len(after_keys - before_keys)
    total      = len(store.records)

    # --- Re-export (TXT intentionally omitted here; use label-intel for a fresh scrape) ---
    output_dir.mkdir(parents=True, exist_ok=True)
    records = store.values()
    exporters.export_json(records,   output_dir / "labels.json")
    exporters.export_csv(records,    output_dir / "labels.csv")
    exporters.export_sqlite(records, output_dir / "labels.db")

    log.info("Label enrichment from library complete:")
    log.info("  %d new label(s) discovered from library", n_new)
    log.info("  %d existing label(s) enriched (bpm / genres / energy)", n_will_enrich)
    log.info("  %d total label(s) in database", total)
    log.info("  Exported to: %s", output_dir)
    log_action(
        f"LABEL-ENRICH-LIBRARY DONE: {n_new} new, {n_will_enrich} enriched → {output_dir}"
    )
    return 0


# ---------------------------------------------------------------------------
# Label Clean
# ---------------------------------------------------------------------------
def run_label_clean(args) -> int:
    """
    Detect, normalize, and (optionally) write back label metadata.

    Modes:
      default / --dry-run   scan + report, no file writes
      --write-tags          scan + report + write high-confidence labels
      --review-only         scan + export only unresolved / low-confidence cases
    """
    _setup_logging(getattr(args, "verbose", False))

    try:
        from label_intel.cleaner import (
            scan_tracks, write_label_tag, WRITE_THRESHOLD,
        )
        from label_intel.normalizer import AliasRegistry
        from label_intel import reports as _reports
    except ImportError as exc:
        log.error("label_intel package not found (%s). "
                  "Ensure label_intel/ is at the project root.", exc)
        return 2

    # Provider placeholder warnings
    if getattr(args, "use_discogs", False):
        log.warning("--use-discogs: Discogs provider is not yet implemented (Phase 2) — skipped.")
    if getattr(args, "use_beatport", False):
        log.warning("--use-beatport: Beatport clean provider is not yet implemented (Phase 2) — skipped.")

    db.init_db()

    rows  = db.get_all_ok_tracks()
    paths = [Path(row["filepath"]) for row in rows if Path(row["filepath"]).exists()]

    if not paths:
        log.warning(
            "No processed tracks found in the library database.\n"
            "Run the full pipeline first so tracks are organised (status='ok')."
        )
        return 0

    threshold   = getattr(args, "confidence_threshold", config.LABEL_CLEAN_THRESHOLD)
    do_write    = getattr(args, "write_tags", False) and not getattr(args, "dry_run", False)
    review_only = getattr(args, "review_only", False)
    output_dir  = config.LABEL_CLEAN_OUTPUT

    log.info("Scanning %d track(s) for label metadata ...", len(paths))
    log.info("Confidence threshold : %.2f   write-back: %s   review-only: %s",
             threshold, do_write, review_only)
    log_action("LABEL-CLEAN START")

    alias_registry = AliasRegistry()
    results = scan_tracks(paths, write_threshold=threshold, alias_registry=alias_registry)

    # --- Write-back ---
    written = 0
    if do_write:
        for r in results:
            if r.writable and r.cleaned_label:
                if write_label_tag(Path(r.filepath), r.cleaned_label):
                    r.action_taken = "written"
                    written += 1
                    log.info("WROTE label %r → %s", r.cleaned_label, Path(r.filepath).name)

    # --- Reports ---
    report_paths = _reports.generate_all(
        results, output_dir, written=written, review_only=review_only,
    )
    _reports.print_summary(results, written)

    log.info("Reports written to: %s", output_dir)
    for label, rpath in report_paths.items():
        log.info("  %-15s %s", label, rpath.name)

    alias_merges = alias_registry.alias_count()
    if alias_merges:
        log.info("Alias merges detected: %d label(s) have multiple spellings", alias_merges)

    log_action(
        f"LABEL-CLEAN DONE: {len(results)} scanned, {written} written, "
        f"{alias_merges} alias merges → {output_dir}"
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="DJ Toolkit — automated library preparation pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Label Clean (local, Phase 1):\n"
            "  python pipeline.py label-clean                   # scan + report, no writes\n"
            "  python pipeline.py label-clean --write-tags      # write high-confidence labels\n"
            "  python pipeline.py label-clean --review-only     # export unresolved only\n"
            "  python pipeline.py label-clean --confidence-threshold 0.75  # broader writes\n\n"
            "Label Intelligence (web scrape):\n"
            "  python pipeline.py label-intel\n"
            "  python pipeline.py label-intel --label-seeds /music/data/labels/seeds.txt\n\n"
            "Label Enrichment from Library:\n"
            "  python pipeline.py --label-enrich-from-library\n"
        ),
    )
    # ----- existing pipeline flags -----
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run all detection/analysis but make no file changes"
    )
    parser.add_argument(
        "--skip-beets", action="store_true",
        help="Skip beets import (use pure-Python organizer only)"
    )
    parser.add_argument(
        "--skip-analysis", action="store_true",
        help="Skip BPM and key detection (useful for re-tagging only)"
    )
    parser.add_argument(
        "--reanalyze", action="store_true",
        help="Re-run BPM+key analysis on sorted library tracks missing those values"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--label-enrich-from-library", action="store_true",
        help=(
            "Enrich the label database using BPM/genre data from your local library. "
            "Reads the label tag (TPUB/organization) from all OK tracks — no re-analysis. "
            "Example: python pipeline.py --label-enrich-from-library"
        ),
    )

    # ----- subcommands -----
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_li = subparsers.add_parser(
        "label-intel",
        help="Scrape and export label metadata from Beatport/Traxsource",
    )
    p_li.add_argument(
        "--label-seeds", metavar="FILE",
        default=config.LABEL_INTEL_SEEDS,
        help=f"Seeds file (one label name per line). Default: {config.LABEL_INTEL_SEEDS}",
    )
    p_li.add_argument(
        "--label-output", metavar="DIR",
        default=config.LABEL_INTEL_OUTPUT,
        help=f"Output directory for exported files. Default: {config.LABEL_INTEL_OUTPUT}",
    )
    p_li.add_argument(
        "--label-cache", metavar="DIR",
        default=config.LABEL_INTEL_CACHE,
        help=f"HTTP cache directory. Default: {config.LABEL_INTEL_CACHE}",
    )
    p_li.add_argument(
        "--label-sources", nargs="+", metavar="SOURCE",
        default=config.LABEL_INTEL_SOURCES,
        choices=["beatport", "traxsource"],
        help="Sources to scrape. Default: beatport traxsource",
    )
    p_li.add_argument(
        "--label-delay", type=float, metavar="SECS",
        default=config.LABEL_INTEL_DELAY,
        help=f"Per-host request delay in seconds. Default: {config.LABEL_INTEL_DELAY}",
    )
    p_li.add_argument(
        "--label-skip-enrich", action="store_true",
        help="Skip label page enrichment (faster; search results only)",
    )
    p_li.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    # ----- label-clean subcommand -----
    p_lc = subparsers.add_parser(
        "label-clean",
        help="Detect, normalize, and optionally write back label metadata (Phase 1: local only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Scan all processed tracks for label metadata.\n\n"
            "Detection order:\n"
            "  1. organization/TPUB embedded tag    (confidence 0.95)\n"
            "  2. grouping tag fallback             (confidence 0.75)\n"
            "  3. comment tag fallback              (confidence 0.60)\n"
            "  4. filename pattern parsing          (confidence 0.55-0.70)\n"
            "  5. unresolved                        (confidence 0.00)\n\n"
            "Write-back (--write-tags) only applies when confidence >= threshold (default 0.85).\n"
            "At the default threshold only embedded-tag results are written automatically.\n"
        ),
    )
    p_lc.add_argument(
        "--dry-run", action="store_true",
        help="Scan and report only — make no file changes (default behavior)",
    )
    p_lc.add_argument(
        "--write-tags", action="store_true",
        help=(
            f"Write high-confidence labels (>= {config.LABEL_CLEAN_THRESHOLD}) "
            "back to the organization/TPUB tag"
        ),
    )
    p_lc.add_argument(
        "--review-only", action="store_true",
        help="Only export the review file (unresolved / low-confidence tracks)",
    )
    p_lc.add_argument(
        "--confidence-threshold", type=float, metavar="FLOAT",
        default=config.LABEL_CLEAN_THRESHOLD,
        help=f"Minimum confidence for write-back. Default: {config.LABEL_CLEAN_THRESHOLD}",
    )
    p_lc.add_argument(
        "--use-discogs", action="store_true",
        help="[Phase 2 — not yet implemented] Match unresolved labels via Discogs API",
    )
    p_lc.add_argument(
        "--use-beatport", action="store_true",
        help="[Phase 2 — not yet implemented] Match unresolved labels via Beatport",
    )
    p_lc.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.command == "label-intel":
        sys.exit(run_label_intel(args))

    if args.command == "label-clean":
        sys.exit(run_label_clean(args))

    if args.label_enrich_from_library:
        sys.exit(run_label_enrichment_from_library(args.verbose))

    sys.exit(run_pipeline(
        dry_run=args.dry_run,
        skip_beets=args.skip_beets,
        skip_analysis=args.skip_analysis,
        verbose=args.verbose,
        reanalyze=args.reanalyze,
    ))


if __name__ == "__main__":
    main()
