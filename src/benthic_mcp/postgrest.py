import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

import httpx

from benthic_mcp.catalog import ColumnDefinition, RelationDefinition
from benthic_mcp.config import Settings
from benthic_mcp.errors import BenthicMCPError, QueryValidationError, UpstreamError
from benthic_mcp.models import FilterOperator, FilterSpec, RelationSource, SourceOrder

# A scan that turns out to be over the cap has already paid for the whole page budget by the time
# anyone knows, and the caller needs the number that put it over either way. PostgREST returns the
# count in `Content-Range` for a HEAD, so the same answer costs one request and no row bytes.
_COUNT_HEADERS = {"Prefer": "count=exact", "Range": "0-0"}


@dataclass(frozen=True, slots=True)
class MatchCount:
    total: int
    request_url: str


@dataclass(frozen=True, slots=True)
class FetchedSource:
    source: RelationSource
    definition: RelationDefinition
    rows: list[dict[str, Any]]
    request_url: str
    truncated: bool
    # Rows matching the filters, when the count request answered. None means it did not, which is
    # not an error: it only means the caller cannot be told how far over the cap it is.
    matched_rows: int | None = None


class PostgrestTransport:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def fetch(self, source: RelationSource, definition: RelationDefinition) -> FetchedSource:
        limit = min(source.limit or self.settings.default_query_limit, self.settings.max_rows)
        page = await self._fetch_page(source, definition, limit, source.offset)
        if not page.truncated:
            # Nothing was cut off, so the rows in hand are the whole answer and the caller has no
            # reason to ask how big it is.
            return page
        # `truncated` on its own says only that a page ended. A caller cannot tell 100 rows out of
        # 100 from 100 out of 1,416,153, so it cannot judge whether another call would finish or
        # whether it already holds the answer. One HEAD, no row bytes, and the size is known.
        count = await self.count_matching(source, definition)
        if count is None:
            return page
        return FetchedSource(source, definition, page.rows, page.request_url, True, count.total)

    async def fetch_complete(
        self,
        source: RelationSource,
        definition: RelationDefinition,
    ) -> FetchedSource:
        rows: list[dict[str, Any]] = []
        offset = source.offset
        remaining = self.settings.aggregate_scan_limit
        request_url = ""
        scan_source = source.model_copy(
            update={
                "order": [SourceOrder(column=column) for column in definition.primary_key]
                if definition.primary_key
                else source.order
            }
        )
        stable_order = bool(scan_source.order)

        count = await self.count_matching(scan_source, definition)
        if count is not None and count.total - offset > remaining:
            return FetchedSource(source, definition, [], count.request_url, True, count.total)
        matched = count.total if count is not None else None

        while remaining > 0:
            limit = min(self.settings.max_rows, remaining)
            page = await self._fetch_page(scan_source, definition, limit, offset, sentinel=False)
            request_url = page.request_url
            rows.extend(page.rows)
            offset += len(page.rows)
            remaining -= len(page.rows)
            if len(page.rows) < limit:
                return FetchedSource(source, definition, rows, request_url, False, matched)
            if not stable_order:
                # The page filled, so a second one is due, and `offset` only means anything against
                # a total order. 63 of the 99 queryable relations declare no primary key and
                # `source.order` is never set by the query builder, so these requests carry no
                # ORDER BY at all and PostgREST guarantees nothing about row order between them: a
                # row can come back twice or be skipped outright. The aggregate would then be
                # quietly wrong on the one path whose entire purpose is to be exact.
                #
                # A single page is unaffected, because no offset is used. Refusing only once paging
                # is genuinely required keeps small relations and narrow filters working.
                raise BenthicMCPError(
                    f"{definition.dataset}.{definition.name} declares no primary key, so its rows cannot be "
                    f"paged deterministically and an aggregate over them would be unreliable. "
                    f"Narrow the filter so the result fits in one page of {self.settings.max_rows} rows, "
                    f"or aggregate over a relation that declares a primary key."
                )

        probe = await self._fetch_page(scan_source, definition, 1, offset, sentinel=False)
        return FetchedSource(source, definition, rows, probe.request_url or request_url, bool(probe.rows), matched)

    async def count_matching(self, source: RelationSource, definition: RelationDefinition) -> MatchCount | None:
        """How many rows match the source's filters, or None when that cannot be established.

        The filters and select are the ones the scan itself would send, so the count describes the
        same rows the scan would page. It is an optimisation, never a dependency: any failure here
        leaves the caller paging exactly as before, because a count that cannot be trusted is worth
        less than the query that does not need one.

        The order is dropped. It exists so `offset` means something across pages, and a HEAD fetches
        no rows and pages nothing, so it cannot change how many rows match - it can only make the
        question slower. On usaspending.all_entities (17.9M rows) the unfiltered count took 2.1s
        with the order and 26.1s without it.
        """
        params = self._build_params(source.model_copy(update={"order": []}), definition, None, 0)
        url = self._relation_url(definition)
        self._validate_endpoint(url)
        query = httpx.QueryParams([(key, str(value)) for key, value in params])

        try:
            async with self.client.stream("HEAD", url, params=query, headers=_COUNT_HEADERS) as response:
                if response.status_code >= 400:
                    return None
                content_range = response.headers.get("content-range", "")
        except httpx.HTTPError:
            return None

        # `0-<last row>/<total>`, or `*/<total>` once the range is past the end of the result.
        total = re.search(r"/(\d+)$", content_range)
        if total is None:
            return None
        return MatchCount(int(total.group(1)), str(httpx.URL(url, params=query)))

    async def _fetch_page(
        self,
        source: RelationSource,
        definition: RelationDefinition,
        limit: int,
        offset: int,
        *,
        sentinel: bool = True,
    ) -> FetchedSource:
        params = self._build_params(source, definition, limit, offset, sentinel=sentinel)
        url = self._relation_url(definition)
        self._validate_endpoint(url)
        query = httpx.QueryParams([(key, str(value)) for key, value in params])

        try:
            async with self.client.stream("GET", url, params=query) as response:
                if response.status_code >= 400:
                    body = (await response.aread())[:4096].decode("utf-8", errors="replace")
                    raise UpstreamError(
                        f"Benthic API returned {response.status_code} for "
                        f"{definition.dataset}.{definition.name}: {body}"
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.settings.max_response_bytes:
                        raise UpstreamError(
                            f"Benthic API response exceeded {self.settings.max_response_bytes} bytes for "
                            f"{definition.dataset}.{definition.name}"
                        )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Benthic API request failed for {definition.dataset}.{definition.name}: {exc}"
            ) from exc

        try:
            rows = json.loads(content)
        except json.JSONDecodeError as exc:
            raise UpstreamError(
                f"Benthic API returned invalid JSON for {definition.dataset}.{definition.name}"
            ) from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise UpstreamError(f"Benthic API returned an unexpected result for {definition.dataset}.{definition.name}")

        truncated = len(rows) > limit
        return FetchedSource(
            source=source,
            definition=definition,
            rows=rows[:limit],
            request_url=str(httpx.URL(url, params=query)),
            truncated=truncated,
        )

    def _build_params(
        self,
        source: RelationSource,
        definition: RelationDefinition,
        limit: int | None,
        offset: int,
        *,
        sentinel: bool = True,
    ) -> list[tuple[str, str | int]]:
        if source.select:
            self._validate_columns(definition, source.select)
        filter_columns = [item.column for item in source.filters]
        order_columns = [item.column for item in source.order]
        self._validate_columns(definition, [*source.select, *filter_columns, *order_columns])

        params: list[tuple[str, str | int]] = []
        if source.select:
            params.append(("select", ",".join(source.select)))
        if source.order:
            params.append(("order", ",".join(self._format_order(item) for item in source.order)))
        grouped_filters: dict[str, list[FilterSpec]] = {}
        for item in source.filters:
            grouped_filters.setdefault(item.column, []).append(item)
        for column, filters in grouped_filters.items():
            if len(filters) == 1:
                params.append(
                    (
                        column,
                        self._format_filter(filters[0], definition.columns[column], inner=False),
                    )
                )
                continue
            expressions = ",".join(
                self._format_filter(item, definition.columns[column], inner=True) for item in filters
            )
            params.append(("and", f"({expressions})"))
        # A count asks for no rows at all, so it carries no limit: PostgREST reads `limit=0` as
        # unset, and the 0-row range on the request is what actually bounds it.
        if limit is not None:
            params.append(("limit", limit + 1 if sentinel else limit))
        if offset:
            params.append(("offset", offset))
        return params

    @staticmethod
    def _validate_columns(definition: RelationDefinition, columns: list[str]) -> None:
        unknown = sorted({column for column in columns if column not in definition.columns})
        if unknown:
            raise QueryValidationError(
                f"Unknown columns for {definition.dataset}.{definition.name}: {', '.join(unknown)}"
            )

    @staticmethod
    def _format_order(order: SourceOrder) -> str:
        value = f"{order.column}.desc" if order.descending else order.column
        if order.nulls_first is not None:
            nulls = "nullsfirst" if order.nulls_first else "nullslast"
            value += f".{nulls}"
        return value

    @classmethod
    def _format_filter(cls, item: FilterSpec, column: ColumnDefinition, *, inner: bool) -> str:
        if item.operator == FilterOperator.IS_NULL:
            expression = f"{item.column}.is.null"
        elif item.operator == FilterOperator.NOT_IS_NULL:
            expression = f"{item.column}.not.is.null"
        elif item.operator == FilterOperator.IN:
            values = [cls._coerce_value(value, column) for value in item.value]
            expression = f"{item.column}.in.{cls._format_in_value(values)}"
        else:
            value = cls._coerce_value(item.value, column)
            cls._validate_value(value, column)
            expression = f"{item.column}.{item.operator.value}.{cls._format_scalar(value)}"
        if inner:
            return expression
        prefix = f"{item.column}."
        if not expression.startswith(prefix):
            raise QueryValidationError(f"Invalid filter expression for {item.column}")
        return expression[len(prefix) :]

    @classmethod
    def _format_in_value(cls, value: Any) -> str:
        if not isinstance(value, list) or not value:
            raise QueryValidationError("in requires a non-empty list")
        return "(" + ",".join(cls._format_scalar(item) for item in value) + ")"

    @staticmethod
    def _format_scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            raise QueryValidationError("null requires the is.null or not.is.null operator")
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        text = str(value)
        if re.fullmatch(r"[A-Za-z0-9_.:%/-]+", text):
            return text
        return json.dumps(text, ensure_ascii=True)

    @staticmethod
    def _coerce_value(value: Any, column: ColumnDefinition) -> Any:
        if column.type == "integer" and isinstance(value, str) and value.isdigit():
            return int(value)
        if column.type == "number" and isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return value
        return value

    @staticmethod
    def _validate_value(value: Any, column: ColumnDefinition) -> None:
        valid = True
        if column.type in {"integer"}:
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif column.type == "number":
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif column.type == "boolean":
            valid = isinstance(value, bool)
        elif column.type == "date":
            try:
                date.fromisoformat(str(value))
            except ValueError:
                valid = False
        elif column.type == "timestamp":
            try:
                datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                valid = False
        elif column.type == "uuid":
            try:
                UUID(str(value))
            except ValueError:
                valid = False
        if not valid:
            raise QueryValidationError(f"Value {value!r} does not match column type {column.type}")

    @staticmethod
    def _relation_url(definition: RelationDefinition) -> str:
        endpoint = definition.endpoint
        if endpoint is None:
            raise QueryValidationError(f"Relation {definition.dataset}.{definition.name} has no endpoint")
        return f"{endpoint}{definition.name}"

    @staticmethod
    def _validate_endpoint(url: str) -> None:
        parsed = httpx.URL(url)
        host = parsed.host.lower()
        if parsed.scheme != "https" or not (host == "benthic.io" or host.endswith(".benthic.io")):
            raise QueryValidationError("Signed manifest endpoint must use HTTPS on benthic.io")
