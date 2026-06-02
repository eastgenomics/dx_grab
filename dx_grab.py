#!/usr/bin/env python3
"""
dx-grab — Find and download files from DNAnexus projects.

Usage:
    python dx_grab.py --name PATTERN [--project PATTERN] [--folder PATTERN]
                      [--output DIR] [--dry-run]

Exit codes:
    0  Success (files downloaded, or --dry-run completed)
    1  Error (bad arguments, auth failure, etc.)
    2  No matching files found

This module is also importable as dx_grab. Public API:
    check_auth()       -- authenticate and return dxpy
    resolve_project()  -- resolve a project ID or name glob to (id, name)
    find_projects()    -- search projects by name pattern
    find_files()       -- search files across projects
    handle_archives()  -- prompt/unarchive/poll archived files
    download_files()   -- download a list of files
    resolve_local_path() -- assign local output paths
    fmt_size()         -- human-readable file size
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime


PRESETS = {
    "haem-vcf": {
        "project": "002_26*MYE",
        "name": "*.vcf.gz",
        "folder": "*eggd_vcf_rescue*",
    },
    "tso-vcf": {
        "project": "002_26*TSO500",
        "name": "*.filter.vcf.gz",
        "folder": "*eggd_generate_variant_workbook*",
    },
    "twe-vcf": {
        "project": "002_26*TWE",
        "name": "*.optimised_filtered.vcf.gz",
        "folder": "*eggd_optimised_filtering*",
    },
    "cen-vcf": {
        "project": "002_26*CEN",
        "name": "*.optimised_filtered.vcf.gz",
        "folder": "*eggd_optimised_filtering*",
    },
}


def parse_args():
    """Parse and return command-line arguments."""
    preset_names = ", ".join(PRESETS)
    parser = argparse.ArgumentParser(
        description="Find and download files across DNAnexus projects.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Presets ({preset_names}):
  haem-vcf  HaemOnc diagnostic pre-workbook mutect2 VCFs (2026)
            --project "002_26*MYE" --name "*.vcf.gz" --folder "*eggd_vcf_rescue*"
  tso-vcf   Solid tumour diagnostic pre-workbook VCFs (2026)
            --project "002_26*TSO500" --name "*.filter.vcf.gz" --folder "*eggd_generate_variant_workbook*"
  twe-vcf   RD diagnostic pre-workbook VCFs, TWE (2026)
            --project "002_26*TWE" --name "*.optimised_filtered.vcf.gz" --folder "*eggd_optimised_filtering*"
  cen-vcf   RD diagnostic pre-workbook VCFs, CEN (2026)
            --project "002_26*CEN" --name "*.optimised_filtered.vcf.gz" --folder "*eggd_optimised_filtering*"

Examples:
  python dx_grab.py --preset haem-vcf --dry-run
  python dx_grab.py --name "*.vcf.gz" --dry-run
  python dx_grab.py --project "*230601*" --name "*.vcf.gz" --dry-run
  python dx_grab.py --project "run_*" --folder "*/fastq*" --name "*.fastq.gz" --output ./fastqs
  python dx_grab.py --project "project-xxxx" --name "*.bam"
        """,
    )
    parser.add_argument(
        "--preset",
        default=None,
        metavar="NAME",
        choices=PRESETS,
        help=f"Named search preset. One of: {preset_names}",
    )
    parser.add_argument(
        "--project",
        default=None,
        metavar="PATTERN",
        help="Project name glob pattern (e.g. '*230601*', 'run_*') or bare project ID (project-xxxx). Default: all projects.",
    )
    parser.add_argument(
        "--folder",
        default=None,
        metavar="PATTERN",
        help="Folder path glob pattern to filter by (e.g. '*/fastq*'). Default: all folders.",
    )
    parser.add_argument(
        "--name",
        default=None,
        metavar="PATTERN",
        help="Filename glob pattern (e.g. '*.vcf.gz')",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Exclude files whose name matches this glob (e.g. '*Q*'). Repeatable.",
    )
    parser.add_argument(
        "--output",
        default="./downloads",
        metavar="DIR",
        help="Local download directory. Default: ./downloads",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Limit download to the first N matched files. All files are listed; only N are downloaded.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matched files without downloading.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Automatically confirm unarchiving of archived files without prompting.",
    )
    parser.add_argument(
        "--skip-archived",
        action="store_true",
        help="Automatically skip archived files without prompting.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that already exist at the local destination path.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON summary of matched/downloaded files to stdout instead of human-readable output.",
    )
    parser.add_argument(
        "--dedupe",
        action="store_true",
        help="De-duplicate by filename (global), keeping the most recently modified file.",
    )
    args = parser.parse_args()

    if args.preset:
        preset = PRESETS[args.preset]
        if args.project is None:
            args.project = preset.get("project")
        if args.name is None:
            args.name = preset.get("name")
        if args.folder is None:
            args.folder = preset.get("folder")

    if args.name is None:
        parser.error("the following arguments are required: --name (or use --preset)")

    return args


def check_auth():
    """Authenticate with DNAnexus and return the dxpy module.

    Raises RuntimeError if not logged in or if the API call fails.
    """
    import dxpy
    try:
        dxpy.whoami()
    except dxpy.exceptions.DXAPIError as e:
        if e.code == 401:
            raise RuntimeError("Not logged in to DNAnexus. Run `dx login` first.") from e
        raise RuntimeError(f"DNAnexus API error: {e}") from e
    except Exception as e:
        raise RuntimeError(str(e)) from e
    return dxpy


def fmt_size(n_bytes):
    """Return n_bytes as a human-readable string (e.g. '1.5 MB')."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} PB"


def _log(msg, emit_json=False):
    """Print msg to stderr when emit_json is True, stdout otherwise."""
    print(msg, file=sys.stderr if emit_json else sys.stdout)


def _glob_to_iregex(pattern):
    """Convert a glob to a case-insensitive regex using [xX] character classes.

    Uses explicit character classes rather than (?i) so it works across both
    findProjects and findDataObjects, which use different regex engines.
    """
    parts = []
    for c in pattern:
        if c == "*":
            parts.append(".*")
        elif c == "?":
            parts.append(".")
        elif c.isalpha():
            parts.append(f"[{c.lower()}{c.upper()}]")
        else:
            parts.append(re.escape(c))
    return "^" + "".join(parts) + "$"


def find_projects(dxpy, pattern, emit_json=False):
    """Return DNAnexus projects whose name matches pattern.

    Print matching projects to stdout. Raises ValueError if none are found.
    When pattern is None, return all accessible projects.
    """
    if pattern:
        _log(f"\nSearching for projects matching: {pattern!r}", emit_json)
        projects = list(dxpy.find_projects(describe=True, name=_glob_to_iregex(pattern), name_mode="regexp"))
    else:
        _log("\nSearching all accessible projects...", emit_json)
        projects = list(dxpy.find_projects(describe=True))
    if not projects:
        msg = f"No projects found matching {pattern!r}." if pattern else "No accessible projects found."
        raise ValueError(msg)
    _log(f"Found {len(projects)} project(s):", emit_json)
    for p in projects:
        _log(f"  {p['describe']['name']}  ({p['id']})", emit_json)
    return projects


def find_files(dxpy, projects, name_pattern, folder_pattern, emit_json=False):
    """Return files matching name_pattern across the given projects.

    name_pattern is a glob converted to a case-insensitive regex.
    folder_pattern is an optional glob; only files in matching folders
    are returned. Skips projects the caller lacks permission to access.
    """
    _log(
        f"\nSearching for files matching name={name_pattern!r}"
        + (f", folder={folder_pattern!r}" if folder_pattern else "")
        + " ...",
        emit_json,
    )

    results = []
    for proj in projects:
        proj_id = proj["id"]
        proj_name = proj["describe"]["name"]

        try:
            hits = dxpy.find_data_objects(
                classname="file",
                project=proj_id,
                name=_glob_to_iregex(name_pattern),
                name_mode="regexp",
                folder="/",
                recurse=True,
                describe=True,
            )
            for h in hits:
                desc = h["describe"]
                folder = desc.get("folder", "/")

                if folder_pattern and not fnmatch.fnmatch(folder.lower(), folder_pattern.lower()):
                    continue

                results.append({
                    "file_id": h["id"],
                    "project_id": proj_id,
                    "project_name": proj_name,
                    "name": desc["name"],
                    "folder": folder,
                    "size": desc.get("size", 0),
                    "archival_state": desc.get("archivalState", "live"),
                    "created": desc.get("created", 0),
                    "modified": desc.get("modified", 0),
                })
        except dxpy.exceptions.PermissionDenied:
            print(f"  WARNING: Permission denied for project {proj_name} ({proj_id}), skipping.",
                  file=sys.stderr)
        except dxpy.exceptions.ResourceNotFound:
            print(f"  WARNING: Project {proj_name} ({proj_id}) not found, skipping.",
                  file=sys.stderr)

    return results


def dedupe_by_name_keep_newest(files):
    """De-duplicate by filename (case-insensitive), keeping most recently modified.

    Uses 'modified' when available, falling back to 'created'. Tie-breakers:
    prefer live files, then stable ordering by file_id.
    """
    best = {}
    for f in files:
        key = f["name"].lower()
        cur = best.get(key)
        if cur is None:
            best[key] = f
            continue

        f_ts = f.get("modified") or f.get("created") or 0
        cur_ts = cur.get("modified") or cur.get("created") or 0

        if f_ts > cur_ts:
            best[key] = f
            continue
        if f_ts < cur_ts:
            continue

        f_live = 1 if f.get("archival_state") == "live" else 0
        cur_live = 1 if cur.get("archival_state") == "live" else 0
        if f_live > cur_live:
            best[key] = f
            continue
        if f_live < cur_live:
            continue

        if str(f.get("file_id", "")) > str(cur.get("file_id", "")):
            best[key] = f

    return list(best.values())


def print_table(files, emit_json=False):
    """Print a formatted table of files, or a JSON array when emit_json is True.

    When emit_json is True, progress messages go to stderr and the JSON
    array goes to stdout so it can be piped to other tools.
    """
    if not files:
        if emit_json:
            print("[]")
        else:
            print("No files found.")
        return

    if emit_json:
        print(json.dumps([{
            "file_id": f["file_id"],
            "project_id": f["project_id"],
            "project_name": f["project_name"],
            "folder": f["folder"],
            "name": f["name"],
            "size": f["size"],
            "archival_state": f["archival_state"],
        } for f in files]))
        return

    col_proj = max(len(f["project_name"]) for f in files)
    col_folder = max(len(f["folder"]) for f in files)
    col_name = max(len(f["name"]) for f in files)
    col_size = max(len(fmt_size(f["size"])) for f in files)
    col_state = max(len(f["archival_state"]) for f in files)

    # header
    h = (f"{'Project':<{col_proj}}  {'Folder':<{col_folder}}  "
         f"{'Name':<{col_name}}  {'Size':>{col_size}}  {'State':<{col_state}}")
    print("\n" + h)
    print("-" * len(h))
    for f in files:
        print(f"{f['project_name']:<{col_proj}}  {f['folder']:<{col_folder}}  "
              f"{f['name']:<{col_name}}  {fmt_size(f['size']):>{col_size}}  "
              f"{f['archival_state']:<{col_state}}")

    total = sum(f["size"] for f in files)
    print(f"\nTotal: {len(files)} file(s), {fmt_size(total)}")


def handle_archives(dxpy, files, auto_yes=False, skip_archived=False, on_live=None, emit_json=False):
    """Prompt about archived files, unarchive if needed, and poll until live.

    Both 'archived' and 'archival' (archiving in progress) files require
    unarchiving; for 'archival' files, unarchiving cancels the operation.
    Returns the (possibly updated) file list.

    Args:
        auto_yes:     submit unarchive requests without prompting.
        skip_archived: remove archived files from the list without prompting.
        on_live:      optional callback called with each batch of newly-live
                      files as they become available during polling, so
                      downloads can start immediately rather than waiting
                      for all files.
    """
    # Both 'archived' and 'archival' (currently being archived) can be unarchived;
    # for 'archival' files, unarchiving cancels the in-progress archive operation.
    needs_unarchive = [f for f in files if f["archival_state"] in ("archived", "archival")]
    unarchiving = [f for f in files if f["archival_state"] == "unarchiving"]

    if needs_unarchive:
        archival_count = sum(1 for f in needs_unarchive if f["archival_state"] == "archival")
        archived_count = sum(1 for f in needs_unarchive if f["archival_state"] == "archived")
        parts = []
        if archived_count:
            parts.append(f"{archived_count} archived")
        if archival_count:
            parts.append(f"{archival_count} currently being archived (unarchiving will cancel this)")
        _log(f"\n{len(needs_unarchive)} file(s) need unarchiving ({', '.join(parts)}):", emit_json)
        for f in needs_unarchive:
            _log(
                f"  [{f['archival_state']}] {f['project_name']}{f['folder']}/{f['name']}  ({fmt_size(f['size'])})",
                emit_json,
            )

        if skip_archived:
            _log("Skipping archived files (--skip-archived).", emit_json)
            files = [f for f in files if f["archival_state"] not in ("archived", "archival")]
        elif auto_yes:
            _log("Unarchiving automatically (--yes).", emit_json)
            submitted = _submit_unarchive(dxpy, needs_unarchive, emit_json=emit_json)
            for f in needs_unarchive:
                if f["file_id"] in submitted:
                    f["archival_state"] = "unarchiving"
                    unarchiving.append(f)
        else:
            if emit_json:
                _log("\nUnarchive them? Unarchiving typically takes several hours. [y/N]", emit_json=True)
                answer = input().strip().lower()
            else:
                answer = input("\nUnarchive them? Unarchiving typically takes several hours. [y/N] ").strip().lower()
            if answer in ("y", "yes"):
                submitted = _submit_unarchive(dxpy, needs_unarchive, emit_json=emit_json)
                for f in needs_unarchive:
                    if f["file_id"] in submitted:
                        f["archival_state"] = "unarchiving"
                        unarchiving.append(f)
            else:
                _log(f"Skipping {len(needs_unarchive)} file(s) (got: {answer!r}).", emit_json)
                files = [f for f in files if f["archival_state"] not in ("archived", "archival")]

    if unarchiving:
        files = _poll_until_live(dxpy, files, unarchiving, on_live=on_live, emit_json=emit_json)

    return files


def _submit_unarchive(dxpy, files, emit_json=False):
    """Group files by project and submit unarchive requests (max 1000 per call).

    Returns the set of file IDs whose unarchive request was accepted.
    """
    by_project = defaultdict(list)
    for f in files:
        by_project[f["project_id"]].append(f["file_id"])

    submitted_ids = set()
    for proj_id, file_ids in by_project.items():
        for i in range(0, len(file_ids), 1000):
            batch = file_ids[i:i + 1000]
            try:
                dxpy.api.project_unarchive(proj_id, {"files": batch})
                submitted_ids.update(batch)
            except Exception as e:
                print(f"  WARNING: Unarchive request failed for {proj_id}: {e}", file=sys.stderr)

    _log(f"Unarchive requested for {len(submitted_ids)} of {len(files)} file(s).", emit_json)
    return submitted_ids


def _poll_until_live(dxpy, all_files, waiting, on_live=None, emit_json=False):
    """Poll every 10 minutes until all waiting files are live.

    Calls on_live(batch) with each batch of newly-live files as they appear,
    so downloads can start immediately rather than waiting for all files.
    """
    waiting_ids = {f["file_id"] for f in waiting}
    file_index = {f["file_id"]: f for f in all_files}
    total = len(waiting_ids)   # original batch size — denominator stays fixed
    done  = 0                  # cumulative count of files that have gone live

    _log(f"\nWaiting for {total} file(s) to unarchive (polling every 10 minutes).", emit_json)
    _log("Press Ctrl+C to abort — re-run the same command to resume.\n", emit_json)

    try:
        while waiting_ids:
            now = datetime.now().strftime("%H:%M")
            still_waiting = set()
            newly_live = []
            for fid in waiting_ids:
                f = file_index[fid]
                try:
                    state = dxpy.DXFile(fid, project=f["project_id"]).describe(
                        fields={"archivalState": True}
                    )["archivalState"]
                    f["archival_state"] = state
                    if state != "live":
                        still_waiting.add(fid)
                    else:
                        newly_live.append(f)
                except Exception as e:
                    print(f"  WARNING: Could not check state of {fid}: {e}", file=sys.stderr)
                    still_waiting.add(fid)

            done += len(newly_live)
            _log(f"[{now}] Waiting for unarchive: {done}/{total} file(s) live ...", emit_json)

            if newly_live and on_live:
                on_live(newly_live)

            if not still_waiting:
                break

            waiting_ids = still_waiting
            time.sleep(600)  # 10 minutes

    except KeyboardInterrupt:
        _log("\n\nUnarchiving in progress on DNAnexus. Re-run the same command to resume.", emit_json)
        sys.exit(0)

    return all_files


def resolve_local_path(output_dir, files):
    """Assign a local path to each file dict in-place and return the list.

    When two files from different projects share the same filename, prefix
    each local name with the sanitised project name to avoid collisions.
    """
    name_to_projects = defaultdict(set)
    for f in files:
        name_to_projects[f["name"]].add(f["project_id"])

    for f in files:
        if len(name_to_projects[f["name"]]) > 1:
            safe_proj = f["project_name"].replace("/", "_")
            local_name = f"{safe_proj}__{f['name']}"
        else:
            local_name = f["name"]
        f["local_path"] = os.path.join(output_dir, local_name)

    return files


def download_files(dxpy, files, output_dir, skip_existing=False, emit_json=False):
    """Download live files to output_dir.

    Non-live files are skipped with a warning. When skip_existing is True,
    files already present at their local destination are also skipped.
    When emit_json is True, progress goes to stderr and a JSON summary of
    downloaded files goes to stdout.
    """
    os.makedirs(output_dir, exist_ok=True)
    if any("local_path" not in f for f in files):
        files = resolve_local_path(output_dir, files)

    live = [f for f in files if f["archival_state"] == "live"]
    skipped = len(files) - len(live)

    if skipped:
        _log(f"\nSkipping {skipped} non-live file(s).", emit_json)

    if skip_existing:
        existing = [f for f in live if os.path.exists(f["local_path"])]
        if existing:
            _log(f"Skipping {len(existing)} file(s) that already exist locally (--skip-existing).", emit_json)
            skipped += len(existing)
        live = [f for f in live if not os.path.exists(f["local_path"])]

    if not live:
        _log("Nothing to download.", emit_json)
        if emit_json:
            print(json.dumps({"downloaded": [], "skipped": skipped}))
        return

    total_size = sum(f["size"] for f in live)
    _log(f"\nDownloading {len(live)} file(s) ({fmt_size(total_size)}) to {output_dir}/\n", emit_json)

    downloaded = []
    for i, f in enumerate(live, 1):
        local = f["local_path"]
        _log(f"[{i}/{len(live)}] {f['name']}  ({fmt_size(f['size'])})...", emit_json)
        try:
            dxpy.download_dxfile(f["file_id"], local, project=f["project_id"])
            _log(f"  -> {local}", emit_json)
            downloaded.append({
                "file_id": f["file_id"],
                "project_id": f["project_id"],
                "project_name": f["project_name"],
                "name": f["name"],
                "folder": f["folder"],
                "size": f["size"],
                "local_path": local,
            })
        except dxpy.exceptions.ResourceNotFound:
            print(f"  WARNING: File not found: {f['file_id']}", file=sys.stderr)
        except Exception as e:
            print(f"  WARNING: Download failed for {f['name']}: {e}", file=sys.stderr)

    _log("\nDone.", emit_json)

    if emit_json:
        print(json.dumps({"downloaded": downloaded, "skipped": skipped}))


def resolve_project(dxpy, project_arg):
    """Resolve a project ID or name glob to (project_id, project_name).

    Accepts:
      - A project ID directly (project-xxx...)
      - A name glob pattern — must match exactly one project.

    Raises ValueError if the project cannot be resolved unambiguously.
    """
    if project_arg.startswith("project-"):
        try:
            desc = dxpy.DXProject(project_arg).describe()
            return project_arg, desc["name"]
        except Exception as e:
            raise ValueError(f"Could not describe project '{project_arg}': {e}") from e

    projects = find_projects(dxpy, project_arg)  # prints matches, raises ValueError if none found

    if len(projects) > 1:
        raise ValueError(
            f"Pattern '{project_arg}' matches {len(projects)} projects — be more specific."
        )

    p = projects[0]
    return p["id"], p["describe"]["name"]


def main():
    """Entry point: find and download files, handling archives interactively."""
    args = parse_args()

    try:
        dxpy = check_auth()

        if args.project and args.project.startswith("project-"):
            proj_id, proj_name = resolve_project(dxpy, args.project)
            projects = [{"id": proj_id, "describe": {"name": proj_name}}]
        else:
            projects = find_projects(dxpy, args.project, emit_json=args.json)
        files = find_files(dxpy, projects, args.name, args.folder, emit_json=args.json)

        if args.exclude:
            before = len(files)
            files = [
                f for f in files
                if not any(fnmatch.fnmatch(f["name"].lower(), pat.lower()) for pat in args.exclude)
            ]
            excluded = before - len(files)
            if excluded:
                _log(f"Excluded {excluded} file(s) matching: {', '.join(args.exclude)}", emit_json=args.json)

        if args.dedupe:
            before_dedup = len(files)
            files = dedupe_by_name_keep_newest(files)
            if len(files) != before_dedup:
                _log(
                    f"De-duplicated by filename: kept {len(files)} newest of {before_dedup} matched file(s).",
                    emit_json=args.json,
                )

        if not files:
            _log("\nNo matching files found.", emit_json=args.json)
            sys.exit(2)

        print_table(files, emit_json=args.json)

        if args.dry_run:
            sys.exit(0)

        if args.limit is not None:
            if args.limit < len(files):
                # Sort live files first so the limit is filled without touching archived files
                files = sorted(files, key=lambda f: 0 if f["archival_state"] == "live" else 1)
                _log(
                    f"\nLimit set: downloading {args.limit} of {len(files)} matched file(s) "
                    f"(live files preferred).",
                    emit_json=args.json,
                )
            files = files[:args.limit]

        # Resolve destination paths once across the full selection so collision
        # detection is stable across incremental download batches.
        files = resolve_local_path(args.output, files)

        def _download(batch):
            download_files(dxpy, batch, args.output, skip_existing=args.skip_existing, emit_json=args.json)

        # Download already-live files immediately without waiting for unarchiving ones
        live = [f for f in files if f["archival_state"] == "live"]
        if live:
            _download(live)

        handle_archives(
            dxpy,
            files,
            auto_yes=args.yes,
            skip_archived=args.skip_archived,
            on_live=_download,
            emit_json=args.json,
        )

    except (ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
