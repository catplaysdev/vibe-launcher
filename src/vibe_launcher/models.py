from typing import Literal, NotRequired, TypedDict


class AccountProfile(TypedDict):
    id: NotRequired[str]
    name: str
    tool: Literal["codex", "claude"]
    auth: Literal["subscription", "api"]
    model: NotRequired[str]
    base_url: NotRequired[str]
    allow_insecure_loopback: NotRequired[bool]
    credential_revision: NotRequired[str]


class Environment(TypedDict):
    id: NotRequired[str]
    name: str
    kind: Literal["local", "ssh"]
    ssh_target: NotRequired[str]
    ssh_auth_mode: NotRequired[Literal["interactive", "password", "key"]]
    remember_seconds: NotRequired[int]
    credential_epoch: NotRequired[str]


class ProjectPreset(TypedDict):
    id: NotRequired[str]
    name: str
    account_id: str
    environment_id: str
    path: str
    favorite: NotRequired[bool]
    last_used: NotRequired[str | None]


class Session(TypedDict):
    id: NotRequired[str]
    preset_id: NotRequired[str]
    account_id: str
    environment_id: str
    name: str
    path: str
    status: NotRequired[str]
    updated_at: NotRequired[str]
    pid: NotRequired[int]
    runtime_id: NotRequired[str]
