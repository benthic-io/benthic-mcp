from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from benthic_mcp.bdp import CatalogSnapshot
from benthic_mcp.catalog import Catalog
from benthic_mcp.config import Settings
from benthic_mcp.playbook import relation_hints
from benthic_mcp.seed import seed_playbook
from tests.factories import make_collection, make_manifest, public_key_base64, relation


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def settings(tmp_path: Path, private_key: Ed25519PrivateKey) -> Settings:
    return Settings(
        collections=("ngopen",),
        trusted_keys=(public_key_base64(private_key),),
        cache_dir=tmp_path / "cache",
        cache_ttl_seconds=900,
        max_cache_age_seconds=604800,
        request_timeout_seconds=5.0,
        max_rows=1000,
        default_query_limit=100,
        max_response_bytes=1_048_576,
    )


@pytest.fixture
def manifests(private_key: Ed25519PrivateKey) -> dict[str, dict[str, Any]]:
    return {
        "usaspending": make_manifest(
            private_key,
            "usaspending",
            [
                relation(
                    "state_data", [("code", "string"), ("name", "string"), ("type", "string"), ("fips", "string")]
                ),
                relation(
                    "overall_totals",
                    [("fiscal_year", "integer"), ("total_budget_authority", "number")],
                ),
                relation(
                    "vw_published_dabs_toptier_agency",
                    [("toptier_code", "string"), ("name", "string"), ("abbreviation", "string")],
                ),
                relation(
                    "all_entities",
                    [
                        ("uei", "string"),
                        ("duns", "string"),
                        ("legal_business_name", "string"),
                        ("name", "string"),
                        ("state", "string"),
                        ("congressional_district", "string"),
                        ("total_obligation", "number"),
                        ("date_last_award", "date"),
                    ],
                    description="Federal award recipients",
                ),
                relation(
                    "prime_awards",
                    [
                        ("recipient_uei", "string"),
                        ("recipient_name", "string"),
                        ("award_amount", "number"),
                        ("action_date", "date"),
                        ("recipient_state", "string"),
                        ("recipient_congressional_district", "string"),
                        ("pop_congressional_district", "string"),
                    ],
                    description="Federal award transactions",
                ),
                relation(
                    "subawards",
                    [("subaward_id", "string"), ("recipient_name", "string"), ("subaward_amount", "number")],
                ),
                # Already aggregated upstream in the signed manifest. The seed guidance points a
                # refused `group_by` here rather than leaving the caller stuck, so the fixture needs
                # the real column set - the verifier strips guidance naming a column the catalog
                # does not carry, which is how a wrong preferred_columns list gets caught.
                relation(
                    "mv_district_spending",
                    [
                        ("state", "string"),
                        ("district", "string"),
                        ("fiscal_year", "integer"),
                        ("award_count", "bigint"),
                        ("total_obligation", "number"),
                    ],
                ),
                relation("lineage", [("id", "integer")], queryable=False),
            ],
            "https://benthic.io/ngopen/usaspending/",
        ),
        "samer": make_manifest(
            private_key,
            "samer",
            [relation("sam_registrations", [("uei", "string"), ("name", "string"), ("duns", "string")])],
            "https://benthic.io/ngopen/samer/",
        ),
        "irs_ng": make_manifest(
            private_key,
            "irs_ng",
            [
                relation(
                    "bmf_organizations",
                    [
                        ("ein", "string"),
                        ("org_name_current", "string"),
                        ("f990_org_addr_city", "string"),
                        ("f990_org_addr_state", "string"),
                        ("f990_org_addr_zip", "string"),
                        ("latitude", "number"),
                        ("longitude", "number"),
                    ],
                ),
                relation(
                    "form990_details",
                    [("ein", "string"), ("tax_period", "string"), ("total_revenue", "number")],
                ),
            ],
            "https://benthic.io/ngopen/irs_ng/",
        ),
        "up_cdmaps": make_manifest(
            private_key,
            "up_cdmaps",
            [relation("congressional_districts", [("district_id", "string"), ("district", "string")])],
            "https://benthic.io/ngopen/up_cdmaps/",
        ),
        "usp_cl": make_manifest(
            private_key,
            "usp_cl",
            [
                # A person registry keyed by bioguide_id, with no `name`, `state` or `district`
                # column. The seed guidance names exactly those absences, and the verifier strips
                # guidance for relations the catalog does not carry, so the fixture has to model it.
                relation(
                    "legislators",
                    [
                        ("bioguide_id", "string"),
                        ("official_full", "string"),
                        ("first_name", "string"),
                        ("last_name", "string"),
                        ("is_current", "boolean"),
                        ("first_term_start", "date"),
                        ("last_term_end", "date"),
                    ],
                ),
                relation(
                    "legislator_terms",
                    [
                        ("bioguide_id", "string"),
                        ("state", "string"),
                        ("district", "string"),
                        ("term_start", "date"),
                        ("term_end", "date"),
                        ("party", "string"),
                        ("url", "string"),
                    ],
                    description="Historical legislator terms",
                ),
                relation(
                    "executive_terms",
                    [
                        ("bioguide_id", "string"),
                        ("term_start", "date"),
                        ("term_end", "date"),
                        ("office", "string"),
                    ],
                    description="Executive terms",
                ),
                relation(
                    "mv_current_lawmakers",
                    [
                        ("bioguide_id", "string"),
                        ("official_full", "string"),
                        ("state", "string"),
                        ("district", "string"),
                        ("term_start", "date"),
                        ("term_end", "date"),
                        ("party", "string"),
                    ],
                    description="Current lawmakers only",
                ),
            ],
            "https://benthic.io/ngopen/usp_cl/",
        ),
    }


@pytest.fixture
def collection(private_key: Ed25519PrivateKey, manifests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return make_collection(
        private_key,
        manifests,
        [
            {
                "from": {"dataset_name": "usaspending", "relation": "all_entities", "column": "uei"},
                "to": {"dataset_name": "samer", "relation": "sam_registrations", "column": "uei"},
                "join_type": "identifier",
                "reliability": "reliable",
            },
            {
                "from": {"dataset_name": "samer", "relation": "sam_registrations", "column": "duns"},
                "to": {"dataset_name": "irs_ng", "relation": "bmf_organizations", "column": "ein"},
                "join_type": "heuristic",
                "reliability": "heuristic",
                "notes": "Identifier systems differ.",
            },
            {
                "from": {"dataset_name": "usaspending", "relation": "all_entities", "column": "congressional_district"},
                "to": {"dataset_name": "usp_cl", "relation": "legislator_terms", "column": "district"},
                "join_type": "identifier",
                "reliability": "partial",
                "notes": "Must pair with state; bracket action_date by congress_start/end",
            },
            {
                # Same dataset, unlike every other edge here, and reliable rather than
                # partial: bioguide_id is an identifier and every term resolves to a
                # legislator. The combination suite found the model asking for this twice
                # with no path in the collection. Whether the catalog agrees is the
                # pipeline's call, and `test_a_signed_hop_is_reachable_from_both_ends`
                # is the contract that says so either way.
                "from": {"dataset_name": "usp_cl", "relation": "legislators", "column": "bioguide_id"},
                "to": {"dataset_name": "usp_cl", "relation": "legislator_terms", "column": "bioguide_id"},
                "join_type": "identifier",
                "reliability": "reliable",
                "notes": "Total referential integrity: every term resolves to a legislator.",
            },
        ],
    )


@pytest.fixture
def catalog(manifests: dict[str, dict[str, Any]], collection: dict[str, Any]) -> Catalog:
    # Production builds the catalog with the playbook's hints, so the fixture does too.
    return Catalog(
        CatalogSnapshot(collections={"ngopen": collection}, manifests=manifests),
        relation_hints=relation_hints(seed_playbook()),
    )


@pytest.fixture
def bdp_documents(
    manifests: dict[str, dict[str, Any]],
    collection: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    documents = {
        "https://benthic.io/bdp/v1/collection.schema.json": {"type": "object"},
        "https://benthic.io/bdp/v1/manifest.schema.json": {"type": "object"},
        "https://benthic.io/bdp/ngopen/collection.json": collection,
    }
    for dataset, manifest in manifests.items():
        documents[f"https://benthic.io/bdp/ngopen/{dataset}/manifest.json"] = manifest
    return documents


@pytest.fixture
def bdp_client_factory(
    bdp_documents: dict[str, dict[str, Any]],
) -> Callable[[dict[str, Any] | None], httpx.AsyncClient]:
    def factory(overrides: dict[str, Any] | None = None) -> httpx.AsyncClient:
        documents = {**bdp_documents, **(overrides or {})}

        def handler(request: httpx.Request) -> httpx.Response:
            document = documents.get(str(request.url))
            if document is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=document)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory
