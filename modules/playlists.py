"""
Playlist generator — writes M3U playlists and a Rekordbox XML import file.

M3U strategy:
    - One playlist per first-letter folder (A.m3u8, B.m3u8, etc.)
    - One "All Tracks" master playlist
    - Genre playlists under Genre/<GenreName>.m3u8
    - Paths are RELATIVE from the playlist file location — survives drive letter changes
    - UTF-8, .m3u8 extension (Rekordbox handles this fine)

Rekordbox XML strategy:
    - Paths in the XML use WINDOWS paths (file://localhost/E:/music/...)
    - This is the most reliable import method — carries BPM, key, genre, comments
    - One XML file covers the entire collection
    - Playlist nodes include letter folders AND a Genre folder with sub-nodes

The Windows path is built by substituting MUSIC_ROOT with WINDOWS_BASE_URL.
"""
import html
import logging
import os
import re
import sqlite3
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional

import config
import db
from modules.textlog import log_action

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Genre normalization
# ---------------------------------------------------------------------------
_RE_GENRE_SPLIT = re.compile(r'[,;/|]')
_RE_GENRE_HYPHEN = re.compile(r'[-_]+')
_RE_GENRE_SPACES = re.compile(r'\s+')
# Characters not safe in filenames (cross-platform conservative list)
_RE_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_genre(genre: Optional[str]) -> str:
    """
    Normalize a genre string for consistent playlist grouping.

    - Takes only the first value if multiple are separated by , ; / |
    - Replaces hyphens/underscores with spaces so "Afro-House" == "Afro House"
    - Lowercases for de-duplication, title-cases for display
    - Returns empty string for missing / blank / meaningless genres

    >>> normalize_genre("afro house")
    'Afro House'
    >>> normalize_genre("Afro-House")
    'Afro House'
    >>> normalize_genre("DEEP HOUSE, Tech House")
    'Deep House'
    >>> normalize_genre(None)
    ''
    """
    if not genre or not genre.strip():
        return ''
    # Take first segment only
    first = _RE_GENRE_SPLIT.split(genre.strip())[0].strip()
    if not first:
        return ''
    # Normalize separators and whitespace
    normalized = _RE_GENRE_HYPHEN.sub(' ', first)
    normalized = _RE_GENRE_SPACES.sub(' ', normalized).strip()
    # Title-case for display ("afro house" → "Afro House")
    return normalized.title()


def _genre_filename(genre_name: str) -> str:
    """Return a filesystem-safe filename for a genre (no extension)."""
    return _RE_UNSAFE_FILENAME.sub('', genre_name).strip() or '_Unknown'


# ---------------------------------------------------------------------------
# Path conversion helpers
# ---------------------------------------------------------------------------
def _linux_to_windows_url(linux_path: str) -> str:
    """
    Convert a Linux path like /music/library/sorted/A/Artist/file.mp3
    to a Rekordbox XML location like file://localhost/E:/music/library/sorted/A/Artist/file.mp3
    """
    rel = Path(linux_path).relative_to(config.MUSIC_ROOT)
    # PurePosixPath parts → join with / → prepend Windows base URL
    parts = list(rel.parts)
    win_rel = "/".join(parts)
    return f"{config.WINDOWS_BASE_URL}/{win_rel}"


def _relative_m3u_path(track_path: Path, playlist_path: Path) -> str:
    """Return track path relative to the playlist file's directory."""
    try:
        rel = os.path.relpath(str(track_path), start=str(playlist_path.parent))
        # Always use forward slashes in M3U (cross-platform convention)
        return rel.replace("\\", "/")
    except ValueError:
        # Different drives on Windows — fall back to absolute
        return str(track_path).replace("\\", "/")


# ---------------------------------------------------------------------------
# M3U generation
# ---------------------------------------------------------------------------
def _write_m3u(playlist_path: Path, tracks: List[sqlite3.Row], dry_run: bool) -> int:
    """Write a single .m3u8 file. Returns number of tracks written."""
    if not tracks:
        return 0
    if dry_run:
        log.info("DRY-RUN: would write %s (%d tracks)", playlist_path.name, len(tracks))
        return len(tracks)

    playlist_path.parent.mkdir(parents=True, exist_ok=True)
    with open(playlist_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for row in tracks:
            artist  = row["artist"] or "Unknown"
            title   = row["title"]  or Path(row["filepath"]).stem
            dur     = int(row["duration_sec"] or -1)
            rel     = _relative_m3u_path(Path(row["filepath"]), playlist_path)
            f.write(f"#EXTINF:{dur},{artist} - {title}\n")
            f.write(f"{rel}\n")
    return len(tracks)


def generate_m3u(dry_run: bool = False) -> int:
    """
    Generate per-letter and master M3U playlists from all 'ok' tracks.
    Returns total number of tracks written.
    """
    all_tracks = db.get_all_ok_tracks()
    if not all_tracks:
        log.info("M3U: no tracks with status=ok in DB")
        return 0

    # Group by first letter
    by_letter: dict = {}
    for row in all_tracks:
        path   = Path(row["filepath"])
        # Determine letter from the parent folder structure
        try:
            rel = path.relative_to(config.SORTED)
            letter = rel.parts[0] if rel.parts else "#"
        except ValueError:
            letter = "#"
        by_letter.setdefault(letter, []).append(row)

    total = 0
    for letter, tracks in sorted(by_letter.items()):
        playlist_path = config.M3U_DIR / f"{letter}.m3u8"
        n = _write_m3u(playlist_path, tracks, dry_run)
        total += n
        log.debug("M3U %s: %d tracks", letter, n)

    # Master "All Tracks" playlist
    master_path = config.M3U_DIR / "_all_tracks.m3u8"
    _write_m3u(master_path, list(all_tracks), dry_run)
    log.info("M3U: wrote %d letter playlists + master (%d tracks)", len(by_letter), total)
    log_action(f"PLAYLIST: {len(by_letter)} letter M3U playlists + master ({total} tracks)")
    return total


def generate_genre_m3u(dry_run: bool = False) -> int:
    """
    Generate per-genre M3U playlists from all 'ok' tracks.

    Uses only the first genre value per track and normalizes the genre string
    so "Afro-House", "afro house" and "AFRO HOUSE" all map to "Afro House".
    Writes to GENRE_M3U_DIR (<M3U_DIR>/Genre/).
    Returns total number of tracks written across all genre files.
    """
    all_tracks = db.get_all_ok_tracks()
    if not all_tracks:
        log.info("Genre M3U: no tracks with status=ok in DB")
        return 0

    # Group tracks by normalized genre
    by_genre: dict = {}
    for row in all_tracks:
        genre = normalize_genre(row["genre"])
        if not genre:
            genre = "_Unknown Genre"
        by_genre.setdefault(genre, []).append(row)

    total = 0
    for genre_name, tracks in sorted(by_genre.items()):
        safe = _genre_filename(genre_name)
        playlist_path = config.GENRE_M3U_DIR / f"{safe}.m3u8"
        n = _write_m3u(playlist_path, tracks, dry_run)
        total += n
        log.debug("Genre M3U '%s': %d tracks", genre_name, n)

    log.info("Genre M3U: wrote %d genre playlists (%d tracks)", len(by_genre), total)
    log_action(f"PLAYLIST: {len(by_genre)} genre M3U playlists ({total} tracks)")
    return total


# ---------------------------------------------------------------------------
# Rekordbox XML generation
# ---------------------------------------------------------------------------
def _xml_escape(s: str) -> str:
    return html.escape(str(s or ""), quote=True)


def _format_bpm(bpm) -> str:
    if bpm is None:
        return "0.00"
    try:
        return f"{float(bpm):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _total_time(dur) -> str:
    try:
        return str(int(float(dur or 0)))
    except (TypeError, ValueError):
        return "0"


def _added_date() -> str:
    return date.today().isoformat()


def generate_rekordbox_xml(dry_run: bool = False) -> Path:
    """
    Generate a Rekordbox-importable XML file from all 'ok' tracks.
    Returns the path of the written XML file.
    """
    all_tracks = db.get_all_ok_tracks()
    output_path = config.XML_DIR / "rekordbox_library.xml"

    if dry_run:
        log.info("DRY-RUN: would write Rekordbox XML with %d tracks", len(all_tracks))
        return output_path

    config.XML_DIR.mkdir(parents=True, exist_ok=True)

    # Build track entries; collect groupings for letter and genre playlist nodes
    track_entries: List[str] = []
    playlist_nodes: dict = {}   # letter  → [TrackID, ...]
    genre_nodes:    dict = {}   # genre   → [TrackID, ...]
    track_id = 1

    for row in all_tracks:
        linux_path = row["filepath"]
        win_url    = _xml_escape(_linux_to_windows_url(linux_path))
        name       = _xml_escape(row["title"]  or Path(linux_path).stem)
        artist     = _xml_escape(row["artist"] or "")
        # Use normalized genre for display so XML matches M3U genre names
        raw_genre  = row["genre"] or ""
        norm_genre = normalize_genre(raw_genre)
        genre_attr = _xml_escape(norm_genre or raw_genre)
        bpm        = _format_bpm(row["bpm"])
        key        = _xml_escape(row["key_camelot"] or "")
        comment    = _xml_escape(_build_comment(row))
        total_time = _total_time(row["duration_sec"])
        bitrate    = str(row["bitrate_kbps"] or 0)
        kind       = _kind_from_path(linux_path)
        size       = str(row["filesize_bytes"] or 0)

        track_entries.append(
            f'        <TRACK TrackID="{track_id}"'
            f' Name="{name}"'
            f' Artist="{artist}"'
            f' Composer=""'
            f' Album=""'
            f' Grouping=""'
            f' Genre="{genre_attr}"'
            f' Kind="{kind}"'
            f' Size="{size}"'
            f' TotalTime="{total_time}"'
            f' DiscNumber="0"'
            f' TrackNumber="0"'
            f' Year=""'
            f' AverageBpm="{bpm}"'
            f' DateAdded="{_added_date()}"'
            f' BitRate="{bitrate}"'
            f' SampleRate="44100"'
            f' Comments="{comment}"'
            f' PlayCount="0"'
            f' Rating="0"'
            f' Location="{win_url}"'
            f' Remixer=""'
            f' Tonality="{key}"'
            f' Label=""'
            f' Mix="">'
            f'</TRACK>'
        )

        # Group by first letter for letter playlists
        try:
            rel    = Path(linux_path).relative_to(config.SORTED)
            letter = rel.parts[0] if rel.parts else "#"
        except ValueError:
            letter = "#"
        playlist_nodes.setdefault(letter, []).append(track_id)

        # Group by normalized genre for genre playlists
        if norm_genre:
            genre_nodes.setdefault(norm_genre, []).append(track_id)

        track_id += 1

    collection_count = track_id - 1

    # --- Letter playlist XML nodes ---
    playlist_xml_parts: List[str] = []
    for letter, tids in sorted(playlist_nodes.items()):
        track_refs = "\n".join(
            f'                    <TRACK Key="{tid}"/>' for tid in tids
        )
        playlist_xml_parts.append(
            f'            <NODE Name="{_xml_escape(letter)}" Type="1" KeyType="0" Entries="{len(tids)}">\n'
            f'{track_refs}\n'
            f'            </NODE>'
        )

    # --- All-tracks playlist node ---
    all_refs = "\n".join(
        f'                <TRACK Key="{tid}"/>' for tid in range(1, track_id)
    )
    all_tracks_node = (
        f'            <NODE Name="All Tracks" Type="1" KeyType="0" Entries="{collection_count}">\n'
        f'{all_refs}\n'
        f'            </NODE>'
    )

    # --- Genre folder node (nested sub-nodes, one per genre) ---
    genre_sub_parts: List[str] = []
    for genre_name, tids in sorted(genre_nodes.items()):
        refs = "\n".join(
            f'                    <TRACK Key="{tid}"/>' for tid in tids
        )
        genre_sub_parts.append(
            f'                <NODE Name="{_xml_escape(genre_name)}" Type="1" KeyType="0" Entries="{len(tids)}">\n'
            f'{refs}\n'
            f'                </NODE>'
        )
    genre_folder_node = (
        f'            <NODE Type="0" Name="Genre" Count="{len(genre_nodes)}">\n'
        + "\n".join(genre_sub_parts) + "\n"
        + f'            </NODE>'
    )

    # ROOT count = All Tracks node + letter nodes + Genre folder node
    root_count = 1 + len(playlist_nodes) + 1

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
    <PRODUCT Name="rekordbox" Version="6.0.0" Company="Pioneer DJ"/>
    <COLLECTION Entries="{collection_count}">
{chr(10).join(track_entries)}
    </COLLECTION>
    <PLAYLISTS>
        <NODE Type="0" Name="ROOT" Count="{root_count}">
{all_tracks_node}
{chr(10).join(playlist_xml_parts)}
{genre_folder_node}
        </NODE>
    </PLAYLISTS>
</DJ_PLAYLISTS>
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    log.info("Rekordbox XML: %d tracks written to %s", collection_count, output_path)
    log_action(f"XML: Rekordbox XML written — {collection_count} tracks, {len(genre_nodes)} genre playlists [{output_path.name}]")
    return output_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_comment(row: sqlite3.Row) -> str:
    parts = []
    if row["key_camelot"]:
        parts.append(row["key_camelot"])
    if row["key_musical"]:
        parts.append(row["key_musical"])
    if row["bpm"]:
        parts.append(f"{int(round(float(row['bpm'])))} BPM")
    return " | ".join(parts)


def _kind_from_path(path: str) -> str:
    ext = Path(path).suffix.lower()
    kinds = {
        ".mp3":  "MP3 File",
        ".flac": "FLAC File",
        ".wav":  "WAV File",
        ".aiff": "AIFF File",
        ".aif":  "AIFF File",
        ".m4a":  "M4A File",
        ".ogg":  "OGG File",
        ".opus": "OGG File",
    }
    return kinds.get(ext, "Audio File")


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------
def run(files: List[Path], run_id: int, dry_run: bool = False) -> List[Path]:
    """Generate M3U playlists (letter + genre) and Rekordbox XML. Returns files unchanged."""
    generate_m3u(dry_run)
    generate_genre_m3u(dry_run)
    generate_rekordbox_xml(dry_run)
    return files
