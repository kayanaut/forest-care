# Forest Care Bonn

A working prototype for robot-assisted monitoring of one invasive plant in Bonn:
***Prunus serotina*** (Spätblühende Traubenkirsche). LANUK NRW classifies it as invasive
because it forms dense shrub layers that suppress the regeneration of oak, rowan and pine.

A ground robot surveys forest and wooded park land and reports candidate detections. Each
one is placed in real Bonn and NRW context: district, land use, nature reserves, FFH
sites, protected biotopes, and GBIF and LANUK records. An expert confirms or rejects it.
Repeated surveys then show where stands **appear, expand, stay stable, decline, or need
inspection**. The system never recommends removal; ecological decisions stay with people.

![Review of a simulated detection at NSG Düne Tannenbusch](docs/img/review.png)

## Quick start

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                               # install dependencies into .venv
uv run python -m forestcare seed      # build the demo database from simulated robot missions
uv run python -m forestcare serve     # open http://127.0.0.1:8000
```

The API is documented at http://127.0.0.1:8000/docs, including the robot ingest contract
(`POST /api/missions`).

## A five-minute tour

1. **Review tab.** The 34 detections from the September 2026 survey round wait for a
   decision. Enter your name at the top right, open an item, and look at the (simulated)
   camera frame: glossy, serrated leaves and fruit in racemes point to *P. serotina*;
   fruit in the leaf axils points to *Frangula*. Decide with the buttons or the keys
   C / R / U / F. The next item opens automatically.
2. **Why is it in the queue?** Each item lists its reasons, for example "Detected after
   management recorded on 2026-02-12 (possible resprouting)", "In or near NSG Kottenforst"
   or "Known look-alike location: 12 earlier detection(s) rejected (*Prunus padus* ×12)".
3. **Stands tab.** Filter by status. Open **Expanding**: a stand inside NSG Düne
   Tannenbusch that grew from 4 to 16 confirmed plants over three years. Its page shows
   the survey history, the reasons to look again, and which of LANUK's published
   priority situations apply ("not a recommendation"). The stand log holds human
   entries only.
4. **Try this.** Confirm the pending seedlings of the unverified stand in NSG Ennert. It
   turns into a **New** stand in a protected area.
5. **Missions tab.** Every survey with its driven track. In 2026 the Kottenforst missions
   skipped a blocked transect, so the stand behind it is **Not surveyed recently** instead
   of being counted as gone. The table at the bottom shows how reviewers judged detections at each model
   probability.
6. **Data & provenance tab.** Which data is real and which is simulated, licences,
   retrieval dates, hashes and the audit log.
7. **Exports.** Stands as GeoJSON, and a CSV draft that uses the field names and value
   classes of LANUK's Neobiota reporting layer, with ETRS89/UTM32 coordinates.

## What is real, what is simulated

| Real | Simulated |
|---|---|
| Bonn districts and wooded land use (Stadt Bonn, CC0) | Robot missions, tracks, detections |
| NSG, FFH, protected biotopes (LANUK @LINFOS, dl-de/zero-2-0) | Camera images (labelled drawings, not photos) |
| GBIF records of *P. serotina* in Bonn (16) | Classifier scores and GNSS error |
| LANUK Neobiota find points (0 in Bonn, 5 in NRW) and species facts | Plant positions and stand dynamics |
| Orthophotos (Geobasis NRW) and forest layers (Wald und Holz NRW), live | Review history before Sept 2026, two stand log entries |

Simulated plants were placed in real Bonn woodland so the context lookups are exercised.
**They say nothing about where the species actually grows.** Every simulated record
carries `source_kind = simulated` and a yellow badge in the UI.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design choices, components, workflow,
  status logic, data model, provenance, robot contract
- [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md): the Bonn and NRW sources explored, used
  and not used, and the gaps found
- [docs/ASSUMPTIONS_AND_VALIDATION.md](docs/ASSUMPTIONS_AND_VALIDATION.md): what works
  (with tests), what is simulated, and the questions for local forest and ecology experts

## Tests

```bash
uv run pytest              # 62 tests, including 2 browser tests (headless Chromium)
uv run pytest -m "not browser"
```

The browser tests need the Playwright Chromium build (`uv run playwright install chromium`).

## Project layout

```
forestcare/         core service: ingest, reference context, stands, review, export, API
simulator/          simulated world, robot missions, images, demo reviewer
web/                map + review UI (plain JS, Leaflet vendored)
data/reference/     real reference snapshots + manifest.json (committed)
data/runtime/       SQLite database, images, generated payloads (created by `seed`, not committed)
scripts/            fetch_reference_data.py: refresh the real snapshots
tests/              pytest suite
docs/               architecture, data sources, assumptions
```

## Refreshing the reference data

```bash
uv run python scripts/fetch_reference_data.py
```

This downloads the current data from Offene Daten Bonn, the LANUK WFS and Neobiota
service, and GBIF, and rewrites `data/reference/*` and the manifest.

## Data attribution

Stadt Bonn (CC0) · LANUK NRW (dl-de/zero-2-0) · Geobasis NRW (dl-de/zero-2-0) ·
Landesbetrieb Wald und Holz NRW · GBIF.org occurrence data (per-record licences: CC0,
CC BY 4.0, CC BY-NC 4.0) · © OpenStreetMap contributors (basemap) · Leaflet (BSD-2-Clause).
