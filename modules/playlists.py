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
# Energy classification
# ---------------------------------------------------------------------------
# Genres that always map to Peak regardless of BPM
_PEAK_GENRES: frozenset = frozenset({
    "afro tech", "techno", "hard techno", "industrial techno",
    "peak time techno", "rave",
})
# Genres that always map to Chill regardless of BPM
_CHILL_GENRES: frozenset = frozenset({
    "deep house", "organic house", "melodic house", "melodic techno",
    "downtempo", "ambient", "lo-fi", "nu-disco",
})
# BPM thresholds
_BPM_PEAK  = 126.0
_BPM_MID   = 118.0

# Target genres for combined playlists: (lowercase match key, display name)
_COMBINED_TARGET_GENRES: list = [
    ("afro house",  "Afro House"),
    ("amapiano",    "Amapiano"),
    ("deep house",  "Deep House"),
    ("afro tech",   "Afro Tech"),
]
_ENERGY_LEVELS = ("Peak", "Mid", "Chill")


def _classify_energy(bpm, genre: str) -> str:
    """
    Return the energy tier for a track: 'Peak', 'Mid', or 'Chill'.

    Genre classification takes priority over BPM so that e.g. Afro Tech is
    always Peak even when a specific track sits at a lower BPM than usual.
    Unknown BPM with no genre signal defaults to 'Mid'.
    """
    genre_l = (genre or "").strip().lower()

    for g in _PEAK_GENRES:
        if g in genre_l:
            return "Peak"
    for g in _CHILL_GENRES:
        if g in genre_l:
            return "Chill"

    try:
        bpm_val = float(bpm or 0)
    except (TypeError, ValueError):
        bpm_val = 0.0

    if bpm_val >= _BPM_PEAK:
        return "Peak"
    if bpm_val >= _BPM_MID:
        return "Mid"
    if bpm_val > 0:
        return "Chill"
    return "Mid"


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


def generate_energy_m3u(dry_run: bool = False) -> int:
    """
    Generate Peak / Mid / Chill M3U playlists from all 'ok' tracks.

    Classification is based on BPM and genre (see _classify_energy).
    Writes to ENERGY_M3U_DIR (<M3U_DIR>/Energy/).
    Returns total tracks written across all three energy playlists.
    """
    if not getattr(config, 'GENERATE_ENERGY_PLAYLISTS', True):
        log.info("Energy M3U: disabled via GENERATE_ENERGY_PLAYLISTS=False")
        return 0

    all_tracks = db.get_all_ok_tracks()
    if not all_tracks:
        log.info("Energy M3U: no tracks with status=ok in DB")
        return 0

    by_energy: dict = {level: [] for level in _ENERGY_LEVELS}
    for row in all_tracks:
        level = _classify_energy(row["bpm"], row["genre"])
        by_energy[level].append(row)

    total = 0
    written_playlists = 0
    for level in _ENERGY_LEVELS:
        tracks = by_energy[level]
        if not tracks:
            continue
        playlist_path = config.ENERGY_M3U_DIR / f"{level}.m3u8"
        n = _write_m3u(playlist_path, tracks, dry_run)
        total += n
        written_playlists += 1
        log.debug("Energy M3U '%s': %d tracks", level, n)

    log.info("Energy M3U: wrote %d energy playlists (%d tracks)", written_playlists, total)
    log_action(f"PLAYLIST: {written_playlists} energy M3U playlists (Peak/Mid/Chill) ({total} tracks)")
    return total


def generate_combined_m3u(dry_run: bool = False) -> int:
    """
    Generate combined genre+energy M3U playlists for the four target genres
    (Afro House, Amapiano, Deep House, Afro Tech) × three energy tiers.

    Only playlists that contain at least one track are written.
    Writes to COMBINED_M3U_DIR (<M3U_DIR>/Combined/).
    Returns total tracks written across all combined playlists.
    """
    if not getattr(config, 'GENERATE_COMBINED_PLAYLISTS', True):
        log.info("Combined M3U: disabled via GENERATE_COMBINED_PLAYLISTS=False")
        return 0

    all_tracks = db.get_all_ok_tracks()
    if not all_tracks:
        log.info("Combined M3U: no tracks with status=ok in DB")
        return 0

    # Build index: (genre_display, energy) → [rows]
    combined: dict = {}
    for row in all_tracks:
        norm_g = normalize_genre(row["genre"]).lower()
        energy = _classify_energy(row["bpm"], row["genre"])
        for genre_key, genre_display in _COMBINED_TARGET_GENRES:
            if norm_g == genre_key or norm_g.startswith(genre_key):
                combined.setdefault((genre_display, energy), []).append(row)
                break  # each track belongs to at most one target genre

    total = 0
    written_playlists = 0
    for (genre_display, energy), tracks in sorted(combined.items()):
        if not tracks:
            continue
        name      = f"{energy} {genre_display}"
        safe_name = _genre_filename(name)
        playlist_path = config.COMBINED_M3U_DIR / f"{safe_name}.m3u8"
        n = _write_m3u(playlist_path, tracks, dry_run)
        total += n
        written_playlists += 1
        log.debug("Combined M3U '%s': %d tracks", name, n)

    log.info("Combined M3U: wrote %d combined playlists (%d tracks)", written_playlists, total)
    log_action(f"PLAYLIST: {written_playlists} combined genre+energy M3U playlists ({total} tracks)")
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

    Playlist hierarchy:
      ROOT
        ├── All Tracks
        ├── A … Z  (letter folders)
        ├── Genre/  (one sub-node per genre)
        ├── Energy/ (Peak / Mid / Chill sub-nodes)
        └── Combined/ (genre+energy sub-nodes, e.g. "Peak Afro House")

    Returns the path of the written XML file.
    """
    all_tracks = db.get_all_ok_tracks()
    output_path = config.XML_DIR / "rekordbox_library.xml"

    if dry_run:
        log.info("DRY-RUN: would write Rekordbox XML with %d tracks", len(all_tracks))
        return output_path

    config.XML_DIR.mkdir(parents=True, exist_ok=True)

    track_entries:  List[str] = []
    playlist_nodes: dict = {}   # letter            → [TrackID, ...]
    genre_nodes:    dict = {}   # genre display name → [TrackID, ...]
    energy_nodes:   dict = {}   # energy tier        → [TrackID, ...]
    combined_nodes: dict = {}   # (genre_display, energy) → [TrackID, ...]
    track_id = 1

    for row in all_tracks:
        linux_path = row["filepath"]
        win_url    = _xml_escape(_linux_to_windows_url(linux_path))
        name       = _xml_escape(row["title"]  or Path(linux_path).stem)
        artist     = _xml_escape(row["artist"] or "")
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
        label      = _xml_escape(_read_label_from_file(linux_path))

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
            f' Label="{label}"'
            f' Mix="">'
            f'</TRACK>'
        )

        # Letter grouping
        try:
            rel    = Path(linux_path).relative_to(config.SORTED)
            letter = rel.parts[0] if rel.parts else "#"
        except ValueError:
            letter = "#"
        playlist_nodes.setdefault(letter, []).append(track_id)

        # Genre grouping
        if norm_genre:
            genre_nodes.setdefault(norm_genre, []).append(track_id)

        # Energy grouping
        energy = _classify_energy(row["bpm"], raw_genre)
        energy_nodes.setdefault(energy, []).append(track_id)

        # Combined genre+energy grouping (target genres only)
        norm_genre_l = norm_genre.lower()
        for genre_key, genre_display in _COMBINED_TARGET_GENRES:
            if norm_genre_l == genre_key or norm_genre_l.startswith(genre_key):
                combined_nodes.setdefault((genre_display, energy), []).append(track_id)
                break

        track_id += 1

    collection_count = track_id - 1

    # --- Helper: build a leaf (Type=1) playlist node ---
    def _leaf_node(name: str, tids: list, indent: str) -> str:
        refs = "\n".join(f'{indent}    <TRACK Key="{t}"/>' for t in tids)
        return (
            f'{indent}<NODE Name="{_xml_escape(name)}" Type="1"'
            f' KeyType="0" Entries="{len(tids)}">\n'
            f'{refs}\n'
            f'{indent}</NODE>'
        )

    # --- Letter playlist nodes ---
    playlist_xml_parts: List[str] = [
        _leaf_node(letter, tids, "            ")
        for letter, tids in sorted(playlist_nodes.items())
    ]

    # --- All-tracks playlist node ---
    all_refs = "\n".join(
        f'                <TRACK Key="{tid}"/>' for tid in range(1, track_id)
    )
    all_tracks_node = (
        f'            <NODE Name="All Tracks" Type="1" KeyType="0"'
        f' Entries="{collection_count}">\n'
        f'{all_refs}\n'
        f'            </NODE>'
    )

    # --- Genre folder node ---
    genre_sub_parts: List[str] = [
        _leaf_node(gname, tids, "                ")
        for gname, tids in sorted(genre_nodes.items())
    ]
    genre_folder_node = (
        f'            <NODE Type="0" Name="Genre" Count="{len(genre_sub_parts)}">\n'
        + "\n".join(genre_sub_parts) + "\n"
        + '            </NODE>'
    )

    # --- Energy folder node ---
    energy_sub_parts: List[str] = [
        _leaf_node(level, energy_nodes[level], "                ")
        for level in _ENERGY_LEVELS
        if energy_nodes.get(level)
    ]
    energy_folder_node = (
        f'            <NODE Type="0" Name="Energy" Count="{len(energy_sub_parts)}">\n'
        + "\n".join(energy_sub_parts) + "\n"
        + '            </NODE>'
    ) if energy_sub_parts else ""

    # --- Combined folder node ---
    combined_sub_parts: List[str] = [
        _leaf_node(f"{energy} {genre_display}", tids, "                ")
        for (genre_display, energy), tids in sorted(combined_nodes.items())
        if tids
    ]
    combined_folder_node = (
        f'            <NODE Type="0" Name="Combined" Count="{len(combined_sub_parts)}">\n'
        + "\n".join(combined_sub_parts) + "\n"
        + '            </NODE>'
    ) if combined_sub_parts else ""

    # --- Assemble folder nodes list for ROOT ---
    extra_folder_nodes = [n for n in [energy_folder_node, combined_folder_node] if n]
    root_count = 1 + len(playlist_nodes) + 1 + len(extra_folder_nodes)

    playlist_section_parts = (
        [all_tracks_node]
        + playlist_xml_parts
        + [genre_folder_node]
        + extra_folder_nodes
    )

    xml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<DJ_PLAYLISTS Version="1.0.0">\n'
        '    <PRODUCT Name="rekordbox" Version="6.0.0" Company="Pioneer DJ"/>\n'
        f'    <COLLECTION Entries="{collection_count}">\n'
        + "\n".join(track_entries) + "\n"
        + '    </COLLECTION>\n'
        + '    <PLAYLISTS>\n'
        + f'        <NODE Type="0" Name="ROOT" Count="{root_count}">\n'
        + "\n".join(playlist_section_parts) + "\n"
        + '        </NODE>\n'
        + '    </PLAYLISTS>\n'
        + '</DJ_PLAYLISTS>\n'
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    extra_counts = (
        f", {len(energy_sub_parts)} energy"
        + (f", {len(combined_sub_parts)} combined" if combined_sub_parts else "")
    )
    log.info("Rekordbox XML: %d tracks → %s (%d genre%s playlists)",
             collection_count, output_path, len(genre_nodes), extra_counts)
    log_action(
        f"XML: Rekordbox XML written — {collection_count} tracks, "
        f"{len(genre_nodes)} genre, {len(energy_sub_parts)} energy, "
        f"{len(combined_sub_parts)} combined [{output_path.name}]"
    )
    return output_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _read_label_from_file(path: str) -> str:
    """
    Read the organization/TPUB tag from an audio file and return it as a
    clean label string.

    Returns empty string on any failure or if the tag looks like a URL /
    domain watermark (those should have been cleared by the sanitizer, but
    this provides a safety net for the XML export).
    """
    try:
        from mutagen import File as MFile
        audio = MFile(path, easy=True)
        if audio is None:
            return ""
        vals = audio.get("organization")
        if not vals:
            return ""
        label = str(vals[0]).strip()
        # Inline junk filter: reject anything that looks like a URL or domain
        if not label:
            return ""
        if re.search(r'https?://|www\.|\.(?:com|net|org|fm|dj|co|io)\b',
                     label, re.IGNORECASE):
            return ""
        return label
    except Exception:
        return ""


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
    """
    Generate all playlists and Rekordbox XML. Returns files unchanged.

    Outputs:
      M3U_DIR/           letter playlists (A.m3u8 … Z.m3u8) + _all_tracks.m3u8
      M3U_DIR/Genre/     per-genre playlists
      M3U_DIR/Energy/    Peak.m3u8, Mid.m3u8, Chill.m3u8
      M3U_DIR/Combined/  Peak Afro House.m3u8, Chill Deep House.m3u8, etc.
      XML_DIR/           rekordbox_library.xml  (all playlists in one file)
    """
    generate_m3u(dry_run)
    generate_genre_m3u(dry_run)
    generate_energy_m3u(dry_run)
    generate_combined_m3u(dry_run)
    generate_rekordbox_xml(dry_run)
    return files
