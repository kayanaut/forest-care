# Bonn and NRW data sources

Explored on 2026-09-26. Everything under "Used" was downloaded or tested live, and the
retrieved snapshots are in `data/reference/` with a manifest (licence, URL, time,
SHA-256). Run `uv run python scripts/fetch_reference_data.py` to refresh them.

## Used in the prototype

### City of Bonn — Offene Daten Bonn

| Dataset | What we use it for | Access | Licence |
|---|---|---|---|
| [Statistische Bezirke](https://opendata.bonn.de/dataset/statistische-bezirke) | Bonn boundary (ingest filter); district and Stadtbezirk names in the review view and the LANUK draft | GeoJSON, `stadtplan.bonn.de/geojson?OD=4859` | CC0 |
| [Landnutzungskartierung Realnutzung](https://opendata.bonn.de/dataset/landnutzungskartierung-realnutzung) | Woodland mask; land-use class shown to reviewers; placement of simulated survey areas | GeoJSON, `?OD=4409` (8,974 polygons, 47 classes) | CC0 |

Findings:

- **Woodland.** The land-use map has 488 "Wald" polygons (4,131 ha). It also has wooded
  urban classes: parks with trees, roadside greenery with trees, small woody areas, and
  grassland with trees. We keep all five classes and label them `forest` or
  `urban_green`, because the brief covers both environments.
- **Stadtbezirk.** The 65 statistical districts carry a three-digit code whose first
  digit is the Stadtbezirk: 1 Bonn, 2 Bad Godesberg, 3 Beuel, 4 Hardtberg. We derived this
  mapping from the district names; confirm it with the city.
- **Rhine gap.** The union of the districts is not exactly the city boundary: the Rhine
  water surface belongs to no district. A simulated detection on the Rheinaue riverbank
  fell 0.7 m outside every district. Points within 15 m of a district are therefore
  assigned to it (`boundary_snap_m`).
- **Data age.** The portal says the land-use map was last modified on 2021-09-06. New
  clearings or plantings since then are missing.

### LANUK NRW — Landesamt für Natur, Umwelt und Klima

| Source | What we use it for | Access | Licence |
|---|---|---|---|
| @LINFOS: Naturschutzgebiete | "In or near NSG" context; review priority; LANUK prioritisation context | WFS 2.0 `www.wfs.nrw.de/umwelt/linfos`, type `nsg_polygon`, GeoJSON output | dl-de/zero-2-0 |
| @LINFOS: FFH-Gebiete | Same, plus habitat types and threats shown in LANUK's own words | WFS, `ffh_polygon` | dl-de/zero-2-0 |
| @LINFOS: gesetzlich geschützte Biotope | "In or near protected biotope" context | WFS, `gbt_polygon` | assumed dl-de/zero-2-0 (see below) |
| @LINFOS WMS: Biotopkataster | Map overlay only | WMS `www.wms.nrw.de/umwelt/linfos` | dl-de/zero-2-0 |
| [Neobiota in NRW](https://neobiota.naturschutzinformationen.nrw.de/neobiota/de/arten/pflanzen/148392/kurzbeschreibung): species pages | ID features, look-alikes, phenology, impacts and management priorities, summarised in `species_profile_prunus_serotina.json` with a citation per statement | web pages | cited, not copied |
| Neobiota in NRW: public find-point layer | Number of validated *P. serotina* points in Bonn; field vocabulary for the report draft | ArcGIS FeatureServer `…/Hosted/erfassungen_neobiota_sichtlayer_public/FeatureServer/0` (found in the portal's web-app configuration) | not stated |
| [Biodiversitätsmonitoring NRW](https://www.biodiversitaetsmonitoring.nrw/monitoring/de/arten/pflanzen/prunus_serotina) | "Rising trend since 2006" statement in the species profile | web page | cited |

Bonn reserves that intersect the survey areas:

- **NSG:** BN-003 Kottenforst, BN-007 Düne Tannenbusch, BN-001K1 Siebengebirge Teilgebiet
  Ennert. Bonn has 11 NSG with `BN-` identifiers in total.
- **FFH:** DE-5308-303 Waldreservat Kottenforst, DE-5309-301 Siebengebirge. DE-5208-301
  Siegaue and DE-5309-302 Rodderberg also intersect Bonn. DE-4405-301 (Rhine fish
  protection zones) is deliberately excluded: it is aquatic and one very large polygon.

Findings that matter for the design:

- **Almost no validated find points.** LANUK's public Neobiota layer has **5 validated
  *P. serotina* points in all of NRW and 0 in Bonn** (as of 2026-09-26). Its species
  field `artikelart` marks the species as "nicht Unionsliste": it is not an EU species of
  concern, so reporting is voluntary. Official occurrence data for Bonn is therefore
  close to empty. This is the gap that systematic local monitoring could fill.
- **Reporting format.** The layer's fields define what LANUK records per find:
  - `individuen`: count classes, e.g. "2-5 Ind."
  - `bed_fl`: area classes, e.g. "6-25 qm"
  - `stadium`, `repro`, `lebensraum`, `beseitigt`

  The CSV export uses exactly these names and value classes. A test checks the export
  against the vocabulary downloaded from the service.
- **Reporting channel.** The portal's reporting form (Survey123) asks for a map scale of
  1:25,000 or larger and a photo for validation. Validation takes several days. Our
  export is a draft for a person to transfer. No automated submission exists, and none
  should be built without LANUK's agreement.
- **Prioritisation criteria.** LANUK's measures page names where control should have
  priority: near rare or endangered species and biotopes, early invasions with few trees,
  low infestation, and fruiting solitary specimens. It says eradication is unrealistic
  and follow-up monitoring over several years is needed. The prototype shows these as
  context only.

### Landesbetrieb Wald und Holz NRW

| Source | Use | Access | Terms |
|---|---|---|---|
| WMS Wald und Holz NRW: `baumartenklassifikation_nrw` | Map overlay: tree species classification around a stand | WMS `www.wms.nrw.de/umwelt/waldNRW` | WMS says "Es gelten keine Bedingungen" |
| same WMS: `landeseigener_forstbetrieb_staatswald` | Map overlay: whether a stand lies in state forest (relevant for who to talk to) | WMS | same |

The same service also offers forest types, site nutrients, water balance, forest district
boundaries and wilderness areas. We did not use them yet. See "Candidates" below.

### Geobasis NRW

| Source | Use | Access | Licence |
|---|---|---|---|
| Digital orthophotos (DOP) RGB and colour-infrared | Alternative base maps for checking canopy context | WMS `www.wms.nrw.de/geobasis/wms_nw_dop` | dl-de/zero-2-0 |

### GBIF

[GBIF occurrence search](https://www.gbif.org/species/3021850), taxon key 3021850
(*Prunus serotina* Ehrh.), filtered to the Bonn boundary: **16 records**. Their origins
vary:

- iNaturalist (research grade)
- Pl@ntNet, including one record from Pl@ntNet's "automatically identified" dataset with
  no human confirmation
- ArtenFinder and NABU|naturgucker
- one 2014 herbarium specimen
- one "Flora von Deutschland" record with a ±3.2 km grid-cell uncertainty

We keep the dataset, year, coordinate uncertainty, licence (per record: CC0, CC BY or
CC BY-NC), whether the identification was automatic, and a link. We drop observer names.
Records coarser than 1 km are shown as "grid-level record covers this spot", never as a
point next to the detection. External records are context for the reviewer. They never
change a stand's status.

## Explored, not integrated (yet)

| Source | Why not now |
|---|---|
| OpenStreetMap forest paths (Overpass) | Would give realistic robot routes; transects in the land-use mask are enough for the simulation. ODbL licence. |
| Wald und Holz: Waldtypen, Standorttypen, Wasserhaushalt | Useful for interpreting where *P. serotina* thrives (LANUK stresses light pine and oak woodland on sandy soils). They need a query interface (WFS or downloads from opengeodata.nrw.de). A good next step, agreed with foresters. |
| Bonn "Baumstandorte" (street trees) | Street trees are not the habitat in question. Could flag planted *Prunus* in urban areas later. |
| BfN FloraWeb / neobiota.de | National scale. The brief asks to avoid national features unless they help interpret a Bonn observation; the LANUK pages already cover what a reviewer needs. |
| iNaturalist / observation.org APIs directly | Already reach us through GBIF with licences normalised. |

## Open questions about data (to confirm with the providers)

- **LANUK:** licence and intended reuse of the public Neobiota feature service. Would
  LANUK accept robot-assisted, expert-verified records (in the public layer, every plant record has
  `erfasmeth` = "citizen science")? In what form, and with which validation?
- **LANUK:** is the protected-biotope layer (`gbt_polygon`) under the same dl-de/zero-2-0
  licence as the NSG and FFH layers?
- **Stadt Bonn:** is there a newer land-use map than 2021, or a city forest (Stadtwald)
  layer? Is the Stadtbezirk-from-district-code rule correct?
- **Stadt Bonn / Biologische Station Bonn / Rhein-Erft:** are there unpublished local
  *P. serotina* mappings, for example from the Kottenforst LIFE+ project that the
  Biologische Station ran with Regionalforstamt Rhein-Sieg-Erft (2014–2022)? These could
  validate the system against real ground truth.
