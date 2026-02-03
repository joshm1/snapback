# Snapback Development

## Running the CLI

```bash
uv run python snapback.py <command>
```

Examples:
```bash
uv run python snapback.py list
uv run python snapback.py daemon plist
uv run python snapback.py --help
```

## Project Structure

- `snapback.py` - Single-file CLI application
- `pyproject.toml` - Project dependencies (uses uv)

## Key Constants

- `DAEMON_NAMESPACE = "io.github.joshm1.snapback"` - LaunchAgent namespace
- `CONFIG_DIR = ~/.config/snapback/` - Config directory
- `JOBS_FILE = ~/.config/snapback/jobs.json` - Saved job configurations

## Testing Changes

After modifying snapback.py, test with:
```bash
uv run python snapback.py list
uv run python snapback.py daemon plist --raw
```
