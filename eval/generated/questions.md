# Benthic MCP evaluation questions

Generated cases: 33
Signed join paths: 6
RPC operations: districts_in_bbox, find_district, nonprofits_nearby

## discover_irs_ng_bmf_organization_snapshots
Capability: `discovery`

Use the signed catalog to identify the relevant relation and fields in irs_ng.bmf_organization_snapshots for a small sample of id, ein, release_date.

## discover_irs_ng_bmf_organizations
Capability: `discovery`

Use the signed catalog to identify the relevant relation and fields in irs_ng.bmf_organizations for a small sample of id, ein, ein2.

## discover_samer_mv_contractor_registry
Capability: `discovery`

Use the signed catalog to identify the relevant relation and fields in samer.mv_contractor_registry for a small sample of uei, entity_id, duns.

## discover_samer_sam_registrations
Capability: `discovery`

Use the signed catalog to identify the relevant relation and fields in samer.sam_registrations for a small sample of id, uei, entity_id.

## discover_up_cdmaps_congressional_districts
Capability: `discovery`

Use the signed catalog to identify the relevant relation and fields in up_cdmaps.congressional_districts for a small sample of id, congress_number, statename.

## discover_usaspending_agency
Capability: `discovery`

Use the signed catalog to identify the relevant relation and fields in usaspending.agency for a small sample of id, create_date, update_date.

## discover_usaspending_agency_by_subtier_and_optionally_toptier
Capability: `discovery`

Use the signed catalog to identify the relevant relation and fields in usaspending.agency_by_subtier_and_optionally_toptier for a small sample of subtier_code, cgac_code, id.

## discover_usp_cl_committee_membership
Capability: `discovery`

Use the signed catalog to identify the relevant relation and fields in usp_cl.committee_membership for a small sample of membership_id, committee_thomas_id, bioguide_id.

## join_samer_sam_registrations_irs_ng_bmf_organizations_evidence
Capability: `heuristic_heuristic_join_evidence`

Use the signed join path to find the relationship for samer.sam_registrations value '7S5S4' in irs_ng.bmf_organizations, explain the match evidence, and report whether it is reliable, partial, or heuristic. Treat these as heuristic matches and clearly label them as non-exact. Retrieve a bounded context row first, then call benthic_join. Call benthic_join with left_source='samer.sam_registrations', right_source='irs_ng.bmf_organizations', left_column='duns', right_column='ein', left_where=["duns=eq.7S5S4"].

## join_samer_sam_registrations_irs_ng_bmf_organizations_match
Capability: `heuristic_heuristic_join`

Use the signed join path to check the samer.sam_registrations record with duns equal to '7S5S4' against irs_ng.bmf_organizations on ein. Treat these as heuristic matches and clearly label them as non-exact. Retrieve a bounded context row first, then call benthic_join. Call benthic_join with left_source='samer.sam_registrations', right_source='irs_ng.bmf_organizations', left_column='duns', right_column='ein', left_where=["duns=eq.7S5S4"].

## join_usaspending_all_entities_irs_ng_bmf_organizations_evidence
Capability: `heuristic_heuristic_join_evidence`

Use the signed join path to find the relationship for usaspending.all_entities value '142362594' in irs_ng.bmf_organizations, explain the match evidence, and report whether it is reliable, partial, or heuristic. Treat these as heuristic matches and clearly label them as non-exact. Retrieve a bounded context row first, then call benthic_join. Call benthic_join with left_source='usaspending.all_entities', right_source='irs_ng.bmf_organizations', left_column='duns', right_column='ein', left_where=["duns=eq.142362594"].

## join_usaspending_all_entities_irs_ng_bmf_organizations_match
Capability: `heuristic_heuristic_join`

Use the signed join path to check the usaspending.all_entities record with duns equal to '142362594' against irs_ng.bmf_organizations on ein. Treat these as heuristic matches and clearly label them as non-exact. Retrieve a bounded context row first, then call benthic_join. Call benthic_join with left_source='usaspending.all_entities', right_source='irs_ng.bmf_organizations', left_column='duns', right_column='ein', left_where=["duns=eq.142362594"].

## join_usaspending_all_entities_samer_sam_registrations_evidence
Capability: `identifier_reliable_join_evidence`

Use the signed join path to find the relationship for usaspending.all_entities value 'ESELKUJSAM45' in samer.sam_registrations, explain the match evidence, and report whether it is reliable, partial, or heuristic. Include the signed join evidence and state its reliability. Retrieve a bounded context row first, then call benthic_join. Call benthic_join with left_source='usaspending.all_entities', right_source='samer.sam_registrations', left_column='uei', right_column='uei', left_where=["uei=eq.ESELKUJSAM45"].

## join_usaspending_all_entities_samer_sam_registrations_match
Capability: `identifier_reliable_join`

Use the signed join path to check the usaspending.all_entities record with uei equal to 'ESELKUJSAM45' against samer.sam_registrations on uei. Include the signed join evidence and state its reliability. Retrieve a bounded context row first, then call benthic_join. Call benthic_join with left_source='usaspending.all_entities', right_source='samer.sam_registrations', left_column='uei', right_column='uei', left_where=["uei=eq.ESELKUJSAM45"].

## join_usaspending_all_entities_usp_cl_legislator_terms_evidence
Capability: `identifier_partial_join_evidence`

Use the signed join path to find the relationship for usaspending.all_entities value '03' in usp_cl.legislator_terms, explain the match evidence, and report whether it is reliable, partial, or heuristic. Include the signed join evidence and state its reliability. Use the sampled state='MD' context, then call benthic_join. Include the context fields and distinguish partial evidence. Bound the left side with uei='ESELKUJSAM45'. Call benthic_join with left_source='usaspending.all_entities', right_source='usp_cl.legislator_terms', left_column='congressional_district', right_column='district', left_where=["congressional_district=eq.03", "uei=eq.ESELKUJSAM45"]. context_conditions=["state=state"].

## join_usaspending_all_entities_usp_cl_legislator_terms_match
Capability: `identifier_partial_join`

Use the signed join path to check the usaspending.all_entities record with congressional_district equal to '03' against usp_cl.legislator_terms on district. Include the signed join evidence and state its reliability. Use the sampled state='MD' context, then call benthic_join. Include the context fields and distinguish partial evidence. Bound the left side with uei='ESELKUJSAM45'. Call benthic_join with left_source='usaspending.all_entities', right_source='usp_cl.legislator_terms', left_column='congressional_district', right_column='district', left_where=["congressional_district=eq.03", "uei=eq.ESELKUJSAM45"]. context_conditions=["state=state"].

## multi_step_0_0_usaspending_irs_ng
Capability: `multi_step_join`

Starting from the row in usaspending.all_entities with uei 'ESELKUJSAM45', follow the signed path to samer.sam_registrations and then the signed path to irs_ng.bmf_organizations. Report the final identifier and state each step's reliability.

## multi_step_0_1_usaspending_irs_ng
Capability: `multi_step_join`

Two signed paths connect usaspending.all_entities to irs_ng.bmf_organizations through samer.sam_registrations. Walk both of them, starting from the row in usaspending.all_entities with uei 'ESELKUJSAM45', and report how many rows in irs_ng.bmf_organizations the path reaches.

## negative_unsigned_0
Capability: `unsigned_join_rejection`

Check whether the signed catalog authorizes any join between usaspending.agency and usaspending.agency_by_subtier_and_optionally_toptier. Do not inspect data or call benthic_join unless discovery returns a path. If no path exists, state that no signed path exists and stop.

## negative_unsigned_1
Capability: `unsigned_join_rejection`

Check whether the signed catalog authorizes any join between usaspending.agency_by_subtier_and_optionally_toptier and usaspending.agency_lookup. Do not inspect data or call benthic_join unless discovery returns a path. If no path exists, state that no signed path exists and stop.

## negative_unsigned_2
Capability: `unsigned_join_rejection`

Check whether the signed catalog authorizes any join between usaspending.agency_lookup and usaspending.all_entities. Do not inspect data or call benthic_join unless discovery returns a path. If no path exists, state that no signed path exists and stop.

## relation_trap_0_0_usp_cl_legislator_terms
Capability: `relation_trap`

Who held the office recorded in usp_cl.legislator_terms as of a past date, and what were the term boundaries? Answer from the historical record and state the term window you used. Do not answer from a view that only holds present-day rows.

## relation_trap_0_1_usp_cl_legislator_terms
Capability: `relation_trap`

List the officeholders in usp_cl.legislator_terms in chronological order with their term boundaries. The present-day view is not a substitute for the historical record.

## rpc_districts_in_bbox_limits
Capability: `districts_in_bbox_rpc_limits`

List signed districts in the small bounding box around latitude 52.6285576 and longitude 1.2923954; explain the limits of the result. Include the operation arguments and the returned completeness/truncation information.

## rpc_districts_in_bbox_rows
Capability: `districts_in_bbox_rpc`

List signed districts in the small bounding box around latitude 52.6285576 and longitude 1.2923954; explain the limits of the result.

## rpc_find_district_limits
Capability: `find_district_rpc_limits`

Find the signed district containing latitude 52.6285576 and longitude 1.2923954, and state the result's geographic scope. Include the operation arguments and the returned completeness/truncation information.

## rpc_find_district_rows
Capability: `find_district_rpc`

Find the signed district containing latitude 52.6285576 and longitude 1.2923954, and state the result's geographic scope.

## rpc_nonprofits_nearby_limits
Capability: `nonprofits_nearby_rpc_limits`

Find signed nonprofit records within 1000 meters of latitude 52.6285576 and longitude 1.2923954; do not describe them as exact unless the returned evidence supports that. Include the operation arguments and the returned completeness/truncation information.

## rpc_nonprofits_nearby_rows
Capability: `nonprofits_nearby_rpc`

Find signed nonprofit records within 1000 meters of latitude 52.6285576 and longitude 1.2923954; do not describe them as exact unless the returned evidence supports that.

## sequential_0_usaspending_irs_ng
Capability: `sequential_lookup`

First look up a value of id in usaspending.agency using 1, then separately look up that value in irs_ng.bmf_organization_snapshots. Do not assume the two relations have a signed join.

## sequential_1_usaspending_irs_ng
Capability: `sequential_lookup`

First look up a value of id in usaspending.agency using 1, then separately look up that value in irs_ng.bmf_organizations. Do not assume the two relations have a signed join.

## sequential_2_usaspending_irs_ng
Capability: `sequential_lookup`

First look up a value of id in usaspending.agency using 1, then separately look up that value in irs_ng.census_demographics. Do not assume the two relations have a signed join.

## sequential_3_usaspending_irs_ng
Capability: `sequential_lookup`

First look up a value of id in usaspending.agency using 1, then separately look up that value in irs_ng.form990_details. Do not assume the two relations have a signed join.
