# Snapback Configure TUI Design

## Overview

Add a Textual-based TUI for managing backup jobs via `snapback configure`. Replace `jobs.json` with a human-editable `manifest.toml` for configuration and a separate `state.json` for runtime data.

## Goals

- Human-readable, version-controllable config file
- Interactive editor for adding/editing/deleting jobs
- Daemon install/uninstall directly from TUI
- Run backups on-demand from TUI
- Migrate existing `jobs.json` data seamlessly

## File Structure

```
~/.config/snapback/
├── manifest.toml   # Human-editable config (jobs + defaults)
├── state.json      # Machine-managed runtime state (last_runs, timestamps)
└── jobs.json       # Deprecated, migrated on first run
```

## Manifest Format

```toml
[defaults]
dest = "~/Backups"
format = "7z"  # tar.gz | 7z | restic | hybrid
restic_interval_hours = 4
full_interval_days = 7

[[jobs]]
name = "projects"
source = "~/projects"

[[jobs]]
name = "dotclaude"
source = "~/.claude"

[[jobs]]
name = "dotconfig"
source = "~/.config"
```

Jobs inherit from `[defaults]` unless they override a field.

## State Format

```json
{
  "~/projects": {
    "last_runs": {
      "7z": "2026-02-03T10:30:00",
      "restic": "2026-02-03T14:30:00"
    },
    "daemon_plist": "/Users/josh/Library/LaunchAgents/io.github.joshm1.snapback.projects.plist"
  }
}
```

Keyed by resolved source path. Contains runtime data only.

## TUI Design

### Main View

```
┌─ Snapback ──────────────────────────────────────────────────────────┐
│ Jobs                                                                │
├─────────────────────────────────────────────────────────────────────┤
│ NAME        SOURCE          DEST        FORMAT   DAEMON   LAST RUN │
│ ▶ projects  ~/projects      ~/Backups   7z       ●        2h ago   │
│   dotclaude ~/.claude       ~/Backups   7z       ○        never    │
│   dotconfig ~/.config       ~/Backups   7z       ○        never    │
├─────────────────────────────────────────────────────────────────────┤
│ [a]dd  [e]dit  [d]elete  [i]nstall daemon  [u]ninstall  [r]un now  │
│ [s] Edit defaults                                       [q]uit     │
└─────────────────────────────────────────────────────────────────────┘
```

- `●` = daemon installed and running
- `○` = no daemon
- Arrow keys to navigate jobs
- Single keypress for actions

### Edit Form (Modal)

```
┌─ Edit: projects ────────────────────────────────────────┐
│                                                         │
│  Name:     [projects                              ]     │
│  Source:   [~/projects                            ]     │
│  Dest:     [~/Backups                             ] ◀ inherited
│  Format:   ( ) tar.gz  (●) 7z  ( ) restic  ( ) hybrid   │
│                                                         │
│  ─── Restic Options ───                                 │
│  Interval: [4    ] hours                                │
│  1Password vault: [                               ]     │
│                                                         │
│  ─── Full Backup Options ───                            │
│  Interval: [7    ] days                                 │
│                                                         │
│            [Save]  [Cancel]                             │
└─────────────────────────────────────────────────────────┘
```

- Fields showing "◀ inherited" use the default value
- Editing an inherited field creates an override
- Clearing an overridden field reverts to inherited

### Defaults Editor

Same form layout but edits the `[defaults]` section. Changes propagate to all jobs that don't override.

### Add Job Flow

1. Press `[a]` from main view
2. Opens edit form with empty/default values
3. Name and source are required
4. Save adds to manifest

### Delete Job

1. Press `[d]` on selected job
2. Confirmation prompt: "Delete 'projects'? [y/n]"
3. If daemon installed, prompt: "Uninstall daemon first? [y/n]"

### Daemon Management

- `[i]` Install daemon for selected job
  - Runs existing `daemon install` logic
  - Updates indicator to `●` on success
  - Shows error in footer on failure

- `[u]` Uninstall daemon
  - Confirmation prompt
  - Runs existing `daemon uninstall` logic
  - Updates indicator to `○`

### Run Now

- `[r]` Run backup immediately
  - Non-blocking execution
  - Progress shown in footer/status bar
  - Updates "LAST RUN" column on completion

## Implementation

### New Dependency

Add to `pyproject.toml`:
```toml
"textual>=0.50.0",
```

### New Modules

Option A: Single file addition
- `snapback_tui.py` - Textual app, imported by `snapback.py`

Option B: Keep in `snapback.py`
- Add Textual app class at end of file
- Keeps single-file simplicity

Recommend **Option B** to maintain single-file architecture.

### New/Modified Functions

```python
# Manifest handling
def load_manifest() -> dict
def save_manifest(manifest: dict) -> None
def get_job_defaults(manifest: dict) -> dict
def resolve_job_config(job: dict, defaults: dict) -> dict

# State handling
def load_state() -> dict
def save_state(state: dict) -> None
def get_job_state(source: Path) -> dict
def update_job_state(source: Path, **updates) -> None

# Migration
def migrate_jobs_json() -> bool  # Returns True if migration occurred

# TUI
class SnapbackApp(App)
class JobsTable(DataTable)
class EditJobModal(ModalScreen)
class EditDefaultsModal(ModalScreen)
class ConfirmModal(ModalScreen)
```

### CLI Addition

```python
@cli.command()
def configure():
    """Launch interactive configuration editor."""
    from textual.app import App
    # ... launch TUI
```

### Migration Strategy

On `snapback configure` or any command that reads config:

1. If `manifest.toml` exists, use it
2. Else if `jobs.json` exists:
   - Parse and convert to manifest format
   - Extract runtime state to `state.json`
   - Write both files
   - Prompt to delete `jobs.json`
3. Else create empty `manifest.toml`

### Backward Compatibility

- Existing commands (`daemon install`, `list`, etc.) should read from manifest
- Update `load_jobs()` to read manifest instead of `jobs.json`
- Update `save_job_config()` to write to manifest
- Update `update_job_last_run()` to write to `state.json`

## Testing

- Manual testing of TUI interactions
- Unit tests for manifest/state parsing
- Migration test with sample `jobs.json`

## Initial Jobs

```toml
[[jobs]]
name = "projects"
source = "~/projects"

[[jobs]]
name = "dotclaude"
source = "~/.claude"

[[jobs]]
name = "dotconfig"
source = "~/.config"
```
