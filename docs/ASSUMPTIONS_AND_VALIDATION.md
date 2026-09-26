# What works, what is simulated, what needs validation

Status as of 2026-09-26.

## What works (and is tested)

85 automated tests (`uv run pytest`) pass, including three that drive the real web UI in
headless Chromium.

| Capability | Evidence |
|---|---|
| Robot mission ingest with schema validation, idempotent re-send, per-detection rejection reasons | `tests/test_ingest.py` |
| Bonn-only filter using the city's real district boundaries (incl. the Rhine-bank gap) | `test_observations_outside_bonn_are_rejected_not_stored`, `test_rhine_bank_gap_is_snapped` |
| Context from real data: district, Stadtbezirk, land use, NSG, FFH, protected biotopes, GBIF records, LANUK Neobiota count | `test_context_in_kottenforst`, `test_gbif_context_separates_grid_records` |
| GNSS-aware stand linking | `test_stand_linking_respects_gnss_uncertainty` |
| Stand status and trend logic, including "not surveyed" vs "not re-detected" from mission tracks | `test_classify` (11 cases), `test_absence_needs_coverage`, `test_covered_absence_is_not_redetected_and_needs_inspection` |
| Unreviewed detections never change a trend | `test_unreviewed_detections_do_not_change_the_trend` |
| Review workflow: four decisions, reviewer required, append-only history, audit log | `tests/test_stands_and_review.py`, `test_review_workflow_turns_a_candidate_into_a_new_stand` |
| Review queue with visible reasons | `test_queue_prioritises_and_explains` |
| Human-only stand log; a management action is followed by a resprouting check | `test_management_is_only_recorded_by_people` |
| No API field recommends an intervention | `test_system_never_recommends_management` |
| Exports: stands GeoJSON (confirmed only), LANUK draft CSV with UTM32 coordinates and LANUK value classes | `test_exports_contain_only_confirmed_stands`, `test_lanuk_*` |
| UTM32 conversion exact against real LANUK records | `test_utm32_matches_lanuk_records` |
| Whole demo scenario produces the intended statuses | `test_demo_scenario_statuses` (9 stands) |
| Everything simulated is labelled simulated | `test_everything_in_the_demo_is_marked_simulated` |
| UI: review a detection (incl. keyboard shortcut), open a stand, see provenance | `tests/test_browser.py` |
| Field photos: EXIF position, accuracy, heading, altitude and capture time, each with its source; missing time zone or accuracy flagged; GPS time used when no offset | `test_read_photo_with_full_exif`, `test_missing_offset_and_accuracy_are_flagged_not_invented`, `test_gps_timestamp_is_preferred_over_an_unzoned_local_time` |
| Folder import report: accepted, duplicate, rejected (no GPS, outside Bonn), skipped (non-image, HEIC) | `test_import_folder_report_and_preservation` |
| Originals kept byte-for-byte; SHA-256 recorded; manifest written; previews without GPS metadata | same test, plus `test_upload_endpoint_and_original_download` |
| Tampered originals detected; download refused with 409; `verify-originals` reports them | `test_upload_endpoint_and_original_download` |
| Re-importing a folder adds nothing; a file changed between the two passes is not stored | `test_reimport_is_idempotent`, `test_file_changed_between_passes_is_not_stored`, `test_adding_photos_to_an_existing_photo_mission` |
| Synthetic test photos are always stored as simulated | `test_synthetic_marker_forces_simulated` |
| Expert labels (count, height, phenology) only with "confirmed"; they override robot estimates and feed trends | `test_labels_only_for_confirmed`, `test_counted_photo_label_feeds_the_trend`, `test_expert_label_overrides_robot_estimate` |
| Photos are presence-only: never coverage, a rejected photo is no evidence of absence, uncounted presence is not a trend | `test_photo_walk_never_counts_as_coverage`, `test_rejected_photo_is_not_evidence_of_absence`, `test_uncounted_photo_confirms_presence_but_not_a_trend` |
| Schema migration from the robot-only database keeps all rows | `test_migration_from_v1_keeps_data` |
| UI: import photos through the browser form, label one, download the identical original | `test_import_photo_folder_and_label_in_the_ui` |

Also checked by hand with screenshots: light and dark themes, and a 390 px phone width
without horizontal scrolling.

## What is simulated or mock

| Item | Real or simulated | How it is marked |
|---|---|---|
| Robot missions, tracks, detections | **Simulated** (`simulator/robot.py`) | `source_kind = simulated`, yellow SIMULATED badge, demo banner |
| Camera images | **Simulated drawings** with botanical cues (leaf gloss, raceme vs. axillary fruit). They are not photos. | "SIMULATED IMAGE" bar and watermark on every frame |
| Classifier probabilities | **Simulated** (beta distributions; look-alike scores overlap the target's) | model name `ps-detector-sim 0.3.0-sim` |
| GNSS error | **Simulated**: about 2–6 m (1σ), 4 % multipath outliers | — |
| Plant positions and stand dynamics | **Invented**, placed in real Bonn woodland. **They say nothing about real occurrences.** | ground truth in `data/runtime/sim_ground_truth.json` |
| Review history for 2024 to June 2026 | **Simulated reviewer** ("Demo reviewer (simulated)") | `source_kind = simulated` on each review |
| Stand log entries (one management action, one monitoring decision) | **Simulated** | named "Demo forester/ecologist (simulated)" |
| Demo photo walk (6 imported photos, plus 1 duplicate, 2 rejected and 1 non-image file) | **Synthetic drawings** with real EXIF structure | "SYNTHETIC TEST PHOTO" in pixels and EXIF, so the importer stores them as `simulated` |
| Photo import itself (EXIF reading, storage, hashing, review) | **Real code path**; tested with generated JPEGs, not yet with a real phone or camera folder | — |
| Bonn districts, land use, NSG, FFH, biotopes, GBIF records, LANUK Neobiota counts | **Real**, snapshots of 2026-09-26 | green "real" badge; manifest with licence and hash |
| Species facts and ID hints | **Real**, summarised from LANUK pages with a citation per statement | sources listed in the UI |
| Orthophotos and forest layers | **Real**, live WMS | "live" badge in the layer switcher |

The September 2026 round (34 robot detections and 6 field photos) is left unreviewed on
purpose, so a person can try the workflow. Confirming the pending seedlings in NSG Ennert turns stand EN-1
into a new stand in a protected area.

## Assumptions that need validation with local experts

Numbers are the current defaults in `forestcare/config.py`. All of them are prototype
choices, not validated values.

### Ecology and forestry (Biologische Station Bonn/Rhein-Erft, Stadt Bonn Untere Naturschutzbehörde, Wald und Holz NRW Regionalforstamt Rhein-Sieg-Erft)

1. **What counts as a stand?** We group detections within 8 m plus twice the combined
   GNSS error. Is that how local practitioners delimit a *P. serotina* stand, or do they
   think in forest compartments (Abteilungen) or fixed grid cells?
2. **Trend thresholds.**
   - Expanding: ≥ 1.5× and at least +3 plants.
   - Declining: ≤ 0.67× and at least −3 plants.

   Are these sensible for a species that forms dense shrub layers? Would cover (m²) or
   height class matter more than counts?
3. **Survey timing.** Two rounds a year: early June (flowering) and mid-September (black
   fruit). Is that right for detecting both mature shrubs and seedlings in Bonn's
   forests? Would a leaf-out or autumn-colour survey add value?
4. **Look-alikes.** LANUK names *Prunus padus*. We also assumed *Frangula alnus* and
   *Prunus avium* as confusion species. Which species are actually confused in the
   field here?
5. **What should raise "needs inspection"?** Current reasons:
   - expanding
   - new or expanding in or near a protected area (100 m buffer)
   - not re-detected
   - detections after management
   - mixed confirmed and rejected detections
   - unresolved uncertain reviews
   - poor GNSS

   Which of these do experts want, and which would be noise?
6. **Reading LANUK's criteria.** We read "early invasion with few trees" as ≤ 5 confirmed
   plants, and "fruiting solitary specimen" as fruiting seen on a stand of ≤ 2 plants.
   "Low infestation" cannot be judged from detections at all. Is this reading useful, or
   misleading?
7. **Which habitats matter most in Bonn?** LANUK stresses light pine and oak woodland on
   sand. Düne Tannenbusch is an inland dune NSG, but the prototype does not yet use soil
   or forest-type layers. Should survey planning focus on particular forest types?
8. **Who records management?** Who writes management actions into the stand log, and
   what detail is needed (method, stems treated, follow-up date)? How many years of
   follow-up surveys do they want after treatment?

### Robotics and data

9. **Detection range and coverage.** We assume a 10 m camera range each side of the
   track. The real range depends on undergrowth density and camera placement, and it
   decides whether "not re-detected" is trustworthy.
10. **GNSS under canopy.** We assume the receiver reports honest 1σ accuracy. Real
    multipath under Kottenforst oaks may be worse and under-reported. RTK or visual
    odometry may be needed before stand-level trends are reliable.
11. **Plant counts from images** are optimistic in the simulation. Counting stems in
    dense *P. serotina* thickets from a ground camera is hard, and counts should be
    treated as lower bounds or replaced by cover classes.
12. **Model reporting threshold** (p ≥ 0.35). Set it from real review outcomes. The
    Missions tab tabulates them by probability band.
13. **Access and permissions.** Driving a robot in NSG Kottenforst, NSG Ennert or NSG
    Düne Tannenbusch needs permission from the responsible authorities and landowners
    (state forest, city forest, private). The prototype does not model access rules.

### Field photos

14. **Real camera files.** The importer has been tested with generated JPEGs only. Before
    relying on it, import a real folder from each device in use (iPhone, Android, GPS
    camera, robot camera). Check the following:
    - Is `GPSHPositioningError` present?
    - Is `OffsetTimeOriginal` present?
    - Does rotation and orientation come out right?
    - What does the device do when there is no GPS fix: no position, or a stale one?
15. **Accuracy when missing.** 10 m is assumed for stand grouping when a photo carries no
    accuracy. Is that realistic for phones under the Bonn forest canopy, or is a larger
    value safer?
16. **Time zone.** Local time without an offset is read as Europe/Berlin. That is right
    for cameras set to Bonn time, wrong for travel cameras left on another zone.
17. **What an expert labels on a photo.** Is a plant count from a single photo meaningful,
    or should photos only confirm presence plus a cover class? Which labels do
    practitioners want (height class, phenology, reproduction, "stump sprouts")?
18. **Personal data in EXIF.** Originals keep all metadata, including camera serial
    numbers or owner names if the device writes them, and the photographer's route.
    Retention and access rules are needed before real use. Previews shown in the UI carry
    no metadata.

### Reporting and governance

19. **Reporting to LANUK.** Would LANUK accept robot-assisted, expert-verified records in
    the Neobiota portal? In what form, and with what validation? The export is only a
    draft, and nothing is submitted automatically.
20. **Data protection.** Robot cameras in urban parks can capture people. Real
    deployments need a data-protection assessment (blurring, retention, legal basis).
    The simulation has no such content.
21. **Licences.** Confirm the licence of LANUK's public Neobiota feature service and the
    protected-biotope layer before any publication of derived data.

## Known limitations

- **Stand geometry.** Stands are never merged or split automatically, and there is no UI
  to do it by hand yet. With single-linkage grouping, two nearby stands can end up
  sharing one id if a detection bridges them.
- **Reviewer identity** is a typed name stored in the browser. There is no login.
- **Land-use mask** is from 2021 (the city's latest open version).
- **Plant counts** are the maximum over one year's missions. With imperfect detection,
  they are lower bounds.
- **Network needs.** The OpenStreetMap basemap and the WMS layers need internet. All
  analysis and the reference layers work offline.
- **Photos that cannot be imported.** Photos without GPS cannot be placed by hand yet,
  and HEIC (the iPhone default) must be exported as JPEG first. Browser uploads are
  limited to 500 files. Use the CLI for larger folders.
- **Photo counts.** One photo usually shows one part of a stand. Photo counts are only
  used for trends when an expert enters them, and they are still lower bounds.
- **Simulated images** carry the same cues in every frame. Real photos will be much
  harder to judge, and the review UI may need zoom, several frames per detection, and
  side-by-side comparison with earlier years.
