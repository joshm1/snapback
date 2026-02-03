# Format Refactor Design

## Goal

Replace confusing `format` field (with "hybrid" option) with clearer separation:
- **Full Backups**: archive format choice (7z, tar.gz, or none)
- **Incremental**: restic on/off

## Manifest Schema

```toml
[defaults]
dest = "~/Backups"

# Full Backups (pick one or none)
archive_format = "7z"  # "7z", "tar.gz", or "" (disabled)
full_interval_days = 7

# Incremental (on/off)
use_restic = true
restic_interval_hours = 4
op_vault = ""

[[jobs]]
name = "projects"
source = "~/projects"
use_restic = true
archive_format = "7z"
```

## Validation

- `archive_format` must be `""`, `"7z"`, or `"tar.gz"`
- At least one of `archive_format` or `use_restic` must be enabled
- Can't have both 7z and tar.gz (enforced by single field)

## TUI Layout

```
─── Full Backups ───
Archive Format: ( ) None  (●) 7z  ( ) tar.gz
Interval:       [7    ] days

─── Incremental ───
[x] Enable restic
Interval:       [4    ] hours
```

## CLI Changes

New flags:
- `--archive-format 7z|tar.gz|none`
- `--restic / --no-restic`

Kept for backward compat:
- `--7z` → alias for `--archive-format 7z`
- `--tar-gz` → alias for `--archive-format tar.gz`

Deprecated:
- `--hybrid` → warning + treat as `--restic --archive-format 7z`

## Migration

| Old `format` | New `archive_format` | New `use_restic` |
|--------------|---------------------|------------------|
| `"7z"` | `"7z"` | `false` |
| `"tar.gz"` | `"tar.gz"` | `false` |
| `"restic"` | `""` | `true` |
| `"hybrid"` | `"7z"` | `true` |

Migration happens transparently when loading manifest.
