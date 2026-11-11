#!/usr/bin/env bash
set -eu
test "${1:-}" = '--sandbox-root' && test -n "${2:-}" || { echo 'usage: validate.sh --sandbox-root ABSOLUTE_ROOT [--python VENV_PYTHON]' >&2; exit 2; }
BASE=$(cd "$2" && pwd -P)
test "$BASE" != '/' || exit 2
shift 2
PYTHON=${VALIDATION_PYTHON:-$BASE/validation-venv/bin/python}
if test "${1:-}" = '--python'; then PYTHON=$2; shift 2; fi
test "$#" -eq 0 && test -x "$PYTHON" || exit 2
SOURCE=$(cd "$(dirname "$0")/.." && pwd -P)
case "$SOURCE/" in "$BASE/"*) ;; *) echo 'source outside explicit authorized sandbox' >&2; exit 2;; esac
export CORPUS_AUTHORIZED_SANDBOX_ROOT="$BASE"
mkdir -p "$BASE/evidence" "$BASE/tmp"
EVIDENCE=$(mktemp -d "$BASE/evidence/validation-XXXXXXXX")
RUN=$(mktemp -d "$BASE/tmp/validation-XXXXXXXX")
export HOME="$RUN/home" TMPDIR="$RUN/tmp"
mkdir -p "$HOME" "$TMPDIR" "$EVIDENCE/dist" "$RUN/source" "$RUN/wheel"
export PYTHONDONTWRITEBYTECODE=1 CORPUS_OFFLINE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONNOUSERSITE=1
export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONPYCACHEPREFIX="$RUN/compile-cache"
unset PYTHONPATH CORPUS_SETTINGS_FILE CORPUS_TEST_ROOT
export CORPUS_SANDBOX_ROOT="$RUN/data" CORPUS_DATA_ROOT="$RUN/data"
cd "$SOURCE"
run() {
    label=$1
    shift
    printf 'cwd=%s\n' "$PWD" > "$EVIDENCE/$label.log"
    printf 'command=' >> "$EVIDENCE/$label.log"
    printf '%q ' "$@" >> "$EVIDENCE/$label.log"
    printf '\n' >> "$EVIDENCE/$label.log"
    set +e
    "$@" >> "$EVIDENCE/$label.log" 2>&1
    status=$?
    set -e
    printf '\nexit=%s\n' "$status" >> "$EVIDENCE/$label.log"
    printf '%s\n' "$status" > "$EVIDENCE/$label.exit"
    cat "$EVIDENCE/$label.log"
    test "$status" -eq 0
}
crash_stress() {
    form=$1
    attempt=1
    while test "$attempt" -le 20; do
        status=0
        run "$form-crash-$attempt" "$PYTHON" -m pytest tests/safe/test_crash_recovery.py::test_real_process_kill_before_generation_activation_recovers --sandbox-root "$BASE" --basetemp "$RUN/$form-crash-$attempt" -o cache_dir="$RUN/$form-crash-cache" -q || status=$?
        find "$RUN/$form-crash-$attempt" -name child-execution.json -exec cp '{}' "$EVIDENCE/$form-crash-$attempt-child.json" \;
        test "$status" -eq 0 || return "$status"
        attempt=$((attempt + 1))
    done
}
run environment "$PYTHON" -c 'import json,os,sqlite3,sys; print(json.dumps({"python":sys.version,"executable":sys.executable,"sqlite":sqlite3.sqlite_version,"HOME":os.environ["HOME"],"TMPDIR":os.environ["TMPDIR"],"network":"offline"},indent=2))'
run source-import "$PYTHON" -c 'from pathlib import Path; import corpus_core; path=Path(corpus_core.__file__).resolve(); print(path); assert path.is_relative_to(Path.cwd())'
run source-collection "$PYTHON" -m pytest tests/safe --collect-only --sandbox-root "$BASE" --basetemp "$RUN/source-collection" -o cache_dir="$RUN/source-cache" -q
run source-full "$PYTHON" -m pytest tests/safe --sandbox-root "$BASE" --basetemp "$RUN/source-tests" -o cache_dir="$RUN/source-cache" --junitxml="$EVIDENCE/source.xml" -q
find "$RUN/source-tests" -name recall-working-set.json -exec cp '{}' "$EVIDENCE/source-working-set.json" \;
run source-compile "$PYTHON" -m compileall -q -f corpus_core tests/safe tools build_backend.py __init__.py
crash_stress source
run package "$PYTHON" -m build_backend --out "$EVIDENCE/dist"
run installed-venv "$PYTHON" -m venv --without-pip "$RUN/installed-venv"
run installed-wheel "$RUN/installed-venv/bin/python" -I -B "$SOURCE/tools/offline_install_wheel.py" "$EVIDENCE/dist/corpus_candidate-2.0.0-py3-none-any.whl"
cd "$RUN"
run installed-import "$RUN/installed-venv/bin/python" -I -B -c 'import pathlib,sys,corpus_core; path=pathlib.Path(corpus_core.__file__).resolve(); print(path); assert path.is_relative_to(pathlib.Path(sys.prefix))'
run installed-cli "$RUN/installed-venv/bin/corpus" --root "$RUN/installed-probe" probe
run installed-directory-import "$RUN/installed-venv/bin/python" -I -B -c 'import importlib.util,pathlib,sys; path=pathlib.Path(sys.prefix)/"hermes-plugins/corpus-candidate/__init__.py"; spec=importlib.util.spec_from_file_location("installed_corpus",path); module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module); assert len(sys.modules["installed_corpus.corpus_core.contracts"].CONTRACTS)==6; assert "corpus_core" not in sys.modules; print(module.register.__module__); print("entrypoint import only; real Hermes discovery remains parent gate")'
cd "$SOURCE"
run unpack-source "$PYTHON" -m tarfile -e "$EVIDENCE/dist/corpus_candidate-2.0.0.tar.gz" "$RUN/source"
cd "$RUN/source/corpus_candidate-2.0.0"
run sdist-import "$PYTHON" -c 'from pathlib import Path; import corpus_core; path=Path(corpus_core.__file__).resolve(); print(path); assert path.is_relative_to(Path.cwd())'
run sdist-collection "$PYTHON" -m pytest tests/safe --collect-only --sandbox-root "$RUN" --basetemp "$RUN/sdist-collection" -o cache_dir="$RUN/sdist-cache" -q
run sdist-full "$PYTHON" -m pytest tests/safe --sandbox-root "$RUN" --basetemp "$RUN/sdist-tests" -o cache_dir="$RUN/sdist-cache" --junitxml="$EVIDENCE/sdist.xml" -q
find "$RUN/sdist-tests" -name recall-working-set.json -exec cp '{}' "$EVIDENCE/sdist-working-set.json" \;
run sdist-compile "$PYTHON" -m compileall -q -f corpus_core tests/safe tools build_backend.py __init__.py
crash_stress sdist
cd "$SOURCE"
run unpack-wheel "$PYTHON" -m zipfile -e "$EVIDENCE/dist/corpus_candidate-2.0.0-py3-none-any.whl" "$RUN/wheel"
cp -R tests "$RUN/wheel/"
cp pyproject.toml build_backend.py plugin.yaml __init__.py LEGACY_SCHEMA.json "$RUN/wheel/"
cd "$RUN/wheel"
run wheel-import "$PYTHON" -c 'from pathlib import Path; import corpus_core; path=Path(corpus_core.__file__).resolve(); print(path); assert path.is_relative_to(Path.cwd())'
run wheel-collection "$PYTHON" -m pytest tests/safe --collect-only --sandbox-root "$RUN" --basetemp "$RUN/wheel-collection" -o cache_dir="$RUN/wheel-cache" -q
run wheel-full "$PYTHON" -m pytest tests/safe --sandbox-root "$RUN" --basetemp "$RUN/wheel-tests" -o cache_dir="$RUN/wheel-cache" --junitxml="$EVIDENCE/wheel.xml" -q
find "$RUN/wheel-tests" -name recall-working-set.json -exec cp '{}' "$EVIDENCE/wheel-working-set.json" \;
run wheel-compile "$PYTHON" -m compileall -q -f corpus_core
crash_stress wheel
cd "$SOURCE"
"$PYTHON" -c 'import json; from corpus_core.contracts import CONTRACTS,schema_for; print(json.dumps({name:schema_for(name) for name in CONTRACTS},ensure_ascii=False,indent=2))' > "$EVIDENCE/action-contracts.json"
"$PYTHON" -c 'import os; from pathlib import Path; from corpus_core.service import Corpus; c=Corpus(Path(os.environ["CORPUS_AUTHORIZED_SANDBOX_ROOT"])/"tmp"/"schema-evidence"); print("PRAGMA user_version="+str(c.db.execute("PRAGMA user_version").fetchone()[0])+";"); print(";\n".join(row[0] for row in c.db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"))+";"); c.close()' > "$EVIDENCE/schema-v4.sql"
run capabilities "$PYTHON" -m corpus_core.cli --root "$RUN/probe" probe
sha256sum "$EVIDENCE/dist/"* > "$EVIDENCE/artifact-sha256.txt"
printf 'Safe source, sdist, wheel suites and compile finished. Evidence: %s\n' "$EVIDENCE"
printf 'Sensitive operations are disabled in the product; old approved-path tests explicitly monkeypatch only the isolated unit-test gate. Real Hermes discovery, trusted human approval, paid services and real-copy adoption remain parent gates.\n'
