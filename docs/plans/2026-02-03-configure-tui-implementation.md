# Configure TUI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `snapback configure` command with a Textual TUI for managing backup jobs, backed by `manifest.toml` config and `state.json` runtime data.

**Architecture:** Replace `jobs.json` with two files: `manifest.toml` (human-editable config with defaults + jobs) and `state.json` (machine-managed runtime state). Build a Textual app with DataTable for job list, modal forms for editing, and keybindings for daemon management.

**Tech Stack:** Python 3.10+, Textual, tomllib (stdlib), tomli-w (for writing TOML)

---

## Task 1: Add Dependencies

**Files:**
- Modify: `pyproject.toml:27-33`

**Step 1: Add textual and tomli-w to dependencies**

```toml
dependencies = [
    "click>=8.3.1",
    "loguru>=0.7.3",
    "questionary>=2.1.1",
    "rich>=14.2.0",
    "rich-click>=1.9.5",
    "textual>=0.50.0",
    "tomli-w>=1.0.0",
]
```

**Step 2: Install dependencies**

Run: `uv sync`
Expected: Dependencies installed successfully

**Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add textual and tomli-w dependencies"
```

---

## Task 2: Manifest and State File Constants

**Files:**
- Modify: `snapback.py:44-45`

**Step 1: Add new file path constants after existing ones**

After line 45 (`JOBS_FILE = CONFIG_DIR / "jobs.json"`), add:

```python
MANIFEST_FILE = CONFIG_DIR / "manifest.toml"
STATE_FILE = CONFIG_DIR / "state.json"
```

**Step 2: Add tomllib and tomli_w imports**

At top of file after `import json`, add:

```python
import tomllib
import tomli_w
```

**Step 3: Verify syntax**

Run: `uv run python -c "import snapback"`
Expected: No errors

**Step 4: Commit**

```bash
git add snapback.py
git commit -m "feat: add manifest and state file constants"
```

---

## Task 3: Manifest Load/Save Functions

**Files:**
- Modify: `snapback.py` (after line 100, after `get_job_key`)

**Step 1: Add default manifest structure constant**

```python
DEFAULT_MANIFEST: dict = {
    "defaults": {
        "dest": "~/Backups",
        "format": "7z",
        "restic_interval_hours": 4,
        "full_interval_days": 7,
    },
    "jobs": [],
}
```

**Step 2: Add load_manifest function**

```python
def load_manifest() -> dict:
    """Load manifest configuration."""
    if not MANIFEST_FILE.exists():
        return DEFAULT_MANIFEST.copy()
    try:
        return tomllib.loads(MANIFEST_FILE.read_text())
    except tomllib.TOMLDecodeError:
        logger.warning(f"Failed to parse {MANIFEST_FILE}, using defaults")
        return DEFAULT_MANIFEST.copy()
```

**Step 3: Add save_manifest function**

```python
def save_manifest(manifest: dict) -> None:
    """Save manifest configuration."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(tomli_w.dumps(manifest))
```

**Step 4: Add resolve_job_config function**

```python
def resolve_job_config(job: dict, defaults: dict) -> dict:
    """Resolve a job config by applying defaults for missing fields."""
    resolved = defaults.copy()
    resolved.update(job)
    return resolved
```

**Step 5: Verify syntax**

Run: `uv run python -c "from snapback import load_manifest, save_manifest; print(load_manifest())"`
Expected: Prints default manifest dict

**Step 6: Commit**

```bash
git add snapback.py
git commit -m "feat: add manifest load/save functions"
```

---

## Task 4: State Load/Save Functions

**Files:**
- Modify: `snapback.py` (after manifest functions)

**Step 1: Add load_state function**

```python
def load_state() -> dict:
    """Load runtime state."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
```

**Step 2: Add save_state function**

```python
def save_state(state: dict) -> None:
    """Save runtime state."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
```

**Step 3: Add get_job_state function**

```python
def get_job_state(source: Path) -> dict:
    """Get runtime state for a specific job."""
    state = load_state()
    key = get_job_key(source)
    return state.get(key, {})
```

**Step 4: Add update_job_state function**

```python
def update_job_state(source: Path, **updates) -> None:
    """Update runtime state for a job."""
    state = load_state()
    key = get_job_key(source)
    if key not in state:
        state[key] = {}
    state[key].update(updates)
    save_state(state)
```

**Step 5: Verify syntax**

Run: `uv run python -c "from snapback import load_state, save_state; print(load_state())"`
Expected: Prints empty dict `{}`

**Step 6: Commit**

```bash
git add snapback.py
git commit -m "feat: add state load/save functions"
```

---

## Task 5: Migration Function

**Files:**
- Modify: `snapback.py` (after state functions)

**Step 1: Add migrate_jobs_json function**

```python
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
```

**Step 2: Verify syntax**

Run: `uv run python -c "from snapback import migrate_jobs_json; print(migrate_jobs_json())"`
Expected: Prints `False` (no jobs.json to migrate in clean state)

**Step 3: Commit**

```bash
git add snapback.py
git commit -m "feat: add jobs.json migration function"
```

---

## Task 6: Update Existing Functions to Use Manifest

**Files:**
- Modify: `snapback.py:81-144` (the existing job functions)

**Step 1: Update load_jobs to read from manifest**

Replace the existing `load_jobs` function (lines 81-88):

```python
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

        jobs[key] = {
            "source": source,
            "dest": resolved.get("dest", ""),
            "name": resolved.get("name", ""),
            "options": {
                "use_7z": resolved.get("format") == "7z",
                "use_restic": resolved.get("format") == "restic",
                "hybrid": resolved.get("format") == "hybrid",
                "op_vault": resolved.get("op_vault"),
                "restic_interval_hours": resolved.get("restic_interval_hours"),
                "full_interval_days": resolved.get("full_interval_days"),
                "daemon_plist": job_state.get("daemon_plist"),
            },
            "last_runs": job_state.get("last_runs", {}),
        }

    return jobs
```

**Step 2: Update save_jobs to write manifest**

Replace existing `save_jobs` function (lines 91-94):

```python
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
        if opts.get("hybrid"):
            job_config["format"] = "hybrid"
        elif opts.get("use_restic"):
            job_config["format"] = "restic"
        elif not opts.get("use_7z", True):
            job_config["format"] = "tar.gz"
        # else: inherits default

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
```

**Step 3: Update update_job_last_run to use state**

Replace existing function (lines 136-144):

```python
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
```

**Step 4: Verify existing commands still work**

Run: `uv run python snapback.py list`
Expected: Shows job list (may be empty)

Run: `uv run python snapback.py jobs`
Expected: Shows jobs (may be empty)

**Step 5: Commit**

```bash
git add snapback.py
git commit -m "refactor: update job functions to use manifest/state"
```

---

## Task 7: Create Initial Manifest with User's Jobs

**Files:**
- Create: `~/.config/snapback/manifest.toml` (via code)

**Step 1: Add a helper command temporarily to create manifest**

For now, manually create the manifest:

Run:
```bash
mkdir -p ~/.config/snapback
cat > ~/.config/snapback/manifest.toml << 'EOF'
[defaults]
dest = "~/Backups"
format = "7z"
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
EOF
```

**Step 2: Verify manifest loads**

Run: `uv run python -c "from snapback import load_manifest; import json; print(json.dumps(load_manifest(), indent=2))"`
Expected: Shows the manifest with 3 jobs

**Step 3: Verify jobs command reads from manifest**

Run: `uv run python snapback.py jobs`
Expected: Shows 3 jobs: projects, dotclaude, dotconfig

**Step 4: Commit** (no code changes, just verification)

---

## Task 8: Basic Textual App Skeleton

**Files:**
- Modify: `snapback.py` (add TUI classes before CLI commands, around line 1690)

**Step 1: Add Textual imports at top of file**

After the existing imports, add:

```python
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RadioButton, RadioSet, Static
```

**Step 2: Add SnapbackApp class**

Before the `@cli.group()` decorator for daemon (around line 1696), add:

```python
class SnapbackApp(App):
    """Textual app for managing snapback jobs."""

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
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="jobs-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
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

            # Last run
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

            table.add_row(
                resolved.get("name", ""),
                source,
                resolved.get("dest", ""),
                resolved.get("format", "7z"),
                daemon_status,
                last_run,
            )

    def action_quit(self) -> None:
        self.exit()

    def action_add_job(self) -> None:
        self.notify("Add job (not implemented yet)")

    def action_edit_job(self) -> None:
        self.notify("Edit job (not implemented yet)")

    def action_delete_job(self) -> None:
        self.notify("Delete job (not implemented yet)")

    def action_install_daemon(self) -> None:
        self.notify("Install daemon (not implemented yet)")

    def action_uninstall_daemon(self) -> None:
        self.notify("Uninstall daemon (not implemented yet)")

    def action_run_now(self) -> None:
        self.notify("Run now (not implemented yet)")

    def action_edit_defaults(self) -> None:
        self.notify("Edit defaults (not implemented yet)")
```

**Step 3: Verify syntax**

Run: `uv run python -c "from snapback import SnapbackApp; print('OK')"`
Expected: Prints `OK`

**Step 4: Commit**

```bash
git add snapback.py
git commit -m "feat: add basic Textual app skeleton"
```

---

## Task 9: Add Configure Command

**Files:**
- Modify: `snapback.py` (add new CLI command after existing commands)

**Step 1: Add configure command before main()**

Before the `def main():` function (around line 2518), add:

```python
@cli.command()
def configure():
    """Launch interactive configuration editor."""
    app = SnapbackApp()
    app.run()
```

**Step 2: Test the TUI launches**

Run: `uv run python snapback.py configure`
Expected: TUI launches showing jobs table with 3 jobs, footer with keybindings. Press `q` to quit.

**Step 3: Commit**

```bash
git add snapback.py
git commit -m "feat: add snapback configure command"
```

---

## Task 10: Edit Job Modal

**Files:**
- Modify: `snapback.py` (add modal class before SnapbackApp)

**Step 1: Add EditJobModal class**

Before the `SnapbackApp` class, add:

```python
class EditJobModal(ModalScreen):
    """Modal for editing a job."""

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
            dest_placeholder = f"{self.defaults.get('dest', '~/Backups')} (inherited)"
            yield Label("Dest:")
            yield Input(value=dest_value, id="dest-input", placeholder=dest_placeholder)

            yield Label("Format:")
            with RadioSet(id="format-radio"):
                current_format = resolved.get("format", "7z")
                yield RadioButton("tar.gz", value=current_format == "tar.gz")
                yield RadioButton("7z", value=current_format == "7z")
                yield RadioButton("restic", value=current_format == "restic")
                yield RadioButton("hybrid", value=current_format == "hybrid")

            yield Label("1Password Vault (optional):")
            yield Input(value=self.job.get("op_vault", ""), id="op-vault-input", placeholder="Vault name")

            with Horizontal(id="buttons"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            name = self.query_one("#name-input", Input).value.strip()
            source = self.query_one("#source-input", Input).value.strip()
            dest = self.query_one("#dest-input", Input).value.strip()
            op_vault = self.query_one("#op-vault-input", Input).value.strip()

            radio_set = self.query_one("#format-radio", RadioSet)
            format_map = {0: "tar.gz", 1: "7z", 2: "restic", 3: "hybrid"}
            format_value = format_map.get(radio_set.pressed_index, "7z")

            if not name or not source:
                self.notify("Name and source are required", severity="error")
                return

            new_job = {"name": name, "source": source}
            if dest:
                new_job["dest"] = dest
            if format_value != self.defaults.get("format", "7z"):
                new_job["format"] = format_value
            if op_vault:
                new_job["op_vault"] = op_vault

            self.dismiss(new_job)
        else:
            self.dismiss(None)
```

**Step 2: Update SnapbackApp to use modal for add/edit**

Update the `action_add_job` method:

```python
    def action_add_job(self) -> None:
        manifest = load_manifest()
        defaults = manifest.get("defaults", {})
        self.push_screen(EditJobModal(job=None, defaults=defaults), self._on_job_edited)

    def _on_job_edited(self, result: dict | None) -> None:
        if result is None:
            return

        manifest = load_manifest()

        # Check if editing existing or adding new
        existing_idx = None
        for i, job in enumerate(manifest.get("jobs", [])):
            if job.get("name") == result.get("name"):
                existing_idx = i
                break

        if existing_idx is not None:
            manifest["jobs"][existing_idx] = result
        else:
            if "jobs" not in manifest:
                manifest["jobs"] = []
            manifest["jobs"].append(result)

        save_manifest(manifest)
        self.refresh_jobs()
        self.notify(f"Saved job: {result.get('name')}")
```

Update the `action_edit_job` method:

```python
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
```

**Step 3: Test add and edit**

Run: `uv run python snapback.py configure`
- Press `a` to add a job
- Fill in fields, press Save
- Press `e` on a job to edit
- Press `q` to quit

**Step 4: Commit**

```bash
git add snapback.py
git commit -m "feat: add job edit modal"
```

---

## Task 11: Delete Job with Confirmation

**Files:**
- Modify: `snapback.py`

**Step 1: Add ConfirmModal class**

Before `EditJobModal`, add:

```python
class ConfirmModal(ModalScreen):
    """Modal for confirmation dialogs."""

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
```

**Step 2: Update action_delete_job**

```python
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
            return

        manifest = load_manifest()
        jobs = manifest.get("jobs", [])
        if index < len(jobs):
            deleted = jobs.pop(index)
            manifest["jobs"] = jobs
            save_manifest(manifest)
            self.refresh_jobs()
            self.notify(f"Deleted job: {deleted.get('name')}")
```

**Step 3: Test delete**

Run: `uv run python snapback.py configure`
- Navigate to a job, press `d`
- Confirm deletion
- Verify job is removed

**Step 4: Commit**

```bash
git add snapback.py
git commit -m "feat: add delete job with confirmation"
```

---

## Task 12: Daemon Install/Uninstall from TUI

**Files:**
- Modify: `snapback.py`

**Step 1: Find the daemon install/uninstall logic**

The existing `daemon_install` and `daemon_uninstall` functions need to be callable from the TUI.

**Step 2: Update action_install_daemon**

```python
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
            # Build options
            use_restic = resolved.get("format") in ("restic", "hybrid")
            use_7z = resolved.get("format") in ("7z", "hybrid")
            hybrid = resolved.get("format") == "hybrid"
            op_vault = resolved.get("op_vault")

            self.notify(f"Installing daemon for {name}...")
            self.call_later(
                lambda: self._do_daemon_install(source, dest, name, use_restic, use_7z, hybrid, op_vault)
            )
        except Exception as e:
            self.notify(f"Failed to install daemon: {e}", severity="error")

    def _do_daemon_install(self, source: Path, dest: Path, name: str,
                           use_restic: bool, use_7z: bool, hybrid: bool, op_vault: str | None) -> None:
        """Run daemon install in background."""
        import subprocess

        cmd = [
            sys.executable, __file__, "daemon", "install",
            "--source", str(source),
            "--dest", str(dest),
            "--name", name,
        ]
        if use_restic:
            cmd.append("--restic")
        if not use_7z:
            cmd.extend(["--format", "tar.gz"])
        if hybrid:
            cmd.append("--hybrid")
        if op_vault:
            cmd.extend(["--op-vault", op_vault])

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            self.notify(f"Daemon installed for {name}")
        else:
            self.notify(f"Daemon install failed: {result.stderr}", severity="error")

        self.refresh_jobs()
```

**Step 3: Update action_uninstall_daemon**

```python
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
            return

        import subprocess

        cmd = [sys.executable, __file__, "daemon", "uninstall", "--name", name]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            self.notify(f"Daemon uninstalled for {name}")
        else:
            self.notify(f"Uninstall failed: {result.stderr}", severity="error")

        self.refresh_jobs()
```

**Step 4: Test daemon install/uninstall**

Run: `uv run python snapback.py configure`
- Select a job, press `i` to install daemon
- Press `u` to uninstall

**Step 5: Commit**

```bash
git add snapback.py
git commit -m "feat: add daemon install/uninstall from TUI"
```

---

## Task 13: Run Now from TUI

**Files:**
- Modify: `snapback.py`

**Step 1: Update action_run_now**

```python
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
        self.notify(f"Running backup for {name}...")
        self.call_later(lambda: self._do_run_backup(resolved))

    def _do_run_backup(self, job: dict) -> None:
        """Run backup in background."""
        import subprocess

        source = job.get("source", "")
        dest = job.get("dest", "")
        name = job.get("name", "")
        fmt = job.get("format", "7z")

        cmd = [
            sys.executable, __file__,
            "--source", source,
            "--dest", dest,
            "--name", name,
        ]

        if fmt == "restic":
            cmd.append("--restic")
        elif fmt == "hybrid":
            cmd.append("--hybrid")
        elif fmt == "tar.gz":
            cmd.extend(["--format", "tar.gz"])

        if job.get("op_vault"):
            cmd.extend(["--op-vault", job["op_vault"]])

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            self.notify(f"Backup completed for {name}")
        else:
            self.notify(f"Backup failed: {result.stderr}", severity="error")

        self.refresh_jobs()
```

**Step 2: Test run now**

Run: `uv run python snapback.py configure`
- Select a job, press `r`
- Verify backup runs

**Step 3: Commit**

```bash
git add snapback.py
git commit -m "feat: add run backup from TUI"
```

---

## Task 14: Edit Defaults Modal

**Files:**
- Modify: `snapback.py`

**Step 1: Add EditDefaultsModal class**

After `EditJobModal`, add:

```python
class EditDefaultsModal(ModalScreen):
    """Modal for editing defaults."""

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

            yield Label("Default Format:")
            with RadioSet(id="format-radio"):
                current_format = self.defaults.get("format", "7z")
                yield RadioButton("tar.gz", value=current_format == "tar.gz")
                yield RadioButton("7z", value=current_format == "7z")
                yield RadioButton("restic", value=current_format == "restic")
                yield RadioButton("hybrid", value=current_format == "hybrid")

            yield Label("Restic Interval (hours):")
            yield Input(value=str(self.defaults.get("restic_interval_hours", 4)), id="restic-interval-input")

            yield Label("Full Backup Interval (days):")
            yield Input(value=str(self.defaults.get("full_interval_days", 7)), id="full-interval-input")

            with Horizontal(id="defaults-buttons"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            dest = self.query_one("#dest-input", Input).value.strip()
            restic_interval = self.query_one("#restic-interval-input", Input).value.strip()
            full_interval = self.query_one("#full-interval-input", Input).value.strip()

            radio_set = self.query_one("#format-radio", RadioSet)
            format_map = {0: "tar.gz", 1: "7z", 2: "restic", 3: "hybrid"}
            format_value = format_map.get(radio_set.pressed_index, "7z")

            try:
                restic_hours = int(restic_interval)
                full_days = int(full_interval)
            except ValueError:
                self.notify("Intervals must be integers", severity="error")
                return

            new_defaults = {
                "dest": dest or "~/Backups",
                "format": format_value,
                "restic_interval_hours": restic_hours,
                "full_interval_days": full_days,
            }

            self.dismiss(new_defaults)
        else:
            self.dismiss(None)
```

**Step 2: Update action_edit_defaults**

```python
    def action_edit_defaults(self) -> None:
        manifest = load_manifest()
        defaults = manifest.get("defaults", {})
        self.push_screen(EditDefaultsModal(defaults), self._on_defaults_edited)

    def _on_defaults_edited(self, result: dict | None) -> None:
        if result is None:
            return

        manifest = load_manifest()
        manifest["defaults"] = result
        save_manifest(manifest)
        self.refresh_jobs()
        self.notify("Defaults saved")
```

**Step 3: Test edit defaults**

Run: `uv run python snapback.py configure`
- Press `s` to edit defaults
- Change values, save
- Verify changes persist

**Step 4: Commit**

```bash
git add snapback.py
git commit -m "feat: add edit defaults modal"
```

---

## Task 15: Final Polish and Cleanup

**Files:**
- Modify: `snapback.py`

**Step 1: Add title to app**

Update `SnapbackApp`:

```python
class SnapbackApp(App):
    """Textual app for managing snapback jobs."""

    TITLE = "Snapback"
    SUB_TITLE = "Backup Configuration"
```

**Step 2: Add cursor styling to table**

Update `on_mount`:

```python
    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("NAME", "SOURCE", "DEST", "FORMAT", "DAEMON", "LAST RUN")
        self.refresh_jobs()
```

**Step 3: Verify all commands still work**

Run:
- `uv run python snapback.py --help`
- `uv run python snapback.py list`
- `uv run python snapback.py jobs`
- `uv run python snapback.py configure`

**Step 4: Final commit**

```bash
git add snapback.py
git commit -m "feat: polish TUI appearance"
```

---

## Summary

After completing all tasks:

1. `manifest.toml` replaces `jobs.json` as the config source
2. `state.json` holds runtime data (last_runs, daemon paths)
3. `snapback configure` launches full Textual TUI
4. TUI supports: add/edit/delete jobs, edit defaults, daemon management, run-now
5. Existing commands continue to work via updated `load_jobs`/`save_jobs`
