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
    excludes: list[str] | None = None,
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
        f"find -L {shlex.quote(dest)} -type f -printf '%T@ %s %p\\n'",
    ]
    excludes = excludes or []
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
    excluded_files = 0
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
        if excludes:
            fname = posixpath.basename(rel)
            if any(
                fnmatch.fnmatch(fname, pat) or fnmatch.fnmatch(rel, pat)
                or any(fnmatch.fnmatch(part, pat) for part in rel.split("/")[:-1])
                for pat in excludes
            ):
                excluded_files += 1
                debug(f"Remote excluded: {rel}")
                continue
        snapshot[rel] = (float(mtime_str), int(size_str))

    debug(
        f"Remote snapshot: {len(snapshot)} files parsed, "
        f"{skipped} skipped, {excluded_files} excluded"
    )
    return snapshot


def compute_diff(
    source: dict[str, tuple[float, int]],
    dest: dict[str, tuple[float, int]],
    mtime_tolerance: float = 2,
) -> tuple[list[str], list[str]]:
    """Return (files_to_copy, files_to_delete) based on mtime and size.

    source is the authoritative side, dest is the mirror.
    to_copy: files in source that are new or changed vs dest.
    to_delete: files in dest that are not in source.
    """
    to_copy = []
    for rel, (s_mtime, s_size) in source.items():
        if rel not in dest:
            to_copy.append(rel)
            debug(f"Diff: new file: {rel}")
        else:
            d_mtime, d_size = dest[rel]
            size_diff = s_size != d_size
            mtime_diff = abs(s_mtime - d_mtime)
            if size_diff or mtime_diff > mtime_tolerance:
                to_copy.append(rel)
                debug(
                    f"Diff: changed: {rel} "
                    f"(size {'DIFFERS' if size_diff else 'same'}: "
                    f"source={s_size} dest={d_size}, "
                    f"mtime delta={mtime_diff:.1f}s)"
                )
    to_delete = [rel for rel in dest if rel not in source]
    for rel in to_delete:
        debug(f"Diff: to delete (not in source): {rel}")
    debug(
        f"Diff result: {len(to_copy)} to copy, {len(to_delete)} to delete "
        f"(source={len(source)}, dest={len(dest)})"
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
        f"find -L {shlex.quote(dest)} -type d -empty -delete",
    ]
    debug(f"Cleaning empty dirs: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def pull_files(
    host: str, remote_source: str, port: int,
    local_dest: Path, files: list[str],
) -> None:
    """SCP files from remote to local, preserving mtime."""
    if not files:
        return

    # Create all needed local directories
    dirs = {os.path.dirname(f) for f in files if os.path.dirname(f)}
    for d in sorted(dirs):
        local_dir = local_dest / d
        local_dir.mkdir(parents=True, exist_ok=True)

    for rel in files:
        remote_path = _scp_remote_path(host, posixpath.join(remote_source, rel))
        local_path = local_dest / rel
        console.print(f"  [cyan]pulling[/cyan] {rel}")
        cmd = ["scp", "-p", *_scp_opts(port), remote_path, str(local_path)]
        debug(f"scp pull cmd: {' '.join(cmd)}")
        t0 = time.monotonic()
        subprocess.run(cmd, check=True)
        debug(f"scp pull completed in {time.monotonic() - t0:.3f}s")


def delete_local_files(local_dest: Path, files: list[str]) -> None:
    """Delete local files and clean up empty directories."""
    if not files:
        return

    for rel in files:
        file_path = local_dest / rel
        try:
            file_path.unlink()
            console.print(f"  [red]deleted[/red] {rel}")
        except FileNotFoundError:
            debug(f"Already gone: {rel}")

    # Clean up empty directories bottom-up
    for dirpath, dirnames, filenames in os.walk(local_dest, topdown=False):
        dp = Path(dirpath)
        if dp == local_dest:
            continue
        if not dirnames and not filenames:
            try:
                dp.rmdir()
                debug(f"Removed empty dir: {dirpath}")
            except OSError:
                pass


@click.command()
@click.option(
    "-c", "--config",
    default=DEFAULT_CONFIG,
    type=click.Path(exists=True),
    help="Path to YAML config file.",
)
@click.option("--source", default=None, help="Override push source directory.")
@click.option("--host", default=None, help="Override remote host.")
@click.option("--dest", default=None, help="Override push remote destination directory.")
@click.option("--pull-source", default=None, help="Override pull remote source directory.")
@click.option("--pull-dest", default=None, help="Override pull local destination directory.")
@click.option("--interval", default=None, type=int, help="Override default poll interval (seconds).")
@click.option("--push-interval", default=None, type=int, help="Override push poll interval (seconds).")
@click.option("--pull-interval", default=None, type=int, help="Override pull poll interval (seconds).")
@click.option("--ssh-port", default=None, type=int, help="Override SSH port.")
@click.option("--mtime-tolerance", default=None, type=float, help="Override mtime tolerance (seconds).")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable debug output.")
def mirror(
    config: str,
    source: str | None,
    host: str | None,
    dest: str | None,
    pull_source: str | None,
    pull_dest: str | None,
    interval: int | None,
    push_interval: int | None,
    pull_interval: int | None,
    ssh_port: int | None,
    mtime_tolerance: float | None,
    verbose: bool,
) -> None:
    """Mirror files between a local directory and a remote Linux host."""
    global _verbose
    _verbose = verbose

    cfg = load_config(config)
    debug(f"Loaded config from {config}: {cfg}")

    # Shared config
    remote_host = host or cfg["host"]
    default_interval: int = interval if interval is not None else cfg.get("interval", 5)
    push_ivl: int = push_interval if push_interval is not None else cfg.get("push_interval", default_interval)
    pull_ivl: int = pull_interval if pull_interval is not None else cfg.get("pull_interval", default_interval)
    port = ssh_port if ssh_port is not None else cfg.get("ssh_port", 22)
    tolerance = mtime_tolerance if mtime_tolerance is not None else cfg.get("mtime_tolerance", 2)
    excludes: list[str] = cfg.get("excludes", [])

    # Push config (optional)
    source_dir: str | None = source or cfg.get("source")
    remote_dest: str | None = dest or cfg.get("dest")
    do_push = bool(source_dir and remote_dest)
    source_path: Path | None = None
    if do_push:
        assert source_dir is not None and remote_dest is not None
        source_path = Path(source_dir)
        if not source_path.is_dir():
            raise click.BadParameter(f"Source directory does not exist: {source_dir}")

    # Pull config (optional)
    remote_pull_source: str | None = pull_source or cfg.get("pull_source")
    local_pull_dest: str | None = pull_dest or cfg.get("pull_dest")
    do_pull = bool(remote_pull_source and local_pull_dest)
    pull_dest_path: Path | None = None
    if do_pull:
        assert remote_pull_source is not None and local_pull_dest is not None
        pull_dest_path = Path(local_pull_dest)
        if not pull_dest_path.is_dir():
            raise click.BadParameter(
                f"Pull local dest does not exist: {local_pull_dest}"
            )

    if not do_push and not do_pull:
        raise click.UsageError(
            "Config must define 'source'+'dest' (push) and/or "
            "'pull_source'+'pull_dest' (pull)."
        )

    # Startup banner
    if do_push:
        console.print(
            f"[bold]Push:[/bold] {source_dir} -> {remote_host}:{remote_dest}"
        )
    if do_pull:
        console.print(
            f"[bold]Pull:[/bold] {remote_host}:{remote_pull_source} -> {local_pull_dest}"
        )
    if push_ivl == pull_ivl:
        console.print(f"[dim]Poll interval: {push_ivl}s | SSH port: {port}[/dim]")
    else:
        parts = []
        if do_push:
            parts.append(f"push {push_ivl}s")
        if do_pull:
            parts.append(f"pull {pull_ivl}s")
        console.print(f"[dim]Poll intervals: {', '.join(parts)} | SSH port: {port}[/dim]")
    if excludes:
        console.print(f"[dim]Excludes: {', '.join(excludes)}[/dim]")
    debug(
        f"mtime_tolerance={tolerance}s, "
        f"ssh_multiplexing={'ON (' + _CONTROL_PATH + ')' if _CAN_MULTIPLEX else 'OFF (Windows)'}"
    )

    # Push state
    push_dir_ensured = False
    previous_push_snapshot: dict[str, tuple[float, int]] | None = None

    # Pull state
    previous_pull_snapshot: dict[str, tuple[float, int]] | None = None

    host_was_reachable = True
    cycle = 0
    retry_interval = min(push_ivl if do_push else pull_ivl,
                         pull_ivl if do_pull else push_ivl)
    next_push_at = 0.0
    next_pull_at = 0.0

    try:
        while True:
            now = time.monotonic()

            # Sleep until next scheduled event
            next_times: list[float] = []
            if do_push:
                next_times.append(next_push_at)
            if do_pull:
                next_times.append(next_pull_at)
            wait = max(0.0, min(next_times) - now) if next_times else float(retry_interval)
            if wait > 0:
                debug(f"Sleeping {wait:.1f}s until next event")
                time.sleep(wait)
                now = time.monotonic()

            cycle += 1
            run_push = do_push and now >= next_push_at
            run_pull = do_pull and now >= next_pull_at
            debug(f"--- Cycle {cycle}: push={'YES' if run_push else 'no'}, pull={'YES' if run_pull else 'no'} ---")

            if not run_push and not run_pull:
                continue

            if not check_host_reachable(remote_host, port):
                if host_was_reachable:
                    console.print(
                        "[yellow]Host unreachable, waiting for connection...[/yellow]"
                    )
                    host_was_reachable = False
                # Push both schedules forward so we retry after a short wait
                if run_push:
                    next_push_at = now + retry_interval
                if run_pull:
                    next_pull_at = now + retry_interval
                continue

            if not host_was_reachable:
                console.print("[green]Host reachable, resuming.[/green]")
                host_was_reachable = True

            # --- PUSH ---
            if run_push:
                assert source_path is not None and remote_dest is not None
                local_snapshot = compute_local_snapshot(source_path, excludes)
                try:
                    if not push_dir_ensured:
                        cmd = [
                            "ssh", *_ssh_opts(port), remote_host,
                            f"mkdir -p {shlex.quote(remote_dest)}",
                        ]
                        debug(f"Ensuring remote dir: {' '.join(cmd)}")
                        subprocess.run(cmd, check=True)
                        push_dir_ensured = True

                    if previous_push_snapshot is None:
                        console.print(
                            "\n[bold]Push: first sync, checking consistency "
                            "with remote...[/bold]"
                        )
                        remote_snapshot = compute_remote_snapshot(
                            remote_host, remote_dest, port, excludes,
                        )
                        to_copy, to_delete = compute_diff(
                            local_snapshot, remote_snapshot, tolerance,
                        )
                    elif local_snapshot == previous_push_snapshot:
                        debug("Push: no local changes")
                        to_copy, to_delete = [], []
                    else:
                        console.print(
                            "\n[bold yellow]Push: changes detected, "
                            "syncing...[/bold yellow]"
                        )
                        if _verbose:
                            added = set(local_snapshot) - set(previous_push_snapshot)
                            removed = set(previous_push_snapshot) - set(local_snapshot)
                            modified = {
                                k for k in set(local_snapshot) & set(previous_push_snapshot)
                                if local_snapshot[k] != previous_push_snapshot[k]
                            }
                            if added:
                                debug(f"Push local added: {sorted(added)}")
                            if removed:
                                debug(f"Push local removed: {sorted(removed)}")
                            if modified:
                                debug(f"Push local modified: {sorted(modified)}")

                        remote_snapshot = compute_remote_snapshot(
                            remote_host, remote_dest, port, excludes,
                        )
                        to_copy, to_delete = compute_diff(
                            local_snapshot, remote_snapshot, tolerance,
                        )

                    if to_copy:
                        console.print(
                            f"[cyan]Push: copying {len(to_copy)} file(s)[/cyan]"
                        )
                        copy_files(
                            source_path, remote_host, remote_dest, port,
                            to_copy,
                        )
                    if to_delete:
                        console.print(
                            f"[red]Push: deleting {len(to_delete)} file(s)[/red]"
                        )
                        delete_remote_files(
                            remote_host, remote_dest, port, to_delete,
                        )

                    if to_copy or to_delete:
                        console.print(
                            f"[green]Push synced:[/green] {len(to_copy)} copied, "
                            f"{len(to_delete)} deleted"
                        )
                    elif previous_push_snapshot is None:
                        console.print("[green]Push: already in sync.[/green]")

                    previous_push_snapshot = local_snapshot
                except subprocess.CalledProcessError as e:
                    console.print(f"[red]Push sync error:[/red] {e}")
                    debug(f"CalledProcessError: cmd={e.cmd}, rc={e.returncode}")
                    if e.stderr:
                        debug(f"stderr: {e.stderr}")
                next_push_at = time.monotonic() + push_ivl

            # --- PULL ---
            if run_pull:
                assert pull_dest_path is not None and remote_pull_source is not None
                try:
                    remote_snapshot = compute_remote_snapshot(
                        remote_host, remote_pull_source, port, excludes,
                    )

                    if previous_pull_snapshot is not None and remote_snapshot == previous_pull_snapshot:
                        debug("Pull: no remote changes")
                    else:
                        if previous_pull_snapshot is None:
                            console.print(
                                "\n[bold]Pull: first sync, checking consistency "
                                "with local...[/bold]"
                            )
                        else:
                            console.print(
                                "\n[bold yellow]Pull: remote changes detected, "
                                "syncing...[/bold yellow]"
                            )

                        local_snapshot = compute_local_snapshot(
                            pull_dest_path, excludes,
                        )
                        to_copy, to_delete = compute_diff(
                            remote_snapshot, local_snapshot, tolerance,
                        )

                        if to_copy:
                            console.print(
                                f"[cyan]Pull: pulling {len(to_copy)} file(s)[/cyan]"
                            )
                            pull_files(
                                remote_host, remote_pull_source, port,
                                pull_dest_path, to_copy,
                            )
                        if to_delete:
                            console.print(
                                f"[red]Pull: deleting {len(to_delete)} file(s)[/red]"
                            )
                            delete_local_files(pull_dest_path, to_delete)

                        if to_copy or to_delete:
                            console.print(
                                f"[green]Pull synced:[/green] "
                                f"{len(to_copy)} pulled, {len(to_delete)} deleted"
                            )
                        elif previous_pull_snapshot is None:
                            console.print("[green]Pull: already in sync.[/green]")

                        previous_pull_snapshot = remote_snapshot
                except subprocess.CalledProcessError as e:
                    console.print(f"[red]Pull error:[/red] {e}")
                    debug(f"CalledProcessError: cmd={e.cmd}, rc={e.returncode}")
                    if e.stderr:
                        debug(f"stderr: {e.stderr}")
                next_pull_at = time.monotonic() + pull_ivl
    except KeyboardInterrupt:
        console.print("\n[bold]Stopped.[/bold]")


if __name__ == "__main__":
    mirror()
