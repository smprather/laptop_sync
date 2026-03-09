# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- `uv run main.py` — run the mirror tool (requires `laptop_sync.yaml`)
- `uv sync` — install/sync dependencies

## Architecture

Single-file CLI tool (`main.py`) using rich-click. See `doc/architecture.md` for requirements and design constraints.

Key flow: poll loop → check host reachability → compute local snapshot (mtime + size, filtered by excludes) → on first iteration or local changes, fetch remote snapshot via single `ssh find -printf` call → diff → `scp -p` changed files / `ssh rm` deleted files → sleep.

Config is loaded from YAML (`laptop_sync.yaml` default), with CLI flags as overrides. Exclude patterns are YAML-only (no CLI flag).

SSH connection multiplexing (`ControlMaster`) is used on Unix to avoid per-file handshake overhead; on Windows it is automatically disabled since OpenSSH for Windows does not support Unix domain sockets. Host reachability is checked each cycle so the tool survives VPN delays or drops without crashing.

## Workflow

- Keep `README.md` in sync with any changes to configuration, CLI options, usage, or behavior.

## Conventions

- Use `scp` for file transfer and `ssh` for remote commands — no rsync, no SFTP
- Use `shlex.quote()` on all remote paths passed through SSH
- Batch remote operations (mkdir, rm) into single SSH calls to minimize roundtrips
- Use SSH connection multiplexing (`ControlMaster`/`ControlPersist`) on Unix; auto-disabled on Windows
- Check host reachability before each poll cycle; skip gracefully if unreachable
- Catch `CalledProcessError` inside the loop to survive transient SSH failures
- Compare files by mtime + size, never by content hash
- Preserve modification times on copy (`scp -p`) to prevent update loops
