"""Public MCP output contracts. Graph payloads retain platform extension fields."""
from typing import Any, Literal
from typing_extensions import TypedDict, NotRequired
from pydantic import ConfigDict


class Source(TypedDict):
    export_sha256: str
    path: str
    backend: Literal['mock', 'http']
    retrieved_at: str
    export_depth: int | None
    snapshot: str
    served_from: NotRequired[str]
    stale: NotRequired[bool]
    fallback_reason: NotRequired[str]
    checked_at: NotRequired[str]


class Graph(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra='allow')
    title: str
    name: str
    type: str
    meta: str
    value: Any
    successors: list['Graph']
    _source: Source


class ResourceList(TypedDict):
    paths: list[str]
    total: int
    source: Literal['mock', 'configuration']


class Counts(TypedDict):
    nodes: int
    edges: int
    links: int
    _source: NotRequired[Source]


class Subgraph(TypedDict):
    graph: Graph
    depth: int
    _source: Source


class Match(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra='allow')
    path: str
    name: str
    value: Any
    meta: str
    type: str
    resource: str
    snapshot_pointer: str


class QueryResult(TypedDict):
    matches: list[Match]
    total: int
    truncated: bool
    _source: Source


class Coverage(TypedDict):
    __pydantic_config__ = ConfigDict(extra='allow')
    path: str
    status: Literal['read', 'searched', 'failed']
    error: NotRequired[str]
    nodes: NotRequired[int]
    cached: NotRequired[bool]


class SearchResult(TypedDict):
    matches: list[Match]
    total: int
    truncated: bool
    coverage: list[Coverage]
    complete: bool
    searched_resources: int
    configured_resources: int
    cache_ttl_seconds: int
    _sources: list[Source]


class ElementLabel(TypedDict):
    name: str
    symbol: str


class Catalog(TypedDict):
    classes: list[str]
    elements: list[ElementLabel]
    properties: list[str]
    resources: list[str]


class KnowledgeResult(TypedDict):
    status: Literal['ok', 'empty', 'clarify', 'ambiguous', 'not_found', 'unavailable']
    plan: dict[str, Any]
    rows: list[list[Any]]
    total: int
    warnings: list[str]
    references: list[dict[str, str]]
    answer: str
    coverage: list[Coverage]
    complete: bool
    _sources: list[Source]
    catalog: NotRequired[Catalog]
    excluded: NotRequired[int]
    ontology_resources: NotRequired[list[str]]
    __pydantic_config__ = ConfigDict(extra='allow')


class ValidationResult(Counts):
    valid: bool


class WriteResult(TypedDict):
    status: Literal['pending_confirmation', 'done', 'cancelled']
    operation: NotRequired[str]
    preview: NotRequired[dict[str, Any]]
    confirm_token: NotRequired[str]
    result: NotRequired[dict[str, Any]]


class Diagnostic(TypedDict):
    code: str
    message: str
    retryable: bool
    tool: str
    suggestion: str


class ErrorResult(TypedDict):
    error: Diagnostic


class IndexStatus(TypedDict):
    resources: list[dict[str, Any]]


class SyncResult(IndexStatus):
    complete: bool
