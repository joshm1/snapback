# Snapback

A flexible backup tool that supports both traditional compressed archives (7z/tar.gz) and space-efficient incremental backups using [restic](https://restic.net/). Perfect for backing up to cloud storage like Google Drive, Dropbox, or any mounted filesystem.

## Features

- **Hybrid backup mode**: Combine frequent incremental backups (restic) with weekly full backups (7z)
- **7z format by default**: Better compression than tar.gz, with automatic 50MB volume splitting
- **Space efficient**: Restic deduplicates at the block level, typically 40x smaller than full backups over time
- **Smart scheduling**: Only backup when needed based on configurable time thresholds
- **Battery aware**: Skips backups when on battery power (in auto mode) to save energy
- **1Password integration**: Optionally backup restic passwords to 1Password for safekeeping
- **Interactive daemon setup**: Easy `daemon install` command with guided mode selection
- **macOS notifications**: Get notified on backup success or failure
- **Cloud-ready**: Works great with Google Drive, Dropbox, or any mounted storage
- **Configurable exclusions**: Skip node_modules, .venv, and other large directories by default
- **Easy restore**: Full 7z backups are simple to extract; restic provides point-in-time recovery

## Installation

### Using uv (recommended)

```bash
uv tool install git+https://github.com/joshm1/snapback.git
```

### Using pip

```bash
pip install git+https://github.com/joshm1/snapback.git
```

### Using pipx

```bash
pipx install git+https://github.com/joshm1/snapback.git
```

### Dependencies

```bash
# Required for default 7z format
brew install p7zip

# Optional: for incremental backups
brew install restic

# Optional: for 1Password integration
# Install from https://developer.1password.com/docs/cli/get-started/
```

## Quick Start

### Basic backup (7z with 50MB splits, default)

```bash
snapback --source ~/projects/myapp --dest ~/Backups --name myapp
```

### Single-file backup (no splitting)

```bash
snapback --source ~/projects/myapp --dest ~/Backups --name myapp --no-split
```

### Incremental backup with restic

```bash
snapback --source ~/projects/myapp --dest ~/Backups --name myapp --restic
```

### Hybrid mode (recommended for automated backups)

Runs restic every 4 hours and creates a full 7z backup weekly:

```bash
snapback --source ~/projects/myapp --dest ~/Backups --name myapp --hybrid --auto
```

## Usage

```
snapback --source SOURCE --dest DEST --name NAME [options]

Required arguments:
  --source, -s PATH    Source directory to backup
  --dest, -d PATH      Destination directory for backups
  --name, -N NAME      Name for this backup (used in filenames)

Backup formats:
  --7z/--tar-gz        Use 7z (default) or tar.gz format
  --split-size SIZE    Split into volumes (default: 50m)
  --no-split           Don't split backup into volumes

Backup modes:
  --restic             Use restic incremental backup
  --hybrid             Hybrid: restic every 4h + full backup weekly

Options:
  --force, -f          Skip recency check and create backup
  --auto, -a           Daemon mode: skip silently if not needed
  --dry-run, -n        Show what would happen
  --list, -l           List existing backups
  --notify             Send macOS notifications
  --verbose, -v        Verbose output

1Password:
  --1password          Store restic password in 1Password
  --1password-vault    Specify vault name (prompts if not specified)

Exclusions:
  --exclude, -e DIR    Additional directories to exclude
  --no-default-excludes  Don't exclude defaults (node_modules, etc.)
  --include-git        Include .git in full backups (excluded by default)
  --exclude-git-restic Exclude .git from restic (included by default)

Subcommands:
  daemon install       Install as macOS LaunchAgent daemon
  daemon uninstall     Remove daemon
  daemon status        Check daemon status
  daemon logs          View daemon logs
  jobs                 List saved job configurations
  job-remove SOURCE    Remove a saved job configuration
```

## macOS Daemon Setup

The easiest way to set up automated backups on macOS is with the daemon commands:

### Install daemon (interactive)

```bash
snapback daemon install \
  --source ~/projects/myapp \
  --dest ~/Backups \
  --name myapp
```

This will interactively prompt you to:
1. Select backup mode (hybrid, restic-only, or 7z-only)
2. Optionally backup the restic password to 1Password

### Install daemon (non-interactive)

```bash
snapback daemon install \
  --source ~/projects/myapp \
  --dest ~/Backups \
  --name myapp \
  --mode hybrid \
  --1password \
  --1password-vault "My Vault"
```

### Daemon options

```
daemon install options:
  --source, -s PATH      Source directory to backup (required)
  --dest, -d PATH        Destination directory (required)
  --name, -N NAME        Backup name (required)
  --mode, -m MODE        Backup mode: hybrid, restic, or 7z (prompts if not specified)
  --restic-interval N    Min hours between restic backups (default: 4)
  --full-interval N      Min days between full 7z backups (default: 7)
  --1password            Backup restic password to 1Password
  --1password-vault      1Password vault name
```

### Manage daemon

```bash
# Check daemon status
snapback daemon status --name myapp

# View recent logs
snapback daemon logs --name myapp --lines 100

# Uninstall daemon
snapback daemon uninstall --name myapp
```

### Daemon details

- **Logs**: `~/Library/Logs/snapback-{name}.log` (viewable in Console.app)
- **Plist**: `~/Library/LaunchAgents/com.snapback.{name}.plist`
- **Schedule**: Checks hourly (and on login/wake), backs up only when needed

## 1Password Integration

Snapback can backup your restic passwords to 1Password for safekeeping. The daemon still uses the local password file at runtime, but having it in 1Password means you can recover it if needed.

### First-time setup

```bash
# Interactive vault selection
snapback daemon install --source ~/myapp --dest ~/Backups --name myapp --1password

# Or specify vault directly
snapback daemon install --source ~/myapp --dest ~/Backups --name myapp \
  --1password --1password-vault "Snapback"
```

### Re-installing

When re-installing a daemon, snapback remembers your previous vault selection:

```
? Use previously configured vault 'Snapback'? (Y/n)
```

### Viewing saved 1Password info

```bash
snapback jobs
```

Shows:
```
myapp
  Source: /Users/josh/projects/myapp
  Dest:   /Users/josh/Backups
  Mode:   hybrid
  1Password: Snapback: myapp restic password (vault: Snapback)
```

## Examples

### Backup to Google Drive

```bash
snapback \
  --source ~/projects/my-app \
  --dest "/Users/josh/Library/CloudStorage/GoogleDrive-josh@gmail.com/My Drive/Backups" \
  --name my-app \
  --hybrid --auto
```

### Backup with custom exclusions

```bash
snapback \
  --source ~/projects/myapp \
  --dest ~/Backups \
  --name myapp \
  --exclude logs \
  --exclude tmp \
  --exclude "*.pyc"
```

### List existing backups

```bash
# List full backups
snapback --source ~/projects/myapp --dest ~/Backups --name myapp --list

# List restic snapshots
snapback --source ~/projects/myapp --dest ~/Backups --name myapp --list --restic
```

### Saved job configurations

After a successful backup, your configuration is saved to `~/.config/snapback/jobs.json`. This lets you run future backups with just the source path:

```bash
# First run: full config required
snapback --source ~/projects/myapp --dest ~/Backups --name myapp --hybrid

# Subsequent runs: just use --source
snapback --source ~/projects/myapp

# List all saved jobs
snapback jobs

# Remove a saved job
snapback job-remove ~/projects/myapp
```

## Backup Strategy

### Recommended: Hybrid Mode

The `--hybrid` flag (or `--mode hybrid` for daemon) provides a good balance:

- **Restic (every 4 hours)**: Space-efficient incremental backups. Only changed blocks are stored, typically using ~40x less space than full backups over time.
- **Full 7z (weekly)**: Easy-to-restore complete backup. Just extract the archive - no special tools needed.

### Why include .git in restic but not 7z?

- **Restic**: Git directories deduplicate extremely well in restic since most objects are unchanged between commits.
- **7z**: Git directories are already compressed and add significant size to full backups.

## Restoring from Backup

### From 7z (default format)

```bash
# Extract to current directory (handles split volumes automatically)
7z x myapp_2024-01-15_120000.7z.001

# Extract to specific location
7z x myapp_2024-01-15_120000.7z.001 -o/path/to/restore
```

### From tar.gz

```bash
# Extract to current directory
tar -xzf myapp_2024-01-15_120000.tar.gz

# Extract to specific location
tar -xzf myapp_2024-01-15_120000.tar.gz -C /path/to/restore
```

### From restic

```bash
# Set up environment
export RESTIC_PASSWORD_FILE=~/.config/restic/myapp-password
export RESTIC_REPO=/path/to/backups/myapp_restic

# List snapshots
restic snapshots

# Restore latest
restic restore latest --target /path/to/restore

# Restore specific snapshot
restic restore abc123 --target /path/to/restore

# Restore specific files
restic restore latest --target /path/to/restore --include "src/**"
```

## Default Exclusions

The following directories are excluded by default:

- `node_modules`, `.pnpm-store`, `.npm`, `.yarn` - JS package managers
- `.venv`, `venv`, `__pycache__` - Python
- `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox` - Python tools
- `dist`, `build`, `target`, `zig-out`, `out` - Build outputs
- `.next`, `.nuxt`, `.turbo` - JS frameworks
- `Pods`, `DerivedData`, `.build` - iOS/macOS
- `.gradle`, `.cxx` - Android/Java
- `.cache`, `.parcel-cache`, `.nx` - Caches
- `.idea`, `.vscode` - IDEs
- `*.egg-info`, `.eggs` - Python packaging

Use `--no-default-excludes` to include these, or `--exclude` to add more.

## File Naming

Backups are named with timestamps for easy identification:

```
myapp_2024-01-15_120000.7z.001    # First volume of 7z backup from Jan 15 at noon
myapp_2024-01-15_120000.7z.002    # Second volume (if backup is larger than split size)
myapp_2024-01-15_120000.tar.gz    # tar.gz backup (if using --tar-gz)
myapp_restic/                      # Restic repository with incremental snapshots
```

## Requirements

- Python 3.10+
- `7z` (p7zip) for default backup format: `brew install p7zip`
- `tar` and `gzip` (included on macOS/Linux, for --tar-gz format)
- `restic` (optional, for incremental backups): `brew install restic`
- `op` (optional, for 1Password integration): [1Password CLI](https://developer.1password.com/docs/cli/get-started/)
- `osascript` (optional, for macOS notifications)

## License

MIT License - see [LICENSE](LICENSE) for details.
