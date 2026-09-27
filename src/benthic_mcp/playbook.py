"""Dataset access playbook: signed facts from the catalog, prose from the playbook.

The playbook never stores or restates facts that the signed BDP catalog already
carries (relation lists, column types, join paths, RPC endpoints). Those are always
read from the verified catalog at render time, so guidance cannot contradict the
manifest. The playbook only holds semantic prose: what a dataset is for, which
columns to prefer, which mistakes to avoid, and lessons learned at runtime.

Everything a calling LLM submits lands here as `pending`. Nothing a model authored
is served until it has been catalog-verified, consolidated, and passed the A/B
evaluation gate in `eval/promote.py`.
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from benthic_mcp.catalog import Catalog, RelationHint, qualified_relation_name
from benthic_mcp.models import (
    PlaybookDatasetGuide,
    PlaybookJoinRecipe,
    PlaybookKeyColumn,
    PlaybookLessonInfo,
    PlaybookResult,
    PlaybookRpcRecipe,
    StrictModel,
)
from benthic_mcp.rpc import RPC_DEFINITIONS

SCHEMA_VERSION = 1

# Workflow rules that hold regardless of dataset. Kept short: this is injected into
# the benthic_discover description, which is re-sent on every turn.
BASE_CORE: tuple[str, ...] = (
    "Call benthic_discover before benthic_query, and pass its source value verbatim.",
    "Use benthic_join only for a signed path returned by discovery; never invent a join.",
    "Partial joins require context_conditions and must be reported as provisional.",
    "Heuristic matches are not exact; keep the returned reliability warning in the answer.",
    "Never derive totals from truncated or incomplete source data.",
    "Call benthic_playbook(dataset) when a question needs domain conventions for that dataset.",
)

_QUALIFIED = re.compile(r"\b([a-z][a-z0-9_]{1,30})\.([a-z][a-z0-9_]{1,60})\b")
_BACKTICKED = re.compile(r"`([^`]{1,80})`")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# Split on a sentence boundary only when whitespace follows, so `dataset.relation` stays intact.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
# Anti-patterns legitimately name things that do not exist ("there is no `org_name` column"), so a
# sentence carrying a negation cue is allowed to survive unknown identifiers. A hallucinated column
# still has to be asserted rather than disavowed to get past the screen.
_NEGATION = re.compile(
    r"\b(no|not|never|without|instead|rather|absent|missing|cannot|avoid|wrong|avoid|don't|does not)\b",
)
# The always-on slice is BASE_CORE plus this many playbook lines. The cap is deliberately one more
# than the number of seed rules: at 8 the answer-delivery rule was silently dropped in favour of a
# dataset summary that benthic_playbook already serves on demand. The token budget below is what
# actually bounds the slice, and it trims the dataset summaries first.
_MAX_CORE_LINES = 9


def known_identifiers(catalog: Catalog) -> set[str]:
    names = {name.lower() for definition in catalog.relations.values() for name in definition.columns}
    for definition in RPC_DEFINITIONS.values():
        names.update(definition.required_arguments)
        names.update(definition.optional_arguments)
    for join in catalog.joins:
        names.update({join.from_column.lower(), join.to_column.lower()})
    return names


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    for line in text.splitlines():
        parts.extend(piece.strip() for piece in _SENTENCE_BOUNDARY.split(line) if piece.strip())
    return parts


def _now() -> datetime:
    return datetime.now(UTC)


_MATCH_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "if",
        "in",
        "into",
        "instead",
        "is",
        "it",
        "its",
        "may",
        "more",
        "most",
        "not",
        "of",
        "on",
        "only",
        "or",
        "per",
        "rather",
        "should",
        "so",
        "such",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "use",
        "used",
        "using",
        "via",
        "was",
        "were",
        "what",
        "when",
        "which",
        "while",
        "with",
        "would",
        "you",
        "your",
    }
)


def content_words(text: str) -> set[str]:
    """Content words for lesson matching: no stop words, and snake_case split into parts.

    Splitting matters because the server names a signal `incomplete_source` while the model
    describes it as "incomplete", and treating those as unrelated tokens lost a real paraphrase.
    """
    tokens: set[str] = set()
    for raw in re.split(r"[^A-Za-z0-9_]+", text.lower()):
        if not raw:
            continue
        for part in raw.split("_") if "_" in raw else (raw,):
            if len(part) > 2 and part not in _MATCH_STOP_WORDS:
                tokens.add(part)
    return tokens


class RelationGuide(StrictModel):
    """Serialized semantic overlay for one signed relation. Column names are verified, not trusted."""

    terms: dict[str, int] = Field(default_factory=dict)
    preferred_columns: list[str] = Field(default_factory=list)
    description: str | None = None
    anti_patterns: list[str] = Field(default_factory=list)


class DatasetSection(StrictModel):
    summary: str | None = None
    when_to_use: list[str] = Field(default_factory=list)
    canonical_sources: list[str] = Field(default_factory=list)
    key_columns: list[PlaybookKeyColumn] = Field(default_factory=list)
    anti_patterns: list[str] = Field(default_factory=list)


class RpcRecipe(StrictModel):
    summary: str | None = None
    required_arguments: list[str] = Field(default_factory=list)
    optional_arguments: list[str] = Field(default_factory=list)


class LessonRecord(StrictModel):
    """One author-reported mistake and its correction, stored individually so that a
    regression can quarantine the offending lesson instead of the whole playbook."""

    lesson_id: str
    dataset: str | None = None
    relation: str | None = None
    symptom: str
    lesson: str
    confidence: Literal["high", "medium", "low"] = "medium"
    scope: Literal["all", "model"] = "model"
    author_model: str | None = None
    occurrences: int = 1
    status: Literal["pending", "active", "quarantined", "evicted"] = "pending"
    catalog_fingerprint: str = ""
    evidence: list[str] = Field(default_factory=list)
    question_ref: str = ""
    question_summary: str | None = None
    created_at: datetime = Field(default_factory=_now)
    last_seen: datetime = Field(default_factory=_now)


class Playbook(StrictModel):
    schema_version: int = SCHEMA_VERSION
    collection: str
    catalog_fingerprint: str | None = None
    generated_at: datetime | None = None
    generator: str = "unknown"
    baseline_fingerprint: str | None = None
    baseline_run_id: str | None = None
    core: list[str] = Field(default_factory=list)
    relations: dict[str, RelationGuide] = Field(default_factory=dict)
    datasets: dict[str, DatasetSection] = Field(default_factory=dict)
    rpc: dict[str, RpcRecipe] = Field(default_factory=dict)
    lessons: list[LessonRecord] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, payload: str) -> "Playbook":
        return cls.model_validate(json.loads(payload))


@dataclass(slots=True)
class VerifyReport:
    """What catalog verification dropped. A non-empty report is a signal, never a silent success."""

    dropped_relations: list[str] = field(default_factory=list)
    dropped_columns: list[str] = field(default_factory=list)
    dropped_datasets: list[str] = field(default_factory=list)
    dropped_sources: list[str] = field(default_factory=list)
    dropped_sentences: list[str] = field(default_factory=list)
    dropped_lessons: list[str] = field(default_factory=list)

    @property
    def total_dropped(self) -> int:
        return (
            len(self.dropped_relations)
            + len(self.dropped_columns)
            + len(self.dropped_datasets)
            + len(self.dropped_sources)
            + len(self.dropped_sentences)
            + len(self.dropped_lessons)
        )

    def notes(self) -> list[str]:
        messages: list[str] = []
        for label, items in (
            ("relation", self.dropped_relations),
            ("column", self.dropped_columns),
            ("dataset", self.dropped_datasets),
            ("source", self.dropped_sources),
            ("sentence", self.dropped_sentences),
            ("lesson", self.dropped_lessons),
        ):
            if items:
                messages.append(f"unverified {label} references removed: {', '.join(sorted(items)[:10])}")
        return messages

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "relations": self.dropped_relations,
            "columns": self.dropped_columns,
            "datasets": self.dropped_datasets,
            "sources": self.dropped_sources,
            "sentences": self.dropped_sentences,
            "lessons": self.dropped_lessons,
        }


def _token_is_known(token: str, catalog: Catalog, known_columns: set[str]) -> bool:
    candidate = token.strip().lower()
    if not candidate:
        return True
    if "." in candidate:
        dataset, _, relation = candidate.partition(".")
        return (dataset, relation) in catalog.relations
    return candidate in known_columns


def screen_prose(text: str, catalog: Catalog, known_columns: set[str], report: VerifyReport) -> list[str]:
    """Keep only sentences whose qualified, backticked, or join references exist in the signed catalog.

    Deliberately narrow: it inspects `dataset.relation` pairs, backticked identifiers, and
    sentences that assert a join. Free prose without such references passes through untouched,
    so ordinary guidance is not penalised for not being machine-checkable.
    """
    signed = catalog.signed_join_pairs()
    kept: list[str] = []
    for sentence in _sentences(text):
        stripped = sentence.strip()
        if not stripped:
            continue

        rejected = False
        for dataset, relation in _QUALIFIED.findall(stripped):
            if dataset in catalog.datasets and (dataset, relation) not in catalog.relations:
                report.dropped_sentences.append(stripped)
                rejected = True
                break
        if rejected:
            continue

        if not _NEGATION.search(stripped):
            for span in _BACKTICKED.findall(stripped):
                if _IDENTIFIER.match(span) and not _token_is_known(span, catalog, known_columns):
                    report.dropped_sentences.append(stripped)
                    rejected = True
                    break
        if rejected:
            continue

        if "join" in stripped.lower():
            # Unlike the identifier check, a join claim is screened even when negated: "do not join
            # X to Y on A" still asserts that the X/Y/A relationship exists.
            refs = [f"{dataset}.{relation}" for dataset, relation in _QUALIFIED.findall(stripped)]
            if len(refs) >= 2:
                # A relation may be backticked too, so exclude the qualified refs from the column list.
                named = set(refs)
                columns = [
                    span for span in _BACKTICKED.findall(stripped) if _IDENTIFIER.match(span) and span not in named
                ]
                signed_ok = len(columns) >= 2 and any(pair in signed for pair in _pairings(refs, columns))
                if not signed_ok:
                    report.dropped_sentences.append(stripped)
                    continue

        kept.append(stripped)
    return kept


def _pairings(refs: list[str], columns: list[str]) -> list[tuple[str, str, str, str]]:
    pairs: list[tuple[str, str, str, str]] = []
    for first, second in ((refs[0], refs[1]), (refs[1], refs[0])):
        for left_column, right_column in ((columns[0], columns[1]), (columns[1], columns[0])):
            pairs.append((first, left_column, second, right_column))
    return pairs


def verify(playbook: Playbook, catalog: Catalog) -> tuple[Playbook, VerifyReport]:
    """Return a copy of the playbook with every ungrounded reference removed."""
    report = VerifyReport()
    known_columns = known_identifiers(catalog)

    relations: dict[str, RelationGuide] = {}
    for source, guide in playbook.relations.items():
        definition = catalog.relations.get(tuple(source.split(".", 1)))  # type: ignore[arg-type]
        if definition is None:
            report.dropped_relations.append(source)
            continue
        columns = []
        for column in guide.preferred_columns:
            if column in definition.columns:
                columns.append(column)
            else:
                report.dropped_columns.append(f"{source}.{column}")
        relations[source] = guide.model_copy(
            update={
                "preferred_columns": columns,
                "anti_patterns": screen_prose("\n".join(guide.anti_patterns), catalog, known_columns, report),
            }
        )

    datasets: dict[str, DatasetSection] = {}
    for name, section in playbook.datasets.items():
        if name not in catalog.datasets:
            report.dropped_datasets.append(name)
            continue
        sources = []
        for source in section.canonical_sources:
            key = tuple(source.split(".", 1))  # type: ignore[assignment]
            if len(key) == 2 and key in catalog.relations:  # type: ignore[operator]
                sources.append(source)
            else:
                report.dropped_sources.append(source)
        key_columns = []
        for key_column in section.key_columns:
            key = tuple(key_column.relation.split(".", 1))
            if key in catalog.relations and key_column.column in catalog.relations[key].columns:  # type: ignore[index]
                key_columns.append(key_column)
            else:
                report.dropped_columns.append(f"{key_column.relation}.{key_column.column}")
        datasets[name] = section.model_copy(
            update={
                "canonical_sources": sources,
                "key_columns": key_columns,
                "when_to_use": screen_prose("\n".join(section.when_to_use), catalog, known_columns, report),
                "anti_patterns": screen_prose("\n".join(section.anti_patterns), catalog, known_columns, report),
            }
        )

    rpc = {name: recipe for name, recipe in playbook.rpc.items() if name in RPC_DEFINITIONS}
    for name in playbook.rpc:
        if name not in RPC_DEFINITIONS:
            report.dropped_datasets.append(f"rpc:{name}")

    lessons = []
    for record in playbook.lessons:
        if record.status != "active":
            continue
        relation = record.relation
        if relation is not None and tuple(relation.split(".", 1)) not in catalog.relations:  # type: ignore[arg-type]
            report.dropped_lessons.append(record.lesson_id)
            continue
        if not screen_prose(record.lesson, catalog, known_columns, report):
            report.dropped_lessons.append(record.lesson_id)
            continue
        lessons.append(record)

    core = screen_prose("\n".join(playbook.core), catalog, known_columns, report)[:_MAX_CORE_LINES]

    verified = playbook.model_copy(
        update={
            "core": core,
            "relations": relations,
            "datasets": datasets,
            "rpc": rpc,
            "lessons": lessons,
            "catalog_fingerprint": catalog.fingerprint(),
        }
    )
    return verified, report


def _estimated_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def render_core(playbook: Playbook | None, catalog: Catalog, token_budget: int) -> str:
    """The always-on slice injected into tool descriptions. Signed facts come from the catalog.

    Two caps apply and this one is the binding one. verify() screens the joined core into sentences
    and keeps _MAX_CORE_LINES of them; this function then keeps only the first
    _MAX_CORE_LINES - len(BASE_CORE), which is three. verify()'s cap is therefore a screen-time
    guard against a pathological document rather than the served size, and the served size is three
    items. Both are stated here because a seed rule that does not fit in three sentences is dropped
    from the guidance with no error raised, which is how a two-sentence answer-delivery rule lost
    everything after its first sentence.
    """
    lines = list(BASE_CORE)
    if playbook is not None:
        lines.extend(playbook.core[: _MAX_CORE_LINES - len(BASE_CORE)])
    for name, definition in catalog.datasets.items():
        section = playbook.datasets.get(name) if playbook is not None else None
        summary = (section.summary if section else None) or definition.description or definition.title
        if summary:
            lines.append(f"{name}: {summary}")
        elif definition.title:
            lines.append(f"{name}: {definition.title}")

    body = "\n".join(f"- {line}" for line in lines)
    while _estimated_tokens(body) > token_budget and len(lines) > 1:
        lines.pop()
        body = "\n".join(f"- {line}" for line in lines)
    return body


def render_instructions(core: str, catalog: Catalog) -> str:
    """Server `instructions` for spec-compliant clients, built from the same core slice."""
    operations = ", ".join(sorted(operation.value for operation in RPC_DEFINITIONS))
    return "\n".join(
        [
            "Use these tools as a read-only analyst over signed Benthic data.",
            "",
            "Workflow:",
            core,
            "",
            f"Signed RPC operations: {operations}.",
            "Report what the data shows, including source completeness and unsupported interpretations.",
        ]
    )


def build_result(
    playbook: Playbook | None,
    catalog: Catalog,
    status: str,
    dataset: str | None,
    warnings: list[str],
) -> PlaybookResult:
    """Combine catalog-signed facts with playbook prose into the served result."""
    if dataset is not None and dataset not in catalog.datasets:
        raise KeyError(dataset)

    names = [dataset] if dataset is not None else sorted(catalog.datasets)
    guides: list[PlaybookDatasetGuide] = []
    for name in names:
        definition = catalog.datasets[name]
        section = playbook.datasets.get(name) if playbook is not None else None
        relations = [
            relation
            for (relation_dataset, _), relation in sorted(catalog.relations.items())
            if relation_dataset == name
        ]
        canonical = section.canonical_sources if section is not None else []
        if not canonical:
            canonical = [
                qualified_relation_name(relation)
                for relation in relations
                if relation.queryable and relation.endpoint is not None and relation.description
            ][:4]
        guides.append(
            PlaybookDatasetGuide(
                dataset=name,
                title=definition.title,
                summary=(section.summary if section else None) or definition.description,
                when_to_use=section.when_to_use if section is not None else [],
                canonical_sources=canonical,
                key_columns=section.key_columns if section is not None else [],
                join_recipes=_join_recipes(catalog, name),
                rpc_recipes=_rpc_recipes(playbook, name),
                anti_patterns=_anti_patterns(catalog, playbook, name),
                lessons=_lessons(playbook, name),
            )
        )

    return PlaybookResult(
        collection=playbook.collection if playbook is not None else "",
        status=status,
        catalog_fingerprint=catalog.fingerprint(),
        generated_at=playbook.generated_at if playbook is not None else None,
        generator=playbook.generator if playbook is not None else None,
        core=render_core(playbook, catalog, 10_000).splitlines(),
        datasets=guides,
        collection_notes=catalog.collection_notes(),
        warnings=warnings,
    )


def _join_recipes(catalog: Catalog, dataset: str) -> list[PlaybookJoinRecipe]:
    recipes = []
    for join in catalog.joins:
        if dataset not in {join.from_dataset, join.to_dataset}:
            continue
        recipes.append(
            PlaybookJoinRecipe(
                left_source=f"{join.from_dataset}.{join.from_relation}",
                left_column=join.from_column,
                right_source=f"{join.to_dataset}.{join.to_relation}",
                right_column=join.to_column,
                join_type=join.join_type,
                reliability=join.reliability,
                notes=join.notes,
            )
        )
    return recipes


def _rpc_recipes(playbook: Playbook | None, dataset: str) -> list[PlaybookRpcRecipe]:
    recipes = []
    for name, definition in RPC_DEFINITIONS.items():
        if definition.dataset != dataset:
            continue
        override = playbook.rpc.get(name) if playbook is not None else None
        required = list(definition.required_arguments)
        optional = list(definition.optional_arguments)
        if override is not None:
            required = list(override.required_arguments or required)
            optional = list(override.optional_arguments or optional)
        recipes.append(
            PlaybookRpcRecipe(
                operation=name.value,
                dataset=dataset,
                required_arguments=required,
                optional_arguments=optional,
                summary=(override.summary if override else None) or definition.summary,
            )
        )
    return recipes


def _anti_patterns(catalog: Catalog, playbook: Playbook | None, dataset: str) -> list[str]:
    patterns: list[str] = []
    if playbook is not None:
        section = playbook.datasets.get(dataset)
        if section is not None:
            patterns.extend(section.anti_patterns)
    for (relation_dataset, relation_name), definition in sorted(catalog.relations.items()):
        if relation_dataset != dataset or not definition.queryable:
            continue
        if playbook is not None:
            hint = playbook.relations.get(f"{dataset}.{relation_name}")
            if hint is not None:
                patterns.extend(hint.anti_patterns)
        if not definition.queryable:
            patterns.append(f"{dataset}.{relation_name} is not queryable.")
    return patterns


def _lessons(playbook: Playbook | None, dataset: str) -> list[PlaybookLessonInfo]:
    """Lessons for one dataset.

    A lesson with no dataset applies everywhere, and is included for every dataset. An exact
    `record.dataset == dataset` match would silently drop every dataset-agnostic lesson, which is
    most of them, because the reflector is told to leave the scope null for method-level mistakes.
    """
    if playbook is None:
        return []
    return [
        PlaybookLessonInfo(
            symptom=record.symptom,
            lesson=record.lesson,
            relation=record.relation,
            confidence=record.confidence,
            scope=record.scope,
            author_model=record.author_model,
            occurrences=record.occurrences,
        )
        for record in playbook.lessons
        if record.status == "active" and record.dataset in {None, dataset}
    ]


def relation_hints(playbook: Playbook | None) -> dict[tuple[str, str], RelationHint]:
    """Discovery ranking hints, keyed the way Catalog indexes relations."""
    if playbook is None:
        return {}
    hints: dict[tuple[str, str], RelationHint] = {}
    for source, guide in playbook.relations.items():
        dataset, _, relation = source.partition(".")
        if dataset and relation:
            hints[(dataset, relation)] = RelationHint(
                terms=dict(guide.terms),
                preferred_columns=list(guide.preferred_columns),
                description=guide.description,
                anti_patterns=list(guide.anti_patterns),
            )
    return hints


def load_playbook(path: Path) -> tuple[Playbook | None, str]:
    """Load a playbook document. Returns the playbook and a status: active, seed, or unavailable."""
    if not path.is_file():
        return None, "unavailable"
    try:
        return Playbook.from_json(path.read_text(encoding="utf-8")), "active"
    except (OSError, ValueError):
        return None, "unavailable"


def staleness(playbook: Playbook, catalog: Catalog) -> str | None:
    if not playbook.catalog_fingerprint:
        return None
    if playbook.catalog_fingerprint == catalog.fingerprint():
        return None
    return (
        "The signed BDP catalog changed after this playbook was generated. Dataset detail may be "
        "out of date; re-run eval/consolidate.py and the A/B gate before relying on it."
    )


def lesson_id() -> str:
    return uuid.uuid4().hex[:16]


def similar(a: str, b: str) -> float:
    """Containment of the shorter symptom in the longer one, not Jaccard.

    Lessons are paraphrases of each other: the same mistake gets described in a different number of
    words each time, so Jaccard punishes the longer description for having more detail. On the
    first observed round the two genuinely-identical lesson pairs scored 0.37 and 0.71 by
    containment while Jaccard scored 0.17 and 0.41, and the unrelated pairs stayed at or below 0.25.
    Seven pairs is a small sample, so the lesson cap still bounds the damage when this misfires.
    """
    left, right = content_words(a), content_words(b)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def lesson_is_grounded(record: LessonRecord, catalog: Catalog) -> bool:
    report = VerifyReport()
    known = known_identifiers(catalog)
    return bool(screen_prose(record.lesson, catalog, known, report))


def prune_lessons(lessons: list[LessonRecord], max_age_days: int) -> tuple[list[LessonRecord], int]:
    cutoff = _now() - timedelta(days=max_age_days)
    kept = [record for record in lessons if record.last_seen >= cutoff and record.occurrences > 0]
    return kept, len(lessons) - len(kept)


def playbook_paths(cache_dir: Path) -> dict[str, Path]:
    return {
        "active": cache_dir / "playbook.json",
        "candidate": cache_dir / "playbook-candidate.json",
        "lessons": cache_dir / "lessons",
    }
