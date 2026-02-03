#!/usr/bin/env python3
"""
Snapback - Hybrid backup tool with tar.gz and restic support.

Usage:
    snapback --source ~/projects/myrepo --dest ~/Backups --name myrepo
    snapback --source ~/projects/myrepo --dest ~/Backups --name myrepo --restic
    snapback --source ~/projects/myrepo --dest ~/Backups --name myrepo --hybrid --auto

    # Install as macOS daemon:
    snapback daemon install --source ~/projects/myrepo --dest ~/Backups --name myrepo
    snapback daemon status --name myrepo
    snapback daemon uninstall --name myrepo

For more info: https://github.com/joshm1/snapback
"""

import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import rich_click as click
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

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
    """Load saved job configurations."""
    if not JOBS_FILE.exists():
        return {}
    try:
        return json.loads(JOBS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_jobs(jobs: dict) -> None:
    """Save job configurations."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps(jobs, indent=2, default=str))


def get_job_key(source: Path) -> str:
    """Get a normalized key for a source path."""
    return str(source.expanduser().resolve())


def save_job_config(source: Path, dest: Path, name: str, **options) -> None:
    """Save configuration for a backup job (merges with existing options)."""
    jobs = load_jobs()
    key = get_job_key(source)

    # Preserve existing options (like op_vault from daemon install)
    existing_options = jobs.get(key, {}).get("options", {})
    merged_options = {**existing_options, **options}

    jobs[key] = {
        "source": str(source),
        "dest": str(dest),
        "name": name,
        "options": merged_options,
        "last_saved": datetime.now().isoformat(),
    }
    save_jobs(jobs)
    logger.debug(f"Saved job config for {source}")


def load_job_config(source: Path) -> dict | None:
    """Load configuration for a backup job."""
    jobs = load_jobs()
    key = get_job_key(source)
    return jobs.get(key)


def update_job_last_run(source: Path, backup_type: str) -> None:
    """Update the last run timestamp for a job."""
    jobs = load_jobs()
    key = get_job_key(source)
    if key in jobs:
        if "last_runs" not in jobs[key]:
            jobs[key]["last_runs"] = {}
        jobs[key]["last_runs"][backup_type] = datetime.now().isoformat()
        save_jobs(jobs)


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


def setup_logging(verbose: bool = False) -> None:
    """Configure loguru logging."""
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stdout,
        format="<level>{message}</level>",
        level=level,
        colorize=True,
    )


@dataclass
class BackupConfig:
    """Configuration for a backup job."""
    source_dir: Path
    backup_dir: Path
    name: str
    exclude_dirs: list[str] = field(default_factory=list)
    include_git_in_restic: bool = True  # .git deduplicates well in restic
    exclude_git_in_full: bool = True    # Exclude .git from tar.gz (large)
    restic_interval_hours: int = DEFAULT_RESTIC_INTERVAL_HOURS
    full_interval_days: int = DEFAULT_FULL_INTERVAL_DAYS
    split_size: str | None = None  # e.g., "1g", "500m" - None means no splitting
    use_7z: bool = False  # Use 7z format instead of tar.gz

    @property
    def backup_prefix(self) -> str:
        return f"{self.name}_"

    @property
    def backup_suffix(self) -> str:
        return ".7z" if self.use_7z else ".tar.gz"

    @property
    def restic_repo(self) -> Path:
        return self.backup_dir / f"{self.name}_restic"

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
    if not _config.backup_dir.exists():
        logger.info(f"Creating backup directory: {_config.backup_dir}")
        _config.backup_dir.mkdir(parents=True, exist_ok=True)
    return True


def get_existing_backups() -> list[tuple[Path, datetime]]:
    """Get list of existing backups with their timestamps."""
    assert _config is not None
    backups = []
    if not _config.backup_dir.exists():
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
        for f in _config.backup_dir.glob(pattern):
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
    backup_path = _config.backup_dir / backup_name

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

    cmd = ["op", "item", "get", item_title, "--fields", "password"]
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
    backup_path = _config.backup_dir / backup_name

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


def run_hybrid_backup(force: bool, auto: bool, dry_run: bool) -> int:
    """
    Hybrid backup mode:
    - Run restic backup if > restic_interval_hours since last restic backup
    - Run full tar.gz backup if > full_interval_days since last full backup
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
        logger.success(f"All backups are current (restic < {_config.restic_interval_hours}h, full < {_config.full_interval_days}d)")
        return 0

    if restic_needed:
        if dry_run:
            logger.info("[DRY RUN] Would run restic backup")
        else:
            if create_restic_backup(dry_run=False):
                restic_ran = True
            else:
                logger.error("Restic backup failed")

    if full_needed:
        backup_format = "7z" if _config.use_7z else "tar.gz"
        if dry_run:
            logger.info(f"[DRY RUN] Would run full {backup_format} backup (every {_config.full_interval_days} days)")
        else:
            logger.info(f"Running full {backup_format} backup (every {_config.full_interval_days} days)...")
            if _config.use_7z:
                backup_result = create_7z_backup(dry_run=False)
            else:
                backup_result = create_backup(dry_run=False)
            if backup_result:
                full_ran = True
            else:
                logger.error("Full backup failed")

    if dry_run:
        return 0

    if restic_needed and not restic_ran:
        return 1
    if full_needed and not full_ran:
        return 1
    return 0


# =============================================================================
# Daemon Management (macOS LaunchAgent)
# =============================================================================

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.snapback.{name}</string>

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


def get_plist_path(name: str) -> Path:
    """Get the path to the plist file for a named daemon."""
    return Path.home() / "Library/LaunchAgents" / f"com.snapback.{name}.plist"


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
@click.option("--source", "-s", type=click.Path(exists=True, path_type=Path), help="Source directory to backup")
@click.option("--dest", "-d", type=click.Path(path_type=Path), help="Destination directory for backups")
@click.option("--name", "-N", help="Name for this backup (used in filenames)")
@click.option("--restic", is_flag=True, help="Use restic incremental backup")
@click.option("--hybrid", is_flag=True, help="Hybrid mode: restic + full tar.gz")
@click.option("--exclude", "-e", multiple=True, help="Additional directories to exclude")
@click.option("--no-default-excludes", is_flag=True, help="Don't use default exclusions")
@click.option("--include-git", is_flag=True, help="Include .git in tar.gz backups")
@click.option("--exclude-git-restic", is_flag=True, help="Exclude .git from restic backups")
@click.option("--force", "-f", is_flag=True, help="Skip recency check and create backup")
@click.option("--auto", "-a", is_flag=True, help="Automatic mode: skip silently if not needed")
@click.option("--dry-run", "-n", is_flag=True, help="Show what would happen without doing it")
@click.option("--list", "-l", "list_mode", is_flag=True, help="List existing backups")
@click.option("--notify", is_flag=True, help="Send macOS notifications")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.option("--restic-interval", type=int, default=DEFAULT_RESTIC_INTERVAL_HOURS,
              help=f"Hours between restic backups (default: {DEFAULT_RESTIC_INTERVAL_HOURS})")
@click.option("--full-interval", type=int, default=DEFAULT_FULL_INTERVAL_DAYS,
              help=f"Days between full tar.gz backups (default: {DEFAULT_FULL_INTERVAL_DAYS})")
@click.option("--7z/--tar-gz", "use_7z", default=True, help="Use 7z (default) or tar.gz format")
@click.option("--split-size", type=str, default="50m",
              help="Split backup into volumes of this size (default: 50m). Use --no-split to disable")
@click.option("--no-split", is_flag=True, help="Don't split backup into volumes")
@click.option("--save", is_flag=True, help="Save this job config for future runs (auto-saved on successful backup)")
@click.option("--1password", "use_1password", is_flag=True, help="Store restic password in 1Password")
@click.option("--1password-vault", "op_vault", type=str, help="1Password vault name (prompts interactively if not specified)")
@click.version_option(version=__version__)
@click.pass_context
def cli(ctx, source, dest, name, restic, hybrid, exclude, no_default_excludes,
        include_git, exclude_git_restic, force, auto, dry_run, list_mode, notify, verbose,
        restic_interval, full_interval, use_7z, split_size, no_split, save,
        use_1password, op_vault):
    """Snapback - Hybrid backup tool with tar.gz and restic support.

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
            if not hybrid and opts.get("hybrid"):
                hybrid = True
            if not restic and opts.get("restic"):
                restic = True
            if use_7z and "use_7z" in opts:
                use_7z = opts["use_7z"]
            if split_size == "50m" and opts.get("split_size"):
                split_size = opts["split_size"]
            if not no_split and opts.get("no_split"):
                no_split = True
            logger.debug(f"  dest: {dest}")
            logger.debug(f"  name: {name}")
            logger.debug(f"  hybrid: {hybrid}, restic: {restic}")
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
        use_7z=use_7z,
    )

    # Check if 7z is installed when using 7z format
    if use_7z and not check_7z_installed():
        raise click.ClickException(
            "7z is not installed. Install with: brew install p7zip\n"
            "Or use --tar-gz to use tar.gz format instead."
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

    # Hybrid mode
    if hybrid:
        result = run_hybrid_backup(force, auto, dry_run)
        # Save config on successful hybrid backup
        if result == 0 and not dry_run:
            save_job_config(
                source, dest, name,
                hybrid=True,
                restic=False,
                use_7z=use_7z,
                split_size=effective_split_size,
                no_split=no_split,
            )
            update_job_last_run(source, "hybrid")
        ctx.exit(result)

    # Check for recent backup
    if restic:
        last_backup = get_last_restic_backup_time()
        backup_type = "restic"
    else:
        last_backup = get_last_backup_time()
        backup_type = "7z" if use_7z else "tar.gz"

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
    elif use_7z:
        result = create_7z_backup(dry_run=dry_run)
        backup_type_for_save = "7z"
    else:
        result = create_backup(dry_run=dry_run)
        backup_type_for_save = "tar.gz"

    if dry_run:
        ctx.exit(0)

    # On successful backup, save job config and update last run
    if result:
        save_job_config(
            source, dest, name,
            hybrid=hybrid,
            restic=restic,
            use_7z=use_7z,
            split_size=effective_split_size,
            no_split=no_split,
        )
        update_job_last_run(source, backup_type_for_save)

    ctx.exit(0 if result else 1)


@cli.group()
def daemon():
    """Manage the backup daemon (macOS LaunchAgent)."""
    pass


@daemon.command("install")
@click.option("--source", "-s", type=click.Path(exists=True, path_type=Path), required=True,
              help="Source directory to backup")
@click.option("--dest", "-d", type=click.Path(path_type=Path), required=True,
              help="Destination directory for backups")
@click.option("--name", "-N", required=True, help="Name for this backup")
@click.option("--mode", "-m", type=click.Choice(["hybrid", "restic", "7z"]), default=None,
              help="Backup mode: hybrid (restic + 7z), restic only, or 7z only (prompts if not specified)")
@click.option("--restic-interval", type=int, default=DEFAULT_RESTIC_INTERVAL_HOURS,
              help=f"Hours between restic backups (default: {DEFAULT_RESTIC_INTERVAL_HOURS})")
@click.option("--full-interval", type=int, default=DEFAULT_FULL_INTERVAL_DAYS,
              help=f"Days between full 7z backups (default: {DEFAULT_FULL_INTERVAL_DAYS})")
@click.option("--1password", "use_1password", is_flag=True,
              help="Backup restic password to 1Password at install time (daemon uses local password file)")
@click.option("--1password-vault", "op_vault", type=str, help="1Password vault name (prompts if not specified)")
def daemon_install(source, dest, name, mode, restic_interval, full_interval, use_1password, op_vault):
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
    existing_mode = existing_job.get("options", {}).get("mode") if existing_job else None

    # Handle mode selection
    if mode is None:
        if sys.stdout.isatty():
            import questionary

            # Check if we have an existing mode to suggest
            if existing_mode:
                if questionary.confirm(
                    f"Use previously configured mode '{existing_mode}'?",
                    default=True
                ).ask():
                    mode = existing_mode
                    logger.info(f"Using existing mode: {mode}")

            if mode is None:
                mode = questionary.select(
                    "Select backup mode:",
                    choices=[
                        questionary.Choice("hybrid - restic incremental + weekly 7z full backup (recommended)", value="hybrid"),
                        questionary.Choice("restic - incremental backups only (space efficient)", value="restic"),
                        questionary.Choice("7z - compressed archives only (easy to restore)", value="7z"),
                    ],
                    instruction="(↑↓ to move, Enter to select)",
                ).ask()

                if mode is None:
                    raise click.ClickException("Mode selection cancelled")
        else:
            # Non-interactive: default to hybrid
            mode = "hybrid"

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

    # Build mode args for plist
    if mode == "hybrid":
        mode_args = """
        <string>--hybrid</string>
        <string>--restic-interval</string>
        <string>{restic_interval}</string>
        <string>--full-interval</string>
        <string>{full_interval}</string>""".format(restic_interval=restic_interval, full_interval=full_interval)
    elif mode == "restic":
        mode_args = """
        <string>--restic</string>"""
    else:  # 7z
        mode_args = """
        <string>--7z</string>"""

    # Generate plist content (no 1password flags - daemon uses password file)
    plist_content = PLIST_TEMPLATE.format(
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
    if mode == "hybrid":
        logger.info(f"  Mode: hybrid (restic if >{restic_interval}h old, 7z if >{full_interval}d old)")
    elif mode == "restic":
        logger.info(f"  Mode: restic (if >{restic_interval}h since last backup)")
    else:
        logger.info(f"  Mode: 7z (if >{restic_interval}h since last backup)")
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
        mode=mode,
        restic_interval=restic_interval,
        full_interval=full_interval,
        op_vault=selected_vault,
        op_item=op_item_name,
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
    label = f"com.snapback.{name}"

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


# =============================================================================
# Jobs Management
# =============================================================================

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

        # Build mode string
        mode_parts = []
        if opts.get("hybrid"):
            mode_parts.append("hybrid")
        elif opts.get("restic"):
            mode_parts.append("restic")
        else:
            mode_parts.append("7z" if opts.get("use_7z", True) else "tar.gz")

        if opts.get("split_size") and not opts.get("no_split"):
            mode_parts.append(f"split:{opts['split_size']}")

        mode_str = " + ".join(mode_parts)

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
