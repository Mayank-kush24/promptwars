"""Build a state-level India GeoJSON with the complete claimed boundaries.

Takes Highcharts' `in-all.geo.json` as the base (small, clean state outlines for
all of India except northern J&K / Ladakh), then replaces the Jammu and Kashmir
+ Ladakh features with versions dissolved from `udit-001/india-maps-data`'s
district-level file (which extends north into PoK / Aksai Chin / Siachen as per
the official Indian boundary).

The output is written to ``static/geo/in-all-claimed.geo.json``.

If ``in-states-claimed.geo.json`` is absent (district-level GeoJSON from
``udit-001/india-maps-data``), the script writes the Highcharts base unchanged
so maps still load; add the districts file later to merge claimed J&K/Ladakh.
"""
from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
GEO_DIR = ROOT / "static" / "geo"

BASE = GEO_DIR / "in-all.geo.json"
DISTRICTS = GEO_DIR / "in-states-claimed.geo.json"
OUT = GEO_DIR / "in-all-claimed.geo.json"

REPLACE = {"Jammu and Kashmir", "Ladakh"}


def _dissolve(features: list[dict]) -> dict:
    geoms = [shape(f["geometry"]) for f in features if f.get("geometry")]
    merged = unary_union(geoms)
    merged = merged.simplify(0.01, preserve_topology=True)
    return mapping(merged)


def main() -> None:
    if not BASE.is_file():
        raise SystemExit(f"Missing base GeoJSON: {BASE}")

    base = json.loads(BASE.read_text(encoding="utf-8"))

    if not DISTRICTS.is_file():
        out = {
            "type": "FeatureCollection",
            "title": "India admin-1 (Highcharts base — optional in-states-claimed.geo.json not present)",
            "copyright": base.get("copyright") or base.get("copyrightShort") or "",
            "copyrightShort": base.get("copyrightShort", ""),
            "copyrightUrl": base.get("copyrightUrl", ""),
            "features": list(base.get("features", [])),
        }
        OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
        print(
            f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes) from base only; "
            f"add {DISTRICTS.name} to dissolve Jammu and Kashmir + Ladakh from districts."
        )
        return

    districts = json.loads(DISTRICTS.read_text(encoding="utf-8"))

    by_state: dict[str, list[dict]] = {}
    for feat in districts.get("features", []):
        nm = (feat.get("properties") or {}).get("st_nm")
        if nm in REPLACE:
            by_state.setdefault(nm, []).append(feat)

    new_features: list[dict] = []
    for feat in base.get("features", []):
        nm = (feat.get("properties") or {}).get("name")
        if nm in REPLACE and nm in by_state:
            geom = _dissolve(by_state[nm])
            props = dict(feat.get("properties") or {})
            new_props = dict(props)
            new_props.pop("hc-middle-lon", None)
            new_props.pop("hc-middle-lat", None)
            new_features.append({"type": "Feature", "properties": new_props, "geometry": geom})
        else:
            new_features.append(feat)

    out = {
        "type": "FeatureCollection",
        "title": "India admin-1 (Highcharts base + claimed J&K / Ladakh)",
        "copyright": "Highcharts (OpenStreetMap) for most states; J&K + Ladakh dissolved from udit-001/india-maps-data (district shapefiles).",
        "features": new_features,
    }

    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes, {len(new_features)} features)")


if __name__ == "__main__":
    main()
