# laptop-sync

A CLI tool that mirrors files between a Windows machine and a remote Linux host over SSH. It supports both **push** (local → remote) and **pull** (remote → local) directions, polls for changes, and only copies what's needed — a true mirror that also deletes files removed from the source side.

## Requirements

- Python 3.14+
- [Astral uv](https://docs.astral.sh/uv/)
- Passwordless SSH to the remote host

## Install

```bash
uv sync
```

## Configuration

Copy and edit `laptop_sync.yaml`:

```yaml
# Push: local directory → remote host (optional)
source: "C:\\Projects\\myapp"
host: "user@linuxbox"
dest: "/home/user/mirror"

interval: 5            # default poll interval
push_interval: 5       # override for push (optional)
pull_interval: 30      # override for pull (optional)
ssh_port: 22
mtime_tolerance: 2
excludes:
  - ".git"
  - "__pycache__"
  - "*.pyc"
  - "node_modules"
  - ".env"

# Pull: remote directory → local (optional, pull_dest can be a symlink)
pull_source: "/home/user/configs"
pull_dest: "C:\\Users\\me\\configs"
```

You can configure push only, pull only, or both in the same config. All options except `excludes` can be overridden on the command line.

### Exclude patterns

The `excludes` list uses [fnmatch](https://docs.python.org/3/library/fnmatch.html) syntax. Patterns are matched against both filenames and relative paths, and excluded directories are not descended into (so excluding `.git` skips the entire tree).

## Usage

```bash
# Run with default laptop_sync.yaml
uv run main.py

# Use a different config file
uv run main.py -c my_config.yaml

# Override specific options
uv run main.py --host user@otherbox --interval 10

# Different intervals for push and pull
uv run main.py --push-interval 5 --pull-interval 60

# Enable verbose/debug output
uv run main.py -v
```

Push and pull can run on different intervals (e.g. push every 5s, pull every 60s). If `push_interval` or `pull_interval` are not set, they default to `interval`.

The tool runs in a loop, handling push and pull directions on independent schedules:

- **Push**: checks for local file changes (by mtime and size), copies changed files to the remote via `scp -p` (preserving timestamps), and deletes remote files no longer in the source.
- **Pull**: checks for remote file changes, copies changed files from the remote to the local destination, and deletes local files no longer on the remote.

The first iteration always does a full consistency check. Local pull destinations can be symlinks to directories.

If the remote host is unreachable (e.g. VPN not yet connected), the tool waits and retries each poll cycle until the host becomes available. SSH connection multiplexing is used on Unix to avoid per-file handshake overhead (auto-disabled on Windows).

Press `Ctrl+C` to stop.
