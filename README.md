# dx_grab

A command-line tool to find and download files across DNAnexus projects.

## Requirements

- Python 3
- [dxpy](https://pypi.org/project/dxpy/)

## Installation

```bash
git clone https://github.com/eastgenomics/dx_grab.git
cd dx_grab
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate
```

## Authentication

Log in to DNAnexus before use:

```bash
dx login
```

## Usage

```
python3 dx_grab.py --name PATTERN [--project PATTERN] [--folder PATTERN]
                   [--exclude PATTERN] [--output DIR] [--limit N] [--dry-run]
python3 dx_grab.py --preset NAME [--output DIR] [--limit N] [--dry-run]
```

| Argument | Required | Description |
|---|---|---|
| `--name` | Yes* | Filename glob (e.g. `*.vcf.gz`). Case-insensitive. |
| `--preset` | No | Named search preset (see [Presets](#presets)). Replaces `--name`. |
| `--project` | No but HIGHLY recommended | Project name glob (e.g. `*230601*`). Case-insensitive. Default: all projects |
| `--folder` | No | Folder path glob (e.g. `*/fastq*`). Case-insensitive. Default: all folders |
| `--exclude` | No | Exclude files matching this glob (e.g. `*Q*`). Case-insensitive. Repeatable. |
| `--output` | No | Local download directory. Default: `./downloads` |
| `--limit` | No | Limit download to N files (all files are listed; live files are preferred) |
| `--dry-run` | No | List matched files without downloading |
| `--yes` | No | Automatically confirm unarchiving without prompting |
| `--skip-archived` | No | Automatically skip archived files without prompting |
| `--skip-existing` | No | Skip files that already exist at the local destination path |
| `--json` | No | Output matched/downloaded files as JSON instead of human-readable text |
| `--dedupe` | No | De-duplicate by filename (global), keeping the most recently created file |

\* Required unless `--preset` is used.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Error (bad arguments, auth failure, etc.) |
| `2` | No matching files found |

## Duplicate filenames

If multiple matched files share the same filename, you can use `--dedupe` to keep only the most recently created one.

## Presets

Presets are named shortcuts that bundle `--project`, `--name`, and `--folder` values. Individual arguments can still be overridden when using a preset.

| Preset | Description |
|---|---|
| `haem-vcf` | HaemOnc diagnostic pre-workbook mutect2 VCFs (2026) |
| `tso-vcf` | Solid tumour diagnostic pre-workbook VCFs (2026) |
| `twe-vcf` | RD diagnostic pre-workbook VCFs, TWE (2026) |
| `cen-vcf` | RD diagnostic pre-workbook VCFs, CEN (2026) |

## Examples

Use the `haem-vcf` preset to list HaemOnc pre-workbook mutect2 VCFs without downloading:

```bash
python3 dx_grab.py --preset haem-vcf --dry-run
```

List all VCFs across every accessible project without downloading:

```bash
python3 dx_grab.py --name "*.vcf.gz" --dry-run
```

List all VCFs across projects matching `*230601*` without downloading:

```bash
python3 dx_grab.py --project "*230601*" --name "*.vcf.gz" --dry-run
```

Download FASTQs from all matching run projects into a local directory:

```bash
python3 dx_grab.py --project "run_*" --folder "*/fastq*" --name "*.fastq.gz" --output ./fastqs
```

Download all BAMs from a specific project by ID:

```bash
python3 dx_grab.py --project "project-xxxx" --name "*.bam" --output ./downloads
```

Find `*.filter.vcf.gz` but exclude any filenames containing `Q`:

```bash
python3 dx_grab.py --project "*230601*" --name "*.filter.vcf.gz" --exclude "*Q*" --dry-run
```

Exclude multiple patterns:

```bash
python3 dx_grab.py --name "*.vcf.gz" --exclude "*Q*" --exclude "*fail*"
```

Download up to 5 files, preferring live files over archived ones:

```bash
python3 dx_grab.py --project "*230601*" --name "*.vcf.gz" --limit 5
```

Download non-interactively, skipping any archived files without prompting:

```bash
python3 dx_grab.py --preset haem-vcf --limit 20 --skip-archived --output ./vcfs
```

Download non-interactively, automatically submitting unarchive requests for archived files:

```bash
python3 dx_grab.py --preset haem-vcf --yes --output ./vcfs
```

Resume a partial download without re-downloading files already on disk:

```bash
python3 dx_grab.py --preset haem-vcf --limit 20 --skip-existing --output ./vcfs
```

List matched files as JSON (e.g. for scripting):

```bash
python3 dx_grab.py --preset haem-vcf --dry-run --json
```

## Archived files

DNAnexus files may be in one of four states: `live`, `unarchiving`, `archived`, or `archival`.

- **live** — downloaded immediately
- **archived** — dx-grab will prompt you to unarchive; unarchiving typically takes several hours
- **unarchiving** — dx-grab polls every 10 minutes and downloads once all files are live
- **archival** — file is currently being archived and cannot be retrieved; it will be skipped

If you interrupt the tool during polling (`Ctrl+C`), the unarchive request remains active on DNAnexus. Re-run the same command to resume polling and download once the files are ready.
