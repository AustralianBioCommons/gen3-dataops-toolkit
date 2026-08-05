# S3 ingest: the tag contract and provenance columns

The helpers in `src/g3dt/ingest/ingest.py` are **optional,
bring-your-own-ingestion utilities**. Use them when you already have flat
files (CSV / JSON / XLSX) landing in S3 and want them queryable in Athena
with full provenance. They are *not* the platform's primary submission
path: the supported no-code route is filling in a
gen3-metadata-templates workbook, which flows into the bronze layer
automatically — see
[DATA_LAYERS.md](https://github.com/AustralianBioCommons/aws-gen3-pipeline/blob/main/docs/DATA_LAYERS.md).

## The tag contract

Files opt in to ingestion via **S3 object tags** — not by their location,
name, or upload time.

| Tag | Required | Purpose |
| --- | --- | --- |
| `ingest` | for discovery | `get_ingest_true_files` keeps only objects tagged `ingest=true` |
| `node` | yes | names the target table: `{table_prefix}_{node}` |
| `study_id` | yes | becomes the `study_id` column (and a partition for parquet) |
| `submission_date` | yes | parsed to `YYYY-MM-DD` and stamped on every row |

`submission_date` accepts `YYYY-MM-DD` or `DD-MM-YYYY`, with `_` or spaces
as separators (`sanitize_submission_date`); anything else raises
`ValueError`. A missing `node` or `submission_date` tag fails the file
with `ValueError`; a missing `study_id` fails with a `KeyError`.

Every tag on the object — required or not — is also carried into the
table as a `tag_<normalised key>` column.

> **Untagged objects are silently skipped — by design.** This is the #1
> surprise. `get_ingest_true_files` treats the `ingest=true` tag as the
> "this file is ready" signal: an object with no tags, unreadable tags,
> or any value other than the literal string `true` is dropped from the
> scan with only a debug-level log line. If a file you expected is
> missing from the bronze table, check its tags first:
> `aws s3api get-object-tagging --bucket <bucket> --key <key>`.

## Operational sharp edges

- **IAM**: the scanner calls `GetObjectTagging` on every listed object,
  so the ingesting role needs `s3:GetObjectTagging` (plus
  `s3:ListBucket` / `s3:GetObject`). Whoever uploads and tags files
  needs `s3:PutObjectTagging`.
- **Copies lose tags.** The high-level `aws s3 cp` / `aws s3 sync`
  commands do not preserve object tags, so a copied file silently drops
  out of the ingest scan. Use
  `aws s3api copy-object ... --tagging-directive COPY` to propagate
  tags, or re-tag after copying.
- **Console uploads aren't tagged** unless you expand the upload
  wizard's properties step and add tags by hand. Uploads from scripts
  are untagged unless the script tags them.
- Tagging an existing object is one call:

  ```bash
  aws s3api put-object-tagging \
    --bucket my-bucket \
    --key raw/mystudy/samples.csv \
    --tagging 'TagSet=[{Key=ingest,Value=true},{Key=study_id,Value=mystudy},{Key=node,Value=sample},{Key=submission_date,Value=2026-08-05}]'
  ```

  (Note: `put-object-tagging` *replaces* the whole tag set — include
  every tag, not just the one you are adding.)

## Provenance columns

`prepare_ingest_metadata` stamps these columns on every row, alongside
the file's raw columns:

| Column | Meaning |
| --- | --- |
| `study_id` | from the `study_id` tag |
| `submission_date` | the `submission_date` tag, normalised to `YYYY-MM-DD` |
| `ingest_run_id` | one UUID per `ingest_files_to_dataset` call, shared by every file in that call |
| `ingest_received_at` | timestamp the run started (`Australia/Melbourne`) |
| `ingest_timezone` | timezone of the timestamps (`Australia/Melbourne`) |
| `ingest_original_file_path` | full `s3://` URI of the source object |
| `ingest_file_name` | basename of the source object |
| `ingest_submission_id` | caller-supplied submission ID (empty string if none) |
| `ingest_file_etag` | S3 ETag of the object |
| `ingest_file_size_bytes` | object size |
| `ingest_file_last_modified` | S3 LastModified (`Australia/Melbourne`) |
| `tag_<key>` | one column per S3 object tag |
| `ingest_row_hash` | SHA-256 content hash of the row (see below) |

### `ingest_row_hash`: re-ingest as a no-op

`compute_row_hash` hashes **raw columns only** — every `ingest_*` and
`tag_*` column plus `study_id` and `submission_date` are excluded. The
remaining columns are sorted by name, serialised as `col=value` pairs
joined with `||`, and SHA-256 hashed.

Because run-varying metadata (run ID, timestamps, ETag, path) never
enters the hash, re-ingesting the same file — or the same file under a
new name or date — produces identical hashes. Deduplicate on it, e.g.
by passing `merge_cols=["ingest_row_hash"]` to `write_iceberg_to_db`,
or by using it as the unique key in a downstream dbt incremental model,
and a re-ingest becomes a MERGE no-op instead of duplicated rows. (The
default ingest write is an append; deduplication on the hash is up to
the consumer.)

For XLSX files, every sheet is read (via openpyxl), a `sheet_name`
column is added, and the sheets are concatenated; `sheet_name` counts
as a raw column and participates in the hash.

## Table format, parallelism, and knobs

`ingest_table_to_dataset` / `ingest_files_to_dataset` accept:

- `table_format` — `"iceberg"` (default) or `"parquet"`.
  - **iceberg**: written via Athena `MERGE`-capable Iceberg tables
    (`write_iceberg_to_db`). The table must already exist, or the
    writer needs a table location configured.
  - **parquet**: written as a Hive-style dataset
    (`write_parquet_to_db`) partitioned by
    `study_id / submission_date / ingest_file_name` with snappy
    compression and schema evolution enabled. Requires
    `dataset_root` (e.g. `s3://bucket/prefix/`) — the dataset's S3
    root; `mode` chooses `"append"` (default), `"overwrite"`, or
    `"overwrite_partitions"`. Both are ignored for iceberg.
- `exclude_fn` (`ingest_files_to_dataset`) — file names to skip;
  defaults to `['program.json', 'project.json']`.
- `get_ingest_true_files(s3_uri, exclude_directories=None,
  max_workers=32)` — the tag scan issues one `GetObjectTagging` call
  per object, so it runs them in a thread pool; `max_workers` controls
  the concurrency (32 is comfortable for S3).

Supported formats, chosen by file extension: `csv` (delimiter-sniffed,
UTF-8/cp1252 fallback, headers normalised to snake_case), `json`
(records orient), `xlsx` (all sheets, flattened). Everything is read
and stored as strings.

## Worked example

1. Land the file and mark it ready:

   ```bash
   aws s3 cp samples.csv s3://my-bucket/raw/mystudy/samples.csv
   aws s3api put-object-tagging \
     --bucket my-bucket --key raw/mystudy/samples.csv \
     --tagging 'TagSet=[{Key=ingest,Value=true},{Key=study_id,Value=mystudy},{Key=node,Value=sample},{Key=submission_date,Value=2026-08-05}]'
   ```

2. Scan and ingest:

   ```python
   from g3dt.ingest.ingest import get_ingest_true_files, ingest_files_to_dataset

   files = get_ingest_true_files("s3://my-bucket/raw/mystudy/")
   results = ingest_files_to_dataset(
       s3_uris=files,
       database="my_bronze_db",
       table_prefix="raw",
       athena_s3_output="s3://my-bucket/athena-output/",
   )
   ```

3. Query in Athena:

   ```sql
   SELECT sample_id, ingest_file_name, ingest_received_at
   FROM my_bronze_db.raw_sample
   WHERE study_id = 'mystudy'
     AND submission_date = '2026-08-05';
   ```
