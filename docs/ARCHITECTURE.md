# Architecture

Forest Care Bonn is a prototype for monitoring one invasive plant, *Prunus serotina*
(Spätblühende Traubenkirsche), in Bonn's forests and wooded urban green. A ground robot
drives survey transects and reports candidate detections. The system places each detection
in real Bonn and NRW context, and a person confirms or rejects it. Repeated surveys then show
where stands appear, grow, stay stable, shrink or need another look.

The system describes and flags. It never proposes an intervention; decisions about
management stay with the responsible experts and landowners.

## Design choices

The brief asked for the simplest strong implementation, so the prototype is:

| Choice | Why |
|---|---|
| One Python service (FastAPI) + SQLite file | No database server and no build step. Runs on a laptop in the field office, and the database is one file you can copy or inspect. |
| Plain JavaScript + Leaflet (vendored) | No bundler or framework to maintain. The UI is three static files. |
| Dependency-free geometry (`forestcare/geo.py`) | Bonn is small enough that a local projection is exact to well under a metre. The UTM32 conversion is tested against real LANUK records to 1 mm. No GDAL/PROJ install needed. |
| Reference data as committed snapshots + manifest | Works offline and is reproducible. Each file records its publisher, licence, retrieval time and SHA-256 hash. `scripts/fetch_reference_data.py` refreshes them. |
| Live WMS only for imagery and forest layers | Orthophotos and Wald und Holz maps are large, change rarely, and are only looked at, never computed on. |
| Stand status computed on read | No stored status that could drift from the evidence. At prototype scale the computation takes ~100 ms. |

The robot interface is a small JSON contract rather than a middleware stack (ROS bridge,
message queue). A real robot, or a gateway script on its companion computer, posts one
mission when it is back in range. Re-sending the same mission is safe.

Things this prototype deliberately does not have: user accounts, multiple species,
PostGIS, tiled vector layers, a mobile field app. See "Growth path" below.

## Components

```mermaid
flowchart LR
  subgraph Robot["Robot (simulated for now)"]
    R1[Transect drive + camera] --> R2[On-board classifier<br/>p(P. serotina)]
    R2 --> R3[Mission JSON<br/>track + detections + images]
  end
  subgraph Server["forestcare (FastAPI + SQLite)"]
    I[Ingest<br/>validate · Bonn filter · QC flags] --> C[Context capture<br/>district · land use · NSG/FFH · biotopes · GBIF]
    C --> L[Stand linking<br/>GNSS-aware]
    L --> DB[(SQLite)]
    DB --> Q[Review queue<br/>transparent priority]
    DB --> S[Stand status<br/>coverage-aware trends]
    DB --> X[Exports<br/>GeoJSON · LANUK draft CSV]
  end
  subgraph Ref["Real reference data"]
    B[Stadt Bonn open data]
    LA[LANUK @LINFOS · Neobiota]
    G[GBIF]
  end
  R3 -- POST /api/missions --> I
  Ref -. snapshots .-> C
  H((Expert)) -- confirm / reject / uncertain / field visit --> DB
  H -- stand log entries --> DB
  Q --> H
  S --> H
```

| Module | Responsibility |
|---|---|
| `forestcare/models.py` | Pydantic schemas: the robot contract (`MissionIn`), reviews, stand notes |
| `forestcare/ingest.py` | Validation, Bonn boundary filter, quality flags, image storage, stand linking |
| `forestcare/reference.py` | Loads the snapshots and answers "what is at this point?" |
| `forestcare/stands.py` | Per-year stand history, status classification, inspection reasons |
| `forestcare/review.py` | Queue ordering with reasons; append-only review decisions |
| `forestcare/export.py` | Stands GeoJSON and a draft in LANUK Neobiota field format |
| `forestcare/api.py` | HTTP API and static UI (`/docs` has the OpenAPI description) |
| `simulator/` | Ground-truth world, robot missions, image drawings, simulated reviewer |
| `web/` | Map, review queue, stand history, missions, data & provenance |

## Workflow

1. **Survey.** The robot drives transects through a survey area. It records the track it
   actually drove and the camera's detection range (10 m in the simulation).
2. **Detect.** For each plant it sees, the classifier gives p(*P. serotina*) and
   alternatives. Detections at p ≥ 0.35 are sent, with an image, the GNSS position and its
   accuracy, an estimated plant count, a height class and phenology.
3. **Ingest** (`POST /api/missions`):
   - Validate against the schema. Timestamps must carry a timezone.
   - Reject detections outside Bonn, and say why.
   - Skip duplicates by `uid`, so a mission can be re-sent after a dropped connection.
   - Attach quality flags: `poor_gnss`, `ambiguous_prediction`, `predicted_other_taxon`,
     `outside_mapped_woodland`, `no_image`, `timestamp_outside_mission`.
   - Store the context at that point, with the reference data versions used.
   - Link the detection to a stand.
4. **Review.** Detections enter the queue as `pending`. The queue order is a transparent
   score, and every reason is shown on the item:
   - after recorded management (+3)
   - in or near NSG/FFH or a protected biotope (+2)
   - no reviewed record at this place yet (+2)
   - known look-alike location (+1)
   - ambiguous model output (+1)
   - fruiting reported (+1)

   A reviewer confirms, rejects (optionally naming the species seen), marks uncertain, or
   asks for a field visit. Reviews are append-only, so the history is kept and the latest
   review counts.
5. **Interpret.** For every stand and survey year the system computes: covered or not,
   detections, confirmed and rejected counts, and confirmed plants. From that it derives
   a status and any reasons to look again.
6. **Record.** People add stand log entries (notes, monitoring decisions, management
   carried out). The system never writes these itself.
7. **Report.** Confirmed stands export to GeoJSON and to a CSV draft using LANUK Neobiota
   field names. An expert completes and submits it. Nothing is sent automatically.

## Stands and status

**Linking.** A new detection joins the stand of the best-matching earlier detection if
they are closer than `stand_spread_m + link_sigma_factor · √(acc₁² + acc₂²)`. The defaults
(8 m + 2σ) make the allowed distance grow with GNSS uncertainty. A poor fix under canopy
therefore joins its own stand, while precise fixes in open park land keep neighbouring
stands apart. Stands are never merged or split automatically. That stays an expert action
(not built yet).

**Coverage.** A mission counts as covering a stand in a year if its driven track came
within the detection range of the stand's edge. The stand's spread is capped at 10 m for
this, so a wide GNSS scatter cannot fake coverage. Coverage is what lets the system say
"not re-detected" (the robot was there) rather than "not surveyed" (no evidence either way).

**Counting.** Plants confirmed in a year = the maximum over that year's missions of the
summed plant estimates from confirmed detections. Taking the maximum avoids counting the
same plants twice across the June and September runs. Because detection is imperfect,
the number is a lower bound.

**Status** (latest survey year vs. the previous year with confirmed plants):

| Status | Rule |
|---|---|
| New | confirmed now, never confirmed before |
| Expanding | plants ≥ 1.5 × previous **and** at least +3 |
| Stable | neither expanding nor declining |
| Declining | plants ≤ 0.67 × previous **and** at least −3 |
| Not re-detected | surveyed this year, nothing found, confirmed earlier |
| Awaiting confirmation | confirmed earlier, this year's detections pending or uncertain |
| Not surveyed recently | confirmed earlier, no mission covered it this year |
| Unverified candidate | nothing reviewed yet (or only uncertain) |
| Look-alike location | nothing confirmed, detections rejected as another species |

**Inspection reasons** set the "!" badge. They are:

- expanding
- new or expanding in or near a protected area
- not re-detected
- detections after a recorded management action (LANUK warns about stump sprouting)
- confirmed and rejected detections in the latest reviewed year (possibly a mixed stand)
- open uncertain or field-visit reviews
- median GNSS accuracy above 10 m

Each reason is a sentence the expert can check.

**LANUK context.** LANUK publishes where control should have priority: near rare or
endangered species and biotopes, early invasions with few trees, low infestation, and
fruiting solitary specimens. The stand view shows which of these facts the data supports:
yes, no, or unknown, with the evidence. It is labelled as not a recommendation.
"Low infestation" is always "unknown", because robot detections alone cannot measure it.

## Data model (SQLite)

| Table | Content | Notes |
|---|---|---|
| `missions` | robot, area, times, driven track, detection range, provenance | `source_kind` = `simulated` \| `robot` |
| `observations` | position + accuracy, prediction, image, QC flags, captured context, stand | `uid` unique (idempotency) |
| `reviews` | decision, corrected taxon, note, reviewer, role, time | append-only; `source_kind` = `human` \| `simulated` |
| `stands` | id | status is computed, not stored |
| `stand_notes` | notes, monitoring decisions, management actions | human-entered only |
| `events` | audit log of ingests, reviews, notes | append-only |

## Provenance

Every record says where it came from:

- **Missions and observations** carry `source_kind` and the robot id. They also carry the
  model name and version, the simulator name, version and seed (if simulated), the
  SHA-256 of the mission payload and of the image, and the ingest time.
- **Context** is captured at ingest, together with the retrieval date of each reference
  snapshot it used.
- **Reviews and stand notes** carry the person's name and whether they were entered by a
  human or by the demo seed (`simulated`).
- **Reference snapshots** are described in `data/reference/manifest.json`: publisher, URL,
  licence, retrieval time, hash, processing steps and purpose.
- **The UI** marks simulated items with a yellow badge, real reference data with a green
  one, and shows a demo banner whenever any mission is simulated.

## Robot contract

`POST /api/missions`. The full schema is in `/docs`. A trimmed example:

```json
{
  "mission_id": "SIM-202609-TB",
  "robot_id": "SIM-UGV-01",
  "source_kind": "simulated",
  "area_name": "Düne Tannenbusch",
  "started_at": "2026-09-17T09:12:00+02:00",
  "ended_at": "2026-09-17T09:58:31+02:00",
  "track": [[[7.0572, 50.7433], [7.0601, 50.7433]]],
  "detection_range_m": 10.0,
  "sensors": {"camera": "RGB camera (simulated)", "gnss": "GNSS without RTK (simulated)"},
  "model": {"name": "ps-detector-sim", "version": "0.3.0-sim"},
  "simulator": {"name": "forestcare-sim", "version": "0.1.0", "seed": 42, "window": "september"},
  "observations": [{
    "uid": "SIM-202609-TB-0001",
    "observed_at": "2026-09-17T09:22:10+02:00",
    "lat": 50.744101, "lon": 7.062311, "gnss_accuracy_m": 3.4,
    "predicted_taxon": "Prunus serotina", "confidence": 0.91, "target_probability": 0.91,
    "alternatives": [{"taxon": "Prunus padus", "probability": 0.05}],
    "plant_count_est": 2, "height_class": "shrub", "phenology": "fruiting",
    "image": {"media_type": "image/jpeg", "data_base64": "..."}
  }]
}
```

The response lists accepted, duplicate and rejected detections, each rejection with a
reason. The full generated payloads are in `data/runtime/sim_payloads/` after seeding.

What a real robot integration needs:

- set `source_kind` to `robot`
- send JPEGs rather than SVG drawings
- report honest GNSS accuracy (1σ)
- send the track that was actually driven, including gaps
- keep `uid`s stable across retries

## Growth path

The next steps once the workflow is validated, roughly in order:

1. **Reviewer identity and roles** (login, organisation), so decisions are attributable
   beyond a typed name.
2. **Expert tools for stands:** merge, split and move stands, plus a field-visit
   checklist export.
3. **Real imagery:** JPEG capture, EXIF/GNSS cross-checks, and several frames per
   detection.
4. **Model feedback loop:** export reviewed images as a labelled dataset, then retrain.
   The Missions tab already shows review outcomes by model probability.
5. **Scale** only if needed: PostGIS for geometry, object storage for images, and a
   second species as configuration (the target taxon and look-alikes are already
   parameters in `config.py`).
