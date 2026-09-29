"""Human-curated seed playbook.

Everything dataset-specific that used to live in `catalog._RELATION_HINTS` and the
RPC argument wiring now starts here, so the consolidator begins from reviewed content
instead of a blank page. The server falls back to this document when no promoted
playbook is present, which is what makes Phase 5 (deleting the hard-coded tables)
safe to do.
"""

from benthic_mcp.playbook import DatasetSection, Playbook, RelationGuide, RpcRecipe

# Retained verbatim from the previous _RELATION_HINTS weights so discovery ranking is unchanged.
SEED_RELATION_HINTS: dict[str, RelationGuide] = {
    "usaspending.prime_awards": RelationGuide(
        terms={"money": 100, "funding": 90, "award": 50, "recipient": 50, "organization": 20},
        preferred_columns=[
            "recipient_uei",
            "recipient_name",
            "award_amount",
            "action_date",
            "recipient_state",
            "recipient_congressional_district",
            "pop_congressional_district",
        ],
        description="Federal awards; recipient and place-of-performance districts are distinct columns.",
    ),
    "usaspending.all_entities": RelationGuide(
        terms={"organization": 40, "entity": 70, "recipient": 45, "district": 50, "representative": 20},
        preferred_columns=[
            "uei",
            "legal_business_name",
            "total_obligation",
            "congressional_district",
            "state",
            "date_last_award",
        ],
    ),
    "usp_cl.legislator_terms": RelationGuide(
        terms={"representative": 90, "congress": 70, "legislator": 90, "historical": 90, "term": 60},
        preferred_columns=["bioguide_id", "state", "district", "term_start", "term_end", "party", "url"],
        description="Historical legislator terms; query term_start and term_end to avoid current-office mistakes.",
    ),
    "usp_cl.mv_current_lawmakers": RelationGuide(
        terms={"representative": 35, "current": 80, "lawmaker": 70},
        preferred_columns=[
            "bioguide_id",
            "official_full",
            "state",
            "district",
            "term_start",
            "term_end",
            "party",
        ],
        description="Current lawmakers only. Do not use for historical officeholder questions.",
    ),
    "irs_ng.bmf_organizations": RelationGuide(
        terms={"nonprofit": 100, "organization": 60, "irs": 50, "exempt": 70},
        preferred_columns=[
            "ein",
            "org_name_current",
            "f990_org_addr_city",
            "f990_org_addr_state",
            "f990_org_addr_zip",
            "latitude",
            "longitude",
        ],
        description="IRS exempt organizations. `org_name_current` is the current legal name; "
        "`is_current` marks the active BMF record.",
    ),
}

SEED_DATASETS: dict[str, DatasetSection] = {
    "usp_cl": DatasetSection(
        summary="Congressional legislators and executive terms, including historical terms.",
        when_to_use=[
            "Use `usp_cl.legislator_terms` for any question about who held an office at a past date.",
            "Use `usp_cl.mv_current_lawmakers` only when the question is about the current member.",
        ],
        anti_patterns=[
            "`usp_cl.mv_current_lawmakers` returns the sitting member only, so it cannot answer "
            "historical or timeline questions.",
            "A district number alone is ambiguous without the state; always narrow by state as well.",
        ],
    ),
    "usaspending": DatasetSection(
        summary="Federal award transactions and the recipient entity index.",
        when_to_use=[
            "Use `usaspending.prime_awards` for individual award transactions and amounts.",
            "Use `usaspending.all_entities` to look up one organization by UEI before joining to awards.",
        ],
        anti_patterns=[
            "Recipient district and place-of-performance district are different columns; using the wrong "
            "one silently changes the geography.",
        ],
    ),
    "irs_ng": DatasetSection(
        summary="IRS exempt organization records, including Form 990 details.",
        when_to_use=[
            "Use `irs_ng.bmf_organizations` to look up a nonprofit by EIN.",
            "Use `irs_ng.form990_details` for revenue, expenses, and grants, filtered by `tax_period`.",
        ],
        anti_patterns=[
            "The address columns are prefixed `f990_org_addr_`; there is no plain `org_name` or `city` column.",
        ],
    ),
    "samer": DatasetSection(
        summary="SAM entity registrations, the bridge between USAspending and IRS identifiers.",
        when_to_use=[
            "Use `samer.sam_registrations` to translate between UEI and DUNS identifiers.",
        ],
        anti_patterns=[
            "DUNS to EIN is a heuristic identifier change, not an exact identity; say so when reporting.",
        ],
    ),
    "up_cdmaps": DatasetSection(
        summary="Congressional district boundaries, queried through spatial RPCs.",
        when_to_use=[
            "Use the find_district or districts_in_bbox operations instead of joining geometry yourself.",
        ],
        anti_patterns=[
            "Districts are returned per Congress; pass `congress` when the question is about a past election.",
        ],
    ),
}

SEED_RPC: dict[str, RpcRecipe] = {
    "find_district": RpcRecipe(
        summary="Congressional district containing a point.",
        required_arguments=["lat", "lon"],
        optional_arguments=["congress"],
    ),
    "districts_in_bbox": RpcRecipe(
        summary="Districts intersecting a bounding box.",
        required_arguments=["min_lat", "max_lat", "min_lon", "max_lon"],
        optional_arguments=["congress"],
    ),
    "nonprofits_nearby": RpcRecipe(
        summary="Nonprofit organizations near a point.",
        required_arguments=["lat", "lon"],
        optional_arguments=["radius_meters"],
    ),
}

SEED_CORE: tuple[str, ...] = (
    "Dates in this catalog are ISO-8601; filter with gte/lte rather than string comparison.",
    "Never use a current-only view to answer a historical question.",
    # The dominant failure in the traces was not a wrong answer but no answer: the model fetched
    # everything it needed and spent its last turn on another call, scoring zero on correct work.
    # Kept to a single sentence because the core slice is capped by screened sentence count.
    "You have a limited number of turns, so once you have the rows you need you must stop calling "
    "tools and write the final answer, because a question left unanswered scores zero even when "
    "every call succeeded.",
    # This line was removed and restored, and the reason is kept here because the removal was the
    # more expensive mistake. It was measured at 23/50 in both arms and called a null, but
    # attribute_suite keyed its result by case rather than by case and repetition, so half the data
    # was discarded: 44/50 without the rule against 46/50 with it, which crosses min_delta and
    # reads "fixes". A hand-written rule is not evidence, and neither is a measurement made by an
    # instrument that throws away the repetitions it was paid to gather.
)


def seed_playbook(collection: str = "ngopen") -> Playbook:
    # BASE_CORE is the tool contract and is rendered separately, so it is not repeated here.
    return Playbook(
        collection=collection,
        generator="human-curated seed",
        core=list(SEED_CORE),
        relations=SEED_RELATION_HINTS,
        datasets=SEED_DATASETS,
        rpc=SEED_RPC,
    )
