"""Safety guards (T48) -- automated, CI-enforceable checks for the four
controls master plan section 8.3 ("S7 prohibitions as architecture, not
prompt text") requires to exist as real code, not prompt text or a
docstring promise:

1. The tool/endpoint registry is exactly the closed set the spec allows.
2. No browser-automation or payment-processing package is a dependency.
3. No payment/loyalty/passport-shaped field or function name exists
   anywhere in the source tree.
4. The one HTTP endpoint capable of "approving" anything
   (``POST /runs/{run_id}/approve``) is independently re-verified as
   structurally incapable of triggering any action beyond writing its own
   audit record -- a SEPARATE, independently-constructed check from
   ``tests/unit/test_approval.py``'s own guard (different AST analysis,
   plus a real filesystem-delta assertion from an actual HTTP call), not a
   re-import of that file's logic.

Per this task's own brief -- "check if a similar pattern already exists,
e.g. in tests/ as a standalone guard test rather than application code,
since these are checks ABOUT the source tree, not runtime behavior the
application itself needs" -- this lives entirely under ``tests/``,
following the precedent ``test_approval.py``'s own
``TestStructurallyIncapableOfBooking`` class already set. There is no new
``src/flightagent/safety/`` package: these are checks ABOUT the tree
(dependencies, identifiers, registered routes), not a runtime module the
application imports, so putting them under ``src/`` would misrepresent
what they are.

One finding from actually reading the tree before writing anything (per
this task's own instruction): ``search_flights`` -- one of the three
tools master plan section 8.3 names as the closed allowed set -- does not
exist as a callable ANYWHERE in this codebase yet. A repo-wide search
turns up the name only in docstrings/comments describing it as a FUTURE
tool ("lands in a later task, in this same package" -- see
``tools/__init__.py``'s own docstring). Asserting the registered tool set
literally EQUALS ``{search_flights, airport_info, save_json}`` would
therefore assert something false about the tree as it exists today. Test
1 below instead asserts the CURRENT real registered set exactly (catching
any silent regression away from ``{airport_info, save_json}``) and,
independently, that it is a SUBSET of the full spec-allowed set (catching
the addition of anything -- a booking tool, most importantly -- that is
not one of the three allowed names). That second assertion is the one
that actually does the job master plan 8.3 asks for: it fails the moment
anything outside the closed set is registered, forcing the "deliberately
update this test" human decision point the master plan calls for.
"""

from __future__ import annotations

import ast
import re
import tomllib
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import flightagent.tools as tools_pkg
from flightagent.api.app import create_app
from flightagent.api.auth import API_KEY_ENV_VAR
from flightagent.config.loader import load_config
from flightagent.config.models import FlightAgentSettings

_TEST_API_KEY = "test-api-key-not-a-real-secret"

# ---------------------------------------------------------------------------
# Shared paths / helpers
# ---------------------------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[2]  # tests/unit/test_safety.py -> repo root
_SRC_ROOT = _REPO_ROOT / "src" / "flightagent"
_TESTS_ROOT = _REPO_ROOT / "tests"


def _isolated_settings(tmp_path: Path) -> FlightAgentSettings:
    """Same isolation pattern ``test_api.py``/``test_approval.py`` use --
    every output path redirected under ``tmp_path`` so this test never
    touches the repo's own ``out/``/``data/runs``.
    """
    return load_config(
        env={},
        cli_overrides={
            "output": {
                "report_path": str(tmp_path / "out" / "flight_report_2027-07-17.md"),
                "results_path": str(tmp_path / "out" / "flight_results_2027-07-17.json"),
                "runs_dir": str(tmp_path / "data" / "runs"),
            }
        },
    )


_SEARCH_BODY = {
    "origin": "AMS",
    "date": "2027-07-17",
    "max_stops": 1,
    "dest": "DEL",
    "provider": "mock",
}


# ---------------------------------------------------------------------------
# 1. Tool + HTTP endpoint registry -- exact closed set
# ---------------------------------------------------------------------------

ALLOWED_TOOL_NAMES: frozenset[str] = frozenset({"search_flights", "airport_info", "save_json"})
"""Master plan section 8.3's exact closed set. Never add to this set to
make a failing test pass -- the whole point of this guard is that an
addition here is a deliberate, reviewed decision, not a drive-by fix."""

EXPECTED_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/search"),
        ("GET", "/runs/{run_id}"),
        ("GET", "/runs/{run_id}/report.md"),
        ("GET", "/healthz"),
        ("POST", "/runs/{run_id}/approve"),
    }
)
"""Exactly the HTTP surface T46 (``routes_search``, ``routes_runs``) and
T47 (``routes_approval``) built -- five endpoints, no more."""


def _registered_tool_names() -> set[str]:
    """Every public, callable, non-module attribute the ``flightagent.tools``
    package actually exposes -- the closest thing this codebase has to a
    formal "tool registry" object (there is no ``mcp_server.py`` yet).
    """
    return {
        name
        for name, obj in vars(tools_pkg).items()
        if not name.startswith("_") and callable(obj) and not isinstance(obj, types.ModuleType)
    }


def test_registered_tool_and_endpoint_set_matches_exact_spec_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --- Tool registry ----------------------------------------------------
    public_callables = _registered_tool_names()
    assert public_callables == set(tools_pkg.__all__), (
        "flightagent.tools exports a callable not reflected in __all__ (or vice versa): "
        f"public callables={sorted(public_callables)!r}, __all__={sorted(tools_pkg.__all__)!r}"
    )
    assert public_callables <= ALLOWED_TOOL_NAMES, (
        "tool registry contains name(s) outside the spec's closed allowlist -- "
        f"{sorted(public_callables - ALLOWED_TOOL_NAMES)!r} must never be added without "
        "deliberately updating this test (master plan section 8.3)"
    )
    # The exact set registered TODAY -- see this module's own docstring for
    # why this is not bare equality against the full three-name spec set.
    assert public_callables == {"airport_info", "save_json"}

    # --- HTTP endpoint registry --------------------------------------------
    # create_app() now requires FLIGHTAGENT_API_KEY (Phase 8b, api.auth) --
    # only needed here to let app creation itself succeed; this test never
    # issues a real request (app.openapi() is pure route introspection),
    # so no X-Api-Key header is needed.
    monkeypatch.setenv(API_KEY_ENV_VAR, _TEST_API_KEY)
    app = create_app(settings=_isolated_settings(tmp_path))
    schema = app.openapi()
    actual_endpoints = {
        (method.upper(), path) for path, methods in schema["paths"].items() for method in methods
    }
    assert actual_endpoints == EXPECTED_ENDPOINTS, (
        "registered HTTP endpoint set drifted from the closed T46/T47 list -- "
        f"added={sorted(actual_endpoints - EXPECTED_ENDPOINTS)!r}, "
        f"removed={sorted(EXPECTED_ENDPOINTS - actual_endpoints)!r}"
    )


# ---------------------------------------------------------------------------
# 2. Dependency deny-list -- pyproject.toml + uv.lock
# ---------------------------------------------------------------------------

_BROWSER_AUTOMATION_DENYLIST = frozenset(
    {
        "playwright",
        "pytest-playwright",
        "selenium",
        "selenium-wire",
        "pyppeteer",
        "undetected-chromedriver",
        "splinter",
        "helium",
        "robotframework-seleniumlibrary",
    }
)

_CAPTCHA_SOLVER_DENYLIST = frozenset(
    {
        "2captcha",
        "2captcha-python",
        "twocaptcha",
        "anticaptchaofficial",
        "python-anticaptcha",
        "deathbycaptcha",
        "capsolver",
        "capmonster-python",
        "azcaptcha",
    }
)

_PAYMENT_DENYLIST = frozenset(
    {
        "stripe",
        "braintree",
        "braintree-python",
        "paypalrestsdk",
        "paypal-checkout-serversdk",
        "paypal-server-sdk",
        "square",
        "squareup",
        "adyen",
        "razorpay",
        "authorizenet",
        "coinbase-commerce",
    }
)

DENYLISTED_PACKAGES: frozenset[str] = (
    _BROWSER_AUTOMATION_DENYLIST | _CAPTCHA_SOLVER_DENYLIST | _PAYMENT_DENYLIST
)

_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _normalize_package_name(name: str) -> str:
    """PEP 503 normalization -- PyPI (and uv) treat ``-``, ``_`` and ``.``
    as equivalent separators and are case-insensitive, so
    ``Undetected_ChromeDriver`` and ``undetected-chromedriver`` must
    compare equal here.
    """
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _dependency_names_from_pyproject(path: Path) -> set[str]:
    data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for dep in data.get("project", {}).get("dependencies", []):
        match = _DEP_NAME_RE.match(dep.strip())
        if match:
            names.add(_normalize_package_name(match.group(0)))
    for group_deps in data.get("dependency-groups", {}).values():
        for dep in group_deps:
            if not isinstance(dep, str):
                continue  # e.g. {include-group = "..."} -- not a package name
            match = _DEP_NAME_RE.match(dep.strip())
            if match:
                names.add(_normalize_package_name(match.group(0)))
    return names


def _package_names_from_uv_lock(path: Path) -> set[str]:
    data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    return {_normalize_package_name(pkg["name"]) for pkg in data.get("package", [])}


def test_no_browser_automation_or_payment_dependency_in_lockfile() -> None:
    """Reads the ACTUAL ``pyproject.toml`` and ``uv.lock`` content -- not a
    hardcoded assumption about what is installed -- and checks both the
    direct declarations and the fully resolved dependency graph against a
    fixed deny-list of browser-automation, CAPTCHA-solving and payment
    package names (master plan section 8.3).
    """
    pyproject_path = _REPO_ROOT / "pyproject.toml"
    uv_lock_path = _REPO_ROOT / "uv.lock"
    assert pyproject_path.is_file()
    assert uv_lock_path.is_file()

    declared = _dependency_names_from_pyproject(pyproject_path)
    resolved = _package_names_from_uv_lock(uv_lock_path)

    # Sanity checks: prove this test is reading something real, not
    # vacuously passing over empty input.
    assert "fastapi" in declared, "sanity check: pyproject.toml direct dependencies parsed"
    assert len(resolved) > 10, "sanity check: uv.lock resolved package list parsed"

    offending_declared = declared & DENYLISTED_PACKAGES
    assert offending_declared == set(), (
        f"pyproject.toml declares denylisted package(s): {sorted(offending_declared)}"
    )

    offending_resolved = resolved & DENYLISTED_PACKAGES
    assert offending_resolved == set(), (
        "uv.lock resolves denylisted package(s) (possibly only a transitive dependency): "
        f"{sorted(offending_resolved)}"
    )


# ---------------------------------------------------------------------------
# 3. Forbidden field/function names -- src/ and tests/
# ---------------------------------------------------------------------------

_FORBIDDEN_NAMES = ("card_number", "cvv", "passport", "loyalty_id", "passenger_name")
"""Master plan section 8.3's exact call-outs: "card_number, cvv, passport,
loyalty_id" (third bullet) plus "No passenger-name field either" (fourth
bullet, its own explicit scope-creep signal)."""

_FIELD_DEFINING_PATTERN = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_NAMES) + r")\b\s*[:=]", re.IGNORECASE
)
"""Matches a forbidden name in an actual field/parameter/attribute
DEFINING position -- a pydantic/dataclass annotation (``name: type``), a
plain assignment or default (``name = ...``) -- not the name appearing
anywhere in prose. This is deliberately narrower than a bare substring
grep: ``src/flightagent/providers/duffel/mapper.py`` has a code COMMENT
that says, in English, that the mapper never reads a field called
"passport" and that this is exactly what keeps this project's master-plan
denylist meaningful -- a bare-word scan would flag that sentence itself,
which is backwards (it is the mechanism this guard exists to reward, not
violate). Requiring the name be immediately followed by ``:`` or ``=``
matches an actual Python field/parameter/variable definition and does not
match that comment, nor a JSON fixture list ELEMENT such as
``"supported_passenger_identity_document_types": ["passport"]`` in the
captured Duffel API fixture (a verbatim third-party response shape this
project's own mapper deliberately never reads a field out of -- not a
field this project defines)."""

_SQL_WORD_PATTERN = re.compile(r"\b(" + "|".join(_FORBIDDEN_NAMES) + r")\b", re.IGNORECASE)
"""For ``.sql`` schema files specifically: a plain word-boundary match, no
``:``/``=`` requirement (SQL column definitions read ``name TYPE ...``,
not ``name: type``). Safe to be this blunt here -- this project has
exactly one ``.sql`` file (``persistence/schema.sql``, T43's cache
schema) and no prose in it discusses forbidden field names by name the
way the Python comment above does."""


def _scan_python_files(root: Path) -> list[str]:
    hits: list[str] = []
    if not root.is_dir():
        return hits
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == _THIS_FILE or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FIELD_DEFINING_PATTERN.search(line):
                hits.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    return hits


def _scan_sql_files(root: Path) -> list[str]:
    hits: list[str] = []
    if not root.is_dir():
        return hits
    for path in sorted(root.rglob("*.sql")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _SQL_WORD_PATTERN.search(line):
                hits.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    return hits


def test_no_forbidden_field_names_anywhere_in_source_tree() -> None:
    """Permanent regression guard (master plan section 8.3, third and
    fourth bullets): zero occurrences of a payment/loyalty/passport/
    passenger-identity-shaped field, parameter or attribute name anywhere
    under ``src/`` or ``tests/``.

    Deliberately scoped to ``.py`` (field/parameter/attribute definitions)
    and ``.sql`` (this project's one hand-written DB schema) -- NOT the
    captured third-party API fixture JSON under
    ``tests/fixtures/providers/``, which faithfully mirrors Amadeus's/
    Duffel's real response shapes (including fields like
    ``supported_passenger_identity_document_types`` that a real airline
    API genuinely returns) and which this project's own mappers
    deliberately never read a forbidden field out of (finding 0.6 and the
    mapper's own module docstrings). Flagging a verbatim, unmodified
    capture of a third party's API shape would not catch scope creep in
    this project -- it would just break the fixture that proves this
    project's mappers ignore that data.
    """
    hits = (
        _scan_python_files(_SRC_ROOT)
        + _scan_python_files(_TESTS_ROOT)
        + _scan_sql_files(_SRC_ROOT)
    )
    assert hits == [], (
        "forbidden payment/loyalty/passport-shaped field name(s) found:\n" + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# 4. routes_approval.py -- independent re-verification (not a re-import of
#    test_approval.py's own guard)
# ---------------------------------------------------------------------------

_ROUTES_APPROVAL_PATH = _SRC_ROOT / "api" / "routes_approval.py"

_NETWORK_OR_PROCESS_IMPORT_DENYLIST = frozenset(
    {
        "httpx",
        "requests",
        "aiohttp",
        "urllib",
        "urllib.request",
        "socket",
        "subprocess",
        "smtplib",
        "ftplib",
        "paramiko",
    }
)

_BOOKING_PAYMENT_IMPORT_PATTERN = re.compile(
    r"\b(book|payment|checkout|purchase|charge|invoice|stripe|paypal|braintree)\b",
    re.IGNORECASE,
)

_ALWAYS_SUSPICIOUS_METHODS = frozenset({"system", "popen", "Popen", "exec", "eval"})
_HTTP_METHODS_REQUIRING_CLIENT_OBJECT = frozenset(
    {"post", "put", "patch", "get", "request", "send"}
)
_NETWORK_CLIENT_OBJECT_NAMES = frozenset(
    {"httpx", "requests", "aiohttp", "client", "session", "http", "urlopen"}
)


def _parse_routes_approval() -> ast.Module:
    """Reads and parses ``routes_approval.py`` directly from disk --
    deliberately NOT ``inspect.getsource`` on the imported module object
    (which is what ``tests/unit/test_approval.py``'s own guard uses), so
    this check has its own independent path from source file to AST and
    does not depend on that file's import machinery having behaved.
    """
    source = _ROUTES_APPROVAL_PATH.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(_ROUTES_APPROVAL_PATH))


def test_approval_endpoint_cannot_trigger_any_external_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent re-verification of T47's own claim (this task's brief:
    "should be re-verified independently here as the dedicated
    safety-guard suite's own check, not just trusted from T47's report").

    Four separate, independently-constructed angles, none of which reuse
    ``test_approval.py``'s own logic:

    1. Static: every import in the module resolves to a name that is
       neither a known networking/process-execution library NOR
       booking/payment-shaped.
    2. Static: every call SITE in the module (not just its imports) is
       inspected, and none of them looks like an outbound HTTP call from a
       plausible HTTP-client object (``router.post(...)`` -- FastAPI's OWN
       route-registration decorator on THIS module's own router -- is
       explicitly distinguished from e.g. ``httpx.post(...)``) or a
       process-execution call.
    3. Dynamic: a real ``TestClient`` call to the real endpoint against a
       real run is made, and the entire ``runs_dir`` file tree is diffed
       before and after -- the only permitted change is the creation of
       exactly one new file, ``approval.json``. Nothing else is created,
       deleted, or modified.
    4. Cross-check: even if 1-3 had somehow missed something, there is
       still no booking-shaped tool registered anywhere (guard 1, above)
       for this endpoint -- or anything else -- to invoke.
    """
    tree = _parse_routes_approval()

    # --- 1. Import-level check ---------------------------------------------
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert imported_modules, "sanity check: the module has imports to scan"

    offending_network_imports = [
        mod
        for mod in imported_modules
        if any(
            mod == denied or mod.startswith(denied + ".")
            for denied in _NETWORK_OR_PROCESS_IMPORT_DENYLIST
        )
    ]
    assert offending_network_imports == [], (
        f"routes_approval.py imports a networking/process-execution module: "
        f"{offending_network_imports!r}"
    )

    offending_booking_imports = [
        mod for mod in imported_modules if _BOOKING_PAYMENT_IMPORT_PATTERN.search(mod)
    ]
    assert offending_booking_imports == [], (
        f"routes_approval.py imports something booking/payment-shaped: "
        f"{offending_booking_imports!r}"
    )

    # --- 2. Call-site check -------------------------------------------------
    suspicious_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        base = node.func.value
        base_name = base.id if isinstance(base, ast.Name) else str(getattr(base, "attr", ""))
        if method in _ALWAYS_SUSPICIOUS_METHODS:
            suspicious_calls.append(f"{base_name}.{method}(...) at line {node.lineno}")
        elif (
            method in _HTTP_METHODS_REQUIRING_CLIENT_OBJECT
            and base_name.lower() in _NETWORK_CLIENT_OBJECT_NAMES
        ):
            suspicious_calls.append(f"{base_name}.{method}(...) at line {node.lineno}")
    assert suspicious_calls == [], f"suspicious call site(s) found: {suspicious_calls}"

    # --- 3. Runtime filesystem-delta check -----------------------------------
    settings = _isolated_settings(tmp_path)
    monkeypatch.setenv(API_KEY_ENV_VAR, _TEST_API_KEY)
    client = TestClient(create_app(settings=settings), headers={"X-Api-Key": _TEST_API_KEY})

    created = client.post("/search", json=_SEARCH_BODY)
    assert created.status_code == 200, created.text
    assert created.json()["accepted_count"] >= 1
    run_id = created.json()["run_id"]

    runs_dir = Path(settings.output.runs_dir)

    def _snapshot() -> dict[str, bytes]:
        return {
            p.relative_to(runs_dir).as_posix(): p.read_bytes()
            for p in runs_dir.rglob("*")
            if p.is_file()
        }

    before = _snapshot()
    response = client.post(f"/runs/{run_id}/approve", json={"approved": True})
    assert response.status_code == 200, response.text
    after = _snapshot()

    added = set(after) - set(before)
    removed = set(before) - set(after)
    changed = {name for name in (set(before) & set(after)) if before[name] != after[name]}

    assert removed == set(), f"approve call deleted file(s): {removed}"
    assert changed == set(), f"approve call modified pre-existing file(s): {changed}"
    assert added == {f"{run_id}/approval.json"}, (
        f"approve call's on-disk footprint was not exactly one new approval.json: added={added}"
    )

    # --- 4. Cross-check against the tool registry -----------------------------
    registered_tools = _registered_tool_names()
    assert not any(
        re.search(r"book|pay|charge|purchase|checkout", name, re.IGNORECASE)
        for name in registered_tools
    ), f"a booking/payment-shaped tool exists in the registry: {registered_tools}"
