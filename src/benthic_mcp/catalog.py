import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from benthic_mcp.bdp import CatalogSnapshot
from benthic_mcp.errors import QueryValidationError
from benthic_mcp.models import (
    ColumnInfo,
    DiscoverResult,
    JoinEndpointInfo,
    JoinPathInfo,
    RelationInfo,
    Reliability,
)

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "any",
    "are",
    "for",
    "from",
    "in",
    "of",
    "on",
    "over",
    "show",
    "that",
    "the",
    "to",
    "with",
}


def _term_matches(token: str, term: str) -> bool:
    return token == term or token.startswith(term) or term.startswith(token)


def qualified_relation_name(definition: "RelationDefinition") -> str:
    return f"{definition.dataset}.{definition.name}"


@dataclass(frozen=True, slots=True)
class ColumnDefinition:
    name: str
    type: str
    native_type: str | None
    nullable: bool
    description: str | None
    srid: int | None
    unit: str | None


@dataclass(slots=True)
class RelationHint:
    """Discovery ranking overlay for one relation, supplied by the playbook.

    Lives here rather than in playbook.py because Catalog consumes it, and playbook.py
    already depends on this module.
    """

    terms: dict[str, int] = field(default_factory=dict)
    preferred_columns: list[str] = field(default_factory=list)
    description: str | None = None
    anti_patterns: list[str] = field(default_factory=list)

    def get(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default)


@dataclass(frozen=True, slots=True)
class RelationDefinition:
    dataset: str
    name: str
    relation_type: str
    provenance: str
    description: str | None
    queryable: bool
    primary_key: tuple[str, ...]
    row_count_estimate: int | None
    columns: dict[str, ColumnDefinition]
    endpoint: str | None
    manifest_hash: str
    manifest_url: str


@dataclass(frozen=True, slots=True)
class DatasetDefinition:
    name: str
    title: str | None
    description: str | None
    license: str | None
    manifest_hash: str
    manifest_url: str
    commit_hash: str
    migration_status: str | None


@dataclass(frozen=True, slots=True)
class JoinDefinition:
    from_dataset: str
    from_relation: str
    from_column: str
    to_dataset: str
    to_relation: str
    to_column: str
    join_type: str
    reliability: Reliability
    notes: str | None
    from_srid: int | None = None
    to_srid: int | None = None


@dataclass(frozen=True, slots=True)
class JoinEdge:
    """One signed join, oriented so `left` is the relation the caller is standing on."""

    left: str
    left_column: str
    right: str
    right_column: str
    join_type: str
    reliability: Reliability
    notes: str | None = None

    def as_pair(self) -> tuple[str, str, str, str]:
        return (self.left, self.left_column, self.right, self.right_column)


# The one real chain in the signed catalog is two hops. Beyond that a longer route is a sign the
# caller should hop explicitly rather than chain, and the answer stays bounded either way.
_DEFAULT_MAX_HOPS = 2


@dataclass(frozen=True, slots=True)
class CollectionDefinition:
    name: str
    title: str | None
    description: str | None
    purpose: str | None
    license: str | None
    protocol_version: str | None


# How many columns discovery lists inline. The full set is one explicit call away, because 21 of the
# 119 signed relations have more columns than any inline list should carry, and one has 374.
_INLINE_COLUMN_LIMIT = 12


def _token_overlap(left: str, right: str) -> int:
    """Shared underscore-separated tokens, so total_obligation and obligation_date score 1."""
    return len({part for part in left.split("_") if part} & {part for part in right.split("_") if part})


class Catalog:
    def __init__(
        self,
        snapshot: CatalogSnapshot,
        relation_hints: dict[tuple[str, str], RelationHint] | None = None,
    ) -> None:
        self.collections = snapshot.collections
        self.warnings = list(snapshot.warnings)
        self.datasets: dict[str, DatasetDefinition] = {}
        self.collection_definitions: dict[str, CollectionDefinition] = {}
        self.relations: dict[tuple[str, str], RelationDefinition] = {}
        self.joins: list[JoinDefinition] = []
        # Discovery ranking and preferred columns come from the playbook, not from code.
        self.relation_hints = relation_hints or {}
        self._build(snapshot.manifests)

    def _build(self, manifests: dict[str, dict[str, Any]]) -> None:
        for collection_name, collection in self.collections.items():
            self.collection_definitions[collection_name] = CollectionDefinition(
                name=collection_name,
                title=collection.get("title"),
                description=collection.get("description"),
                purpose=collection.get("purpose"),
                license=collection.get("license"),
                protocol_version=collection.get("protocol_version"),
            )

        manifest_urls = {
            member["dataset_name"]: member.get("manifest_url", "")
            for collection in self.collections.values()
            for member in collection.get("members", [])
            if isinstance(member, dict) and isinstance(member.get("dataset_name"), str)
        }
        for dataset_name, manifest in manifests.items():
            signature = manifest["cryptographic_signature"]
            manifest_hash = signature["payload_hash"]
            manifest_url = manifest_urls.get(dataset_name, "")
            etl = manifest.get("etl_provenance", {})
            dataset = DatasetDefinition(
                name=dataset_name,
                title=manifest.get("title"),
                description=manifest.get("description"),
                license=manifest.get("license"),
                manifest_hash=manifest_hash,
                manifest_url=manifest_url,
                commit_hash=etl.get("commit_hash", ""),
                migration_status=etl.get("migration_status"),
            )
            self.datasets[dataset_name] = dataset
            endpoint = self._postgrest_endpoint(manifest.get("endpoints", []))
            for relation in manifest.get("schema_definition", []):
                relation_name = relation.get("name")
                if not isinstance(relation_name, str):
                    continue
                columns: dict[str, ColumnDefinition] = {}
                for column in relation.get("columns", []):
                    column_name = column.get("name")
                    if not isinstance(column_name, str):
                        continue
                    columns[column_name] = ColumnDefinition(
                        name=column_name,
                        type=column.get("type", "unknown"),
                        native_type=column.get("native_type"),
                        nullable=column.get("nullable", True),
                        description=column.get("description"),
                        srid=column.get("srid"),
                        unit=column.get("unit"),
                    )
                self.relations[(dataset_name, relation_name)] = RelationDefinition(
                    dataset=dataset_name,
                    name=relation_name,
                    relation_type=relation.get("relation_type", "table"),
                    provenance=relation.get("provenance", "unknown"),
                    description=relation.get("description"),
                    queryable=relation.get("queryable", True),
                    primary_key=tuple(relation.get("primary_key", [])),
                    row_count_estimate=relation.get("row_count_estimate"),
                    columns=columns,
                    endpoint=endpoint,
                    manifest_hash=manifest_hash,
                    manifest_url=manifest_url,
                )

        for collection in self.collections.values():
            for path in collection.get("join_paths", []):
                self.joins.append(
                    JoinDefinition(
                        from_dataset=path["from"]["dataset_name"],
                        from_relation=path["from"]["relation"],
                        from_column=path["from"]["column"],
                        to_dataset=path["to"]["dataset_name"],
                        to_relation=path["to"]["relation"],
                        to_column=path["to"]["column"],
                        join_type=path["join_type"],
                        reliability=Reliability(path["reliability"]),
                        notes=path.get("notes"),
                        from_srid=path["from"].get("srid"),
                        to_srid=path["to"].get("srid"),
                    )
                )

    @staticmethod
    def _postgrest_endpoint(endpoints: list[dict[str, Any]]) -> str | None:
        for endpoint in endpoints:
            if endpoint.get("transport_type") != "postgrest_api":
                continue
            metadata = endpoint.get("meta", {})
            if metadata.get("requires_auth", False):
                continue
            base_url = endpoint.get("base_url")
            if isinstance(base_url, str):
                return base_url.rstrip("/") + "/"
        return None

    def discover(
        self,
        query: str = "",
        dataset: str | None = None,
        relation: str | None = None,
        limit: int = 20,
        detail: str = "summary",
    ) -> DiscoverResult:
        tokens = [token for token in re.split(r"[^A-Za-z0-9_]+", query.lower()) if token and token not in _STOP_WORDS]
        if dataset and "." in dataset and relation is None:
            dataset, relation = dataset.split(".", 1)
        if relation and "." in relation and dataset is None:
            dataset, relation = relation.split(".", 1)
        if relation and not any(name == relation for _, name in self.relations):
            relation = None
        if dataset and not any(name == dataset for name, _ in self.relations):
            dataset = None
        if detail == "full":
            # detail="full" answers "what columns does this one relation have". Without a relation it
            # would dump every column of every match, which is the response size this parameter
            # exists to avoid.
            if relation is None:
                raise QueryValidationError("detail='full' needs relation='dataset.relation' to name one relation")
            limit = 1
        matches: list[tuple[int, RelationDefinition, list[str], list[ColumnDefinition]]] = []

        for (dataset_name, relation_name), definition in self.relations.items():
            if dataset is not None and dataset_name != dataset:
                continue
            if relation is not None and relation_name != relation:
                continue
            if not definition.queryable or definition.endpoint is None:
                continue

            hints = self.relation_hints.get((dataset_name, relation_name)) or RelationHint()
            hint_terms = hints.get("terms", {})
            relation_text = " ".join(
                filter(None, (dataset_name, relation_name, definition.description, definition.provenance))
            ).lower()
            matching_columns = [
                column
                for column in definition.columns.values()
                if not tokens
                or any(token in column.name.lower() or token in (column.description or "").lower() for token in tokens)
            ]
            matched_terms = [token for token in tokens if token in relation_text or token in matching_columns]
            score = sum(1 for token in tokens if token in relation_text)
            score += min(5, len(matching_columns))
            score += sum(
                int(weight)
                for hint, weight in hint_terms.items()
                if any(_term_matches(token, hint) for token in tokens)
            )
            if not tokens or score > 0:
                matches.append((score, definition, matched_terms, matching_columns))

        explicit_keys = {
            (definition.dataset, definition.name)
            for (definition_dataset, definition_name), definition in self.relations.items()
            if f"{definition_dataset}.{definition_name}".lower() in query.lower()
        }
        existing_keys = {(item[1].dataset, item[1].name) for item in matches}
        matches.extend((10_000, self.relations[key], [], []) for key in sorted(explicit_keys - existing_keys))

        matches.sort(key=lambda item: (-item[0], item[1].dataset, item[1].name))
        selected = matches[: max(1, limit)]
        selected_keys = {(definition.dataset, definition.name) for _, definition, _, _ in selected}
        relations: list[RelationInfo] = []
        for _, definition, matched_terms, matching_columns in selected:
            hints = self.relation_hints.get((definition.dataset, definition.name)) or RelationHint()
            preferred = [name for name in hints.get("columns", []) if name in definition.columns]
            preferred_set = set(preferred)
            column_names = preferred + [column.name for column in matching_columns if column.name not in preferred_set]
            if not column_names:
                column_names = list(definition.columns)[:_INLINE_COLUMN_LIMIT]
            column_names = column_names[:_INLINE_COLUMN_LIMIT]
            if detail == "full":
                # The explicit escape hatch for a relation whose column list does not fit inline.
                # One relation at a time, so this stays a bounded response rather than a second copy
                # of the whole schema.
                column_names = list(definition.columns)
            columns = [definition.columns[name] for name in column_names]
            relations.append(
                RelationInfo(
                    source=qualified_relation_name(definition),
                    dataset=definition.dataset,
                    relation=definition.name,
                    description=hints.get("description", definition.description),
                    relation_type=definition.relation_type,
                    provenance=definition.provenance,
                    primary_key=list(definition.primary_key),
                    row_count_estimate=definition.row_count_estimate,
                    columns=[
                        ColumnInfo(
                            name=column.name,
                            type=column.type,
                            native_type=column.native_type,
                            nullable=column.nullable,
                            description=column.description,
                            srid=column.srid,
                            unit=column.unit,
                        )
                        for column in columns
                    ],
                    columns_truncated=len(columns) < len(definition.columns),
                    match_terms=matched_terms,
                )
            )

        join_paths = [
            JoinPathInfo(
                from_endpoint=JoinEndpointInfo(
                    dataset=join.from_dataset,
                    relation=join.from_relation,
                    column=join.from_column,
                    srid=join.from_srid,
                ),
                to_endpoint=JoinEndpointInfo(
                    dataset=join.to_dataset,
                    relation=join.to_relation,
                    column=join.to_column,
                    srid=join.to_srid,
                ),
                join_type=join.join_type,
                reliability=join.reliability,
                notes=join.notes,
            )
            for join in self.joins
            if (join.from_dataset, join.from_relation) in selected_keys
            or (join.to_dataset, join.to_relation) in selected_keys
        ][:8]

        return DiscoverResult(
            query=query,
            relations=relations,
            join_paths=join_paths,
            total_matches=len(matches),
            more_available=len(matches) > len(selected),
            warnings=self.warnings,
        )

    def resolve_relation(self, dataset: str, relation: str) -> RelationDefinition:
        definition = self.relations.get((dataset, relation))
        if definition is None:
            raise QueryValidationError(f"Relation {dataset}.{relation} is not in the signed BDP manifest")
        if not definition.queryable:
            raise QueryValidationError(f"Relation {dataset}.{relation} is not queryable")
        if definition.endpoint is None:
            raise QueryValidationError(f"Relation {dataset}.{relation} has no anonymous PostgREST endpoint")
        return definition

    def column_candidates(self, definition: RelationDefinition, column: str) -> list[str]:
        """Signed column names close to a guess, best first.

        Without this the unknown-column error was a dead end: discover shows at most 12 columns and
        21 of the 119 relations have more than 40, so a model that guesses wrong has no way to learn
        the real name except guessing again. That is the loop the eval traces show burning the turn
        budget, and prose telling the model to check its columns cannot break it.

        Candidates are only ever names that exist in the signed manifest, so a suggestion can never
        invent a column. An empty list means the model has to look the schema up instead.
        """
        guess = column.strip().lower().replace("-", "_")
        if not guess:
            return []
        variants = {guess, guess.rstrip("s"), guess + "s", guess.replace("_", "")}
        scored: list[tuple[int, str]] = []
        for name in definition.columns:
            lowered = name.lower()
            if lowered in variants:
                scored.append((0, name))
                continue
            shared = _token_overlap(guess, lowered)
            if shared >= 2 or (shared >= 1 and (guess in lowered or lowered in guess)):
                scored.append((1 - shared, name))
        scored.sort()
        return [name for _, name in scored]

    def validate_columns(self, definition: RelationDefinition, columns: list[str]) -> None:
        unknown = sorted({column for column in columns if column not in definition.columns})
        if not unknown:
            return
        hints = []
        for column in unknown:
            candidates = self.column_candidates(definition, column)
            if not candidates:
                continue
            shown = ", ".join(candidates[:3])
            if len(candidates) == 1:
                hints.append(f"{column} -> {shown}")
            else:
                hints.append(f"{column} -> one of {shown}")
        suffix = ""
        if hints:
            suffix = f". Did you mean {'; '.join(hints)}?"
        elif len(definition.columns) > _INLINE_COLUMN_LIMIT:
            suffix = (
                f". This relation has {len(definition.columns)} columns and discovery lists only"
                f" {_INLINE_COLUMN_LIMIT}; call benthic_discover with relation="
                f"'{qualified_relation_name(definition)}' and detail='full' to see them all"
            )
        else:
            suffix = ". Call benthic_discover to list the signed columns"
        raise QueryValidationError(
            f"Unknown columns for {definition.dataset}.{definition.name}: {', '.join(unknown)}{suffix}"
        )

    def find_join(
        self,
        left: RelationDefinition,
        right: RelationDefinition,
        left_column: str,
        right_column: str,
    ) -> JoinDefinition:
        for join in self.joins:
            direct = (
                join.from_dataset == left.dataset
                and join.from_relation == left.name
                and join.from_column == left_column
                and join.to_dataset == right.dataset
                and join.to_relation == right.name
                and join.to_column == right_column
            )
            reverse = (
                join.from_dataset == right.dataset
                and join.from_relation == right.name
                and join.from_column == right_column
                and join.to_dataset == left.dataset
                and join.to_relation == left.name
                and join.to_column == left_column
            )
            if direct or reverse:
                return join
        raise QueryValidationError(
            "The requested key pair is not a signed BDP join path: "
            f"{left.dataset}.{left.name}.{left_column} -> {right.dataset}.{right.name}.{right_column}"
        )

    def endpoint_for(self, dataset: str) -> str:
        for definition in self.datasets.values():
            if definition.name != dataset:
                continue
            for relation in self.relations.values():
                if relation.dataset == dataset and relation.endpoint is not None:
                    return relation.endpoint
        raise QueryValidationError(f"Dataset {dataset} has no anonymous PostgREST endpoint")

    def collection_notes(self) -> list[str]:
        notes: list[str] = []
        for definition in self.collection_definitions.values():
            label = definition.title or definition.name
            notes.extend(f"{label}: {value}" for value in (definition.purpose, definition.description) if value)
        return notes

    def fingerprint(self) -> str:
        """Content identity of the signed catalog; changes when any manifest is republished."""
        hashes = sorted(definition.manifest_hash for definition in self.datasets.values())
        return hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()

    def join_graph(self) -> dict[str, list[JoinEdge]]:
        """Adjacency over the signed join edges, undirected, keyed by qualified relation name."""
        graph: dict[str, list[JoinEdge]] = {}
        for join in self.joins:
            left = f"{join.from_dataset}.{join.from_relation}"
            right = f"{join.to_dataset}.{join.to_relation}"
            graph.setdefault(left, []).append(
                JoinEdge(
                    left=left,
                    left_column=join.from_column,
                    right=right,
                    right_column=join.to_column,
                    join_type=join.join_type,
                    reliability=join.reliability,
                    notes=join.notes,
                )
            )
            graph.setdefault(right, []).append(
                JoinEdge(
                    left=right,
                    left_column=join.to_column,
                    right=left,
                    right_column=join.from_column,
                    join_type=join.join_type,
                    reliability=join.reliability,
                    notes=join.notes,
                )
            )
        return graph

    def join_paths(
        self, from_relation: str, to_relation: str, max_hops: int = _DEFAULT_MAX_HOPS
    ) -> list[list[JoinEdge]]:
        """Every signed route from one relation to another, shortest first.

        Joining was previously only reachable by phrasing a natural-language query and reading the
        answer back out of a search result, which is a re-phraseable operation: a model that asked
        for a path that does not exist got no confirmation and simply asked again. Two exact relation
        names cannot be rephrased, so this turns an unbounded search into a bounded lookup.

        The graph is small and signed, so the answer is a pure function of the two names. There is
        nothing here to memoise; the problem was never memory.
        """
        if from_relation == to_relation:
            return []
        graph = self.join_graph()
        if from_relation not in graph or to_relation not in graph:
            return []
        routes: list[list[JoinEdge]] = []
        frontier: list[tuple[str, list[JoinEdge]]] = [(from_relation, [])]
        seen = {from_relation}
        while frontier:
            following: list[tuple[str, list[JoinEdge]]] = []
            for node, path in frontier:
                if len(path) >= max_hops:
                    continue
                for edge in graph.get(node, []):
                    if edge.right in seen:
                        continue
                    extended = [*path, edge]
                    if edge.right == to_relation:
                        routes.append(extended)
                        continue
                    seen.add(edge.right)
                    following.append((edge.right, extended))
            frontier = following
        return routes

    def resolve_join(self, left_relation: str, right_relation: str) -> tuple[JoinEdge | None, list[JoinEdge]]:
        """The one signed edge to use between two relations, or the candidates and no choice.

        Returning a resolved edge is limited to a single reliable identifier join. A heuristic,
        partial or spatial edge is never selected on the caller's behalf even when it is the only
        one, because the standing rule is that a partial join needs context_conditions and must be
        reported as provisional, and a caller who did not ask for a fuzzy match should not get one.
        """
        candidates = [edge for edge in self.join_graph().get(left_relation, []) if edge.right == right_relation]
        if len(candidates) != 1:
            return None, candidates
        edge = candidates[0]
        if edge.join_type == "identifier" and edge.reliability == "reliable":
            return edge, candidates
        return None, candidates

    def nearest_joins(self, relation: str) -> list[JoinEdge]:
        """The signed edges touching one relation, for when no route exists.

        Answering "there is no path" with nothing leaves the model where it started. Naming what
        *is* connected is what turns a dead end into a next step.
        """
        return self.join_graph().get(relation, [])

    def signed_join_pairs(self) -> set[tuple[str, str, str, str]]:
        return {
            (
                f"{join.from_dataset}.{join.from_relation}",
                join.from_column,
                f"{join.to_dataset}.{join.to_relation}",
                join.to_column,
            )
            for join in self.joins
        }
