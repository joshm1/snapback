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

# Global flag for notifications
_notify_enabled = False

# Global flag for interactive mode (TTY)
_interactive = False


def is_interactive() -> bool:
    """Check if we're running in an interactive terminal."""
    return _interactive and sys.stdout.isatty()

# Default directories to exclude
DEFAULT_EXCLUDES = [
    "node_modules",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    ".tox",
    "dist",
    "build",
    "*.egg-info",
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

    @property
    def backup_prefix(self) -> str:
        return f"{self.name}_"

    @property
    def backup_suffix(self) -> str:
        return ".tar.gz"

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

    for f in _config.backup_dir.glob(f"{_config.backup_prefix}*{_config.backup_suffix}"):
        try:
            date_str = f.stem.replace(_config.backup_prefix, "")
            backup_time = datetime.strptime(date_str, "%Y-%m-%d_%H%M%S")
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
    assert _config is not None
    if _config.restic_password_file.exists():
        return True

    logger.info("Setting up restic password file...")
    _config.restic_password_file.parent.mkdir(parents=True, exist_ok=True)

    password = secrets.token_urlsafe(32)
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
    """Create a compressed backup."""
    assert _config is not None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_name = f"{_config.backup_prefix}{timestamp}{_config.backup_suffix}"
    backup_path = _config.backup_dir / backup_name

    exclude_args = []
    for d in _config.excludes_for_full:
        exclude_args.extend(["--exclude", d])

    cmd = [
        "tar",
        "-czf",
        str(backup_path),
        *exclude_args,
        "-C",
        str(_config.source_dir.parent),
        _config.source_dir.name,
    ]

    if dry_run:
        logger.info(f"[DRY RUN] Would create backup: {backup_name}")
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

        return None

    logger.info(f"Creating backup: {backup_name}")
    logger.info(f"  Source: {_config.source_dir}")
    logger.debug(f"  Excluding: {', '.join(_config.excludes_for_full)}")

    try:
        env = os.environ.copy()
        env["GZIP"] = "-9"

        # In interactive mode, show a progress spinner
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
        if dry_run:
            logger.info(f"[DRY RUN] Would run full tar.gz backup (every {_config.full_interval_days} days)")
        else:
            logger.info(f"Running full backup (every {_config.full_interval_days} days)...")
            if create_backup(dry_run=False):
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
        <string>--hybrid</string>
        <string>--auto</string>
        <string>--restic-interval</string>
        <string>{restic_interval}</string>
        <string>--full-interval</string>
        <string>{full_interval}</string>
    </array>

    <!-- Run every {interval_seconds} seconds ({restic_interval} hours) -->
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>

    <!-- Also run on login/wake to catch up -->
    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{log_path}</string>

    <key>StandardErrorPath</key>
    <string>{log_path}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:{home}/.local/bin</string>
    </dict>
</dict>
</plist>
"""


def get_plist_path(name: str) -> Path:
    """Get the path to the plist file for a named daemon."""
    return Path.home() / "Library/LaunchAgents" / f"com.snapback.{name}.plist"


def get_log_path(name: str) -> Path:
    """Get the path to the log file for a named daemon."""
    return Path(f"/tmp/snapback-{name}.log")


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
@click.version_option(version=__version__)
@click.pass_context
def cli(ctx, source, dest, name, restic, hybrid, exclude, no_default_excludes,
        include_git, exclude_git_restic, force, auto, dry_run, list_mode, notify, verbose,
        restic_interval, full_interval):
    """Snapback - Hybrid backup tool with tar.gz and restic support."""
    ctx.ensure_object(dict)

    # Set up logging
    setup_logging(verbose)

    # If a subcommand is invoked, skip backup logic
    if ctx.invoked_subcommand is not None:
        return

    # For direct backup command, require source/dest/name
    if not all([source, dest, name]):
        if list_mode or any([source, dest, name]):
            raise click.UsageError("--source, --dest, and --name are required for backup operations")
        click.echo(ctx.get_help())
        return

    # Build exclusion list
    excludes = [] if no_default_excludes else list(DEFAULT_EXCLUDES)
    excludes.extend(exclude)

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
    )

    # Enable notifications
    global _notify_enabled
    _notify_enabled = notify or auto

    # Enable interactive mode (Rich progress) when running manually with a TTY
    global _interactive
    _interactive = not auto and sys.stdout.isatty()

    # Validate source exists
    if not _config.source_dir.exists():
        raise click.ClickException(f"Source directory does not exist: {_config.source_dir}")

    # Check destination is accessible
    if not check_dest_accessible():
        if auto:
            ctx.exit(0)
        ctx.exit(1)

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
        ctx.exit(result)

    # Check for recent backup
    if restic:
        last_backup = get_last_restic_backup_time()
        backup_type = "restic"
    else:
        last_backup = get_last_backup_time()
        backup_type = "tar.gz"

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
    else:
        result = create_backup(dry_run=dry_run)

    if dry_run:
        ctx.exit(0)

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
@click.option("--restic-interval", type=int, default=DEFAULT_RESTIC_INTERVAL_HOURS,
              help=f"Hours between restic backups (default: {DEFAULT_RESTIC_INTERVAL_HOURS})")
@click.option("--full-interval", type=int, default=DEFAULT_FULL_INTERVAL_DAYS,
              help=f"Days between full tar.gz backups (default: {DEFAULT_FULL_INTERVAL_DAYS})")
def daemon_install(source, dest, name, restic_interval, full_interval):
    """Install and start the backup daemon."""
    setup_logging()

    source = source.expanduser().resolve()
    dest = dest.expanduser().resolve()
    plist_path = get_plist_path(name)
    log_path = get_log_path(name)

    # Validate paths
    if not source.exists():
        raise click.ClickException(f"Source directory does not exist: {source}")

    # Ensure LaunchAgents directory exists
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Unload if already loaded
    if plist_path.exists():
        logger.info("Unloading existing daemon...")
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    # Find snapback executable
    snapback_path = find_snapback_path()

    # Calculate interval in seconds
    interval_seconds = restic_interval * 3600

    # Generate plist content
    plist_content = PLIST_TEMPLATE.format(
        name=name,
        snapback_path=snapback_path,
        source=str(source),
        dest=str(dest),
        log_path=str(log_path),
        home=str(Path.home()),
        restic_interval=restic_interval,
        full_interval=full_interval,
        interval_seconds=interval_seconds,
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
    logger.info(f"  Schedule: restic every {restic_interval}h, full tar.gz every {full_interval}d")
    logger.info("  Also runs on login/wake")
    logger.info(f"  Logs: {log_path}")
    logger.info(f"  Plist: {plist_path}")


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
                interval_hours = int(interval_match.group(1)) // 3600
                logger.info(f"  Interval: {interval_hours}h")
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
