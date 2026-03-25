"""
Central configuration for the DJ Toolkit pipeline.
Override any value by creating config_local.py in this directory.
"""
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Root paths
# ---------------------------------------------------------------------------
MUSIC_ROOT   = Path(os.environ.get("DJ_MUSIC_ROOT", "/music"))
INBOX        = MUSIC_ROOT / "inbox"
PROCESSING   = MUSIC_ROOT / "processing"
LIBRARY      = MUSIC_ROOT / "library"
SORTED       = LIBRARY / "sorted"
UNSORTED     = SORTED / "_unsorted"
COMPILATIONS = SORTED / "_compilations"

# ---------------------------------------------------------------------------
# Special-purpose route directories
# Files matching route patterns are organised here instead of SORTED.
# ---------------------------------------------------------------------------
ACAPELLA     = LIBRARY / "acapella"
INSTRUMENTAL = LIBRARY / "instrumental"
DJ_TOOLS     = LIBRARY / "dj_tools"
EDITS        = LIBRARY / "edits"
BOOTLEGS     = LIBRARY / "bootlegs"
LIVE         = LIBRARY / "live"
UNKNOWN_ROUTE = LIBRARY / "unknown"   # for tracks with too little metadata

DUPLICATES   = MUSIC_ROOT / "duplicates"
REJECTED     = MUSIC_ROOT / "rejected"
PLAYLISTS    = MUSIC_ROOT / "playlists"
M3U_DIR      = PLAYLISTS / "m3u"
GENRE_M3U_DIR = M3U_DIR / "Genre"   # genre-based M3U playlists go here
XML_DIR      = PLAYLISTS / "xml"
LOGS_DIR         = MUSIC_ROOT / "logs"
DB_PATH          = LOGS_DIR / "processed.db"
REPORTS_DIR      = LOGS_DIR / "reports"
BEETS_LOG        = LOGS_DIR / "beets_import.log"
TEXT_LOG_PATH    = LOGS_DIR / "processing_log.txt"   # human-readable append-only run log
README_PATH      = LOGS_DIR / "README.md"             # auto-generated, overwritten each run

# ---------------------------------------------------------------------------
# Windows transfer — used when generating Rekordbox XML
# Set WINDOWS_DRIVE_LETTER to whatever drive letter you always assign to your
# external SSD/HDD on Windows (fix it in Windows Disk Management).
# ---------------------------------------------------------------------------
WINDOWS_DRIVE_LETTER = os.environ.get("DJ_WIN_DRIVE", "E")
WINDOWS_MUSIC_ROOT   = f"{WINDOWS_DRIVE_LETTER}:\\music"
# Rekordbox XML location attribute format
WINDOWS_BASE_URL     = f"file://localhost/{WINDOWS_DRIVE_LETTER}:/music"

# ---------------------------------------------------------------------------
# Quality thresholds
# ---------------------------------------------------------------------------
MIN_BITRATE_KBPS = 128       # reject files below this bitrate
MIN_DURATION_SEC = 30        # reject files shorter than this
MAX_DURATION_SEC = 7200      # reject files longer than 2 hours (likely mixes/wrong)

# ---------------------------------------------------------------------------
# Audio extensions to process
# ---------------------------------------------------------------------------
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".ogg", ".opus"}

# ---------------------------------------------------------------------------
# BPM sanity bounds (genre-aware halving/doubling happens in analyzer.py)
# ---------------------------------------------------------------------------
BPM_MIN = 60
BPM_MAX = 200

# ---------------------------------------------------------------------------
# ID3 settings
# ---------------------------------------------------------------------------
ID3_VERSION  = 3      # ID3v2.3 — best Rekordbox compatibility
ARTWORK_SIZE = 500    # px square, JPEG

# ---------------------------------------------------------------------------
# Label Intelligence
# ---------------------------------------------------------------------------
LABEL_INTEL_SEEDS   = MUSIC_ROOT / "data" / "labels" / "seeds.txt"
LABEL_INTEL_OUTPUT  = MUSIC_ROOT / "data" / "labels" / "output"
LABEL_INTEL_CACHE   = MUSIC_ROOT / ".cache" / "label_intel"
LABEL_INTEL_SOURCES = ["beatport", "traxsource"]
LABEL_INTEL_DELAY   = 2.0

# label-clean subcommand
LABEL_CLEAN_OUTPUT    = MUSIC_ROOT / "data" / "labels" / "clean"
LABEL_CLEAN_THRESHOLD = 0.85    # minimum confidence for automatic tag write-back

# ---------------------------------------------------------------------------
# Beets
# ---------------------------------------------------------------------------
BEETS_CONFIG = Path.home() / ".config" / "beets" / "config.yaml"

# ---------------------------------------------------------------------------
# Tag sanitization
# ---------------------------------------------------------------------------
# Set to False to disable tag sanitization entirely (useful for debugging)
SANITIZE_TAGS = True

# ---------------------------------------------------------------------------
# Pipeline metadata
# ---------------------------------------------------------------------------
PIPELINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# rmlint binary (override if not in PATH)
# ---------------------------------------------------------------------------
RMLINT_BIN     = os.environ.get("RMLINT_BIN", "rmlint")
# Aubio BPM detection — leave empty for auto-detection (recommended).
# analyzer.py will probe shutil.which("aubio") then shutil.which("aubiotrack").
# Set to an explicit path only if your binary is in a non-standard location,
# e.g.  AUBIO_BIN = "/opt/aubio/bin/aubio"
AUBIO_BIN      = os.environ.get("AUBIO_BIN", "")
# Legacy name kept so existing config_local.py overrides still work.
AUBIOBPM_BIN   = os.environ.get("AUBIOBPM_BIN", "aubiobpm")
KEYFINDER_BIN  = os.environ.get("KEYFINDER_BIN", "keyfinder-cli")
FFPROBE_BIN    = os.environ.get("FFPROBE_BIN", "ffprobe")
BEET_BIN       = os.environ.get("BEET_BIN", "beet")

# ---------------------------------------------------------------------------
# Local overrides (git-ignored, create config_local.py to override anything)
# ---------------------------------------------------------------------------
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
