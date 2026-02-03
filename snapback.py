#!/usr/bin/env python3
"""
Snapback - Backup tool with archive (7z/tar.gz) and restic incremental support.

Usage:
    snapback --source ~/projects/myrepo --dest ~/Backups --name myrepo
    snapback --source ~/projects/myrepo --dest ~/Backups --name myrepo --restic
    snapback --source ~/projects/myrepo --dest ~/Backups --name myrepo --restic --archive-format 7z --auto

    # Install as macOS daemon:
    snapback daemon install --source ~/projects/myrepo --dest ~/Backups --name myrepo
    snapback daemon status --name myrepo
    snapback daemon uninstall --name myrepo

For more info: https://github.com/joshm1/snapback
"""

import json
import tomllib
from enum import Enum
from typing import Literal, TypedDict

import tomli_w

import os
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


# =============================================================================
# Type Definitions
# =============================================================================

class ArchiveFormat(str, Enum):
    """Valid archive format values."""
    NONE = ""
    SEVENZ = "7z"
    TAR_GZ = "tar.gz"


class ManifestDefaults(TypedDict, total=False):
    """Type for manifest defaults section."""
    dest: str
    archive_format: str  # ArchiveFormat value
    use_restic: bool
    restic_interval_hours: int
    full_interval_days: int
    op_vault: str


class ManifestJob(TypedDict, total=False):
    """Type for a job in the manifest."""
    name: str
    source: str
    dest: str
    archive_format: str
    use_restic: bool
    op_vault: str


class Manifest(TypedDict, total=False):
    """Type for the full manifest structure."""
    defaults: ManifestDefaults
    jobs: list[ManifestJob]


# =============================================================================
# CLI Flag Constants (avoid magic strings)
# =============================================================================

class CLIFlags:
    """Constants for CLI flag names to avoid magic strings."""
    SOURCE = "--source"
    DEST = "--dest"
    NAME = "--name"
    RESTIC = "--restic"
    NO_RESTIC = "--no-restic"
    ARCHIVE_FORMAT = "--archive-format"
    SEVENZ = "--7z"
    TAR_GZ = "--tar-gz"
    NO_ARCHIVE = "--no-archive"
    RESTIC_INTERVAL = "--restic-interval"
    FULL_INTERVAL = "--full-interval"
    ONEPASSWORD_VAULT = "--1password-vault"
    AUTO = "--auto"
    FORCE = "--force"

import rich_click as click
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, RadioButton, RadioSet, Static

# Rich-click configuration
click.rich_click.USE_RICH_MARKUP = True
click.rich_click.USE_MARKDOWN = True
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = True

# Console for rich output (only used in TTY mode)
console = Console()

__version__ = "0.1.0"

# Config directory for job metadata
CONFIG_DIR = Path.home() / ".config" / "snapback"
JOBS_FILE = CONFIG_DIR / "jobs.json"
MANIFEST_FILE = CONFIG_DIR / "manifest.toml"
STATE_FILE = CONFIG_DIR / "state.json"
LOGS_DIR = Path.cwd() / "logs"

# Global flag for notifications
_notify_enabled = False

# Global flag for interactive mode (TTY)
_interactive = False

# Global flag for 1Password integration
_1password_enabled = False
_1password_vault: str | None = None


def is_interactive() -> bool:
    """Check if we're running in an interactive terminal."""
    return _interactive and sys.stdout.isatty()


def is_on_battery() -> bool:
    """Check if the Mac is running on battery power."""
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Output contains "Now drawing from 'Battery Power'" or "Now drawing from 'AC Power'"
            return "Battery Power" in result.stdout
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # If pmset not available (non-macOS), assume plugged in
        return False


def load_jobs() -> dict:
    """Load saved job configurations (from manifest, keyed by resolved source path)."""
    # Try migration first
    migrate_jobs_json()

    manifest = load_manifest()
    state = load_state()
    defaults = manifest.get("defaults", {})
    jobs = {}

    for job in manifest.get("jobs", []):
        source = job.get("source", "")
        if not source:
            continue
        key = get_job_key(Path(source))
        resolved = resolve_job_config(job, defaults)

        # Merge in state data for backward compatibility
        job_state = state.get(key, {})

        archive_format = resolved.get("archive_format", "")
        use_restic = resolved.get("use_restic", False)

        jobs[key] = {
            "source": source,
            "dest": resolved.get("dest", ""),
            "name": resolved.get("name", ""),
            "options": {
                "archive_format": archive_format,
                "use_7z": archive_format == "7z",
                "use_tar_gz": archive_format == "tar.gz",
                "use_restic": use_restic,
                "hybrid": use_restic and archive_format != "",  # backward compat
                "op_vault": resolved.get("op_vault"),
                "restic_interval_hours": resolved.get("restic_interval_hours"),
                "full_interval_days": resolved.get("full_interval_days"),
                "daemon_plist": job_state.get("daemon_plist"),
            },
            "last_runs": job_state.get("last_runs", {}),
        }

    return jobs


def save_jobs(jobs: dict) -> None:
    """Save job configurations to manifest."""
    manifest = load_manifest()
    state = load_state()

    new_jobs = []
    for key, job_data in jobs.items():
        job_config = {
            "name": job_data.get("name", ""),
            "source": job_data.get("source", ""),
        }

        dest = job_data.get("dest", "")
        if dest and dest != manifest.get("defaults", {}).get("dest"):
            job_config["dest"] = dest

        opts = job_data.get("options", {})

        # New format fields
        if "archive_format" in opts:
            job_config["archive_format"] = opts["archive_format"]
        elif opts.get("use_7z"):
            job_config["archive_format"] = "7z"
        elif opts.get("use_tar_gz"):
            job_config["archive_format"] = "tar.gz"

        if opts.get("use_restic"):
            job_config["use_restic"] = True

        if opts.get("op_vault"):
            job_config["op_vault"] = opts["op_vault"]

        new_jobs.append(job_config)

        # Update state
        job_state = state.get(key, {})
        if job_data.get("last_runs"):
            job_state["last_runs"] = job_data["last_runs"]
        if opts.get("daemon_plist"):
            job_state["daemon_plist"] = opts["daemon_plist"]
        if job_state:
            state[key] = job_state

    manifest["jobs"] = new_jobs
    save_manifest(manifest)
    save_state(state)


def get_job_key(source: Path) -> str:
    """Get a normalized key for a source path."""
    return str(source.expanduser().resolve())


DEFAULT_MANIFEST: dict = {
    "defaults": {
        "dest": "~/Backups",
        "archive_format": "7z",  # "7z", "tar.gz", or "" (disabled)
        "use_restic": False,
        "restic_interval_hours": 4,
        "full_interval_days": 7,
        "op_vault": "",
    },
    "jobs": [],
}


def _migrate_format_field(config: dict) -> dict:
    """Migrate old 'format' field to 'archive_format' + 'use_restic'."""
    if "format" not in config:
        return config

    old_format = config.pop("format")
    if old_format == "hybrid":
        config["archive_format"] = "7z"
        config["use_restic"] = True
    elif old_format == "restic":
        config["archive_format"] = ""
        config["use_restic"] = True
    elif old_format in ("7z", "tar.gz"):
        config["archive_format"] = old_format
        config["use_restic"] = False
    return config


def load_manifest() -> dict:
    """Load manifest configuration."""
    if not MANIFEST_FILE.exists():
        return DEFAULT_MANIFEST.copy()
    try:
        manifest = tomllib.loads(MANIFEST_FILE.read_text())

        # Migrate old format field in defaults
        if "defaults" in manifest:
            manifest["defaults"] = _migrate_format_field(manifest["defaults"])

        # Migrate old format field in each job
        for job in manifest.get("jobs", []):
            _migrate_format_field(job)

        return manifest
    except tomllib.TOMLDecodeError:
        logger.warning(f"Failed to parse {MANIFEST_FILE}, using defaults")
        return DEFAULT_MANIFEST.copy()


def save_manifest(manifest: dict) -> None:
    """Save manifest configuration."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(tomli_w.dumps(manifest))


def resolve_job_config(job: dict, defaults: dict) -> dict:
    """Resolve a job config by applying defaults for missing fields."""
    resolved = defaults.copy()
    resolved.update(job)
    return resolved


def load_state() -> dict:
    """Load runtime state."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    """Save runtime state."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def get_job_state(source: Path) -> dict:
    """Get runtime state for a specific job."""
    state = load_state()
    key = get_job_key(source)
    return state.get(key, {})


def update_job_state(source: Path, **updates) -> None:
    """Update runtime state for a job."""
    state = load_state()
    key = get_job_key(source)
    if key not in state:
        state[key] = {}
    state[key].update(updates)
    save_state(state)


def migrate_jobs_json() -> bool:
    """Migrate jobs.json to manifest.toml + state.json. Returns True if migration occurred."""
    if not JOBS_FILE.exists():
        return False
    if MANIFEST_FILE.exists():
        return False  # Already migrated

    try:
        old_jobs = json.loads(JOBS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    if not old_jobs:
        return False

    manifest = DEFAULT_MANIFEST.copy()
    manifest["jobs"] = []
    state: dict = {}

    for key, job_data in old_jobs.items():
        # Extract config fields
        job_config = {
            "name": job_data.get("name", ""),
            "source": job_data.get("source", ""),
        }

        # Only add non-default values
        if job_data.get("dest"):
            job_config["dest"] = job_data["dest"]

        opts = job_data.get("options", {})
        if opts.get("use_restic"):
            job_config["format"] = "restic"
        elif opts.get("hybrid"):
            job_config["format"] = "hybrid"
        elif opts.get("use_7z") is False:
            job_config["format"] = "tar.gz"
        # else: inherits default "7z"

        if opts.get("op_vault"):
            job_config["op_vault"] = opts["op_vault"]
        if opts.get("restic_interval_hours"):
            job_config["restic_interval_hours"] = opts["restic_interval_hours"]
        if opts.get("full_interval_days"):
            job_config["full_interval_days"] = opts["full_interval_days"]

        manifest["jobs"].append(job_config)

        # Extract state fields
        job_state = {}
        if job_data.get("last_runs"):
            job_state["last_runs"] = job_data["last_runs"]
        if opts.get("daemon_plist"):
            job_state["daemon_plist"] = opts["daemon_plist"]

        if job_state:
            state[key] = job_state

    save_manifest(manifest)
    if state:
        save_state(state)

    logger.info(f"Migrated {len(manifest['jobs'])} jobs from jobs.json")
    return True


def save_job_config(source: Path, dest: Path, name: str, **options) -> None:
    """Save configuration for a backup job (merges with existing options)."""
    jobs = load_jobs()
    key = get_job_key(source)

    # Preserve existing options (like op_vault from daemon install)
    existing_job = jobs.get(key, {})
    existing_options = existing_job.get("options", {})
    merged_options = {**existing_options, **options}

    # Preserve last_runs if it exists
    last_runs = existing_job.get("last_runs", {})

    jobs[key] = {
        "source": str(source),
        "dest": str(dest),
        "name": name,
        "options": merged_options,
        "last_saved": datetime.now().isoformat(),
    }
    if last_runs:
        jobs[key]["last_runs"] = last_runs

    save_jobs(jobs)
    logger.debug(f"Saved job config for {source}")


def load_job_config(source: Path) -> dict | None:
    """Load configuration for a backup job."""
    jobs = load_jobs()
    key = get_job_key(source)
    return jobs.get(key)


def update_job_last_run(source: Path, backup_type: str) -> None:
    """Update the last run timestamp for a job."""
    state = load_state()
    key = get_job_key(source)
    if key not in state:
        state[key] = {}
    if "last_runs" not in state[key]:
        state[key]["last_runs"] = {}
    state[key]["last_runs"][backup_type] = datetime.now().isoformat()
    save_state(state)


# Default directories to exclude
DEFAULT_EXCLUDES = [
    # JavaScript/Node
    "node_modules",
    ".pnpm-store",
    ".npm",
    ".yarn",
    ".next",
    ".nuxt",
    ".turbo",
    # Python
    ".venv",
    "venv",
    ".virtualenv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "*.egg-info",
    ".eggs",
    # iOS/macOS
    "Pods",
    "DerivedData",
    ".build",  # Swift Package Manager
    # Android
    ".gradle",
    ".cxx",
    # Build outputs
    "dist",
    "build",
    "target",  # Rust/Java
    "zig-out",  # Zig
    "out",
    # Caches
    ".cache",
    ".parcel-cache",
    ".nx",
    # IDE/Editor
    ".idea",
    ".vscode",
    # Docker
    ".docker",
    # Misc
    "*.pyc",
    "*.pyo",
    ".coverage",
    "coverage",
    "htmlcov",
]

# Default schedule intervals
DEFAULT_RESTIC_INTERVAL_HOURS = 4
DEFAULT_FULL_INTERVAL_DAYS = 7


def setup_logging(verbose: bool = False, file_logging: bool = False, console: bool = True) -> None:
    """Configure loguru logging.

    Args:
        verbose: Enable DEBUG level logging
        file_logging: Enable logging to ./logs/snapback.log with rotation
        console: Enable console (stdout) logging - disable for TUI
    """
    logger.remove()

    if console:
        level = "DEBUG" if verbose else "INFO"
        logger.add(
            sys.stdout,
            format="<level>{message}</level>",
            level=level,
            colorize=True,
        )

    if file_logging:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        logger.add(
            LOGS_DIR / "snapback.log",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
            level="DEBUG",  # Always capture debug in file
            rotation="10 MB",
            retention=5,
            compression="gz",
        )


@dataclass
class BackupConfig:
    """Configuration for a backup job."""
    source_dir: Path
    backup_dir: Path
    name: str
    exclude_dirs: list[str] = field(default_factory=list)
    include_git_in_restic: bool = True  # .git deduplicates well in restic
    exclude_git_in_full: bool = True    # Exclude .git from archive (large)
    restic_interval_hours: int = DEFAULT_RESTIC_INTERVAL_HOURS
    full_interval_days: int = DEFAULT_FULL_INTERVAL_DAYS
    split_size: str | None = None  # e.g., "1g", "500m" - None means no splitting
    archive_format: str = ArchiveFormat.SEVENZ.value  # "7z", "tar.gz", or "" (disabled)

    @property
    def use_7z(self) -> bool:
        """Backward-compat property."""
        return self.archive_format == ArchiveFormat.SEVENZ.value

    @property
    def backup_prefix(self) -> str:
        return f"{self.name}_"

    @property
    def backup_suffix(self) -> str:
        return ".7z" if self.use_7z else ".tar.gz"

    @property
    def restic_repo(self) -> Path:
        return self.backup_dir / self.name / "restic"

    @property
    def restic_password_file(self) -> Path:
        return Path.home() / ".config/restic" / f"{self.name}-password"

    @property
    def excludes_for_full(self) -> list[str]:
        """Exclusions for full tar.gz backup."""
        excludes = list(self.exclude_dirs)
        if self.exclude_git_in_full and ".git" not in excludes:
            excludes.append(".git")
        return excludes

    @property
    def excludes_for_restic(self) -> list[str]:
        """Exclusions for restic backup."""
        excludes = list(self.exclude_dirs)
        if not self.include_git_in_restic and ".git" not in excludes:
            excludes.append(".git")
        return excludes


# Global config - set by commands
_config: BackupConfig | None = None


def send_notification(title: str, message: str, sound: bool = True) -> None:
    """Send a macOS notification using osascript."""
    if not _notify_enabled:
        return

    # Escape quotes for AppleScript
    title = title.replace('"', '\\"')
    message = message.replace('"', '\\"')

    sound_str = 'sound name "default"' if sound else ""
    script = f'display notification "{message}" with title "{title}" {sound_str}'

    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # Don't fail backup if notification fails


def check_dest_accessible() -> bool:
    """Verify backup destination is accessible."""
    assert _config is not None
    if not _config.backup_dir.parent.exists():
        logger.error(f"Backup destination not accessible: {_config.backup_dir.parent}")
        send_notification(
            f"Snapback: {_config.name} Failed",
            "Backup destination not accessible.",
            sound=True,
        )
        return False
    return True


def ensure_backup_dir() -> bool:
    """Create backup directory if it doesn't exist."""
    assert _config is not None
    job_backup_dir = _config.backup_dir / _config.name
    if not job_backup_dir.exists():
        logger.info(f"Creating backup directory: {job_backup_dir}")
        job_backup_dir.mkdir(parents=True, exist_ok=True)
    return True


def get_existing_backups() -> list[tuple[Path, datetime]]:
    """Get list of existing backups with their timestamps."""
    assert _config is not None
    backups = []
    job_backup_dir = _config.backup_dir / _config.name
    if not job_backup_dir.exists():
        return backups

    # Look for both split volumes (.7z.001) and single files (.7z, .tar.gz)
    patterns = [
        f"{_config.backup_prefix}*{_config.backup_suffix}",  # Single file: myapp_*.7z or myapp_*.tar.gz
    ]
    if _config.use_7z:
        # Also look for split 7z volumes: myapp_*.7z.001
        patterns.append(f"{_config.backup_prefix}*.7z.001")

    seen_timestamps = set()
    for pattern in patterns:
        for f in job_backup_dir.glob(pattern):
            try:
                # Extract date from filename, handling split volume extensions
                stem = f.stem
                # Remove .7z from stem if it's a split volume (e.g., myapp_2024-01-15_120000.7z.001 -> stem is myapp_2024-01-15_120000.7z)
                if stem.endswith(".7z"):
                    stem = stem[:-3]
                date_str = stem.replace(_config.backup_prefix, "")
                backup_time = datetime.strptime(date_str, "%Y-%m-%d_%H%M%S")
                # Avoid duplicates (only count first volume of split backups)
                if date_str not in seen_timestamps:
                    seen_timestamps.add(date_str)
                    backups.append((f, backup_time))
            except ValueError:
                # Try to use file modification time as fallback
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                backups.append((f, mtime))

    return sorted(backups, key=lambda x: x[1], reverse=True)


def get_last_backup_time() -> datetime | None:
    """Get the timestamp of the most recent backup."""
    backups = get_existing_backups()
    if backups:
        return backups[0][1]
    return None


def get_last_backup_time_for_job(dest: str, name: str) -> datetime | None:
    """Get the timestamp of the most recent full backup for a job (without needing _config)."""
    # New structure: dest/name/ contains backups
    backup_dir = Path(dest) / name
    if not backup_dir.exists():
        # Fall back to old structure: dest/ contains backups
        backup_dir = Path(dest)
        if not backup_dir.exists():
            return None

    prefix = f"{name}_"
    patterns = [f"{prefix}*.7z", f"{prefix}*.7z.*", f"{prefix}*.tar.gz"]

    backups = []
    seen_timestamps = set()
    for pattern in patterns:
        for f in backup_dir.glob(pattern):
            try:
                stem = f.stem
                if stem.endswith(".7z"):
                    stem = stem[:-3]
                date_str = stem.replace(prefix, "")
                backup_time = datetime.strptime(date_str, "%Y-%m-%d_%H%M%S")
                if date_str not in seen_timestamps:
                    seen_timestamps.add(date_str)
                    backups.append(backup_time)
            except ValueError:
                pass

    if backups:
        return max(backups)
    return None


def get_last_restic_time_for_job(dest: str, name: str) -> datetime | None:
    """Get the timestamp of the most recent restic snapshot for a job (without needing _config)."""
    # New structure: dest/name/restic
    restic_repo = Path(dest) / name / "restic"
    if not restic_repo.exists():
        # Fall back to old structure: dest/{name}_restic
        restic_repo = Path(dest) / f"{name}_restic"
        if not restic_repo.exists():
            return None

    # Get password from cached password file
    password_file = Path.home() / ".config/restic" / f"{name}-password"
    if not password_file.exists():
        return None

    env = os.environ.copy()
    env["RESTIC_PASSWORD_FILE"] = str(password_file)

    result = subprocess.run(
        ["restic", "-r", str(restic_repo), "snapshots", "--json"],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    try:
        snapshots = json.loads(result.stdout) or []
        if not snapshots:
            return None
        latest = max(snapshots, key=lambda s: s.get("time", ""))
        time_str = latest.get("time", "")
        if time_str:
            time_str = time_str.split(".")[0]
            return datetime.fromisoformat(time_str)
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def format_age(delta: timedelta) -> str:
    """Format timedelta into human-readable age."""
    if delta.days > 0:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours}h ago"
    minutes = delta.seconds // 60
    return f"{minutes}m ago"


def ensure_restic_password() -> bool:
    """Ensure restic password file exists."""
    global _1password_vault
    assert _config is not None

    if _config.restic_password_file.exists():
        return True

    logger.info("Setting up restic password file...")
    _config.restic_password_file.parent.mkdir(parents=True, exist_ok=True)

    # Try to retrieve from 1Password first if enabled
    password = None
    if _1password_enabled:
        password = get_password_from_1password(_config.name)
        if password:
            logger.info("Retrieved password from 1Password")

    # Generate new password if not found
    if not password:
        password = secrets.token_urlsafe(32)

        # Store in 1Password if enabled (with interactive vault selection)
        if _1password_enabled:
            if not _1password_vault and is_interactive():
                # Interactive vault selection
                _1password_vault = setup_1password_vault_interactive()

            if _1password_vault or not is_interactive():
                store_password_in_1password(_config.name, password)

    _config.restic_password_file.write_text(password)
    _config.restic_password_file.chmod(0o600)

    logger.success(f"Created password file: {_config.restic_password_file}")
    return True


def is_restic_repo_initialized() -> bool:
    """Check if restic repository is initialized."""
    assert _config is not None
    return (_config.restic_repo / "config").exists()


def init_restic_repo() -> bool:
    """Initialize restic repository."""
    assert _config is not None
    if is_restic_repo_initialized():
        return True

    logger.info(f"Initializing restic repository at {_config.restic_repo}...")
    _config.restic_repo.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["restic", "-r", str(_config.restic_repo), "init"],
        env={**os.environ, "RESTIC_PASSWORD_FILE": str(_config.restic_password_file)},
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(f"Failed to initialize restic repo: {result.stderr}")
        return False

    logger.success("Restic repository initialized")
    return True


def get_restic_snapshots() -> list[dict]:
    """Get list of restic snapshots."""
    assert _config is not None
    if not is_restic_repo_initialized():
        return []

    result = subprocess.run(
        ["restic", "-r", str(_config.restic_repo), "snapshots", "--json"],
        env={**os.environ, "RESTIC_PASSWORD_FILE": str(_config.restic_password_file)},
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return []

    try:
        return json.loads(result.stdout) or []
    except json.JSONDecodeError:
        return []


def get_last_restic_backup_time() -> datetime | None:
    """Get the timestamp of the most recent restic snapshot."""
    snapshots = get_restic_snapshots()
    if not snapshots:
        return None

    latest = max(snapshots, key=lambda s: s.get("time", ""))
    time_str = latest.get("time", "")
    if time_str:
        try:
            time_str = time_str.split(".")[0]
            return datetime.fromisoformat(time_str)
        except ValueError:
            pass
    return None


def list_restic_backups() -> None:
    """List restic snapshots."""
    assert _config is not None
    if not is_restic_repo_initialized():
        logger.warning("Restic repository not initialized. Run with --restic to set up.")
        return

    snapshots = get_restic_snapshots()
    if not snapshots:
        logger.info("No restic snapshots found.")
        return

    logger.info(f"Restic Snapshots ({len(snapshots)} total):")
    for snap in sorted(snapshots, key=lambda s: s.get("time", ""), reverse=True):
        time_str = snap.get("time", "")[:19]
        short_id = snap.get("short_id", snap.get("id", "")[:8])
        try:
            snap_time = datetime.fromisoformat(time_str)
            age = datetime.now() - snap_time
            logger.info(f"  {snap_time.strftime('%Y-%m-%d %H:%M')}  {short_id}  ({format_age(age)})")
        except ValueError:
            logger.info(f"  {time_str}  {short_id}")

    # Get repo stats
    result = subprocess.run(
        ["restic", "-r", str(_config.restic_repo), "stats", "--json"],
        env={**os.environ, "RESTIC_PASSWORD_FILE": str(_config.restic_password_file)},
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        try:
            stats = json.loads(result.stdout)
            total_size = stats.get("total_size", 0)
            logger.info(f"Total repo size: {format_size(total_size)}")
        except json.JSONDecodeError:
            pass


def create_restic_backup(dry_run: bool = False) -> bool:
    """Create a restic incremental backup."""
    assert _config is not None

    if not ensure_restic_password():
        return False

    if not init_restic_repo():
        return False

    exclude_args = []
    for d in _config.excludes_for_restic:
        exclude_args.extend(["--exclude", d])

    logger.info(f"{'[DRY RUN] ' if dry_run else ''}Creating restic backup...")
    logger.info(f"  Source: {_config.source_dir}")
    logger.debug(f"  Excluding: {', '.join(_config.excludes_for_restic)}")

    cmd = [
        "restic", "-r", str(_config.restic_repo),
        "backup",
        str(_config.source_dir),
        *exclude_args,
        "--tag", _config.name,
    ]

    if dry_run:
        cmd.append("--dry-run")

    env = {**os.environ, "RESTIC_PASSWORD_FILE": str(_config.restic_password_file)}

    try:
        # In interactive mode, let restic show its native progress
        if is_interactive() and not dry_run:
            result = subprocess.run(cmd, env=env, timeout=600)
            if result.returncode != 0:
                logger.error("Restic backup failed")
                send_notification(
                    f"Snapback: {_config.name} Failed",
                    "Restic backup error",
                    sound=True,
                )
                return False
            logger.success("Restic backup complete")
            send_notification(
                f"Snapback: {_config.name} Complete",
                "Incremental backup saved",
                sound=False,
            )
            return True

        # Non-interactive: capture output for logging
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode != 0:
            logger.error(f"Restic backup failed: {result.stderr}")
            if not dry_run:
                send_notification(
                    f"Snapback: {_config.name} Failed",
                    f"Restic error: {result.stderr[:100]}",
                    sound=True,
                )
            return False

        # Combine stdout and stderr for stats
        output = result.stdout + result.stderr
        if dry_run:
            logger.success("[DRY RUN] Restic backup would succeed")
        else:
            logger.success("Restic backup complete")

        # Show stats from restic output
        for line in output.split("\n"):
            line = line.strip()
            if any(x in line.lower() for x in ["files:", "dirs:", "added", "processed", "would add"]):
                logger.info(f"  {line}")

        send_notification(
            f"Snapback: {_config.name} Complete",
            "Incremental backup saved",
            sound=False,
        )
        return True

    except subprocess.TimeoutExpired:
        logger.error("Restic backup timed out after 10 minutes")
        send_notification(
            f"Snapback: {_config.name} Failed",
            "Backup timed out after 10 minutes",
            sound=True,
        )
        return False
    except Exception as e:
        logger.error(f"Restic backup failed: {e}")
        send_notification(
            f"Snapback: {_config.name} Failed",
            f"Error: {e}",
            sound=True,
        )
        return False


def list_backups() -> None:
    """List all existing backups."""
    backups = get_existing_backups()
    if not backups:
        logger.info("No tar.gz backups found.")
        return

    logger.info(f"Full Backups ({len(backups)} total):")
    total_size = 0
    for path, timestamp in backups:
        size = path.stat().st_size
        total_size += size
        age = datetime.now() - timestamp
        logger.info(f"  {timestamp.strftime('%Y-%m-%d %H:%M')}  {format_size(size):>10}  ({format_age(age)})")

    logger.info(f"Total size: {format_size(total_size)}")


def get_backup_stats() -> tuple[int, int]:
    """Get file count and total size for backup (respecting exclusions)."""
    assert _config is not None

    # Build find command with exclusions
    exclude_args = []
    for d in _config.excludes_for_full:
        if "*" in d:
            # Pattern like *.egg-info
            exclude_args.extend(["-name", d, "-prune", "-o"])
        else:
            # Directory name
            exclude_args.extend(["-name", d, "-prune", "-o"])

    # Use find to list files, excluding specified dirs
    cmd = ["find", str(_config.source_dir)]
    cmd.extend(exclude_args)
    cmd.extend(["-type", "f", "-print"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return 0, 0

        files = [f for f in result.stdout.strip().split("\n") if f]
        file_count = len(files)

        # Sum file sizes
        total_size = 0
        for f in files:
            try:
                total_size += Path(f).stat().st_size
            except (OSError, FileNotFoundError):
                pass

        return file_count, total_size
    except Exception:
        return 0, 0


def create_backup(dry_run: bool = False) -> Path | None:
    """Create a compressed backup, optionally split into multiple parts."""
    assert _config is not None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_name = f"{_config.backup_prefix}{timestamp}{_config.backup_suffix}"
    backup_path = _config.backup_dir / _config.name / backup_name

    exclude_args = []
    for d in _config.excludes_for_full:
        exclude_args.extend(["--exclude", d])

    # Check if we're splitting
    splitting = _config.split_size is not None

    if dry_run:
        logger.info(f"[DRY RUN] Would create backup: {backup_name}")
        if splitting:
            logger.info(f"  Split into: {_config.split_size} chunks")
        logger.info(f"  Source: {_config.source_dir}")
        logger.info(f"  Excluding: {', '.join(_config.excludes_for_full)}")

        # Get stats
        file_count, total_size = get_backup_stats()
        if file_count > 0:
            # Estimate compressed size (typically 30-50% of original for code)
            estimated_compressed = int(total_size * 0.4)
            logger.info(f"  Files: {file_count:,}")
            logger.info(f"  Uncompressed: {format_size(total_size)}")
            logger.info(f"  Estimated compressed: ~{format_size(estimated_compressed)}")
            if splitting:
                # Estimate number of parts
                split_bytes = parse_size(_config.split_size)
                if split_bytes > 0:
                    num_parts = max(1, estimated_compressed // split_bytes + 1)
                    logger.info(f"  Estimated parts: ~{num_parts}")

        return None

    logger.info(f"Creating backup: {backup_name}")
    if splitting:
        logger.info(f"  Split size: {_config.split_size}")
    logger.info(f"  Source: {_config.source_dir}")
    logger.debug(f"  Excluding: {', '.join(_config.excludes_for_full)}")

    try:
        env = os.environ.copy()
        env["GZIP"] = "-9"

        if splitting:
            # Use tar piped to split for chunked output
            # Output: backup.tar.gz.part00, backup.tar.gz.part01, etc.
            tar_cmd = [
                "tar",
                "-czf", "-",  # Output to stdout
                *exclude_args,
                "-C", str(_config.source_dir.parent),
                _config.source_dir.name,
            ]
            split_cmd = [
                "split",
                "-b", _config.split_size,
                "-d",  # Use numeric suffixes
                "-a", "2",  # 2-digit suffixes (00, 01, etc.)
                "-",  # Read from stdin
                f"{backup_path}.part",  # Prefix for output files
            ]

            if is_interactive():
                with console.status(f"[bold blue]Compressing and splitting {_config.source_dir.name}...", spinner="dots"):
                    tar_proc = subprocess.Popen(
                        tar_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                    )
                    split_proc = subprocess.Popen(
                        split_cmd,
                        stdin=tar_proc.stdout,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    tar_proc.stdout.close()  # Allow tar to receive SIGPIPE
                    _, split_stderr = split_proc.communicate(timeout=600)
                    tar_proc.wait()
            else:
                tar_proc = subprocess.Popen(
                    tar_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                split_proc = subprocess.Popen(
                    split_cmd,
                    stdin=tar_proc.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                tar_proc.stdout.close()
                _, split_stderr = split_proc.communicate(timeout=600)
                tar_proc.wait()

            if tar_proc.returncode != 0 or split_proc.returncode != 0:
                logger.error(f"Backup failed: tar={tar_proc.returncode}, split={split_proc.returncode}")
                return None

            # Count parts and total size
            parts = sorted(_config.backup_dir.glob(f"{backup_name}.part*"))
            total_size = sum(p.stat().st_size for p in parts)
            logger.success(f"Backup created: {len(parts)} parts ({format_size(total_size)} total)")
            for part in parts:
                logger.info(f"  {part.name} ({format_size(part.stat().st_size)})")

            send_notification(
                f"Snapback: {_config.name} Complete",
                f"Saved {len(parts)} parts, {format_size(total_size)}",
                sound=False,
            )
            return parts[0] if parts else None

        else:
            # Single file backup (original behavior)
            cmd = [
                "tar",
                "-czf",
                str(backup_path),
                *exclude_args,
                "-C",
                str(_config.source_dir.parent),
                _config.source_dir.name,
            ]

            if is_interactive():
                with console.status(f"[bold blue]Compressing {_config.source_dir.name}...", spinner="dots"):
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=600,
                    )
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=600,
                )

            if result.returncode != 0:
                logger.error(f"Backup failed: {result.stderr}")
                return None

            size = backup_path.stat().st_size
            logger.success(f"Backup created: {backup_name} ({format_size(size)})")
            send_notification(
                f"Snapback: {_config.name} Complete",
                f"Saved {format_size(size)}",
                sound=False,
            )
            return backup_path

    except subprocess.TimeoutExpired:
        logger.error("Backup timed out after 10 minutes")
        send_notification(
            f"Snapback: {_config.name} Failed",
            "Backup timed out after 10 minutes",
            sound=True,
        )
        return None
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        send_notification(
            f"Snapback: {_config.name} Failed",
            f"Error: {e}",
            sound=True,
        )
        return None


def parse_size(size_str: str) -> int:
    """Parse size string like '1G', '500M' into bytes."""
    size_str = size_str.strip().upper()
    multipliers = {
        'B': 1,
        'K': 1024,
        'M': 1024 * 1024,
        'G': 1024 * 1024 * 1024,
        'T': 1024 * 1024 * 1024 * 1024,
    }
    for suffix, mult in multipliers.items():
        if size_str.endswith(suffix):
            try:
                return int(float(size_str[:-1]) * mult)
            except ValueError:
                return 0
    try:
        return int(size_str)
    except ValueError:
        return 0


def check_7z_installed() -> bool:
    """Check if 7z is installed."""
    try:
        result = subprocess.run(["7z", "--help"], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_1password_cli() -> bool:
    """Check if 1Password CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            ["op", "account", "list"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_1password_vaults() -> list[dict]:
    """Get list of available 1Password vaults."""
    try:
        result = subprocess.run(
            ["op", "vault", "list", "--format=json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout) or []
        return []
    except Exception:
        return []


def create_1password_vault(name: str) -> bool:
    """Create a new 1Password vault."""
    try:
        result = subprocess.run(
            ["op", "vault", "create", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def setup_1password_vault_interactive() -> str | None:
    """Interactive setup to select or create a 1Password vault."""
    import questionary

    vaults = get_1password_vaults()

    if not vaults:
        logger.warning("No 1Password vaults found")
        return None

    # Build choices for questionary - sort alphabetically for predictability
    vault_names = sorted([vault.get("name", "Unknown") for vault in vaults])
    choices = vault_names + ["➕ Create new vault..."]

    logger.info("")  # Empty line before prompt
    selected = questionary.select(
        "Select 1Password vault for storing backup passwords (use arrow keys):",
        choices=choices,
        instruction="(↑↓ to move, Enter to select)",
        style=questionary.Style([
            ("selected", "fg:cyan bold"),
            ("pointer", "fg:cyan bold"),
            ("highlighted", "fg:cyan"),
            ("question", "bold"),
        ]),
    ).ask()

    if selected is None:
        return None

    if selected == "➕ Create new vault...":
        new_name = questionary.text(
            "New vault name:",
            default="Snapback",
        ).ask()

        if new_name:
            if create_1password_vault(new_name):
                logger.success(f"Created vault: {new_name}")
                return new_name
            else:
                logger.error("Failed to create vault")
                return None
        return None

    # Confirm selection to avoid accidental picks
    if not questionary.confirm(f"Store password in vault '{selected}'?", default=True).ask():
        return setup_1password_vault_interactive()  # Let them pick again

    return selected


def store_password_in_1password(name: str, password: str, vault: str | None = None) -> bool:
    """Store or update restic password in 1Password (upsert)."""
    item_title = f"Snapback: {name} restic password"
    use_vault = vault or _1password_vault

    # Check if item already exists
    check_cmd = ["op", "item", "get", item_title, "--format=json"]
    if use_vault:
        check_cmd.extend(["--vault", use_vault])

    try:
        check_result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=30)
        item_exists = check_result.returncode == 0

        if item_exists:
            # Update existing item
            cmd = ["op", "item", "edit", item_title, f"password={password}"]
            if use_vault:
                cmd.extend(["--vault", use_vault])
            action = "updated"
        else:
            # Create new item
            cmd = [
                "op", "item", "create",
                "--category=password",
                f"--title={item_title}",
                f"password={password}",
            ]
            if use_vault:
                cmd.insert(3, f"--vault={use_vault}")

            # Add notes with helpful info (only on create)
            notes = f"Restic backup password for '{name}'\nPassword file: ~/.config/restic/{name}-password"
            cmd.append(f"notesPlain={notes}")
            action = "stored"

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            logger.success(f"Password {action} in 1Password: {item_title}")
            if use_vault:
                logger.info(f"  Vault: {use_vault}")
            return True
        else:
            logger.warning(f"Failed to {action[:-1]}e in 1Password: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        logger.warning("1Password CLI timed out")
        return False
    except Exception as e:
        logger.warning(f"1Password error: {e}")
        return False


def get_password_from_1password(name: str, vault: str | None = None) -> str | None:
    """Retrieve restic password from 1Password."""
    item_title = f"Snapback: {name} restic password"
    use_vault = vault or _1password_vault

    cmd = ["op", "item", "get", item_title, "--fields", "password", "--reveal"]
    if use_vault:
        cmd.extend(["--vault", use_vault])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


def create_7z_backup(dry_run: bool = False) -> Path | None:
    """Create a 7z backup with optional volume splitting."""
    assert _config is not None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_name = f"{_config.backup_prefix}{timestamp}{_config.backup_suffix}"
    backup_path = _config.backup_dir / _config.name / backup_name

    # Build exclusion args for 7z
    exclude_args = []
    for d in _config.excludes_for_full:
        # 7z uses -xr! for recursive exclusion
        exclude_args.extend([f"-xr!{d}"])

    # Build the command
    cmd = ["7z", "a", "-mx=9"]  # Maximum compression

    # Add volume splitting if specified
    if _config.split_size:
        cmd.append(f"-v{_config.split_size}")

    cmd.append(str(backup_path))
    cmd.extend(exclude_args)
    cmd.append(str(_config.source_dir))

    if dry_run:
        logger.info(f"[DRY RUN] Would create 7z backup: {backup_name}")
        if _config.split_size:
            logger.info(f"  Split into: {_config.split_size} volumes")
        logger.info(f"  Source: {_config.source_dir}")
        logger.info(f"  Excluding: {', '.join(_config.excludes_for_full)}")

        # Get stats
        file_count, total_size = get_backup_stats()
        if file_count > 0:
            # 7z typically achieves better compression than gzip
            estimated_compressed = int(total_size * 0.3)
            logger.info(f"  Files: {file_count:,}")
            logger.info(f"  Uncompressed: {format_size(total_size)}")
            logger.info(f"  Estimated compressed: ~{format_size(estimated_compressed)}")
            if _config.split_size:
                split_bytes = parse_size(_config.split_size)
                if split_bytes > 0:
                    num_parts = max(1, estimated_compressed // split_bytes + 1)
                    logger.info(f"  Estimated volumes: ~{num_parts}")
        return None

    logger.info(f"Creating 7z backup: {backup_name}")
    if _config.split_size:
        logger.info(f"  Volume size: {_config.split_size}")
    logger.info(f"  Source: {_config.source_dir}")
    logger.debug(f"  Excluding: {', '.join(_config.excludes_for_full)}")

    try:
        # In interactive mode, let 7z show its native progress
        if is_interactive():
            result = subprocess.run(cmd, timeout=3600)  # 1 hour timeout for large backups
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

        if result.returncode != 0:
            error_msg = getattr(result, 'stderr', '') or "Unknown error"
            logger.error(f"7z backup failed: {error_msg}")
            send_notification(
                f"Snapback: {_config.name} Failed",
                "7z backup error",
                sound=True,
            )
            return None

        # Find all created files (could be split volumes)
        if _config.split_size:
            # 7z creates: backup.7z.001, backup.7z.002, etc.
            parts = sorted(_config.backup_dir.glob(f"{backup_name}.*"))
            if parts:
                total_size = sum(p.stat().st_size for p in parts)
                logger.success(f"7z backup created: {len(parts)} volumes ({format_size(total_size)} total)")
                for part in parts[:5]:  # Show first 5
                    logger.info(f"  {part.name} ({format_size(part.stat().st_size)})")
                if len(parts) > 5:
                    logger.info(f"  ... and {len(parts) - 5} more volumes")
                send_notification(
                    f"Snapback: {_config.name} Complete",
                    f"Saved {len(parts)} volumes, {format_size(total_size)}",
                    sound=False,
                )
                return parts[0]
        else:
            size = backup_path.stat().st_size
            logger.success(f"7z backup created: {backup_name} ({format_size(size)})")
            send_notification(
                f"Snapback: {_config.name} Complete",
                f"Saved {format_size(size)}",
                sound=False,
            )
            return backup_path

    except subprocess.TimeoutExpired:
        logger.error("7z backup timed out after 1 hour")
        send_notification(
            f"Snapback: {_config.name} Failed",
            "Backup timed out after 1 hour",
            sound=True,
        )
        return None
    except Exception as e:
        logger.error(f"7z backup failed: {e}")
        send_notification(
            f"Snapback: {_config.name} Failed",
            f"Error: {e}",
            sound=True,
        )
        return None

    return None


def run_combined_backup(force: bool, auto: bool, dry_run: bool) -> int:
    """
    Combined backup mode (restic + archive):
    - Run restic backup if > restic_interval_hours since last restic backup
    - Run full archive backup if > full_interval_days since last full backup
    """
    assert _config is not None
    restic_ran = False
    full_ran = False

    restic_threshold = timedelta(hours=_config.restic_interval_hours)
    full_threshold = timedelta(days=_config.full_interval_days)

    last_restic = get_last_restic_backup_time()
    restic_needed = force
    if last_restic and not force:
        restic_age = datetime.now() - last_restic
        if restic_age < restic_threshold:
            restic_needed = False
        else:
            restic_needed = True
            logger.info(f"Last restic: {last_restic.strftime('%Y-%m-%d %H:%M')} ({format_age(restic_age)})")
    elif not last_restic:
        restic_needed = True
        logger.info("No previous restic backup found.")

    last_full = get_last_backup_time()
    full_needed = force
    if last_full and not force:
        full_age = datetime.now() - last_full
        if full_age < full_threshold:
            full_needed = False
        else:
            full_needed = True
            logger.info(f"Last full backup: {last_full.strftime('%Y-%m-%d %H:%M')} ({format_age(full_age)})")
    elif not last_full:
        full_needed = True
        logger.info("No previous full backup found.")

    if auto and not restic_needed and not full_needed:
        return 0

    if not restic_needed and not full_needed:
        logger.success(f"All backups are current (restic < {_config.restic_interval_hours}h, archive < {_config.full_interval_days}d)")
        return 0

    if restic_needed:
        if dry_run:
            logger.info("[DRY RUN] Would run restic backup")
        else:
            if create_restic_backup(dry_run=False):
                restic_ran = True
                # Update timestamp immediately on success
                update_job_last_run(_config.source_dir, "restic")
            else:
                logger.error("Restic backup failed")

    if full_needed:
        archive_fmt = _config.archive_format
        if dry_run:
            logger.info(f"[DRY RUN] Would run full {archive_fmt} backup (every {_config.full_interval_days} days)")
        else:
            logger.info(f"Running full {archive_fmt} backup (every {_config.full_interval_days} days)...")
            if archive_fmt == ArchiveFormat.SEVENZ.value:
                backup_result = create_7z_backup(dry_run=False)
            else:
                backup_result = create_backup(dry_run=False)
            if backup_result:
                full_ran = True
                # Update timestamp immediately on success
                update_job_last_run(_config.source_dir, archive_fmt)
            else:
                logger.error("Full backup failed")

    if dry_run:
        return 0

    # Return partial success (0) if at least one backup ran
    if restic_ran or full_ran:
        if (restic_needed and not restic_ran) or (full_needed and not full_ran):
            return 2  # Partial success
        return 0
    return 1


# =============================================================================
# Daemon Management (macOS LaunchAgent)
# =============================================================================

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  Snapback v{version}
  Generated: {generated_at}
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{snapback_path}</string>
        <string>--source</string>
        <string>{source}</string>
        <string>--dest</string>
        <string>{dest}</string>
        <string>--name</string>
        <string>{name}</string>
        <string>--auto</string>{mode_args}
    </array>

    <!-- Check hourly; backup logic decides if backup is actually needed -->
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>

    <!-- Also run on login/wake to catch up if needed -->
    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{log_path}</string>

    <key>StandardErrorPath</key>
    <string>{log_path}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:{home}/.local/bin</string>
    </dict>
</dict>
</plist>
"""


DAEMON_NAMESPACE = "io.github.joshm1.snapback"


def get_plist_version(plist_path: Path) -> str | None:
    """Extract snapback version from a plist file's comment header."""
    if not plist_path.exists():
        return None
    try:
        content = plist_path.read_text()
        # Look for: Snapback v0.1.0
        import re
        match = re.search(r"Snapback v([\d.]+)", content)
        if match:
            return match.group(1)
        return None
    except Exception:
        return None


def get_daemon_label(name: str) -> str:
    """Get the launchd label for a named daemon."""
    return f"{DAEMON_NAMESPACE}.{name}"


def get_plist_path(name: str) -> Path:
    """Get the path to the plist file for a named daemon."""
    return Path.home() / "Library/LaunchAgents" / f"{get_daemon_label(name)}.plist"


def find_plist_path(name: str) -> Path | None:
    """Find plist file for a daemon, checking current and legacy namespaces."""
    # Check current namespace first
    current = get_plist_path(name)
    if current.exists():
        return current

    # Check legacy namespace (com.snapback.{name})
    legacy = Path.home() / "Library/LaunchAgents" / f"com.snapback.{name}.plist"
    if legacy.exists():
        return legacy

    return None


def get_log_path(name: str) -> Path:
    """Get the path to the log file for a named daemon."""
    # ~/Library/Logs/ is the standard macOS location for user app logs
    # Visible in Console.app and persists across reboots
    return Path.home() / "Library/Logs" / f"snapback-{name}.log"


def find_snapback_path() -> str:
    """Find the snapback executable path."""
    # Try common locations
    candidates = [
        Path.home() / ".local/bin/snapback",
        Path("/usr/local/bin/snapback"),
        Path(sys.executable).parent / "snapback",
    ]

    for path in candidates:
        if path.exists():
            return str(path)

    # Fall back to using python -m
    return f"{sys.executable} -m snapback"


# =============================================================================
# CLI Commands
# =============================================================================

@click.group(invoke_without_command=True)
@click.option(CLIFlags.SOURCE, "-s", type=click.Path(exists=True, path_type=Path), help="Source directory to backup")
@click.option(CLIFlags.DEST, "-d", type=click.Path(path_type=Path), help="Destination directory for backups")
@click.option(CLIFlags.NAME, "-N", help="Name for this backup (used in filenames)")
@click.option(CLIFlags.RESTIC + "/" + CLIFlags.NO_RESTIC, default=False, help="Enable/disable restic incremental backup")
@click.option(CLIFlags.ARCHIVE_FORMAT, type=click.Choice(["7z", "tar.gz", "none"]), default="7z",
              help="Archive format for full backups (default: 7z)")
@click.option("--exclude", "-e", multiple=True, help="Additional directories to exclude")
@click.option("--no-default-excludes", is_flag=True, help="Don't use default exclusions")
@click.option("--include-git", is_flag=True, help="Include .git in archive backups")
@click.option("--exclude-git-restic", is_flag=True, help="Exclude .git from restic backups")
@click.option(CLIFlags.FORCE, "-f", is_flag=True, help="Skip recency check and create backup")
@click.option(CLIFlags.AUTO, "-a", is_flag=True, help="Automatic mode: skip silently if not needed")
@click.option("--dry-run", "-n", is_flag=True, help="Show what would happen without doing it")
@click.option("--list", "-l", "list_mode", is_flag=True, help="List existing backups")
@click.option("--notify", is_flag=True, help="Send macOS notifications")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.option(CLIFlags.RESTIC_INTERVAL, type=int, default=DEFAULT_RESTIC_INTERVAL_HOURS,
              help=f"Hours between restic backups (default: {DEFAULT_RESTIC_INTERVAL_HOURS})")
@click.option(CLIFlags.FULL_INTERVAL, type=int, default=DEFAULT_FULL_INTERVAL_DAYS,
              help=f"Days between full archive backups (default: {DEFAULT_FULL_INTERVAL_DAYS})")
@click.option("--split-size", type=str, default="50m",
              help="Split backup into volumes of this size (default: 50m). Use --no-split to disable")
@click.option("--no-split", is_flag=True, help="Don't split backup into volumes")
@click.option("--save", is_flag=True, help="Save this job config for future runs (auto-saved on successful backup)")
@click.option("--1password", "use_1password", is_flag=True, help="Store restic password in 1Password")
@click.option(CLIFlags.ONEPASSWORD_VAULT, "op_vault", type=str, help="1Password vault name (prompts interactively if not specified)")
@click.version_option(version=__version__)
@click.pass_context
def cli(ctx, source, dest, name, restic, archive_format, exclude, no_default_excludes,
        include_git, exclude_git_restic, force, auto, dry_run, list_mode, notify, verbose,
        restic_interval, full_interval, split_size, no_split, save,
        use_1password, op_vault):
    """Snapback - Backup tool with archive (7z/tar.gz) and restic support.

    Run with just --source to use saved config from a previous run.
    """
    ctx.ensure_object(dict)

    # Set up logging
    setup_logging(verbose)

    # If a subcommand is invoked, skip backup logic
    if ctx.invoked_subcommand is not None:
        return

    # If only source is provided, try to load saved config
    if source and not dest and not name:
        saved = load_job_config(source)
        if saved:
            logger.info(f"Using saved config for {source}")
            dest = Path(saved["dest"])
            name = saved["name"]
            opts = saved.get("options", {})
            # Apply saved options (CLI flags override saved)
            if not restic and opts.get("use_restic"):
                restic = True
            if archive_format == "7z" and opts.get("archive_format"):
                archive_format = opts["archive_format"]
            if split_size == "50m" and opts.get("split_size"):
                split_size = opts["split_size"]
            if not no_split and opts.get("no_split"):
                no_split = True
            logger.debug(f"  dest: {dest}")
            logger.debug(f"  name: {name}")
            logger.debug(f"  restic: {restic}, archive_format: {archive_format}")
        else:
            raise click.UsageError(
                f"No saved config for {source}. "
                "Run with --dest and --name first, or use 'snapback jobs' to see saved configs."
            )

    # For direct backup command, require source/dest/name
    if not all([source, dest, name]):
        if list_mode or any([source, dest, name]):
            raise click.UsageError("--source, --dest, and --name are required for backup operations")
        click.echo(ctx.get_help())
        return

    # Build exclusion list
    excludes = [] if no_default_excludes else list(DEFAULT_EXCLUDES)
    excludes.extend(exclude)

    # Determine effective split size
    effective_split_size = None if no_split else split_size

    # Normalize archive_format
    if archive_format == "none":
        archive_format = ArchiveFormat.NONE.value

    # Set up global config
    global _config
    _config = BackupConfig(
        source_dir=source.expanduser().resolve(),
        backup_dir=dest.expanduser().resolve(),
        name=name,
        exclude_dirs=excludes,
        include_git_in_restic=not exclude_git_restic,
        exclude_git_in_full=not include_git,
        restic_interval_hours=restic_interval,
        full_interval_days=full_interval,
        split_size=effective_split_size,
        archive_format=archive_format,
    )

    # Check if 7z is installed when using 7z format
    if archive_format == ArchiveFormat.SEVENZ.value and not check_7z_installed():
        raise click.ClickException(
            "7z is not installed. Install with: brew install p7zip\n"
            f"Or use {CLIFlags.ARCHIVE_FORMAT} tar.gz instead."
        )

    # Enable notifications
    global _notify_enabled
    _notify_enabled = notify or auto

    # Enable interactive mode (Rich progress) when running manually with a TTY
    global _interactive
    _interactive = not auto and sys.stdout.isatty()

    # Enable 1Password integration
    global _1password_enabled, _1password_vault
    if use_1password:
        if not check_1password_cli():
            raise click.ClickException(
                "1Password CLI not found or not authenticated.\n"
                "Install: https://developer.1password.com/docs/cli/get-started/\n"
                "Then run: op signin"
            )
        _1password_enabled = True
        _1password_vault = op_vault

    # Validate source exists
    if not _config.source_dir.exists():
        raise click.ClickException(f"Source directory does not exist: {_config.source_dir}")

    # Check destination is accessible
    if not check_dest_accessible():
        if auto:
            ctx.exit(0)
        ctx.exit(1)

    # Skip backup if on battery power (auto mode only - save energy)
    if auto and is_on_battery():
        logger.info("Skipping backup: running on battery power")
        ctx.exit(0)

    # List mode
    if list_mode:
        if restic:
            list_restic_backups()
        else:
            list_backups()
            if is_restic_repo_initialized():
                logger.info("\n--- Restic Incremental Backups ---")
                list_restic_backups()
        ctx.exit(0)

    # Ensure backup directory exists
    if not dry_run:
        ensure_backup_dir()

    # Combined restic + archive mode (replaces old "hybrid" mode)
    if restic and archive_format and archive_format != ArchiveFormat.NONE.value:
        result = run_combined_backup(force, auto, dry_run)
        # Save config on any successful backup (0=full success, 2=partial success)
        if result in (0, 2) and not dry_run:
            save_job_config(
                source, dest, name,
                use_restic=True,
                archive_format=archive_format,
                split_size=effective_split_size,
                no_split=no_split,
            )
            # Timestamps are updated inside run_combined_backup now
        ctx.exit(0 if result in (0, 2) else 1)

    # Check for recent backup
    if restic:
        last_backup = get_last_restic_backup_time()
        backup_type = "restic"
    else:
        last_backup = get_last_backup_time()
        backup_type = archive_format if archive_format else "archive"

    if last_backup:
        age = datetime.now() - last_backup

        if auto and age < timedelta(hours=18):
            ctx.exit(0)

        logger.info(f"Last {backup_type} backup: {last_backup.strftime('%Y-%m-%d %H:%M:%S')} ({format_age(age)})")

        if age < timedelta(hours=18) and not force:
            logger.warning("A backup was made within the last 18 hours.")
            if dry_run:
                logger.info("[DRY RUN] Would prompt for confirmation")
                ctx.exit(0)

            if not click.confirm("Create another backup anyway?", default=False):
                logger.info("Backup cancelled.")
                ctx.exit(0)
    else:
        logger.info(f"No previous {backup_type} backups found.")

    # Create backup
    if restic:
        result = create_restic_backup(dry_run=dry_run)
        backup_type_for_save = "restic"
    elif archive_format == ArchiveFormat.SEVENZ.value:
        result = create_7z_backup(dry_run=dry_run)
        backup_type_for_save = "7z"
    elif archive_format == ArchiveFormat.TAR_GZ.value:
        result = create_backup(dry_run=dry_run)
        backup_type_for_save = "tar.gz"
    else:
        raise click.ClickException("Must specify either --restic or --archive-format")

    if dry_run:
        ctx.exit(0)

    # On successful backup, save job config and update last run
    if result:
        save_job_config(
            source, dest, name,
            use_restic=restic,
            archive_format=archive_format,
            split_size=effective_split_size,
            no_split=no_split,
        )
        update_job_last_run(source, backup_type_for_save)

    ctx.exit(0 if result else 1)


class ConfirmModal(ModalScreen):
    """Modal for confirmation dialogs."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def action_cancel(self) -> None:
        self.dismiss(False)

    CSS = """
    ConfirmModal {
        align: center middle;
    }

    #confirm-dialog {
        width: 50;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $error;
    }

    #confirm-buttons {
        margin-top: 2;
        align: center middle;
    }

    #confirm-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.message)
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", variant="error", id="yes-btn")
                yield Button("No", variant="primary", id="no-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes-btn")


class PlistModal(ModalScreen):
    """Modal for viewing plist content with syntax highlighting."""

    BINDINGS = [Binding("escape", "dismiss_modal", "Close", show=False)]

    def action_dismiss_modal(self) -> None:
        self.dismiss()

    CSS = """
    PlistModal {
        align: center middle;
    }

    #plist-dialog {
        width: 95%;
        height: 90%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #plist-scroll {
        height: 1fr;
        overflow-y: auto;
        overflow-x: auto;
    }

    #plist-buttons {
        margin-top: 1;
        align: center middle;
        height: auto;
    }
    """

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.title_text = title
        self.content = content

    def compose(self) -> ComposeResult:
        from rich.syntax import Syntax

        with Vertical(id="plist-dialog"):
            yield Label(f"[bold]{self.title_text}[/bold]", markup=True)
            with ScrollableContainer(id="plist-scroll"):
                syntax = Syntax(self.content, "xml", theme="monokai", line_numbers=True)
                yield Static(syntax)
            with Horizontal(id="plist-buttons"):
                yield Button("Close", variant="primary", id="close-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class HistoryModal(ModalScreen):
    """Modal for viewing backup history."""

    BINDINGS = [Binding("escape", "dismiss_modal", "Close", show=False)]

    def action_dismiss_modal(self) -> None:
        self.dismiss()

    CSS = """
    HistoryModal {
        align: center middle;
    }

    #history-dialog {
        width: 95%;
        height: 90%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #history-scroll {
        height: 1fr;
        overflow-y: auto;
    }

    #history-buttons {
        margin-top: 1;
        align: center middle;
        height: auto;
    }
    """

    def __init__(self, name: str, dest: Path, source: Path) -> None:
        super().__init__()
        self.job_name = name
        self.dest = dest
        self.source = source

    def compose(self) -> ComposeResult:
        from rich.table import Table
        from rich.text import Text

        content_parts = []

        # Get restic snapshots
        restic_dir = self.dest / self.job_name / "restic"
        password_file = Path.home() / ".config/restic" / f"{self.job_name}-password"

        if restic_dir.exists() and password_file.exists():
            try:
                result = subprocess.run(
                    ["restic", "-r", str(restic_dir), "snapshots", "--json"],
                    capture_output=True, text=True,
                    env={**os.environ, "RESTIC_PASSWORD_FILE": str(password_file)}
                )
                if result.returncode == 0:
                    import json
                    snapshots = json.loads(result.stdout)
                    if snapshots:
                        content_parts.append("[bold cyan]═══ Restic Snapshots ═══[/bold cyan]\n")
                        for snap in sorted(snapshots, key=lambda x: x.get("time", ""), reverse=True)[:20]:
                            time_str = snap.get("time", "")[:19].replace("T", " ")
                            short_id = snap.get("short_id", "")
                            hostname = snap.get("hostname", "")
                            content_parts.append(f"  [green]{short_id}[/green]  {time_str}  [dim]{hostname}[/dim]\n")
                        content_parts.append("\n")
            except Exception:
                pass

        # Get archive files
        archive_patterns = [f"{self.job_name}_*.7z", f"{self.job_name}_*.7z.001", f"{self.job_name}_*.tar.gz"]
        archives = []
        for pattern in archive_patterns:
            archives.extend(self.dest.glob(pattern))

        if archives:
            content_parts.append("[bold yellow]═══ Archive Backups ═══[/bold yellow]\n")
            # Sort by modification time, newest first
            for archive in sorted(archives, key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                mtime = datetime.fromtimestamp(archive.stat().st_mtime)
                size_mb = archive.stat().st_size / (1024 * 1024)
                content_parts.append(f"  [yellow]{archive.name}[/yellow]\n")
                content_parts.append(f"    {mtime.strftime('%Y-%m-%d %H:%M')}  [dim]{size_mb:.1f} MB[/dim]\n")
            content_parts.append("\n")

        if not content_parts:
            content_parts.append("[dim]No backup history found[/dim]")

        content = "".join(content_parts)

        with Vertical(id="history-dialog"):
            yield Label(f"[bold]History: {self.job_name}[/bold]", markup=True)
            with ScrollableContainer(id="history-scroll"):
                yield Static(content, markup=True)
            with Horizontal(id="history-buttons"):
                yield Button("Close", variant="primary", id="close-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class EditJobModal(ModalScreen):
    """Modal for editing a job."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def action_cancel(self) -> None:
        self.dismiss(None)

    CSS = """
    EditJobModal {
        align: center middle;
    }

    #edit-dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #edit-dialog Label {
        margin-top: 1;
    }

    #edit-dialog Input {
        margin-bottom: 1;
    }

    #buttons {
        margin-top: 2;
        align: center middle;
    }

    #buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, job: dict | None = None, defaults: dict | None = None) -> None:
        super().__init__()
        self.job = job or {}
        self.defaults = defaults or {}
        self.is_new = job is None

    def compose(self) -> ComposeResult:
        resolved = resolve_job_config(self.job, self.defaults) if self.job else self.defaults

        with Vertical(id="edit-dialog"):
            yield Label("Name:")
            yield Input(value=self.job.get("name", ""), id="name-input", placeholder="job-name")

            yield Label("Source:")
            yield Input(value=self.job.get("source", ""), id="source-input", placeholder="~/path/to/source")

            dest_value = self.job.get("dest", "")
            default_dest = self.defaults.get("dest", "~/Backups")
            dest_inherited = " [dim](inherited)[/dim]" if not dest_value else ""
            yield Label(f"Dest:{dest_inherited}", id="dest-label", markup=True)
            yield Input(value=dest_value, id="dest-input", placeholder=default_dest)

            yield Static("─── Full Backups ───")
            yield Label("Archive Format:")
            with RadioSet(id="archive-format-radio"):
                current_archive = resolved.get("archive_format", "7z")
                yield RadioButton("None", value=current_archive == "")
                yield RadioButton("7z", value=current_archive == "7z")
                yield RadioButton("tar.gz", value=current_archive == "tar.gz")

            # Full interval - only show job override, not resolved value
            full_interval_value = self.job.get("full_interval_days", "")
            default_full = self.defaults.get("full_interval_days", 7)
            full_inherited = " [dim](inherited)[/dim]" if not full_interval_value else ""
            yield Label(f"Full Backup Interval (days):{full_inherited}", id="full-interval-label", markup=True)
            yield Input(
                value=str(full_interval_value) if full_interval_value else "",
                id="full-interval-input",
                placeholder=str(default_full)
            )

            yield Static("─── Incremental ───")
            yield Checkbox("Enable restic", value=resolved.get("use_restic", False), id="use-restic-checkbox")

            # Restic interval - only show job override, not resolved value
            restic_interval_value = self.job.get("restic_interval_hours", "")
            default_restic = self.defaults.get("restic_interval_hours", 4)
            restic_inherited = " [dim](inherited)[/dim]" if not restic_interval_value else ""
            yield Label(f"Restic Interval (hours):{restic_inherited}", id="restic-interval-label", markup=True)
            yield Input(
                value=str(restic_interval_value) if restic_interval_value else "",
                id="restic-interval-input",
                placeholder=str(default_restic)
            )

            op_vault_value = self.job.get("op_vault", "")
            default_vault = self.defaults.get("op_vault", "")
            vault_inherited = " [dim](inherited)[/dim]" if not op_vault_value and default_vault else ""
            yield Label(f"1Password Vault:{vault_inherited}", id="op-vault-label", markup=True)
            yield Input(value=op_vault_value, id="op-vault-input", placeholder=default_vault if default_vault else "")

            with Horizontal(id="buttons"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Update label to remove (inherited) when user types."""
        label_map = {
            "dest-input": ("dest-label", "Dest:"),
            "full-interval-input": ("full-interval-label", "Full Backup Interval (days):"),
            "restic-interval-input": ("restic-interval-label", "Restic Interval (hours):"),
            "op-vault-input": ("op-vault-label", "1Password Vault:"),
        }
        if event.input.id in label_map:
            label_id, base_text = label_map[event.input.id]
            label = self.query_one(f"#{label_id}", Label)
            if event.value:
                label.update(base_text)
            else:
                label.update(f"{base_text} [dim](inherited)[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            name = self.query_one("#name-input", Input).value.strip()
            source = self.query_one("#source-input", Input).value.strip()
            dest = self.query_one("#dest-input", Input).value.strip()
            op_vault = self.query_one("#op-vault-input", Input).value.strip()

            archive_radio = self.query_one("#archive-format-radio", RadioSet)
            archive_map = {0: "", 1: "7z", 2: "tar.gz"}
            archive_format = archive_map.get(archive_radio.pressed_index, "7z")

            use_restic = self.query_one("#use-restic-checkbox", Checkbox).value

            # Get interval values
            full_interval_str = self.query_one("#full-interval-input", Input).value.strip()
            restic_interval_str = self.query_one("#restic-interval-input", Input).value.strip()

            if not name or not source:
                self.notify("Name and source are required", severity="error")
                return

            if not archive_format and not use_restic:
                self.notify("Must enable at least one backup method", severity="error")
                return

            new_job = {"name": name, "source": source}
            # Only save overrides when different from defaults
            default_dest = self.defaults.get("dest", "")
            if dest and dest != default_dest:
                new_job["dest"] = dest
            if archive_format != self.defaults.get("archive_format", "7z"):
                new_job["archive_format"] = archive_format
            if use_restic != self.defaults.get("use_restic", False):
                new_job["use_restic"] = use_restic
            default_vault = self.defaults.get("op_vault", "")
            if op_vault and op_vault != default_vault:
                new_job["op_vault"] = op_vault

            # Only save intervals if explicitly set and different from defaults
            if full_interval_str:
                try:
                    full_interval = int(full_interval_str)
                    if full_interval != self.defaults.get("full_interval_days", 7):
                        new_job["full_interval_days"] = full_interval
                except ValueError:
                    self.notify("Full interval must be a number", severity="error")
                    return

            if restic_interval_str:
                try:
                    restic_interval = int(restic_interval_str)
                    if restic_interval != self.defaults.get("restic_interval_hours", 4):
                        new_job["restic_interval_hours"] = restic_interval
                except ValueError:
                    self.notify("Restic interval must be a number", severity="error")
                    return

            self.dismiss(new_job)
        else:
            self.dismiss(None)


class EditDefaultsModal(ModalScreen):
    """Modal for editing defaults."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def action_cancel(self) -> None:
        self.dismiss(None)

    CSS = """
    EditDefaultsModal {
        align: center middle;
    }

    #defaults-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #defaults-dialog Label {
        margin-top: 1;
    }

    #defaults-dialog Input {
        margin-bottom: 1;
    }

    #defaults-buttons {
        margin-top: 2;
        align: center middle;
    }

    #defaults-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, defaults: dict) -> None:
        super().__init__()
        self.defaults = defaults

    def compose(self) -> ComposeResult:
        with Vertical(id="defaults-dialog"):
            yield Label("Default Destination:")
            yield Input(value=self.defaults.get("dest", "~/Backups"), id="dest-input")

            yield Static("─── Full Backups ───")
            yield Label("Archive Format:")
            with RadioSet(id="archive-format-radio"):
                current_archive = self.defaults.get("archive_format", "7z")
                yield RadioButton("None", value=current_archive == "")
                yield RadioButton("7z", value=current_archive == "7z")
                yield RadioButton("tar.gz", value=current_archive == "tar.gz")

            yield Label("Full Backup Interval (days):")
            yield Input(value=str(self.defaults.get("full_interval_days", 7)), id="full-interval-input")

            yield Static("─── Incremental ───")
            yield Checkbox("Enable restic", value=self.defaults.get("use_restic", False), id="use-restic-checkbox")

            yield Label("Restic Interval (hours):")
            yield Input(value=str(self.defaults.get("restic_interval_hours", 4)), id="restic-interval-input")

            yield Label("1Password Vault (optional):")
            yield Input(value=self.defaults.get("op_vault", ""), id="op-vault-input", placeholder="Vault name for restic passwords")

            with Horizontal(id="defaults-buttons"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            dest = self.query_one("#dest-input", Input).value.strip()
            restic_interval = self.query_one("#restic-interval-input", Input).value.strip()
            full_interval = self.query_one("#full-interval-input", Input).value.strip()
            op_vault = self.query_one("#op-vault-input", Input).value.strip()

            archive_radio = self.query_one("#archive-format-radio", RadioSet)
            archive_map = {0: "", 1: "7z", 2: "tar.gz"}
            archive_format = archive_map.get(archive_radio.pressed_index, "7z")

            use_restic = self.query_one("#use-restic-checkbox", Checkbox).value

            try:
                restic_hours = int(restic_interval)
                full_days = int(full_interval)
            except ValueError:
                self.notify("Intervals must be integers", severity="error")
                return

            new_defaults = {
                "dest": dest or "~/Backups",
                "archive_format": archive_format,
                "use_restic": use_restic,
                "restic_interval_hours": restic_hours,
                "full_interval_days": full_days,
                "op_vault": op_vault,
            }

            self.dismiss(new_defaults)
        else:
            self.dismiss(None)


class SnapbackApp(App):
    """Textual app for managing snapback jobs."""

    TITLE = "Snapback"
    SUB_TITLE = "Backup Configuration"

    CSS = """
    Screen {
        align: center middle;
    }

    #jobs-table {
        height: 1fr;
        margin: 1 2;
    }

    Footer {
        background: $primary-background;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("a", "add_job", "Add"),
        Binding("e", "edit_job", "Edit"),
        Binding("d", "delete_job", "Delete"),
        Binding("i", "install_daemon", "Install"),
        Binding("u", "uninstall_daemon", "Uninstall"),
        Binding("r", "run_now", "Run"),
        Binding("s", "edit_defaults", "Defaults"),
        Binding("p", "view_plist", "Plist"),
        Binding("h", "view_history", "History"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._running_backups: set[str] = set()  # Track multiple concurrent backups

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="jobs-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("NAME", "SOURCE", "DEST", "FORMAT", "DAEMON", "LAST RUN")
        self.refresh_jobs()

    def refresh_jobs(self) -> None:
        """Refresh the jobs table from manifest."""
        table = self.query_one(DataTable)
        table.clear()

        manifest = load_manifest()
        state = load_state()
        defaults = manifest.get("defaults", {})

        for job in manifest.get("jobs", []):
            resolved = resolve_job_config(job, defaults)
            source = job.get("source", "")
            key = get_job_key(Path(source)) if source else ""
            job_state = state.get(key, {})

            # Daemon status
            daemon_plist = job_state.get("daemon_plist", "")
            daemon_status = "●" if daemon_plist and Path(daemon_plist).exists() else "○"

            # Last run - show running indicator if this job is currently running
            job_name = resolved.get("name", "")
            if job_name in self._running_backups:
                last_run = "⏳ running..."
            else:
                last_runs = job_state.get("last_runs", {})
                last_run = "never"
                if last_runs:
                    latest = max(last_runs.values())
                    try:
                        dt = datetime.fromisoformat(latest)
                        delta = datetime.now() - dt
                        if delta.days > 0:
                            last_run = f"{delta.days}d ago"
                        elif delta.seconds >= 3600:
                            last_run = f"{delta.seconds // 3600}h ago"
                        else:
                            last_run = f"{delta.seconds // 60}m ago"
                    except ValueError:
                        last_run = latest

            # Build format display
            archive_fmt = resolved.get("archive_format", "")
            use_restic = resolved.get("use_restic", False)
            if archive_fmt and use_restic:
                format_display = f"{archive_fmt}+restic"
            elif archive_fmt:
                format_display = archive_fmt
            elif use_restic:
                format_display = "restic"
            else:
                format_display = "none"

            # Shorten paths by replacing home dir with ~
            home = str(Path.home())
            display_source = source.replace(home, "~") if source.startswith(home) else source
            display_dest = resolved.get("dest", "").replace(home, "~")

            table.add_row(
                resolved.get("name", ""),
                display_source,
                display_dest,
                format_display,
                daemon_status,
                last_run,
            )

    def action_quit(self) -> None:
        self.exit()

    def action_add_job(self) -> None:
        manifest = load_manifest()
        defaults = manifest.get("defaults", {})
        self.push_screen(EditJobModal(job=None, defaults=defaults), self._on_job_edited)

    def action_edit_job(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            self.notify("No job selected", severity="warning")
            return

        manifest = load_manifest()
        jobs = manifest.get("jobs", [])
        if table.cursor_row >= len(jobs):
            return

        job = jobs[table.cursor_row]
        defaults = manifest.get("defaults", {})
        self.push_screen(EditJobModal(job=job, defaults=defaults), self._on_job_edited)

    def _on_job_edited(self, result: dict | None) -> None:
        if result is None:
            logger.debug("TUI: Job edit cancelled")
            return

        manifest = load_manifest()

        # Check if editing existing or adding new
        existing_idx = None
        for i, job in enumerate(manifest.get("jobs", [])):
            if job.get("name") == result.get("name"):
                existing_idx = i
                break

        job_name = result.get('name', 'unknown')
        if existing_idx is not None:
            manifest["jobs"][existing_idx] = result
            logger.info(f"TUI: Updated job '{job_name}': {result}")
        else:
            if "jobs" not in manifest:
                manifest["jobs"] = []
            manifest["jobs"].append(result)
            logger.info(f"TUI: Added new job '{job_name}': {result}")

        save_manifest(manifest)
        self.refresh_jobs()
        self.notify(f"Saved job: {job_name}")

    def action_delete_job(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            self.notify("No job selected", severity="warning")
            return

        manifest = load_manifest()
        jobs = manifest.get("jobs", [])
        if table.cursor_row >= len(jobs):
            return

        job = jobs[table.cursor_row]
        job_name = job.get("name", "unknown")

        self.push_screen(
            ConfirmModal(f"Delete job '{job_name}'?"),
            lambda result: self._on_delete_confirmed(result, table.cursor_row)
        )

    def _on_delete_confirmed(self, confirmed: bool, index: int) -> None:
        if not confirmed:
            logger.debug("TUI: Job deletion cancelled")
            return

        manifest = load_manifest()
        jobs = manifest.get("jobs", [])
        if index < len(jobs):
            deleted = jobs.pop(index)
            manifest["jobs"] = jobs
            save_manifest(manifest)
            self.refresh_jobs()
            job_name = deleted.get('name', 'unknown')
            logger.info(f"TUI: Deleted job '{job_name}'")
            self.notify(f"Deleted job: {job_name}")

    def action_install_daemon(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            self.notify("No job selected", severity="warning")
            return

        manifest = load_manifest()
        jobs = manifest.get("jobs", [])
        if table.cursor_row >= len(jobs):
            return

        job = jobs[table.cursor_row]
        defaults = manifest.get("defaults", {})
        resolved = resolve_job_config(job, defaults)

        source = Path(resolved.get("source", "")).expanduser()
        dest = Path(resolved.get("dest", "")).expanduser()
        name = resolved.get("name", "")

        if not source.exists():
            self.notify(f"Source does not exist: {source}", severity="error")
            return

        # Call the daemon install logic
        try:
            archive_format = resolved.get("archive_format", ArchiveFormat.SEVENZ.value)
            use_restic = resolved.get("use_restic", False)
            op_vault = resolved.get("op_vault")

            self.notify(f"Installing daemon for {name}...")
            # Capture values to avoid closure issues
            self.call_later(
                lambda s=source, d=dest, n=name, r=use_restic, a=archive_format, o=op_vault:
                    self._do_daemon_install(s, d, n, r, a, o)
            )
        except Exception as e:
            self.notify(f"Failed to install daemon: {e}", severity="error")

    def _do_daemon_install(self, source: Path, dest: Path, name: str,
                           use_restic: bool, archive_format: str, op_vault: str | None) -> None:
        """Run daemon install in background."""
        logger.info(f"TUI: Installing daemon for '{name}'")
        cmd = [
            sys.executable, __file__, "daemon", "install",
            CLIFlags.SOURCE, str(source),
            CLIFlags.DEST, str(dest),
            CLIFlags.NAME, name,
        ]
        if use_restic:
            cmd.append(CLIFlags.RESTIC)
        else:
            cmd.append(CLIFlags.NO_RESTIC)
        if archive_format and archive_format != ArchiveFormat.NONE.value:
            cmd.extend([CLIFlags.ARCHIVE_FORMAT, archive_format])
        else:
            cmd.extend([CLIFlags.ARCHIVE_FORMAT, "none"])
        if op_vault:
            cmd.extend([CLIFlags.ONEPASSWORD_VAULT, op_vault])

        logger.debug(f"TUI: Daemon install command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            logger.info(f"TUI: Daemon installed successfully for '{name}'")
            self.notify(f"Daemon installed for {name}")
        else:
            logger.error(f"TUI: Daemon install failed for '{name}': {result.stderr}")
            self.notify(f"Daemon install failed. See logs/snapback.log", severity="error")

        self.refresh_jobs()

    def action_uninstall_daemon(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            self.notify("No job selected", severity="warning")
            return

        manifest = load_manifest()
        jobs = manifest.get("jobs", [])
        if table.cursor_row >= len(jobs):
            return

        job = jobs[table.cursor_row]
        name = job.get("name", "")

        self.push_screen(
            ConfirmModal(f"Uninstall daemon for '{name}'?"),
            lambda result: self._on_uninstall_confirmed(result, name)
        )

    def _on_uninstall_confirmed(self, confirmed: bool, name: str) -> None:
        if not confirmed:
            logger.debug(f"TUI: Daemon uninstall cancelled for '{name}'")
            return

        logger.info(f"TUI: Uninstalling daemon for '{name}'")
        import subprocess

        cmd = [sys.executable, __file__, "daemon", "uninstall", "--name", name]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            logger.info(f"TUI: Daemon uninstalled successfully for '{name}'")
            self.notify(f"Daemon uninstalled for {name}")
        else:
            logger.error(f"TUI: Daemon uninstall failed for '{name}': {result.stderr}")
            self.notify(f"Uninstall failed. See logs/snapback.log", severity="error")

        self.refresh_jobs()

    def action_run_now(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            self.notify("No job selected", severity="warning")
            return

        manifest = load_manifest()
        jobs = manifest.get("jobs", [])
        if table.cursor_row >= len(jobs):
            return

        job = jobs[table.cursor_row]
        defaults = manifest.get("defaults", {})
        resolved = resolve_job_config(job, defaults)

        name = resolved.get("name", "")
        self._running_backups.add(name)
        self.refresh_jobs()  # Show "running..." indicator immediately
        self.notify(f"Running backup for {name}...", timeout=10)
        self._run_backup_worker(resolved)

    @work(thread=True)  # Allow multiple concurrent backups
    def _run_backup_worker(self, job: dict) -> None:
        """Run backup in background thread."""
        import traceback
        name = job.get("name", "unknown")
        try:
            source = Path(job.get("source", "")).expanduser()
            dest = Path(job.get("dest", "")).expanduser()
            archive_format = job.get("archive_format", ArchiveFormat.SEVENZ.value)
            use_restic = job.get("use_restic", False)

            logger.info(f"TUI: Starting backup for job '{name}'")
            logger.debug(f"TUI: Job config: source={source}, dest={dest}, archive_format={archive_format}, use_restic={use_restic}")

            cmd = [
                sys.executable, __file__,
                CLIFlags.SOURCE, str(source),
                CLIFlags.DEST, str(dest),
                CLIFlags.NAME, name,
                CLIFlags.FORCE,  # Skip recency prompt - user explicitly clicked Run
            ]

            # Add format flags using new schema
            if use_restic:
                cmd.append(CLIFlags.RESTIC)
            if archive_format and archive_format != ArchiveFormat.NONE.value:
                cmd.extend([CLIFlags.ARCHIVE_FORMAT, archive_format])
            else:
                # Explicitly disable archive when not wanted (CLI defaults to 7z)
                cmd.extend([CLIFlags.ARCHIVE_FORMAT, "none"])

            # Note: Don't pass op_vault - backup uses password file, not 1Password at runtime

            logger.debug(f"TUI: Running command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)  # 30 min timeout

            if result.returncode == 0:
                logger.info(f"TUI: Backup completed successfully for job '{name}'")
            else:
                logger.error(f"TUI: Backup failed for job '{name}': {result.stderr}")

            # Use call_from_thread to safely update UI from worker thread
            self.call_from_thread(self._on_backup_complete, result.returncode == 0, name, result.stderr)
        except subprocess.TimeoutExpired:
            logger.error(f"TUI: Backup timed out for job '{name}' (30 min limit)")
            self.call_from_thread(self._on_backup_complete, False, name, "Backup timed out after 30 minutes")
        except Exception as e:
            error_msg = str(e)
            full_traceback = traceback.format_exc()
            logger.error(f"TUI: Exception running backup for job '{name}': {error_msg}\n{full_traceback}")
            self.call_from_thread(self._on_backup_complete, False, name, error_msg)

    def _on_backup_complete(self, success: bool, name: str, error: str) -> None:
        """Handle backup completion (called from main thread)."""
        self._running_backups.discard(name)
        if success:
            self.notify(f"Backup completed for {name}")
        else:
            # Show brief error in UI, full error is in logs
            self.notify(f"Backup failed for {name}. See logs/snapback.log", severity="error")
            logger.error(f"TUI: Backup failed for {name}: {error}")
        self.refresh_jobs()

    def action_view_plist(self) -> None:
        """View the daemon plist for the selected job."""
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            self.notify("No job selected", severity="warning")
            return

        manifest = load_manifest()
        jobs = manifest.get("jobs", [])
        if table.cursor_row >= len(jobs):
            return

        job = jobs[table.cursor_row]
        source = job.get("source", "")
        name = job.get("name", "")
        key = get_job_key(Path(source)) if source else ""

        # Check if daemon is installed
        state = load_state()
        job_state = state.get(key, {})
        plist_path = job_state.get("daemon_plist", "")

        if not plist_path or not Path(plist_path).exists():
            self.notify(f"No daemon installed for '{name}'", severity="warning")
            return

        # Read and display the plist
        try:
            content = Path(plist_path).read_text()
            self.push_screen(PlistModal(f"Daemon: {name}", content))
        except Exception as e:
            self.notify(f"Error reading plist: {e}", severity="error")

    def action_view_history(self) -> None:
        """View backup history for the selected job."""
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            self.notify("No job selected", severity="warning")
            return

        manifest = load_manifest()
        jobs = manifest.get("jobs", [])
        if table.cursor_row >= len(jobs):
            return

        job = jobs[table.cursor_row]
        defaults = manifest.get("defaults", {})
        resolved = resolve_job_config(job, defaults)

        name = resolved.get("name", "")
        source = Path(resolved.get("source", "")).expanduser()
        dest = Path(resolved.get("dest", "")).expanduser()

        self.push_screen(HistoryModal(name, dest, source))

    def action_edit_defaults(self) -> None:
        manifest = load_manifest()
        defaults = manifest.get("defaults", {})
        self.push_screen(EditDefaultsModal(defaults), self._on_defaults_edited)

    def _on_defaults_edited(self, result: dict | None) -> None:
        if result is None:
            logger.debug("TUI: Defaults edit cancelled")
            return

        logger.info(f"TUI: Updated defaults: {result}")
        manifest = load_manifest()
        manifest["defaults"] = result
        save_manifest(manifest)
        self.refresh_jobs()
        self.notify("Defaults saved")


@cli.group()
def daemon():
    """Manage the backup daemon (macOS LaunchAgent)."""
    pass


@daemon.command("install")
@click.option(CLIFlags.SOURCE, "-s", type=click.Path(exists=True, path_type=Path), required=True,
              help="Source directory to backup")
@click.option(CLIFlags.DEST, "-d", type=click.Path(path_type=Path), required=True,
              help="Destination directory for backups")
@click.option(CLIFlags.NAME, "-N", required=True, help="Name for this backup")
@click.option(CLIFlags.RESTIC + "/" + CLIFlags.NO_RESTIC, default=None,
              help="Enable/disable restic incremental backup")
@click.option(CLIFlags.ARCHIVE_FORMAT, type=click.Choice(["7z", "tar.gz", "none"]), default=None,
              help="Archive format for full backups (7z, tar.gz, or none)")
@click.option(CLIFlags.RESTIC_INTERVAL, type=int, default=DEFAULT_RESTIC_INTERVAL_HOURS,
              help=f"Hours between restic backups (default: {DEFAULT_RESTIC_INTERVAL_HOURS})")
@click.option(CLIFlags.FULL_INTERVAL, type=int, default=DEFAULT_FULL_INTERVAL_DAYS,
              help=f"Days between full archive backups (default: {DEFAULT_FULL_INTERVAL_DAYS})")
@click.option("--1password", "use_1password", is_flag=True,
              help="Backup restic password to 1Password at install time (daemon uses local password file)")
@click.option(CLIFlags.ONEPASSWORD_VAULT, "op_vault", type=str, help="1Password vault name (prompts if not specified)")
def daemon_install(source, dest, name, restic, archive_format, restic_interval, full_interval, use_1password, op_vault):
    """Install and start the backup daemon."""
    setup_logging()

    source = source.expanduser().resolve()
    dest = dest.expanduser().resolve()
    plist_path = get_plist_path(name)
    log_path = get_log_path(name)

    # Validate paths
    if not source.exists():
        raise click.ClickException(f"Source directory does not exist: {source}")

    # Check for existing job config (for re-installs)
    existing_job = load_job_config(source)
    existing_vault = existing_job.get("options", {}).get("op_vault") if existing_job else None
    existing_opts = existing_job.get("options", {}) if existing_job else {}
    existing_use_restic = existing_opts.get("use_restic")
    existing_archive = existing_opts.get("archive_format")

    # Handle restic/archive selection
    use_restic = restic
    if use_restic is None and archive_format is None:
        if sys.stdout.isatty():
            import questionary

            # Check if we have an existing config to suggest
            if existing_use_restic is not None or existing_archive is not None:
                existing_desc = []
                if existing_use_restic:
                    existing_desc.append("restic")
                if existing_archive:
                    existing_desc.append(existing_archive)
                existing_mode_str = " + ".join(existing_desc) if existing_desc else "7z"

                if questionary.confirm(
                    f"Use previously configured mode ({existing_mode_str})?",
                    default=True
                ).ask():
                    use_restic = existing_use_restic or False
                    archive_format = existing_archive or ArchiveFormat.SEVENZ.value
                    logger.info(f"Using existing config: restic={use_restic}, archive={archive_format}")

            if use_restic is None:
                mode = questionary.select(
                    "Select backup mode:",
                    choices=[
                        questionary.Choice("restic + 7z - incremental + weekly archive (recommended)", value="restic_7z"),
                        questionary.Choice("restic only - incremental backups only (space efficient)", value="restic"),
                        questionary.Choice("7z only - compressed archives only (easy to restore)", value="7z"),
                    ],
                    instruction="(↑↓ to move, Enter to select)",
                ).ask()

                if mode is None:
                    raise click.ClickException("Mode selection cancelled")

                # Map selection to new schema
                if mode == "restic_7z":
                    use_restic = True
                    archive_format = ArchiveFormat.SEVENZ.value
                elif mode == "restic":
                    use_restic = True
                    archive_format = ArchiveFormat.NONE.value
                else:  # 7z
                    use_restic = False
                    archive_format = ArchiveFormat.SEVENZ.value
        else:
            # Non-interactive: default to restic + 7z
            use_restic = True
            archive_format = ArchiveFormat.SEVENZ.value

    # Apply defaults if still None
    if use_restic is None:
        use_restic = True
    if archive_format is None:
        archive_format = ArchiveFormat.SEVENZ.value
    if archive_format == "none":
        archive_format = ArchiveFormat.NONE.value

    # Handle 1Password backup of restic password
    selected_vault = None
    if use_1password:
        if not check_1password_cli():
            raise click.ClickException(
                "1Password CLI not found or not authenticated.\n"
                "Install: https://developer.1password.com/docs/cli/get-started/\n"
                "Then run: op signin"
            )
        if op_vault:
            # Explicit vault from CLI
            selected_vault = op_vault
        elif existing_vault:
            # Re-use previously configured vault
            import questionary
            if questionary.confirm(
                f"Use previously configured vault '{existing_vault}'?",
                default=True
            ).ask():
                selected_vault = existing_vault
                logger.info(f"Using existing vault: {selected_vault}")
            else:
                # Let them pick a new one
                selected_vault = setup_1password_vault_interactive()
                if not selected_vault:
                    raise click.ClickException("1Password vault selection cancelled")
        else:
            # Interactive vault selection at install time
            selected_vault = setup_1password_vault_interactive()
            if not selected_vault:
                raise click.ClickException("1Password vault selection cancelled")

    # Ensure LaunchAgents directory exists
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Unload if already loaded
    if plist_path.exists():
        logger.info("Unloading existing daemon...")
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    # Find snapback executable
    snapback_path = find_snapback_path()

    # Run hourly - the backup logic will skip if not needed based on restic_interval/full_interval
    interval_seconds = 3600

    # Build mode args for plist using new schema
    mode_args_list = []
    if use_restic:
        mode_args_list.append(f"        <string>{CLIFlags.RESTIC}</string>")
        mode_args_list.append(f"        <string>{CLIFlags.RESTIC_INTERVAL}</string>")
        mode_args_list.append(f"        <string>{restic_interval}</string>")
    if archive_format and archive_format != ArchiveFormat.NONE.value:
        mode_args_list.append(f"        <string>{CLIFlags.ARCHIVE_FORMAT}</string>")
        mode_args_list.append(f"        <string>{archive_format}</string>")
        mode_args_list.append(f"        <string>{CLIFlags.FULL_INTERVAL}</string>")
        mode_args_list.append(f"        <string>{full_interval}</string>")
    mode_args = "\n" + "\n".join(mode_args_list) if mode_args_list else ""

    # Generate plist content (no 1password flags - daemon uses password file)
    plist_content = PLIST_TEMPLATE.format(
        version=__version__,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        label=get_daemon_label(name),
        name=name,
        snapback_path=snapback_path,
        source=str(source),
        dest=str(dest),
        log_path=str(log_path),
        home=str(Path.home()),
        interval_seconds=interval_seconds,
        mode_args=mode_args,
    )

    # Write plist
    logger.info(f"Installing plist to {plist_path}")
    plist_path.write_text(plist_content)

    # Load the daemon
    logger.info("Loading daemon...")
    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise click.ClickException(f"Failed to load daemon: {result.stderr}")

    logger.success(f"Daemon '{name}' installed and running")
    logger.info("  Checks: every hour (and on login/wake)")
    if use_restic and archive_format:
        logger.info(f"  Mode: restic + {archive_format} (restic if >{restic_interval}h old, {archive_format} if >{full_interval}d old)")
    elif use_restic:
        logger.info(f"  Mode: restic only (if >{restic_interval}h since last backup)")
    else:
        logger.info(f"  Mode: {archive_format} only (if >{full_interval}d since last backup)")
    logger.info(f"  Logs: {log_path}")
    logger.info(f"  Plist: {plist_path}")

    # Backup restic password to 1Password if requested
    op_item_name = None
    if use_1password and selected_vault:
        # Ensure password file exists
        password_file = Path.home() / ".config/restic" / f"{name}-password"

        if not password_file.exists():
            # Create the password
            password_file.parent.mkdir(parents=True, exist_ok=True)
            password = secrets.token_urlsafe(32)
            password_file.write_text(password)
            password_file.chmod(0o600)
            logger.info(f"Created password file: {password_file}")
        else:
            password = password_file.read_text().strip()

        # Store in 1Password
        op_item_name = f"Snapback: {name} restic password"
        if store_password_in_1password(name, password, selected_vault):
            logger.success("Password backed up to 1Password")
        else:
            logger.warning("Failed to backup password to 1Password (password file still created)")
            op_item_name = None  # Don't save if it failed

    # Save job config with 1Password info
    save_job_config(
        source, dest, name,
        use_restic=use_restic,
        archive_format=archive_format,
        restic_interval=restic_interval,
        full_interval=full_interval,
        op_vault=selected_vault,
        op_item=op_item_name,
        daemon_plist=str(plist_path),
        daemon_label=get_daemon_label(name),
    )


@daemon.command("uninstall")
@click.option("--name", "-N", required=True, help="Name of the backup daemon")
def daemon_uninstall(name):
    """Stop and remove the backup daemon."""
    setup_logging()

    plist_path = get_plist_path(name)

    if not plist_path.exists():
        logger.warning(f"Daemon '{name}' is not installed")
        return

    # Unload the daemon
    logger.info("Stopping daemon...")
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    # Remove the plist
    logger.info(f"Removing {plist_path}")
    plist_path.unlink()

    logger.success(f"Daemon '{name}' uninstalled")


@daemon.command("status")
@click.option("--name", "-N", required=True, help="Name of the backup daemon")
def daemon_status(name):
    """Check daemon status."""
    setup_logging()

    plist_path = get_plist_path(name)
    log_path = get_log_path(name)
    label = get_daemon_label(name)

    # Check if installed
    installed = plist_path.exists()

    # Check if running
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    running = label in result.stdout

    logger.info(f"Daemon: {name}")
    logger.info(f"  Installed: {'Yes' if installed else 'No'}")
    logger.info(f"  Running: {'Yes' if running else 'No'}")

    if installed:
        logger.info(f"  Plist: {plist_path}")

        # Try to read interval from plist
        try:
            import re
            content = plist_path.read_text()
            interval_match = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", content)
            if interval_match:
                interval_seconds = int(interval_match.group(1))
                if interval_seconds == 3600:
                    logger.info("  Checks: every hour")
                else:
                    logger.info(f"  Checks: every {interval_seconds // 3600}h")
        except Exception:
            pass

    if log_path.exists():
        size = log_path.stat().st_size
        logger.info(f"  Log file: {log_path} ({format_size(size)})")


@daemon.command("logs")
@click.option("--name", "-N", required=True, help="Name of the backup daemon")
@click.option("--lines", "-n", default=50, help="Number of lines to show")
def daemon_logs(name, lines):
    """Show recent daemon logs."""
    setup_logging()

    log_path = get_log_path(name)

    if not log_path.exists():
        raise click.ClickException(f"Log file not found: {log_path}")

    logger.info(f"Last {lines} lines of {log_path}:\n")
    with open(log_path) as f:
        all_lines = f.readlines()
        for line in all_lines[-lines:]:
            print(line.rstrip())


def generate_plist_content(job_name: str, job: dict) -> str:
    """Generate plist content from job config."""
    opts = job.get("options", {})
    source_key = job.get("source", "")

    # Build mode args using new schema
    use_restic = opts.get("use_restic", True)
    archive_format = opts.get("archive_format", ArchiveFormat.SEVENZ.value)
    restic_interval = opts.get("restic_interval", DEFAULT_RESTIC_INTERVAL_HOURS)
    full_interval = opts.get("full_interval", DEFAULT_FULL_INTERVAL_DAYS)

    mode_args_list = []
    if use_restic:
        mode_args_list.append(f"        <string>{CLIFlags.RESTIC}</string>")
        mode_args_list.append(f"        <string>{CLIFlags.RESTIC_INTERVAL}</string>")
        mode_args_list.append(f"        <string>{restic_interval}</string>")
    if archive_format and archive_format != ArchiveFormat.NONE.value:
        mode_args_list.append(f"        <string>{CLIFlags.ARCHIVE_FORMAT}</string>")
        mode_args_list.append(f"        <string>{archive_format}</string>")
        mode_args_list.append(f"        <string>{CLIFlags.FULL_INTERVAL}</string>")
        mode_args_list.append(f"        <string>{full_interval}</string>")
    mode_args = "\n" + "\n".join(mode_args_list) if mode_args_list else ""

    return PLIST_TEMPLATE.format(
        version=__version__,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        label=get_daemon_label(job_name),
        name=job_name,
        snapback_path=find_snapback_path(),
        source=source_key,
        dest=job.get("dest", ""),
        log_path=str(get_log_path(job_name)),
        home=str(Path.home()),
        interval_seconds=3600,
        mode_args=mode_args,
    )


@daemon.command("update")
@click.option("--yes", "-y", is_flag=True, help="Skip selection and confirmation prompts (update all outdated)")
def daemon_update(yes):
    """Update daemon plists to the latest version.

    Shows a multi-select list of outdated daemons, then displays a diff
    for each selected one and asks for confirmation before applying.
    """
    setup_logging()
    import difflib
    import questionary
    from rich.syntax import Syntax
    from rich.panel import Panel

    jobs = load_jobs()

    # Get launchctl status once
    launchctl_result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)

    # Collect outdated daemons
    outdated = []
    for source_key, job in jobs.items():
        job_name = job.get("name", "unnamed")
        found_plist = find_plist_path(job_name)
        if not found_plist:
            continue
        plist_version = get_plist_version(found_plist)
        if plist_version != __version__:
            outdated.append({
                "source_key": source_key,
                "job": job,
                "job_name": job_name,
                "found_plist": found_plist,
                "plist_version": plist_version,
            })

    if not outdated:
        console.print("[green]All daemons are up to date.[/green]")
        return

    # Let user select which to update (unless --yes)
    if yes:
        selected_names = [d["job_name"] for d in outdated]
    else:
        choices = [
            questionary.Choice(
                title=f"{d['job_name']} ({d['plist_version'] or 'no version'} → {__version__})",
                value=d["job_name"],
                checked=True,
            )
            for d in outdated
        ]
        selected_names = questionary.checkbox(
            "Select daemons to update:",
            choices=choices,
        ).ask()

        if selected_names is None:  # User cancelled
            console.print("[dim]Cancelled.[/dim]")
            return

        if not selected_names:
            console.print("[dim]No daemons selected.[/dim]")
            return

    # Filter to selected
    selected = [d for d in outdated if d["job_name"] in selected_names]

    updated = 0
    skipped = 0

    for daemon_info in selected:
        job_name = daemon_info["job_name"]
        job = daemon_info["job"]
        found_plist = daemon_info["found_plist"]
        plist_version = daemon_info["plist_version"]
        source_key = daemon_info["source_key"]

        # Read current plist
        old_content = found_plist.read_text()

        # Generate new plist
        new_content = generate_plist_content(job_name, job)

        # Determine new path
        new_plist_path = get_plist_path(job_name)
        old_label = found_plist.stem
        new_label = get_daemon_label(job_name)

        # Show header
        console.print(f"\n[bold cyan]{'─' * 60}[/bold cyan]")
        console.print(f"[bold]{job_name}[/bold]: {plist_version or 'no version'} → {__version__}")
        if found_plist != new_plist_path:
            console.print(f"[dim]Path: {found_plist.name} → {new_plist_path.name}[/dim]")

        # Generate and show diff
        diff = list(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=found_plist.name,
            tofile=new_plist_path.name,
        ))

        if diff:
            diff_text = ''.join(diff)
            syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
            console.print(Panel(syntax, title="[bold]Changes[/bold]", border_style="dim"))
        else:
            console.print("[dim]No content changes (only metadata)[/dim]")

        # Ask for confirmation (unless --yes)
        if not yes:
            if not click.confirm("Apply this update?", default=True):
                logger.info(f"Skipped {job_name}")
                skipped += 1
                continue

        # Check if daemon is running
        was_running = old_label in launchctl_result.stdout

        # Unload old daemon
        if was_running:
            subprocess.run(["launchctl", "unload", str(found_plist)], capture_output=True)

        # Remove old plist if different path
        if found_plist != new_plist_path and found_plist.exists():
            found_plist.unlink()

        # Write new plist
        new_plist_path.write_text(new_content)

        # Update job config
        save_job_config(
            Path(source_key), Path(job.get("dest", "")), job_name,
            daemon_plist=str(new_plist_path),
            daemon_label=new_label,
        )

        # Reload daemon
        result = subprocess.run(["launchctl", "load", str(new_plist_path)], capture_output=True, text=True)
        if result.returncode == 0:
            logger.success(f"Updated and restarted {job_name}")
        else:
            logger.warning(f"Updated but failed to reload: {result.stderr}")

        updated += 1

    console.print(f"\n[bold]Done:[/bold] {updated} updated, {skipped} skipped")


@daemon.command("plist")
@click.option("--name", "-N", help="Show plist for specific daemon (shows all if not specified)")
@click.option("--raw", is_flag=True, help="Output raw plist content without formatting")
def daemon_plist(name, raw):
    """Show plist files for installed daemons.

    Without --name, shows all installed snapback daemons.
    With --name, shows the plist for that specific daemon.
    """
    setup_logging()
    from rich.syntax import Syntax
    from rich.panel import Panel

    launch_agents_dir = Path.home() / "Library/LaunchAgents"

    if name:
        # Show specific daemon
        plist_path = get_plist_path(name)
        if not plist_path.exists():
            raise click.ClickException(f"Daemon '{name}' is not installed (no plist at {plist_path})")

        content = plist_path.read_text()
        if raw:
            print(content)
        else:
            syntax = Syntax(content, "xml", theme="monokai", line_numbers=True)
            console.print(Panel(syntax, title=f"[bold]{plist_path.name}[/bold]", subtitle=str(plist_path)))
    else:
        # Show all snapback daemons - check both jobs.json and filesystem
        plist_files_set = set()

        # Check jobs.json for stored plist paths
        jobs = load_jobs()
        for job in jobs.values():
            stored_plist = job.get("options", {}).get("daemon_plist")
            if stored_plist and Path(stored_plist).exists():
                plist_files_set.add(Path(stored_plist))

        # Also check current and legacy namespaces in filesystem
        plist_files_set.update(launch_agents_dir.glob(f"{DAEMON_NAMESPACE}.*.plist"))
        plist_files_set.update(launch_agents_dir.glob("com.snapback.*.plist"))

        plist_files = sorted(plist_files_set)

        if not plist_files:
            logger.info("No snapback daemons installed.")
            logger.info("Install with: snapback daemon install --source <path> --dest <path> --name <name>")
            return

        logger.info(f"Found {len(plist_files)} installed daemon(s):\n")

        for plist_path in plist_files:
            content = plist_path.read_text()

            if raw:
                print(f"=== {plist_path.name} ===")
                print(content)
                print()
            else:
                syntax = Syntax(content, "xml", theme="monokai", line_numbers=True)
                console.print(Panel(syntax, title=f"[bold]{plist_path.name}[/bold]", subtitle=str(plist_path)))
                console.print()


# =============================================================================
# Jobs Management
# =============================================================================

@cli.command("config")
def show_config():
    """Show current configuration with rich formatting."""
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text

    manifest = load_manifest()
    state = load_state()
    defaults = manifest.get("defaults", {})

    # Defaults panel
    defaults_text = Text()
    defaults_text.append("Destination: ", style="bold")
    defaults_text.append(f"{defaults.get('dest', '~/Backups')}\n")

    archive_fmt = defaults.get("archive_format", "7z")
    use_restic = defaults.get("use_restic", False)
    defaults_text.append("Archive Format: ", style="bold")
    defaults_text.append(f"{archive_fmt or 'none'}\n")
    defaults_text.append("Restic: ", style="bold")
    defaults_text.append(f"{'enabled' if use_restic else 'disabled'}\n")

    defaults_text.append("Full Backup Interval: ", style="bold")
    defaults_text.append(f"{defaults.get('full_interval_days', 7)} days\n")
    defaults_text.append("Restic Interval: ", style="bold")
    defaults_text.append(f"{defaults.get('restic_interval_hours', 4)} hours\n")

    op_vault = defaults.get("op_vault", "")
    if op_vault:
        defaults_text.append("1Password Vault: ", style="bold")
        defaults_text.append(f"{op_vault}\n")

    console.print(Panel(defaults_text, title="[bold]Defaults[/bold]", border_style="blue"))

    # Jobs table
    table = Table(title="Jobs", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan")
    table.add_column("Source")
    table.add_column("Dest")
    table.add_column("Format")
    table.add_column("Daemon")
    table.add_column("Last Run")

    for job in manifest.get("jobs", []):
        resolved = resolve_job_config(job, defaults)
        source = job.get("source", "")
        key = get_job_key(Path(source)) if source else ""
        job_state = state.get(key, {})

        # Format display
        archive_fmt = resolved.get("archive_format", "")
        use_restic = resolved.get("use_restic", False)
        if archive_fmt and use_restic:
            format_display = f"{archive_fmt}+restic"
        elif archive_fmt:
            format_display = archive_fmt
        elif use_restic:
            format_display = "restic"
        else:
            format_display = "none"

        # Daemon status
        daemon_plist = job_state.get("daemon_plist", "")
        daemon_status = "[green]●[/green]" if daemon_plist and Path(daemon_plist).exists() else "[dim]○[/dim]"

        # Last run
        last_runs = job_state.get("last_runs", {})
        last_run = "[dim]never[/dim]"
        if last_runs:
            latest = max(last_runs.values())
            try:
                dt = datetime.fromisoformat(latest)
                delta = datetime.now() - dt
                if delta.days > 0:
                    last_run = f"{delta.days}d ago"
                elif delta.seconds >= 3600:
                    last_run = f"{delta.seconds // 3600}h ago"
                else:
                    last_run = f"{delta.seconds // 60}m ago"
            except ValueError:
                last_run = latest

        table.add_row(
            resolved.get("name", ""),
            source,
            resolved.get("dest", ""),
            format_display,
            daemon_status,
            last_run,
        )

    console.print(table)
    console.print(f"\n[dim]Config file: {MANIFEST_FILE}[/dim]")


@cli.command("jobs")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed info for each job")
def list_jobs(verbose):
    """List saved backup job configurations.

    Jobs are automatically saved after successful backups, so you can later run:

        snapback --source /path/to/project

    and have all the options loaded from the saved config.
    """
    setup_logging()

    jobs = load_jobs()

    if not jobs:
        logger.info("No saved jobs found.")
        logger.info("Run a backup to automatically save the job config.")
        return

    logger.info(f"Saved Jobs ({len(jobs)}):\n")

    for source_key, job in sorted(jobs.items(), key=lambda x: x[1].get("name", "")):
        name = job.get("name", "unnamed")
        dest = job.get("dest", "?")
        opts = job.get("options", {})

        # Build mode string using new schema
        mode_parts = []
        use_restic = opts.get("use_restic", False)
        archive_format = opts.get("archive_format", ArchiveFormat.SEVENZ.value)

        if use_restic:
            mode_parts.append("restic")
        if archive_format and archive_format != ArchiveFormat.NONE.value:
            mode_parts.append(archive_format)

        if opts.get("split_size") and not opts.get("no_split"):
            mode_parts.append(f"split:{opts['split_size']}")

        mode_str = " + ".join(mode_parts) if mode_parts else "none"

        logger.info(f"  {name}")
        logger.info(f"    Source: {source_key}")
        logger.info(f"    Dest:   {dest}")
        logger.info(f"    Mode:   {mode_str}")

        # Show 1Password info if present
        op_vault = opts.get("op_vault")
        op_item = opts.get("op_item")
        if op_vault and op_item:
            logger.info(f"    1Password: {op_item} (vault: {op_vault})")

        if verbose:
            last_saved = job.get("last_saved", "never")
            logger.info(f"    Saved:  {last_saved}")

            last_runs = job.get("last_runs", {})
            if last_runs:
                logger.info("    Last runs:")
                for backup_type, timestamp in last_runs.items():
                    logger.info(f"      {backup_type}: {timestamp}")

        logger.info("")


@cli.command("job-remove")
@click.argument("source", type=click.Path(path_type=Path))
def remove_job(source):
    """Remove a saved job configuration.

    SOURCE is the path to the source directory for the job to remove.
    """
    setup_logging()

    jobs = load_jobs()
    key = get_job_key(source)

    if key not in jobs:
        raise click.ClickException(f"No saved job for {source}")

    job = jobs[key]
    name = job.get("name", "unnamed")

    del jobs[key]
    save_jobs(jobs)

    logger.success(f"Removed job '{name}' ({source})")


@cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_all_jobs(as_json):
    """List all backup jobs with stats and daemon status.

    Shows every directory you've backed up, including:
    - Last backup times (restic, 7z/tar.gz)
    - Daemon status (installed, running)
    - Backup mode and settings
    """
    setup_logging()
    from rich.table import Table

    jobs = load_jobs()

    if not jobs:
        logger.info("No backup jobs found.")
        logger.info("Run a backup to create a job, or use 'snapback daemon install'.")
        return

    if as_json:
        # Add daemon status to each job
        result = {}
        launchctl_result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)

        for source_key, job in jobs.items():
            name = job.get("name", "unnamed")
            opts = job.get("options", {})

            # Check stored path first, then search for plist
            stored_plist = opts.get("daemon_plist")
            stored_label = opts.get("daemon_label")
            if stored_plist and Path(stored_plist).exists():
                plist_path = Path(stored_plist)
                label = stored_label or get_daemon_label(name)
                daemon_installed = True
            else:
                found_plist = find_plist_path(name)
                if found_plist:
                    plist_path = found_plist
                    daemon_installed = True
                    label = plist_path.stem
                else:
                    plist_path = get_plist_path(name)
                    label = get_daemon_label(name)
                    daemon_installed = False

            daemon_running = label in launchctl_result.stdout if label else False

            plist_version = get_plist_version(plist_path) if daemon_installed else None

            result[source_key] = {
                **job,
                "daemon": {
                    "installed": daemon_installed,
                    "running": daemon_running,
                    "plist_path": str(plist_path) if daemon_installed else None,
                    "label": label,
                    "plist_version": plist_version,
                    "current_version": __version__,
                    "outdated": plist_version is not None and plist_version != __version__,
                }
            }
        print(json.dumps(result, indent=2, default=str))
        return

    # Rich table output
    table = Table(title="Snapback Jobs", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Source")
    table.add_column("Mode")
    table.add_column("Last Restic")
    table.add_column("Last Full")
    table.add_column("Daemon")
    table.add_column("Version")

    # Get launchctl list once
    launchctl_result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)

    for source_key, job in sorted(jobs.items(), key=lambda x: x[1].get("name", "")):
        name = job.get("name", "unnamed")
        opts = job.get("options", {})
        last_runs = job.get("last_runs", {})

        # Mode - use new schema
        use_restic = opts.get("use_restic", False)
        archive_format = opts.get("archive_format", ArchiveFormat.SEVENZ.value)

        mode_parts = []
        if use_restic:
            mode_parts.append("restic")
        if archive_format and archive_format != ArchiveFormat.NONE.value:
            mode_parts.append(archive_format)
        mode = " + ".join(mode_parts) if mode_parts else "none"

        # Last runs - check jobs.json first, then fall back to checking actual files
        dest = job.get("dest", "")

        last_restic_str = last_runs.get("restic")
        last_full_str = last_runs.get("7z") or last_runs.get("tar.gz")

        # Fall back to checking actual backup files if not in jobs.json
        last_restic_dt = None
        last_full_dt = None

        if last_restic_str:
            try:
                last_restic_dt = datetime.fromisoformat(last_restic_str)
            except ValueError:
                pass
        elif dest:
            last_restic_dt = get_last_restic_time_for_job(dest, name)

        if last_full_str:
            try:
                last_full_dt = datetime.fromisoformat(last_full_str)
            except ValueError:
                pass
        elif dest:
            last_full_dt = get_last_backup_time_for_job(dest, name)

        def format_timestamp(dt: datetime | None) -> str:
            if not dt:
                return "[dim]never[/dim]"
            age = datetime.now() - dt
            return f"{format_age(age)}"

        # Daemon status - check stored path first, then search for plist
        stored_plist = opts.get("daemon_plist")
        stored_label = opts.get("daemon_label")
        if stored_plist and Path(stored_plist).exists():
            plist_path = Path(stored_plist)
            label = stored_label or get_daemon_label(name)
            daemon_installed = True
        else:
            # Search for plist (current namespace, then legacy)
            found_plist = find_plist_path(name)
            if found_plist:
                plist_path = found_plist
                daemon_installed = True
                # Extract label from plist filename
                label = plist_path.stem  # e.g., "com.snapback.myname"
            else:
                plist_path = get_plist_path(name)
                label = get_daemon_label(name)
                daemon_installed = False
        daemon_running = label in launchctl_result.stdout if label else False

        if daemon_running:
            daemon_status = "[green]running[/green]"
        elif daemon_installed:
            daemon_status = "[yellow]stopped[/yellow]"
        else:
            daemon_status = "[dim]none[/dim]"

        # Check plist version
        if daemon_installed:
            plist_version = get_plist_version(plist_path)
            if plist_version is None:
                version_status = "[yellow]no version[/yellow]"
            elif plist_version == __version__:
                version_status = f"[green]{plist_version}[/green]"
            else:
                version_status = f"[red]{plist_version} → {__version__}[/red]"
        else:
            version_status = "[dim]-[/dim]"

        # Shorten source path for display
        source_display = source_key
        home = str(Path.home())
        if source_display.startswith(home):
            source_display = "~" + source_display[len(home):]
        if len(source_display) > 35:
            source_display = "..." + source_display[-32:]

        table.add_row(
            name,
            source_display,
            mode,
            format_timestamp(last_restic_dt),
            format_timestamp(last_full_dt),
            daemon_status,
            version_status,
        )

    console.print(table)
    console.print(f"\n[dim]Config: {JOBS_FILE}[/dim]")


@cli.command("check-passwords")
@click.option("--1password", "check_1password", is_flag=True, help="Compare local passwords with 1Password")
def check_passwords(check_1password: bool):
    """Verify restic password files work with their repositories."""
    from rich.table import Table

    manifest = load_manifest()
    defaults = manifest.get("defaults", {})
    jobs = manifest.get("jobs", [])

    if not jobs:
        logger.info("No jobs configured.")
        return

    # Check 1Password CLI if needed
    if check_1password and not check_1password_cli():
        logger.error("1Password CLI not available. Install and authenticate first.")
        return

    console.print("\n[bold]Checking restic passwords...[/bold]\n")

    results = []
    for job in jobs:
        resolved = resolve_job_config(job, defaults)
        name = resolved.get("name", "unknown")
        source = Path(resolved.get("source", "")).expanduser()
        dest = Path(resolved.get("dest", "")).expanduser()
        use_restic = resolved.get("use_restic", False)
        op_vault = resolved.get("op_vault", "")

        if not use_restic:
            results.append((name, "skip", "restic not enabled", None))
            continue

        # Check password file
        password_file = Path.home() / ".config/restic" / f"{name}-password"
        if not password_file.exists():
            results.append((name, "missing", "no password file", None))
            continue

        local_password = password_file.read_text().strip()

        # Check restic repo
        restic_repo = dest / name / "restic"
        if not (restic_repo / "config").exists():
            results.append((name, "no-repo", "restic repo not initialized", None))
            continue

        # Try to access repo with password
        cmd = [
            "restic", "-r", str(restic_repo),
            "--password-file", str(password_file),
            "cat", "config"
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                repo_status = "ok"
                repo_details = "password valid"
            else:
                if "wrong password" in result.stderr.lower():
                    repo_status = "wrong"
                    repo_details = "wrong password"
                else:
                    repo_status = "error"
                    repo_details = result.stderr.strip()[:50]
        except subprocess.TimeoutExpired:
            repo_status = "timeout"
            repo_details = "restic timed out"
        except Exception as e:
            repo_status = "error"
            repo_details = str(e)[:50]

        # Check 1Password if requested
        op_status = None
        if check_1password and op_vault:
            op_password = get_password_from_1password(name, op_vault)
            if op_password is None:
                op_status = "not-found"
            elif op_password == local_password:
                op_status = "match"
            else:
                op_status = "mismatch"
        elif check_1password and not op_vault:
            op_status = "no-vault"

        results.append((name, repo_status, repo_details, op_status))

    # Display results
    table = Table(title="Restic Password Check")
    table.add_column("Job", style="cyan")
    table.add_column("Repo")
    table.add_column("Details", style="dim")
    if check_1password:
        table.add_column("1Password")

    for name, status, details, op_status in results:
        if status == "ok":
            status_display = "[green]✓ OK[/green]"
        elif status == "skip":
            status_display = "[dim]- skip[/dim]"
        elif status == "wrong":
            status_display = "[red]✗ WRONG[/red]"
        elif status == "missing":
            status_display = "[yellow]! missing[/yellow]"
        elif status == "no-repo":
            status_display = "[yellow]! no repo[/yellow]"
        else:
            status_display = f"[red]✗ {status}[/red]"

        if check_1password:
            if op_status == "match":
                op_display = "[green]✓ synced[/green]"
            elif op_status == "mismatch":
                op_display = "[red]✗ differs[/red]"
            elif op_status == "not-found":
                op_display = "[yellow]! not in 1P[/yellow]"
            elif op_status == "no-vault":
                op_display = "[dim]no vault[/dim]"
            else:
                op_display = "[dim]-[/dim]"
            table.add_row(name, status_display, details, op_display)
        else:
            table.add_row(name, status_display, details)

    console.print(table)


@cli.command()
def configure():
    """Launch interactive configuration editor."""
    setup_logging(file_logging=True, console=False)  # No console output in TUI
    logger.info("Starting TUI configuration editor")
    app = SnapbackApp()
    app.run()
    logger.info("TUI configuration editor closed")


def main() -> int:
    """Main entry point."""
    try:
        cli(standalone_mode=False)
        return 0
    except click.ClickException as e:
        e.show()
        return 1
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0


if __name__ == "__main__":
    sys.exit(main())
