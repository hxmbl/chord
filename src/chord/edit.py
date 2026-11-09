"""Changing chord.toml from the command line, without losing what's in it.

`chord route add|rm`, `chord set` and `chord config migrate` all edit a file a
person also edits by hand, so every write here keeps comments and order
(tomlkit), proves the result is a config Chord would load before anything
reaches the disk, and lands with a rename so a crash mid-write can't leave
half a file. Invalid means nothing is written.
"""

import difflib
import os
import tempfile
import tomllib
from pathlib import Path

import tomlkit
from tomlkit.items import AoT, Comment, Table, Whitespace
from tomlkit.toml_document import TOMLDocument

from chord.config import (
    CONFIG_VERSION,
    MACHINE_KEYS,
    ConfigError,
    T3Options,
    as_route,
    check_text,
    parse_route,
    route_name_problem,
)
from chord.paths import PROJECT_CONFIG, project_target, user_config_path

# What `chord set` may change, and how to read the value given for it.
_SETTABLE = {
    "version": "int",
    "label": "str",
    "harness": "str",
    "model": "str",
    "allowed_actors": "list",
    "interval": "int",
    "webhook_port": "int",
    "webhook_secret": "str",
    "search_dirs": "list",
    "notify.received": "bool",
    "linear_user_id": "str",
}


def user_target() -> Path:
    """The user file: the one there is, or where it would go."""
    return user_config_path() or Path.home() / PROJECT_CONFIG


def target(user: bool) -> Path:
    """The file a write goes to: the user file with `--user`, else the project's."""
    return user_target() if user else project_target()


def read(path: Path) -> TOMLDocument:
    if not path.is_file():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text())
    except Exception as exc:  # tomlkit raises its own family of parse errors
        raise ConfigError(f"{path} isn't valid TOML: {exc}.") from exc


def write(path: Path, doc: TOMLDocument | str) -> None:
    """Validate, then write atomically: a temp file beside it, then rename."""
    text = doc if isinstance(doc, str) else tomlkit.dumps(doc)
    check_text(text, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".chord-", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as out:
            out.write(text)
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _table(doc: TOMLDocument, key: str) -> Table:
    existing = doc.get(key)
    if isinstance(existing, Table):
        return existing
    if existing is not None:
        raise ConfigError(f"`{key}` in this file isn't a table, so it can't be edited.")
    table = tomlkit.table()
    doc.append(key, table)
    return table


def route_add(path: Path, name: str, route: str) -> str:
    """Add or replace `[routes] name = "route"`. Returns the route as written."""
    problem = route_name_problem(name)
    if problem:
        raise ConfigError(f"{name!r} isn't a usable route name: {problem}.")
    spec = parse_route(route, f"The route for {name!r}")
    doc = read(path)
    _table(doc, "routes")[name] = str(spec)
    write(path, doc)
    return str(spec)


def route_rm(path: Path, name: str) -> bool:
    """Remove a route by name, from `[routes]` or the old `[harnesses]`."""
    doc = read(path)
    removed = False
    for key in ("routes", "harnesses"):
        table = doc.get(key)
        if isinstance(table, Table) and name in table:
            del table[name]
            removed = True
    if removed:
        write(path, doc)
    return removed


def _value(key: str, text: str) -> object:
    kind = _SETTABLE[key]
    try:
        parsed = tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        parsed = text
    if kind == "str":
        return parsed if isinstance(parsed, str) else text
    if kind == "list":
        if isinstance(parsed, list):
            return parsed
        return [part.strip() for part in text.split(",") if part.strip()]
    if kind == "int":
        if isinstance(parsed, int) and not isinstance(parsed, bool):
            return parsed
        raise ConfigError(f"`{key}` takes a whole number, not {text!r}.")
    if kind == "bool":
        if isinstance(parsed, bool):
            return parsed
        raise ConfigError(f"`{key}` takes true or false, not {text!r}.")
    raise AssertionError(kind)  # pragma: no cover


def set_value(path: Path, key: str, text: str, *, user: bool) -> object:
    """Set `key` to `text` (read as TOML, else as a string). Returns the value."""
    if key not in _SETTABLE:
        known = ", ".join(sorted(_SETTABLE))
        raise ConfigError(f"Chord has no setting `{key}` to set. It has: {known}.")
    top = key.split(".")[0]
    if top in MACHINE_KEYS and not user:
        raise ConfigError(
            f"`{key}` is a machine setting, which only counts in your own file. "
            "Add `--user` to set it there."
        )
    value = _value(key, text)
    doc = read(path)
    if "." in key:
        table_name, leaf = key.split(".", 1)
        _table(doc, table_name)[leaf] = value
    elif key in doc:
        doc[key] = value
    else:
        _insert_top_level(doc, key, value)
    write(path, doc)
    return value


def _insert_top_level(doc: TOMLDocument, key: str, value: object) -> None:
    """Add a top-level key above the first table.

    The TOML trap: a key written after a `[table]` header belongs to that
    table, and tomlkit appends at the end. So a new top-level key goes after
    the last top-level key, or, when there is none, above the comments that
    introduce the first table.
    """
    body = doc.body
    first = next(
        (i for i, (_, item) in enumerate(body) if isinstance(item, Table | AoT)),
        None,
    )
    if first is None:
        doc.add(key, value)
        return
    keys = [i for i, (k, _) in enumerate(body[:first]) if k is not None]
    if keys:
        at = keys[-1] + 1
    else:
        at = first
        while at > 0 and isinstance(body[at - 1][1], Comment | Whitespace):
            at -= 1
    doc._insert_at(at, key, value)


# --- migrate ---


def _t3_options(table: object) -> T3Options:
    if not isinstance(table, dict):
        return T3Options()
    return T3Options(
        provider=table.get("provider"),
        model=table.get("model"),
        worktree=table.get("worktree"),
        branch=table.get("branch"),
    )


def migrate(project: Path | None, user: Path) -> tuple[dict[Path, str], list[str]]:
    """The new text for each file that changes, and notes on what stayed.

    - machine settings in the project file move to the user file;
    - `harness = "t3"` with `[t3]` becomes a `t3:provider/model@worktree`
      route string, and `[t3]` goes (kept when it sets `branch`, which a
      route string has no place for);
    - `[harnesses]` entries with a route spelling and a usable route name
      move to `[routes]`; others stay, as the escape hatch they are;
    - a file without `version` gets `version = 1`.
    """
    changes: dict[Path, str] = {}
    notes: list[str] = []
    files = [p for p in (user, project) if p is not None and p.is_file()]
    if not files:
        return changes, ["There is no chord.toml to migrate."]
    user_doc = read(user)
    user_before = tomlkit.dumps(user_doc)
    moved_machine: list[str] = []

    for path in files:
        doc = user_doc if path == user else read(path)
        before = tomlkit.dumps(doc)

        if path != user:
            for key in sorted(MACHINE_KEYS):
                if key in doc:
                    value = doc[key]
                    if key not in user_doc:
                        if isinstance(value, Table):
                            user_doc.append(key, value)
                        else:
                            _insert_top_level(user_doc, key, value)
                    else:
                        notes.append(
                            f"`{key}` is in both files; kept the one in {user} "
                            f"and dropped the one in {path}."
                        )
                    del doc[key]
                    moved_machine.append(key)

        t3 = _t3_options(doc.get("t3"))
        if "t3" in doc:
            if t3.branch:
                notes.append(
                    f"`[t3]` in {path} sets `branch`, which a route string can't "
                    "say, so it stays."
                )
            else:
                route = as_route("t3", t3)
                harness = doc.get("harness")
                if harness in ("t3", ["t3"]) and route is not None:
                    doc["harness"] = str(route)
                del doc["t3"]

        harnesses = doc.get("harnesses")
        if isinstance(harnesses, Table):
            for name in list(harnesses.keys()):
                table = harnesses[name]
                command = table.get("command") if isinstance(table, dict) else None
                route = (
                    as_route(command, t3) if isinstance(command, str | list) else None
                )
                if route is None or route_name_problem(name) is not None:
                    if route is not None:
                        notes.append(
                            f'`[harnesses."{name}"]` in {path} stays: a route name '
                            "can't contain `/` or `:`, and renaming it would change "
                            "its Linear label."
                        )
                    continue
                if route.harness == "t3" and t3.branch:
                    continue
                _table(doc, "routes")[name] = str(route)
                del harnesses[name]
            if not harnesses:
                del doc["harnesses"]

        if "version" not in doc:
            _insert_version(doc)

        after = tomlkit.dumps(doc)
        if after != before and path != user:
            changes[path] = after

    user_after = tomlkit.dumps(user_doc)
    if user_after != user_before:
        if "version" not in user_doc:
            _insert_version(user_doc)
            user_after = tomlkit.dumps(user_doc)
        changes[user] = user_after
    if moved_machine:
        notes.append(
            "Moved machine settings to your user file: "
            + ", ".join(f"`{k}`" for k in sorted(set(moved_machine)))
            + "."
        )
    return changes, notes


def _insert_version(doc: TOMLDocument) -> None:
    """`version = 1` at the top, below any leading comment lines."""
    body = doc.body
    at = 0
    while at < len(body) and isinstance(body[at][1], Comment):
        at += 1
    if at >= len(body):
        doc.add("version", CONFIG_VERSION)
    else:
        doc._insert_at(at, "version", CONFIG_VERSION)


def diff(path: Path, after: str) -> str:
    before = path.read_text() if path.is_file() else ""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path} (now)",
            tofile=f"{path} (migrated)",
        )
    )
