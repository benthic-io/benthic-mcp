from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reliability(StrEnum):
    RELIABLE = "reliable"
    PARTIAL = "partial"
    HEURISTIC = "heuristic"


class FilterOperator(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    LIKE = "like"
    ILIKE = "ilike"
    IN = "in"
    IS_NULL = "is.null"
    NOT_IS_NULL = "not.is.null"


class JoinMode(StrEnum):
    INNER = "inner"
    LEFT = "left"


class SourceErrorPolicy(StrEnum):
    FAIL = "fail"
    PARTIAL = "partial"


class AggregateFunction(StrEnum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class FilterSpec(StrictModel):
    column: str = Field(min_length=1, max_length=128)
    operator: FilterOperator
    value: Any = None

    @model_validator(mode="after")
    def validate_value(self) -> "FilterSpec":
        if self.operator in {FilterOperator.IS_NULL, FilterOperator.NOT_IS_NULL}:
            if self.value is not None:
                raise ValueError(f"{self.operator.value} does not accept a value")
        elif self.value is None:
            raise ValueError(f"{self.operator.value} requires a value")
        if self.operator == FilterOperator.IN and (not isinstance(self.value, list) or not self.value):
            raise ValueError("in requires a non-empty list")
        return self


class SourceOrder(StrictModel):
    column: str = Field(min_length=1, max_length=128)
    descending: bool = False
    nulls_first: bool | None = None


class RelationSource(StrictModel):
    alias: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    dataset: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    relation: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    select: list[str] = Field(default_factory=list, max_length=128)
    filters: list[FilterSpec] = Field(default_factory=list, max_length=32)
    order: list[SourceOrder] = Field(default_factory=list, max_length=16)
    limit: int | None = Field(default=None, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class JoinCondition(StrictModel):
    left_column: str = Field(min_length=1, max_length=128)
    right_column: str = Field(min_length=1, max_length=128)


class JoinSpec(StrictModel):
    left_alias: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    right_alias: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    left_column: str = Field(min_length=1, max_length=128)
    right_column: str = Field(min_length=1, max_length=128)
    mode: JoinMode = JoinMode.INNER
    extra_conditions: list[JoinCondition] = Field(default_factory=list, max_length=8)


class OutputOrder(StrictModel):
    column: str = Field(min_length=1, max_length=200)
    descending: bool = False


class AggregateSpec(StrictModel):
    function: AggregateFunction
    column: str | None = Field(default=None, min_length=1, max_length=200)
    alias: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class HavingSpec(StrictModel):
    column: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    operator: FilterOperator
    value: Any = None

    @model_validator(mode="after")
    def validate_value(self) -> "HavingSpec":
        if self.operator in {FilterOperator.IS_NULL, FilterOperator.NOT_IS_NULL}:
            if self.value is not None:
                raise ValueError(f"{self.operator.value} does not accept a value")
        elif self.value is None:
            raise ValueError(f"{self.operator.value} requires a value")
        return self


class QueryRequest(StrictModel):
    question: str = Field(min_length=1, max_length=4000)
    sources: list[RelationSource] = Field(min_length=1, max_length=5)
    joins: list[JoinSpec] = Field(default_factory=list, max_length=8)
    output_columns: list[str] = Field(default_factory=list, max_length=128)
    group_by: list[str] = Field(default_factory=list, max_length=32)
    aggregates: list[AggregateSpec] = Field(default_factory=list, max_length=32)
    having: list[HavingSpec] = Field(default_factory=list, max_length=32)
    order: list[OutputOrder] = Field(default_factory=list, max_length=16)
    limit: int | None = Field(default=None, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    allowed_reliability: list[Reliability] = Field(
        default_factory=lambda: [Reliability.RELIABLE],
        min_length=1,
    )
    on_source_error: SourceErrorPolicy = SourceErrorPolicy.FAIL

    @model_validator(mode="after")
    def validate_source_aliases(self) -> "QueryRequest":
        aliases = [source.alias for source in self.sources]
        if len(aliases) != len(set(aliases)):
            raise ValueError("source aliases must be unique")
        known = set(aliases)
        for join in self.joins:
            if join.left_alias == join.right_alias:
                raise ValueError("a join cannot reference the same alias twice")
            if join.left_alias not in known or join.right_alias not in known:
                raise ValueError("join aliases must reference declared sources")
        return self


class ColumnInfo(StrictModel):
    # native_type, srid and unit were measured as removable: nothing in this codebase reads them,
    # and dropping them plus the per-source manifest_hash cut discovery responses by 31% and the
    # whole run's prompt tokens by 0.6%, while tuning accuracy fell from 23/25 to 18/25 with empty
    # answers nearly doubling. Restored on the evidence. The 0.6% is the reason: response bytes are
    # dominated by data rows, so trimming schema metadata buys nothing measurable and the fields
    # may be carrying weight the model uses when choosing a type or a spatial predicate.
    name: str
    type: str
    native_type: str | None = None
    nullable: bool = True
    description: str | None = None
    srid: int | None = None
    unit: str | None = None


class RelationInfo(StrictModel):
    source: str
    dataset: str
    relation: str
    description: str | None = None
    relation_type: str = "table"
    provenance: str = "unknown"
    primary_key: list[str] = Field(default_factory=list)
    row_count_estimate: int | None = None
    columns: list[ColumnInfo] = Field(default_factory=list)
    columns_truncated: bool = False
    match_terms: list[str] = Field(default_factory=list)


class DatasetInfo(StrictModel):
    dataset: str
    title: str | None = None
    description: str | None = None
    license: str | None = None
    manifest_hash: str
    manifest_url: str
    commit_hash: str
    migration_status: str | None = None


class PlaybookKeyColumn(StrictModel):
    relation: str
    column: str
    type: str | None = None
    role: str | None = None


class PlaybookJoinRecipe(StrictModel):
    left_source: str
    left_column: str
    right_source: str
    right_column: str
    join_type: str
    reliability: Reliability
    notes: str | None = None


class PlaybookRpcRecipe(StrictModel):
    operation: str
    dataset: str
    required_arguments: list[str] = Field(default_factory=list)
    optional_arguments: list[str] = Field(default_factory=list)
    summary: str | None = None


class PlaybookLessonInfo(StrictModel):
    symptom: str
    lesson: str
    relation: str | None = None
    confidence: str
    scope: str
    author_model: str | None = None
    occurrences: int = 1


class PlaybookDatasetGuide(StrictModel):
    dataset: str
    title: str | None = None
    summary: str | None = None
    when_to_use: list[str] = Field(default_factory=list)
    canonical_sources: list[str] = Field(default_factory=list)
    key_columns: list[PlaybookKeyColumn] = Field(default_factory=list)
    join_recipes: list[PlaybookJoinRecipe] = Field(default_factory=list)
    rpc_recipes: list[PlaybookRpcRecipe] = Field(default_factory=list)
    anti_patterns: list[str] = Field(default_factory=list)
    lessons: list[PlaybookLessonInfo] = Field(default_factory=list)


class PlaybookRoute(StrictModel):
    """One hop, carrying exactly the arguments benthic_join takes so it can be copied verbatim."""

    hops: list[PlaybookJoinRecipe]

    def as_arguments(self) -> list[dict[str, str]]:
        return [
            {
                "left_source": hop.left_source,
                "left_column": hop.left_column,
                "right_source": hop.right_source,
                "right_column": hop.right_column,
            }
            for hop in self.hops
        ]


class PlaybookPathResult(StrictModel):
    """The answer to "how do I get from relation A to relation B", and nothing else.

    Returned instead of the full playbook when from_relation and to_relation are given, so a
    two-dataset join costs a couple of hundred bytes rather than the whole edge list.
    """

    from_relation: str
    to_relation: str
    max_hops: int
    routes: list[PlaybookRoute] = Field(default_factory=list)
    nearest_from_source: list[PlaybookJoinRecipe] = Field(default_factory=list)
    # A bounded negative with alternatives, never silence.
    note: str | None = None


class PlaybookResult(StrictModel):
    collection: str
    status: str
    catalog_fingerprint: str | None = None
    generated_at: datetime | None = None
    generator: str | None = None
    core: list[str] = Field(default_factory=list)
    datasets: list[PlaybookDatasetGuide] = Field(default_factory=list)
    collection_notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Present only when the caller asked for a route between two relations.
    path: PlaybookPathResult | None = None


class ReportResult(StrictModel):
    recorded: bool
    lesson_id: str
    status: str
    similar_pending: int
    review_hint: str
    dataset_slice: PlaybookDatasetGuide | None = None
    warnings: list[str] = Field(default_factory=list)


class JoinEndpointInfo(StrictModel):
    dataset: str
    relation: str
    column: str
    srid: int | None = None


class JoinPathInfo(StrictModel):
    from_endpoint: JoinEndpointInfo
    to_endpoint: JoinEndpointInfo
    join_type: str
    reliability: Reliability
    notes: str | None = None


class DiscoverResult(StrictModel):
    query: str
    relations: list[RelationInfo]
    join_paths: list[JoinPathInfo]
    total_matches: int
    more_available: bool
    warnings: list[str] = Field(default_factory=list)


class SourceMetadata(StrictModel):
    # manifest_hash is never read by this codebase, and removing it alongside the column metadata
    # was part of a change measured at -0.6% prompt tokens and 23/25 to 18/25 tuning accuracy, so it
    # is restored with them. See ColumnInfo.
    alias: str
    source: str
    manifest_hash: str
    # `row_count` is how many rows came back in this page, which is not how many rows the filters
    # matched. On a truncated page the two differ by three orders of magnitude and the old name read
    # as the total, so `matched_rows` carries the real figure when it is known and None when the
    # server could not establish it.
    row_count: int
    matched_rows: int | None = None
    complete: bool


class JoinMetadata(StrictModel):
    left_alias: str
    right_alias: str
    left_column: str
    right_column: str
    join_type: str
    reliability: Reliability
    notes: str | None = None
    warnings: list[str] = Field(default_factory=list)


class QueryWarning(StrictModel):
    source: str | None = None
    message: str


class QueryResult(StrictModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    source_complete: bool
    truncated: bool
    next_offset: int | None = None
    sources: list[SourceMetadata]
    joins: list[JoinMetadata]
    warnings: list[QueryWarning] = Field(default_factory=list)


class FindDistrictRequest(StrictModel):
    operation: Literal["find_district"]
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    congress: int | None = Field(default=None, ge=1, le=1000)


class DistrictBboxRequest(StrictModel):
    operation: Literal["districts_in_bbox"]
    min_lat: float = Field(ge=-90, le=90)
    max_lat: float = Field(ge=-90, le=90)
    min_lon: float = Field(ge=-180, le=180)
    max_lon: float = Field(ge=-180, le=180)
    congress: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_bounds(self) -> "DistrictBboxRequest":
        if self.min_lat > self.max_lat:
            raise ValueError("min_lat cannot exceed max_lat")
        if self.min_lon > self.max_lon:
            raise ValueError("min_lon cannot exceed max_lon")
        return self


class NonprofitsNearbyRequest(StrictModel):
    operation: Literal["nonprofits_nearby"]
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_meters: int = Field(default=10000, ge=1, le=100000)


RpcRequest = Annotated[
    FindDistrictRequest | DistrictBboxRequest | NonprofitsNearbyRequest,
    Field(discriminator="operation"),
]


class RpcMetadata(StrictModel):
    operation: str
    dataset: str
    function_name: str
    endpoint: str
    request_url: str


class RpcResult(StrictModel):
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    metadata: RpcMetadata
    warnings: list[str] = Field(default_factory=list)
