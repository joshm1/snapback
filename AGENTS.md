# Snapback Development

## Running the CLI

```bash
uv run python snapback.py <command>
```

Examples:
```bash
uv run python snapback.py list
uv run python snapback.py daemon plist
uv run python snapback.py configure  # Launch TUI
uv run python snapback.py --help
```

## Project Structure

- `snapback.py` - Single-file CLI application
- `pyproject.toml` - Project dependencies (uses uv)

## Key Constants

- `DAEMON_NAMESPACE = "io.github.joshm1.snapback"` - LaunchAgent namespace
- `CONFIG_DIR = ~/.config/snapback/` - Config directory
- `MANIFEST_FILE = ~/.config/snapback/manifest.toml` - Job configurations (TUI-managed)
- `STATE_FILE = ~/.config/snapback/state.json` - Runtime state (daemon plist paths, last runs)
- `JOBS_FILE = ~/.config/snapback/jobs.json` - Legacy job configurations

## TUI Logging

When running the TUI (`snapback configure`), logs are written to `./logs/snapback.log` with rotation. This is useful for debugging TUI issues since console output is suppressed.

## Testing Changes

After modifying snapback.py, test with:
```bash
uv run python snapback.py list
uv run python snapback.py daemon plist --raw
uv run python snapback.py configure  # Test TUI
```
