"""Guard: the toolkit must carry no project identity in its code.

The whole point of gen3-dataops-toolkit is that the same wheel operates any
project — every AWS resource name is resolved from SSM at runtime. If a
legacy literal like `acdc-dataops-metadata` or a hard-coded instance id
reappears in source, the toolkit has been re-coupled to a single project and
is no longer generic. This test makes that regression a red build instead of
a code-review hope.

Allowed: the string "acdc" inside comments, docstrings, and help text (e.g.
`acdc_schema.json`, the schema repo's file-layout convention, or provenance
notes) — the guard only scans code by stripping comments; known filename/
layout conventions are explicitly whitelisted.
"""
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "g3dt"

#: Legacy literals that must never appear anywhere in source (code or comment).
FORBIDDEN_EVERYWHERE = (
    "acdc-dataops-metadata",
    "acdc_dataops_metadata_db",
    "acdc-runs",
    "/acdc/jobs",
    "i-0a47d44f8a768b059",
    "acdc_aws_etl_pipeline",
    "deploy_config.yaml",
)

#: Strings containing "acdc" that are legitimate layout conventions, not
#: project coupling (the schema repo's dictionary file is named acdc_schema.json).
ALLOWED_CODE_LITERALS = ("acdc_schema", "acdc-schema-json")


def test_no_legacy_literals_anywhere():
    """Input: every file under src/g3dt. Expected: zero legacy-name hits."""
    offenders = []
    for path in SRC.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".json"}:
            continue
        text = path.read_text(errors="replace")
        for needle in FORBIDDEN_EVERYWHERE:
            if needle in text:
                offenders.append(f"{path.relative_to(SRC)}: {needle}")
    assert not offenders, "legacy identity leaked:\n" + "\n".join(offenders)


def test_no_acdc_in_python_code():
    """Input: every .py module's code lines (comments stripped).

    Expected: 'acdc' appears only via the whitelisted schema-file conventions.
    Why: any other occurrence means a project name is compiled into supposedly
    generic code.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        in_docstring = False
        for n, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # crude but effective docstring tracker for module/function docs
            if stripped.startswith(('"""', "'''")):
                if not (len(stripped) > 3 and stripped.endswith(('"""', "'''"))):
                    in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            code = line.split("#", 1)[0].lower()
            if "acdc" not in code:
                continue
            without_allowed = code
            for allowed in ALLOWED_CODE_LITERALS:
                without_allowed = without_allowed.replace(allowed, "")
            if "acdc" in without_allowed:
                offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not offenders, "acdc leaked into code:\n" + "\n".join(offenders)


def test_dictionary_host_is_not_hardcoded_outside_config():
    """Input: every file under src/g3dt. Expected: the raw-GitHub host appears
    only as the DEFAULT_DICT_BASE_URL fallback in config.py.

    Where the dictionary is fetched from is an env input
    (app/dictionary_base_url, app/dictionary_path). It used to be a literal in
    cli/dict_cmds.py and in two shell scripts, which had to be edited in lockstep
    and meant a project hosting its schema elsewhere could not be operated at
    all. config.py keeps one copy as the default for environments deployed
    before those inputs existed; anywhere else is a re-coupling regression.
    """
    host = "raw.githubusercontent.com"
    offenders = []
    for path in SRC.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".json"}:
            continue
        if path.relative_to(SRC) == pathlib.Path("config.py"):
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if host in line:
                offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not offenders, (
        "dictionary host hardcoded outside config.py — resolve it from the "
        "env's dictionary source inputs instead:\n" + "\n".join(offenders)
    )
