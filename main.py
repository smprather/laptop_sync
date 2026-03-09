from __future__ import annotations

import fnmatch
import os
import posixpath
import shlex
import subprocess
import sys
import time
from pathlib import Path

import rich_click as click
import yaml
from rich.console import Console

console = Console()

DEFAULT_CONFIG = "laptop_sync.yaml"
_CONTROL_PATH = "~/.ssh/laptop-sync-%C"
_CAN_MULTIPLEX = sys.platform != "win32"
_verbose = False


def debug(msg: str) -> None:
    """Print a debug message when verbose mode is enabled."""
    if _verbose:
        console.print(f"[dim][DEBUG] {msg}[/dim]")


def _multiplex_opts() -> list[str]:
    """SSH multiplexing options (Unix only, no-op on Windows)."""
    if not _CAN_MULTIPLEX:
        return []
    return [
        "-o", f"ControlPath={_CONTROL_PATH}",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=60",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
    ]


def _ssh_opts(port: int) -> list[str]:
    """SSH options with port and connection multiplexing."""
    return ["-p", str(port)] + _multiplex_opts()


def _scp_opts(port: int) -> list[str]:
    """SCP options with port and connection multiplexing."""
    return ["-P", str(port)] + _multiplex_opts()


def _scp_remote_path(host: str, path: str) -> str:
    """Build the host:path argument for scp, quoting appropriately per platform.

    Windows OpenSSH uses SFTP mode by default, so the remote path is sent
    literally (not through a remote shell) and must NOT be shell-quoted.
    Unix scp may use legacy mode where the path goes through a remote shell,
    so shell quoting is needed to handle spaces and special characters.
    """
    if sys.platform == "win32":
        return f"{host}:{path}"
    return f"{host}:{shlex.quote(path)}"


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def check_host_reachable(host: str, port: int) -> bool:
    """Quick SSH connectivity check with short timeout."""
    cmd = ["ssh"] + _ssh_opts(port) + [
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        host, "true",
    ]
    debug(f"Reachability check: {' '.join(cmd)}")
    try:
        t0 = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        elapsed = time.monotonic() - t0
        reachable = result.returncode == 0
        debug(
            f"Reachability result: {'OK' if reachable else 'FAILED'} "
            f"(rc={result.returncode}, {elapsed:.2f}s)"
        )
        if not reachable and result.stderr.strip():
            debug(f"Reachability stderr: {result.stderr.strip()}")
        return reachable
    except subprocess.TimeoutExpired:
        debug("Reachability check timed out")
        return False


def compute_local_snapshot(
    source: Path, excludes: list[str] | None = None,
) -> dict[str, tuple[float, int]]:
    """Walk the source directory and return {relative_posix_path: (mtime, size)}."""
    excludes = excludes or []
    snapshot = {}
    t0 = time.monotonic()
    excluded_dirs = 0
    excluded_files = 0
    for root, dirs, files in os.walk(source):
        rel_root = Path(root).relative_to(source).as_posix()
        if rel_root == ".":
            rel_root = ""

        # Prune excluded directories in-place to avoid descending into them
        if excludes:
            kept = []
            for d in dirs:
                if any(fnmatch.fnmatch(d, pat) for pat in excludes):
                    excluded_dirs += 1
                    rel_dir = f"{rel_root}/{d}" if rel_root else d
                    debug(f"Excluded dir: {rel_dir}")
                else:
                    kept.append(d)
            dirs[:] = kept
        dirs.sort()

        for fname in sorted(files):
            rel = f"{rel_root}/{fname}" if rel_root else fname
            if excludes and any(
                fnmatch.fnmatch(fname, pat) or fnmatch.fnmatch(rel, pat)
                for pat in excludes
            ):
                excluded_files += 1
                debug(f"Excluded file: {rel}")
                continue
            file_path = Path(root) / fname
            st = file_path.stat()
            snapshot[rel] = (st.st_mtime, st.st_size)

    elapsed = time.monotonic() - t0
    debug(
        f"Local snapshot: {len(snapshot)} files in {elapsed:.3f}s"
        f" (excluded {excluded_dirs} dirs, {excluded_files} files)"
    )
    return snapshot


def compute_remote_snapshot(
    host: str, dest: str, port: int,
) -> dict[str, tuple[float, int]]:
    """SSH into the remote host and stat all files under dest.

    Uses find -printf with newline delimiters. Null delimiters would be more
    robust for filenames with newlines, but Windows text-mode pipes can drop
    null bytes.
    """
    cmd = [
        "ssh",
        *_ssh_opts(port),
        host,
        f"find {shlex.quote(dest)} -type f -printf '%T@ %s %p\\n'",
    ]
    debug(f"Remote snapshot cmd: {' '.join(cmd)}")
    t0 = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.monotonic() - t0
    debug(f"Remote snapshot SSH completed in {elapsed:.3f}s (rc={result.returncode})")
    debug(f"Remote stdout: {len(result.stdout)} chars")
    if result.returncode != 0:
        if result.stderr.strip():
            console.print(
                f"[yellow]Remote snapshot warning:[/yellow] {result.stderr.strip()}"
            )
        return {}

    dest_prefix = dest.rstrip("/") + "/"
    snapshot = {}
    skipped = 0
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) != 3:
            skipped += 1
            debug(f"Remote snapshot: skipped unparseable line: {line!r}")
            continue
        mtime_str, size_str, abs_path = parts
        if abs_path.startswith(dest_prefix):
            rel = abs_path[len(dest_prefix):]
        else:
            skipped += 1
            continue
        snapshot[rel] = (float(mtime_str), int(size_str))

    debug(f"Remote snapshot: {len(snapshot)} files parsed, {skipped} entries skipped")
    return snapshot


def compute_diff(
    local: dict[str, tuple[float, int]],
    remote: dict[str, tuple[float, int]],
    mtime_tolerance: float = 2,
) -> tuple[list[str], list[str]]:
    """Return (files_to_copy, files_to_delete) based on mtime and size."""
    to_copy = []
    for rel, (l_mtime, l_size) in local.items():
        if rel not in remote:
            to_copy.append(rel)
            debug(f"Diff: new file: {rel}")
        else:
            r_mtime, r_size = remote[rel]
            size_diff = l_size != r_size
            mtime_diff = abs(l_mtime - r_mtime)
            if size_diff or mtime_diff > mtime_tolerance:
                to_copy.append(rel)
                debug(
                    f"Diff: changed: {rel} "
                    f"(size {'DIFFERS' if size_diff else 'same'}: "
                    f"local={l_size} remote={r_size}, "
                    f"mtime delta={mtime_diff:.1f}s)"
                )
    to_delete = [rel for rel in remote if rel not in local]
    for rel in to_delete:
        debug(f"Diff: to delete (not in local): {rel}")
    debug(
        f"Diff result: {len(to_copy)} to copy, {len(to_delete)} to delete "
        f"(local={len(local)}, remote={len(remote)})"
    )
    return to_copy, to_delete


def copy_files(
    source: Path, host: str, dest: str, port: int, files: list[str],
) -> None:
    """Create remote directories and scp each changed file, preserving mtime."""
    if not files:
        return

    # Batch-create all needed remote directories in one SSH call
    dirs = {posixpath.dirname(f) for f in files if posixpath.dirname(f)}
    if dirs:
        mkdir_cmd = " && ".join(
            f"mkdir -p {shlex.quote(posixpath.join(dest, d))}" for d in sorted(dirs)
        )
        cmd = ["ssh", *_ssh_opts(port), host, mkdir_cmd]
        debug(f"Creating {len(dirs)} remote dirs: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)

    for rel in files:
        local_path = source / rel
        remote_path = _scp_remote_path(host, posixpath.join(dest, rel))
        console.print(f"  [cyan]copying[/cyan] {rel}")
        cmd = ["scp", "-p", *_scp_opts(port), str(local_path), remote_path]
        debug(f"scp cmd: {' '.join(cmd)}")
        t0 = time.monotonic()
        subprocess.run(cmd, check=True)
        debug(f"scp completed in {time.monotonic() - t0:.3f}s")


def delete_remote_files(
    host: str, dest: str, port: int, files: list[str],
) -> None:
    """Remove files from remote and clean up empty directories."""
    if not files:
        return

    batch_size = 100
    for i in range(0, len(files), batch_size):
        batch = files[i : i + batch_size]
        rm_cmd = " && ".join(
            f"rm -f {shlex.quote(posixpath.join(dest, f))}" for f in batch
        )
        cmd = ["ssh", *_ssh_opts(port), host, rm_cmd]
        debug(f"rm batch {i // batch_size + 1}: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
        for f in batch:
            console.print(f"  [red]deleted[/red] {f}")

    # Clean up empty directories
    cmd = [
        "ssh", *_ssh_opts(port), host,
        f"find {shlex.quote(dest)} -type d -empty -delete",
    ]
    debug(f"Cleaning empty dirs: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


@click.command()
@click.option(
    "-c", "--config",
    default=DEFAULT_CONFIG,
    type=click.Path(exists=True),
    help="Path to YAML config file.",
)
@click.option("--source", default=None, help="Override source directory.")
@click.option("--host", default=None, help="Override remote host.")
@click.option("--dest", default=None, help="Override remote destination directory.")
@click.option("--interval", default=None, type=int, help="Override poll interval (seconds).")
@click.option("--ssh-port", default=None, type=int, help="Override SSH port.")
@click.option("--mtime-tolerance", default=None, type=float, help="Override mtime tolerance (seconds).")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable debug output.")
def mirror(
    config: str,
    source: str | None,
    host: str | None,
    dest: str | None,
    interval: int | None,
    ssh_port: int | None,
    mtime_tolerance: float | None,
    verbose: bool,
) -> None:
    """Mirror files from a local directory to a remote Linux host."""
    global _verbose
    _verbose = verbose

    cfg = load_config(config)
    debug(f"Loaded config from {config}: {cfg}")

    # CLI flags override config file values
    source_dir = source or cfg["source"]
    remote_host = host or cfg["host"]
    remote_dest = dest or cfg["dest"]
    poll_interval = interval if interval is not None else cfg.get("interval", 5)
    port = ssh_port if ssh_port is not None else cfg.get("ssh_port", 22)
    tolerance = mtime_tolerance if mtime_tolerance is not None else cfg.get("mtime_tolerance", 2)
    excludes: list[str] = cfg.get("excludes", [])

    source_path = Path(source_dir)
    if not source_path.is_dir():
        raise click.BadParameter(f"Source directory does not exist: {source_dir}")

    console.print(f"[bold]Mirroring[/bold] {source_dir} -> {remote_host}:{remote_dest}")
    console.print(f"[dim]Poll interval: {poll_interval}s | SSH port: {port}[/dim]")
    if excludes:
        console.print(f"[dim]Excludes: {', '.join(excludes)}[/dim]")
    debug(
        f"mtime_tolerance={tolerance}s, "
        f"ssh_multiplexing={'ON (' + _CONTROL_PATH + ')' if _CAN_MULTIPLEX else 'OFF (Windows)'}"
    )

    remote_dir_ensured = False
    previous_local_snapshot: dict[str, tuple[float, int]] | None = None
    host_was_reachable = True  # start True so first unreachable message prints
    cycle = 0

    try:
        while True:
            cycle += 1
            debug(f"--- Poll cycle {cycle} ---")

            if not check_host_reachable(remote_host, port):
                if host_was_reachable:
                    console.print(
                        "[yellow]Host unreachable, waiting for connection...[/yellow]"
                    )
                    host_was_reachable = False
                time.sleep(poll_interval)
                continue

            if not host_was_reachable:
                console.print("[green]Host reachable, resuming.[/green]")
                host_was_reachable = True

            local_snapshot = compute_local_snapshot(source_path, excludes)

            try:
                # Ensure remote base directory exists on first successful connection
                if not remote_dir_ensured:
                    cmd = [
                        "ssh", *_ssh_opts(port), remote_host,
                        f"mkdir -p {shlex.quote(remote_dest)}",
                    ]
                    debug(f"Ensuring remote dir: {' '.join(cmd)}")
                    subprocess.run(cmd, check=True)
                    remote_dir_ensured = True

                if previous_local_snapshot is None:
                    console.print(
                        "\n[bold]First sync: checking consistency with remote...[/bold]"
                    )
                    remote_snapshot = compute_remote_snapshot(
                        remote_host, remote_dest, port,
                    )
                    to_copy, to_delete = compute_diff(
                        local_snapshot, remote_snapshot, tolerance,
                    )
                else:
                    if local_snapshot == previous_local_snapshot:
                        debug("No local changes detected, sleeping")
                        time.sleep(poll_interval)
                        continue

                    console.print(
                        "\n[bold yellow]Changes detected, syncing...[/bold yellow]"
                    )
                    # Log what changed locally
                    if _verbose:
                        added = set(local_snapshot) - set(previous_local_snapshot)
                        removed = set(previous_local_snapshot) - set(local_snapshot)
                        modified = {
                            k for k in set(local_snapshot) & set(previous_local_snapshot)
                            if local_snapshot[k] != previous_local_snapshot[k]
                        }
                        if added:
                            debug(f"Local added: {sorted(added)}")
                        if removed:
                            debug(f"Local removed: {sorted(removed)}")
                        if modified:
                            debug(f"Local modified: {sorted(modified)}")

                    remote_snapshot = compute_remote_snapshot(
                        remote_host, remote_dest, port,
                    )
                    to_copy, to_delete = compute_diff(
                        local_snapshot, remote_snapshot, tolerance,
                    )

                if to_copy:
                    console.print(f"[cyan]Copying {len(to_copy)} file(s)[/cyan]")
                    copy_files(source_path, remote_host, remote_dest, port, to_copy)

                if to_delete:
                    console.print(f"[red]Deleting {len(to_delete)} file(s)[/red]")
                    delete_remote_files(remote_host, remote_dest, port, to_delete)

                if not to_copy and not to_delete:
                    console.print("[green]Already in sync.[/green]")
                else:
                    console.print(
                        f"[green]Synced:[/green] {len(to_copy)} copied, "
                        f"{len(to_delete)} deleted"
                    )
            except subprocess.CalledProcessError as e:
                console.print(f"[red]Sync error:[/red] {e}")
                debug(f"CalledProcessError: cmd={e.cmd}, rc={e.returncode}")
                if e.stderr:
                    debug(f"stderr: {e.stderr}")
                console.print("[dim]Will retry next cycle.[/dim]")
                time.sleep(poll_interval)
                continue

            previous_local_snapshot = local_snapshot
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        console.print("\n[bold]Stopped.[/bold]")


if __name__ == "__main__":
    mirror()
