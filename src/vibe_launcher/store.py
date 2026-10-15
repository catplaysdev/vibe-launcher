from .i18n import tr
import json
import ipaddress
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from . import runtime


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def normalize_tags(value: Any) -> list[str]:
    """Tags as a clean list: trimmed, no leading '#', no duplicates (ignoring case), at most 12 of at most 24 characters.

    Accepts a list or the text typed in a field ("工地, 前端；#urgent").
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\n，、;；]+", value)
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise ValueError(tr('store.433cf76b0f29'))
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not isinstance(part, str):
            raise ValueError(tr('store.d74564ad1a94'))
        tag = " ".join(part.split()).lstrip("#").strip()
        if not tag:
            continue
        if len(tag) > 24 or _has_control(tag):
            raise ValueError(tr('store.cc42a1a78260'))
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            result.append(tag)
    if len(result) > 12:
        raise ValueError(tr('store.4afe1e5d72e7'))
    return result


_FINISHED = {"ended", "failed"}

_FIELDS: dict[str, set[str]] = {
    "accounts": {"id", "name", "tool", "auth", "model", "base_url", "allow_insecure_loopback", "credential_revision", "share_project_history", "environment_id"},
    "github_accounts": {"id", "name", "environment_id", "login", "status", "checked_at", "credential_storage", "user_id", "author_name", "author_email"},
    "web_services": {"id", "tool", "account_id", "github_account_id", "environment_id", "status", "port", "local_port", "updated_at", "service_key", "experimental"},
    "environments": {"id", "name", "kind", "ssh_target", "ssh_auth_mode", "remember_seconds", "credential_epoch"},
    "presets": {"id", "name", "account_id", "environment_id", "path", "favorite", "last_used", "persistent", "ask_account", "tags", "github_account_id", "github_repo", "github_remote", "github_branch", "github_access"},
    "sessions": {
        "id", "preset_id", "account_id", "environment_id", "name", "path", "status",
        "updated_at", "pid", "runtime_id", "persistent", "runner_pid", "mode", "terminal_profile", "workspace_id",
    },
    "workspaces": {"id", "name", "layout", "slots"},
    "auth_states": {"id", "status", "checked_at"},
}
_IMPORT_FIELDS = {
    kind: fields - {"credential_revision", "credential_epoch", "login", "status", "checked_at", "credential_storage", "user_id", "github_branch", "github_access"}
    for kind, fields in _FIELDS.items()
}



class Store:
    """SQLite persistence for launcher profiles, projects, and runtime state."""

    def __init__(self, path: Path):
        self.path = Path(path)
        runtime.private_dir(self.path.parent)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS environments (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS presets (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    account_id TEXT REFERENCES accounts(id) ON DELETE RESTRICT,
                    environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    preset_id TEXT REFERENCES presets(id) ON DELETE RESTRICT,
                    account_id TEXT REFERENCES accounts(id) ON DELETE RESTRICT,
                    environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS auth_states (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS github_accounts (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_services (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO environments(id, payload) VALUES (?, ?)",
                ("local", self._dump({
                    "id": "local", "name": "本機", "kind": "local", "ssh_target": "",
                    "ssh_auth_mode": "interactive", "remember_seconds": 0,
                    "credential_epoch": uuid.uuid4().hex,
                })),
            )
        runtime.protect_path(self.path)

    def backup(self, target: Path) -> Path:
        """Consistent copy of the database (SQLite online backup) to a private file that must not exist yet."""
        target = Path(target)
        runtime.private_dir(target.parent)
        runtime.assert_private_path(target)
        if target.exists():
            target = target.with_name(f"{target.stem}-{uuid.uuid4().hex[:6]}{target.suffix}")
        destination = sqlite3.connect(target)
        try:
            with self._connect() as source:
                source.backup(destination)
        finally:
            destination.close()
        runtime.protect_path(target)
        return target

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        runtime.assert_private_paths((self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"),
                                      Path(str(self.path) + "-journal")))
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _dump(item: dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _table(kind: str) -> str:
        if kind not in _FIELDS:
            raise ValueError(f"Unsupported record kind: {kind}")
        return kind

    @staticmethod
    def _validate(kind: str, item: dict[str, Any], *, allow_internal: bool = True) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("Record must be an object")
        allowed = _FIELDS[kind] if allow_internal else _IMPORT_FIELDS[kind]
        unknown = item.keys() - allowed
        if unknown:
            raise ValueError(f"Unknown {kind} fields: {', '.join(sorted(unknown))}")
        result = dict(item)
        result["id"] = result.get("id") or uuid.uuid4().hex

        def required_text(key: str) -> str:
            value = result.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
            if any(c in value for c in "\x00\r\n"):
                raise ValueError(f"{key} contains an invalid character")
            return value

        pattern = r"[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,64}" if kind == "auth_states" else r"[A-Za-z0-9_-]{1,64}"
        if not isinstance(result["id"], str) or not re.fullmatch(pattern, result["id"]):
            raise ValueError("id must be a non-empty string")
        if kind == "accounts":
            required_text("name")
            if result.get("tool") not in {"codex", "claude", "deepseek-harness"}:
                raise ValueError("Unsupported tool")
            if result.get("auth") not in {"subscription", "api"}:
                raise ValueError("auth must be subscription or api")
            result.setdefault("model", "")
            result.setdefault("base_url", "")
            result.setdefault("allow_insecure_loopback", False)
            result.setdefault("share_project_history", False)
            if not isinstance(result["share_project_history"], bool):
                raise ValueError("share_project_history must be a boolean")
            result.setdefault("environment_id", "")
            if not isinstance(result["environment_id"], str) or (
                    result["environment_id"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result["environment_id"])):
                raise ValueError("Invalid environment binding")
            if result["auth"] == "api":
                result["environment_id"] = ""        # a key / endpoint works wherever the CLI is started
            result.setdefault("credential_revision", uuid.uuid4().hex)
            if not isinstance(result["allow_insecure_loopback"], bool):
                raise ValueError("allow_insecure_loopback must be a boolean")
            for key in ("model", "base_url"):
                if not isinstance(result[key], str) or _has_control(result[key]):
                    raise ValueError(f"Invalid {key}")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result["credential_revision"]):
                raise ValueError("Invalid credential revision")
            if result["auth"] == "api":
                required_text("model")
                base_url = result["base_url"]
                if base_url:
                    try:
                        parsed = urlsplit(base_url)
                        hostname = parsed.hostname or ""
                        valid = bool(hostname) and parsed.scheme in {"http", "https"}
                        valid = valid and parsed.username is None and parsed.password is None
                        valid = valid and not parsed.query and not parsed.fragment and "?" not in base_url and "#" not in base_url
                        _ = parsed.port
                        if parsed.scheme == "http":
                            try:
                                loopback = hostname.lower() == "localhost" or ipaddress.ip_address(hostname).is_loopback
                            except ValueError:
                                loopback = False
                            valid = valid and result["allow_insecure_loopback"] and loopback and "%" not in hostname
                    except ValueError:
                        valid = False
                    if not valid:
                        raise ValueError("base_url must use HTTPS, except explicitly allowed loopback HTTP")
            elif result["base_url"]:
                raise ValueError("subscription accounts cannot have a base_url")
        elif kind == "environments":
            if type(result.get("remember_seconds", 0)) is not int or result.get("remember_seconds", 0) not in {0, 900, 3600, 28800, -1}:
                raise ValueError("Invalid password storage duration")
            if result.get("kind") == "local" and result.get("remember_seconds", 0):
                raise ValueError("Password storage is only available for SSH")
            required_text("name")
            if result.get("kind") not in {"local", "ssh"}:
                raise ValueError("kind must be local or ssh")
            result.setdefault("ssh_target", "")
            result.setdefault("ssh_auth_mode", "interactive")
            result.setdefault("credential_epoch", uuid.uuid4().hex)
            target = result["ssh_target"]
            if not isinstance(target, str):
                raise ValueError("ssh_target must be a string")
            if result["kind"] == "local" and target:
                raise ValueError("local environments cannot have an ssh_target")
            if result["kind"] == "ssh" and (target.startswith("-") or not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*", target)):
                raise ValueError("ssh_target must be a host or user@host alias")
            if result["ssh_auth_mode"] not in {"interactive", "password", "key"}:
                raise ValueError("ssh_auth_mode must be interactive, password, or key")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result["credential_epoch"]):
                raise ValueError("Invalid SSH credential epoch")
            if result["ssh_auth_mode"] != "password":
                result["remember_seconds"] = 0
        elif kind == "presets":
            required_text("name")
            result.setdefault("account_id", "")
            if not isinstance(result["account_id"], str) or any(c in result["account_id"] for c in "\x00\r\n"):
                raise ValueError("account_id must be a string")
            result.setdefault("ask_account", True)
            if not isinstance(result["ask_account"], bool):
                raise ValueError("ask_account must be a boolean")
            result["tags"] = normalize_tags(result.get("tags", []))
            required_text("environment_id")
            for key in ("github_account_id", "github_repo", "github_remote", "github_branch", "github_access"):
                result.setdefault(key, "")
                if not isinstance(result[key], str) or _has_control(result[key]):
                    raise ValueError("Invalid GitHub binding")
            if result["github_repo"]:
                runtime.github_url(result["github_repo"])
            path = required_text("path")
            result.setdefault("favorite", False)
            result.setdefault("last_used", None)
            if not isinstance(result["favorite"], bool):
                raise ValueError("favorite must be a boolean")
            # Keep the project running after the terminal closes (tmux on SSH hosts). Existing projects keep doing so.
            result.setdefault("persistent", True)
            if not isinstance(result["persistent"], bool):
                raise ValueError("persistent must be a boolean")
            if result["last_used"] is not None and not isinstance(result["last_used"], str):
                raise ValueError("last_used must be a string or null")
            if "\x00" in path or "\r" in path or "\n" in path:
                raise ValueError("path contains an invalid character")
        elif kind == "sessions":
            for key in ("name", "environment_id", "path"):
                required_text(key)
            result.setdefault("account_id", "")
            if result.get("mode") != "shell":        # an SSH-only shell session involves no account
                required_text("account_id")
            result.setdefault("preset_id", "")
            result.setdefault("status", "")
            result.setdefault("updated_at", "")
            if result["preset_id"] and not isinstance(result["preset_id"], str):
                raise ValueError("preset_id must be a string")
            if "pid" in result and (not isinstance(result["pid"], int) or result["pid"] < 0):
                raise ValueError("pid must be a non-negative integer")
            if "runner_pid" in result and (not isinstance(result["runner_pid"], int) or result["runner_pid"] < 0):
                raise ValueError("runner_pid must be a non-negative integer")
            if "persistent" in result and not isinstance(result["persistent"], bool):
                raise ValueError("persistent must be a boolean")
            if result.get("mode", "agent") not in {"agent", "shell"}:
                raise ValueError("mode must be agent or shell")
            if result.get("terminal_profile"):
                runtime.safe_id(result["terminal_profile"])
                if result.get("mode") != "shell":
                    raise ValueError("Terminal profile requires shell mode")
        elif kind == "workspaces":
            required_text("name")
            result.setdefault("layout", "tabs")
            result.setdefault("slots", [])
            if result["layout"] not in {"tabs", "grid"}:
                raise ValueError("layout must be tabs or grid")
            if not isinstance(result["slots"], list) or len(result["slots"]) > 16:
                raise ValueError("a workspace holds up to 16 terminals")
            clean_slots = []
            for slot in result["slots"]:
                if not isinstance(slot, dict) or slot.keys() - {"preset_id", "kind", "account_id", "environment_id", "terminal_profile", "session_id"}:
                    raise ValueError("Invalid workspace terminal")
                if slot.get("kind", "agent") not in {"agent", "shell"}:
                    raise ValueError("terminal kind must be agent or shell")
                cleaned = {"preset_id": slot.get("preset_id"), "kind": slot.get("kind", "agent")}
                for key in ("preset_id", "account_id", "environment_id", "terminal_profile", "session_id"):
                    value = slot.get(key, "")
                    if not isinstance(value, str) or (value and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)):
                        raise ValueError(f"Invalid workspace {key}")
                    if key != "preset_id" and value:
                        cleaned[key] = value
                if cleaned.get("terminal_profile") and cleaned["kind"] != "shell":
                    raise ValueError("Terminal profile requires shell mode")
                if not cleaned["preset_id"]:
                    raise ValueError("A workspace terminal needs a project")
                clean_slots.append(cleaned)
            result["slots"] = clean_slots
        elif kind == "github_accounts":
            required_text("name")
            safe = required_text("environment_id")
            runtime.safe_id(safe)
            for key in ("login", "status", "checked_at", "credential_storage"):
                result.setdefault(key, "")
                if not isinstance(result[key], str) or _has_control(result[key]) or len(result[key]) > 256:
                    raise ValueError("Invalid GitHub account state")
            for key in ("user_id", "author_name", "author_email"):
                result.setdefault(key, "")
            if not isinstance(result["user_id"], str) or (result["user_id"] and not result["user_id"].isdigit()):
                raise ValueError("Invalid GitHub account state")
            runtime.github_author(result)
        elif kind == "web_services":
            if result.get("tool") != "deepseek-harness":
                raise ValueError("Unsupported Web service")
            for key in ("account_id", "environment_id", "service_key"):
                runtime.safe_id(required_text(key))
            result.setdefault("github_account_id", "")
            if result["github_account_id"]:
                runtime.safe_id(result["github_account_id"])
            for key in ("port", "local_port"):
                if type(result.get(key)) is not int or not 1024 <= result[key] <= 65535:
                    raise ValueError("Invalid Web service port")
            if result.get("status") not in {"running", "starting", "stopped", "unknown", "failed"}:
                raise ValueError("Invalid Web service status")
        elif kind == "auth_states":
            required_text("id")
            result.setdefault("status", "")
            result.setdefault("checked_at", "")
            if not isinstance(result["status"], str) or not isinstance(result["checked_at"], str):
                raise ValueError("auth status fields must be strings")
        return result

    def list(self, kind: str) -> list[dict[str, Any]]:
        return self.list_many(kind)[kind]

    def list_many(self, *kinds: str) -> "dict[str, list[dict[str, Any]]]":
        """Several ``list`` calls over one connection (the UI polls some of them every few seconds)."""
        tables = [self._table(kind) for kind in kinds]
        with self._connect() as connection:
            rows = {table: connection.execute(f"SELECT payload FROM {table} ORDER BY id").fetchall() for table in tables}
        return {table: [self._known(table, json.loads(row["payload"])) for row in rows[table]] for table in tables}

    def get(self, kind: str, id: str) -> dict[str, Any]:
        table = self._table(kind)
        with self._connect() as connection:
            row = connection.execute(f"SELECT payload FROM {table} WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise KeyError(id)
        return self._known(table, json.loads(row["payload"]))

    @staticmethod
    def _known(table: str, item: dict[str, Any]) -> dict[str, Any]:
        """Only the fields this build uses: a field dropped during development simply disappears on the next save."""
        return {key: value for key, value in item.items() if key in _FIELDS[table]}

    def save(self, kind: str, item: dict[str, Any]) -> dict[str, Any]:
        table = self._table(kind)
        record = self._validate(kind, item)
        if kind == "environments" and record["id"] == "local" and (
            record["name"], record["kind"], record["ssh_target"]
        ) != ("本機", "local", ""):
            raise ValueError("The built-in local environment cannot be changed")
        fields = {"id": record["id"], "payload": self._dump(record)}
        if kind == "presets":
            fields.update(account_id=record["account_id"] or None, environment_id=record["environment_id"])
        elif kind == "sessions":
            fields.update(
                preset_id=record.get("preset_id") or None,
                account_id=record["account_id"] or None,
                environment_id=record["environment_id"],
            )
        columns = list(fields)
        updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "id")
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}"
        try:
            with self._connect() as connection:
                self._check_identity(connection, kind, record)
                self._check_bindings(connection, kind, record)
                old = None
                if kind in {"accounts", "environments"}:
                    row = connection.execute(f"SELECT payload FROM {kind} WHERE id=?", (record["id"],)).fetchone()
                    old = json.loads(row[0]) if row else None
                if kind == "accounts":
                    changed = old is not None and any(
                        old.get(key, "") != record.get(key, "")
                        for key in ("base_url", "allow_insecure_loopback")
                    )
                    record["credential_revision"] = (
                        uuid.uuid4().hex if changed or old is None else old["credential_revision"]
                    )
                    if changed:
                        self._reset_auth_states(connection, record["id"])
                elif kind == "environments":
                    changed = old is not None and any(
                        old.get(key, 0 if key == "remember_seconds" else "interactive")
                        != record.get(key, 0 if key == "remember_seconds" else "interactive")
                        for key in ("remember_seconds", "ssh_auth_mode")
                    )
                    record["credential_epoch"] = (
                        uuid.uuid4().hex if changed or old is None else old["credential_epoch"]
                    )
                fields["payload"] = self._dump(record)
                connection.execute(sql, list(fields.values()))
        except sqlite3.IntegrityError as error:
            raise ValueError(f"Invalid {kind} reference: {error}") from error
        return record

    @staticmethod
    def _reset_auth_states(connection: sqlite3.Connection, account_id: str) -> None:
        prefix = account_id + ":"
        ids = [row[0] for row in connection.execute("SELECT id FROM auth_states") if row[0].startswith(prefix)]
        connection.executemany("DELETE FROM auth_states WHERE id=?", ((id,) for id in ids))

    def revoke_ssh(self, id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM environments WHERE id=?", (id,)).fetchone()
            if row is None:
                raise KeyError(id)
            environment = json.loads(row[0])
            if environment.get("kind") != "ssh":
                raise ValueError("Only SSH environments have revocable credentials")
            environment["credential_epoch"] = uuid.uuid4().hex
            connection.execute("UPDATE environments SET payload=? WHERE id=?", (self._dump(environment), id))
        return environment

    @staticmethod
    def _check_bindings(connection: sqlite3.Connection, kind: str, record: dict) -> None:
        def get(table, id):
            row = connection.execute(f"SELECT payload FROM {table} WHERE id=?", (id,)).fetchone()
            if not row:
                raise ValueError("Binding references a missing record")
            return json.loads(row[0])
        if kind in {"github_accounts", "web_services"}:
            environment = get("environments", record["environment_id"])
            if kind == "web_services" and environment["kind"] != "ssh":
                raise ValueError("Harness requires an SSH environment")
        if kind == "accounts" and record.get("tool") == "deepseek-harness":
            if record.get("auth") != "subscription" or not record.get("environment_id") or get("environments", record["environment_id"])["kind"] != "ssh":
                raise ValueError("Harness requires an account bound to SSH")
        if kind in {"presets", "web_services"} and record.get("github_account_id"):
            github = get("github_accounts", record["github_account_id"])
            if github["environment_id"] != record["environment_id"]:
                raise ValueError("GitHub account belongs to another environment")
        if kind == "web_services":
            account = get("accounts", record["account_id"])
            if account["tool"] != "deepseek-harness" or account.get("environment_id") != record["environment_id"]:
                raise ValueError("Harness account belongs to another environment")

    @staticmethod
    def _check_identity(connection: sqlite3.Connection, kind: str, record: dict[str, Any]) -> None:
        fields = {"accounts": ("tool", "auth"), "environments": ("kind", "ssh_target"), "github_accounts": ("environment_id",)}.get(kind)
        if not fields:
            return
        row = connection.execute(f"SELECT payload FROM {kind} WHERE id = ?", (record["id"],)).fetchone()
        if row:
            old = json.loads(row[0])
            if any(old.get(key, "") != record.get(key, "") for key in fields):
                raise ValueError("Record identity cannot be changed; create a new account or environment")

    def delete(self, kind: str, id: str) -> None:
        table = self._table(kind)
        if kind == "environments" and id == "local":
            raise ValueError("The built-in local environment cannot be deleted")
        try:
            with self._connect() as connection:
                if connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (id,)).fetchone() is None:
                    raise KeyError(id)
                column = {"accounts": "account_id", "github_accounts": "github_account_id", "environments": "environment_id"}.get(kind)
                if column:
                    for row in connection.execute("SELECT payload FROM web_services").fetchall():
                        service = json.loads(row[0])
                        if service.get(column) == id and service.get("status") != "stopped":
                            raise ValueError(tr('store.64cc7c60c2cd'))
                    for row in connection.execute("SELECT id,payload FROM web_services").fetchall():
                        if json.loads(row[1]).get(column) == id:
                            connection.execute("DELETE FROM web_services WHERE id=?", (row[0],))
                if kind == "github_accounts":
                    self._rewrite(connection, "presets", "1=1", (), lambda item: item.update(github_account_id="", github_repo="", github_remote="", github_access="", github_branch="") if item.get("github_account_id") == id else None)
                {"presets": self._release_project, "accounts": self._release_account,
                 "environments": self._release_environment}.get(kind, lambda *_: None)(connection, id)
                connection.execute(f"DELETE FROM {table} WHERE id = ?", (id,))
        except sqlite3.IntegrityError as error:
            noun = {"accounts": tr('store.969396f78f3b'), "environments": tr('store.fba18681418f'), "presets": tr('store.e564b916b12e')}.get(kind, tr('store.58e891bce783'))
            raise ValueError(tr('store.b0cf0eaf6da0', v0=f'{noun}')) from error

    # Deleting something must not be blocked by history that merely mentions it. Each helper frees what can be freed
    # and refuses, in plain words, only when a live work session still depends on the record.
    @staticmethod
    def _rewrite(connection: sqlite3.Connection, table: str, where: str, args: tuple, change) -> None:
        for row in connection.execute(f"SELECT id, payload FROM {table} WHERE {where}", args).fetchall():
            item = json.loads(row["payload"])
            change(item)
            connection.execute(f"UPDATE {table} SET payload=? WHERE id=?", (Store._dump(item), row["id"]))

    @staticmethod
    def _live_sessions(connection: sqlite3.Connection, column: str, id: str) -> "list[dict[str, Any]]":
        found = [json.loads(r["payload"]) for r in connection.execute(f"SELECT payload FROM sessions WHERE {column}=?", (id,)).fetchall()]
        return [s for s in found if s.get("status") not in _FINISHED]

    @staticmethod
    def _strip_from_workspaces(connection: sqlite3.Connection, key: str, id: str) -> None:
        def change(item: dict[str, Any]) -> None:
            for slot in item.get("slots", []):
                if slot.get(key) == id:
                    slot.pop(key, None)
        Store._rewrite(connection, "workspaces", "1=1", (), change)

    @staticmethod
    def _release_project(connection: sqlite3.Connection, id: str) -> None:
        """Its past sessions stay (a remote one may still be running and attachable) but no longer point at the project."""
        Store._rewrite(connection, "sessions", "preset_id=?", (id,), lambda item: item.update(preset_id=""))
        connection.execute("UPDATE sessions SET preset_id=NULL WHERE preset_id=?", (id,))

    @staticmethod
    def _release_account(connection: sqlite3.Connection, id: str) -> None:
        if Store._live_sessions(connection, "account_id", id):
            raise ValueError(tr('store.4f05206bc5d7'))
        Store._rewrite(connection, "presets", "account_id=?", (id,), lambda item: item.update(account_id=""))
        connection.execute("UPDATE presets SET account_id=NULL WHERE account_id=?", (id,))
        connection.execute("DELETE FROM sessions WHERE account_id=?", (id,))
        Store._strip_from_workspaces(connection, "account_id", id)
        Store._reset_auth_states(connection, id)

    @staticmethod
    def _release_environment(connection: sqlite3.Connection, id: str) -> None:
        if any(json.loads(row[0]).get("environment_id") == id for row in connection.execute("SELECT payload FROM github_accounts")):
            raise ValueError(tr('store.4bcfee0a9d4c'))
        names = [json.loads(r["payload"]).get("name", "") for r in connection.execute("SELECT payload FROM presets WHERE environment_id=?", (id,)).fetchall()]
        if names:
            shown = "、".join(names[:5]) + (tr('store.09b4f40f59dd', v0=f'{len(names)}') if len(names) > 5 else "")
            raise ValueError(tr('store.200fb4072e87', v0=f'{shown}'))
        if Store._live_sessions(connection, "environment_id", id):
            raise ValueError(tr('store.b5b9bccd72be'))
        connection.execute("DELETE FROM sessions WHERE environment_id=?", (id,))
        Store._rewrite(connection, "accounts", "1=1", (), lambda item: item.update(environment_id="") if item.get("environment_id") == id else None)
        connection.execute("DELETE FROM auth_states WHERE substr(id, instr(id, ':') + 1) = ?", (id,))
        Store._strip_from_workspaces(connection, "environment_id", id)

    def export_config(self, path: Path) -> None:
        output = {
            "version": 2,
            "accounts": self.list("accounts"),
            "environments": self.list("environments"),
            "presets": self.list("presets"),
            "workspaces": self.list("workspaces"),
            "github_accounts": self.list("github_accounts"),
        }
        for account in output["accounts"]:
            account.pop("credential_revision", None)
        for account in output["github_accounts"]:
            for key in ("login", "status", "checked_at", "credential_storage", "user_id"):
                account.pop(key, None)
            for key in ("author_name", "author_email"):     # the commit identity travels with the account when set
                if not account.get(key):
                    account.pop(key, None)
        for project in output["presets"]:
            project.pop("github_branch", None)
            project.pop("github_access", None)
        for environment in output["environments"]:
            environment.pop("credential_epoch", None)
            environment["remember_seconds"] = 0
        Path(path).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def import_config(self, path: Path) -> None:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Invalid configuration file") from error
        if (
            not isinstance(data, dict)
            or not {"version", "accounts", "environments", "presets"} <= set(data) <= {"version", "accounts", "environments", "presets", "workspaces", "github_accounts"}
            or type(data.get("version")) is not int
            or data.get("version") != 2
        ):
            raise ValueError("Configuration must have version 2 and only accounts, environments, presets, workspaces and GitHub accounts")
        records: dict[str, list[dict[str, Any]]] = {}
        for kind in ("accounts", "environments", "presets", "github_accounts"):
            entries = data.get(kind, [])
            if not isinstance(entries, list):
                raise ValueError(f"{kind} must be a list")
            records[kind] = [self._validate(kind, entry, allow_internal=False) for entry in entries]
            ids = [item["id"] for item in records[kind]]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate {kind} id")
        workspaces = data.get("workspaces", [])
        if not isinstance(workspaces, list):
            raise ValueError("workspaces must be a list")
        records["workspaces"] = [self._validate("workspaces", entry, allow_internal=False) for entry in workspaces]
        if len({item["id"] for item in records["workspaces"]}) != len(records["workspaces"]):
            raise ValueError("Duplicate workspaces id")
        if any(
            item["id"] == "local"
            and (item["name"], item["kind"], item["ssh_target"]) != ("本機", "local", "")
            for item in records["environments"]
        ):
            raise ValueError("The built-in local environment cannot be changed")
        environment_ids = {record["id"] for record in records["environments"]} | {"local"}
        account_ids = {record["id"] for record in records["accounts"]}
        with self._connect() as connection:
            account_ids.update(row[0] for row in connection.execute("SELECT id FROM accounts"))
            environment_ids.update(row[0] for row in connection.execute("SELECT id FROM environments"))
            if any((item["account_id"] and item["account_id"] not in account_ids) or item["environment_id"] not in environment_ids
                   for item in records["presets"]):
                raise ValueError("Preset references a missing account or environment")
            try:
                for kind in ("environments", "accounts", "github_accounts", "presets", "workspaces"):
                    for record in records[kind]:
                        self._check_identity(connection, kind, record)
                        self._check_bindings(connection, kind, record)
                        if kind == "environments":
                            # Imports never carry a reusable password policy or credential epoch.
                            record["remember_seconds"] = 0
                            record["credential_epoch"] = uuid.uuid4().hex
                        elif kind == "accounts":
                            old_row = connection.execute(
                                "SELECT payload FROM accounts WHERE id=?", (record["id"],)
                            ).fetchone()
                            old = json.loads(old_row[0]) if old_row else None
                            changed = old is not None and any(
                                old.get(key, "") != record.get(key, "")
                                for key in ("base_url", "allow_insecure_loopback")
                            )
                            record["credential_revision"] = (
                                uuid.uuid4().hex if changed or old is None else old["credential_revision"]
                            )
                            if changed:
                                self._reset_auth_states(connection, record["id"])
                        fields: dict[str, Any] = {"id": record["id"], "payload": self._dump(record)}
                        if kind == "presets":
                            fields.update(account_id=record["account_id"] or None, environment_id=record["environment_id"])
                        columns = list(fields)
                        updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "id")
                        connection.execute(
                            f"INSERT INTO {kind} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
                            f"ON CONFLICT(id) DO UPDATE SET {updates}",
                            list(fields.values()),
                        )
            except sqlite3.IntegrityError as error:
                raise ValueError(f"Invalid configuration reference: {error}") from error
