# Snapback

A flexible backup tool that supports both traditional compressed archives (tar.gz) and space-efficient incremental backups using [restic](https://restic.net/). Perfect for backing up to cloud storage like Google Drive, Dropbox, or any mounted filesystem.

## Features

- **Hybrid backup mode**: Combine frequent incremental backups (restic) with weekly full backups (tar.gz)
- **Space efficient**: Restic deduplicates at the block level, typically 40x smaller than full backups over time
- **Smart scheduling**: Only backup when needed based on configurable time thresholds
- **macOS notifications**: Get notified on backup success or failure
- **Cloud-ready**: Works great with Google Drive, Dropbox, or any mounted storage
- **Configurable exclusions**: Skip node_modules, .venv, and other large directories by default
- **Easy restore**: Full tar.gz backups are simple to extract; restic provides point-in-time recovery

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

### For restic support

If you want to use incremental backups, install restic:

```bash
# macOS
brew install restic

# Ubuntu/Debian
apt install restic

# Or download from https://restic.net/
```

## Quick Start

### Basic tar.gz backup

```bash
snapback --source ~/projects/myapp --dest ~/Backups --name myapp
```

### Incremental backup with restic

```bash
snapback --source ~/projects/myapp --dest ~/Backups --name myapp --restic
```

### Hybrid mode (recommended for automated backups)

Runs restic every 4 hours and creates a full tar.gz backup weekly:

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

Backup modes:
  --restic             Use restic incremental backup
  --hybrid             Hybrid: restic every 4h + tar.gz weekly

Options:
  --force, -f          Skip recency check and create backup
  --auto, -a           Daemon mode: skip silently if not needed
  --dry-run, -n        Show what would happen
  --list, -l           List existing backups
  --notify             Send macOS notifications
  --verbose, -v        Verbose output

Exclusions:
  --exclude, -e DIR    Additional directories to exclude
  --no-default-excludes  Don't exclude defaults (node_modules, etc.)
  --include-git        Include .git in tar.gz (excluded by default)
  --exclude-git-restic Exclude .git from restic (included by default)
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
# List tar.gz backups
snapback --source ~/projects/myapp --dest ~/Backups --name myapp --list

# List restic snapshots
snapback --source ~/projects/myapp --dest ~/Backups --name myapp --list --restic
```

## macOS Daemon Setup

For automatic scheduled backups, create a Launch Agent:

### 1. Create the plist file

Save this as `~/Library/LaunchAgents/com.snapback.myapp.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.snapback.myapp</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOUR_USERNAME/.local/bin/snapback</string>
        <string>--source</string>
        <string>/Users/YOUR_USERNAME/projects/myapp</string>
        <string>--dest</string>
        <string>/Users/YOUR_USERNAME/Library/CloudStorage/GoogleDrive/My Drive/Backups</string>
        <string>--name</string>
        <string>myapp</string>
        <string>--hybrid</string>
        <string>--auto</string>
    </array>

    <!-- Run every 4 hours -->
    <key>StartInterval</key>
    <integer>14400</integer>

    <!-- Also run on login/wake -->
    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/tmp/snapback-myapp.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/snapback-myapp.log</string>
</dict>
</plist>
```

### 2. Load the daemon

```bash
launchctl load ~/Library/LaunchAgents/com.snapback.myapp.plist
```

### 3. Check status

```bash
launchctl list | grep snapback
```

### 4. Unload when needed

```bash
launchctl unload ~/Library/LaunchAgents/com.snapback.myapp.plist
```

## Backup Strategy

### Recommended: Hybrid Mode

The `--hybrid` flag provides a good balance:

- **Restic (every 4 hours)**: Space-efficient incremental backups. Only changed blocks are stored, typically using ~40x less space than full backups over time.
- **Full tar.gz (weekly)**: Easy-to-restore complete backup. Just extract the archive - no special tools needed.

### Why include .git in restic but not tar.gz?

- **Restic**: Git directories deduplicate extremely well in restic since most objects are unchanged between commits.
- **tar.gz**: Git directories are already compressed and add significant size to full backups.

## Restoring from Backup

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

- `node_modules` - npm/yarn dependencies
- `.venv` - Python virtual environments
- `__pycache__` - Python bytecode
- `.mypy_cache`, `.pytest_cache`, `.ruff_cache` - Tool caches
- `.cache` - General cache directory
- `.tox` - Tox testing environments
- `dist`, `build` - Build outputs
- `*.egg-info` - Python package metadata

Use `--no-default-excludes` to include these, or `--exclude` to add more.

## File Naming

Backups are named with timestamps for easy identification:

```
myapp_2024-01-15_120000.tar.gz    # Full backup from Jan 15 at noon
myapp_restic/                      # Restic repository with incremental snapshots
```

## Requirements

- Python 3.10+
- `tar` and `gzip` (included on macOS/Linux)
- `restic` (optional, for incremental backups)
- `osascript` (optional, for macOS notifications)

## License

MIT License - see [LICENSE](LICENSE) for details.
