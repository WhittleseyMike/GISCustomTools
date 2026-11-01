"""
Firezone Buffer
===============

ArcGIS Pro script tool that creates concentric fire zones (rings) around
input polygons. Each zone has its own width, and zones stack outward from
the input polygon edge. For example, widths of 30, 70 and 100 Feet produce
rings covering 0-30, 30-100 and 100-200 Feet from the polygon.

Widths may use different units (e.g. "30 Feet;20 Meters"); they are all
converted to the unit of the first width, which is used for the output.

The output feature class is written to the chosen workspace and named
'firezone_<input name>'. Zones from neighbouring input polygons are merged,
so each zone is a single feature and the zones never overlap.

When added to the map, the output is drawn with unique values on the Fire
Zone field, coloured in even steps from red (zone 1) to yellow (outermost).

Script tool parameters (index: label | data type | direction)
    0: Input Features         | Feature Layer (Polygon)  | Input
    1: Output Workspace       | Workspace                | Input
    2: Zone Widths            | Linear Unit (Multiple values) | Input
    3: Output Features        | Feature Class                 | Output (Derived)

Can also be run from the command line for testing:
    python firezone_buffer.py <input_fc> <out_workspace> "30 Feet;70 Feet;20 Meters"
"""

import json
import os
import sys

import arcpy

OUTPUT_PREFIX = "firezone_"

# Zone colours run from red nearest the input to yellow at the outer edge.
START_COLOR = (255, 0, 0)
END_COLOR = (255, 255, 0)


def build_output_path(in_features, out_workspace):
    """Return a valid output path named 'firezone_<input name>'.

    ValidateTableName strips characters the target workspace does not allow
    (e.g. spaces or a leading digit in a file geodatabase).
    """
    base_name = arcpy.Describe(in_features).baseName
    out_name = arcpy.ValidateTableName(OUTPUT_PREFIX + base_name, out_workspace)

    # Folders produce shapefiles, which need the extension; geodatabases don't.
    if arcpy.Describe(out_workspace).workspaceType == "FileSystem":
        out_name += ".shp"

    return os.path.join(out_workspace, out_name)


def parse_widths(widths_text):
    """Turn multivalue Linear Unit text into (widths, unit) in a single unit.

    Pro passes the values as e.g. "'30 Feet';'20 Meters'". MultipleRingBuffer
    takes one unit for all distances, so every width is converted to the unit
    of the first one.
    """
    entries = []
    for item in widths_text.split(";"):
        item = item.strip().strip("'\"")
        if not item:
            continue
        value, _, unit = item.partition(" ")
        entries.append((float(value), unit.strip() or "Unknown"))
    if not entries:
        raise ValueError("Enter at least one zone width.")

    units = {unit for _, unit in entries}
    if "Unknown" in units:
        # A width typed without a unit can't be converted; it's only safe when
        # every width lacks one, in which case the input's own units are used.
        if len(units) > 1:
            raise ValueError("Give every zone width a unit, or leave the unit off all of them.")
        return [value for value, _ in entries], "Default"

    out_unit = entries[0][1]
    widths = [value * arcpy.LinearUnitConversionFactor(unit, out_unit) for value, unit in entries]
    return widths, out_unit


def add_zone_fields(out_fc, distance_field, distances, widths):
    """Add 'zone' (1 = closest to the input) and 'zone_width' fields.

    MultipleRingBuffer stores each ring's outer distance, so each ring is
    matched to the nearest cumulative distance rather than relying on
    feature order. Nearest-match avoids float rounding from storage.
    """
    arcpy.management.AddField(out_fc, "zone", "SHORT", field_alias="Fire Zone")
    arcpy.management.AddField(out_fc, "zone_width", "DOUBLE", field_alias="Zone Width")
    with arcpy.da.UpdateCursor(out_fc, [distance_field, "zone", "zone_width"]) as cursor:
        for row in cursor:
            i = min(range(len(distances)), key=lambda j: abs(distances[j] - row[0]))
            cursor.updateRow([row[0], i + 1, widths[i]])


def zone_renderer(distances, unit_label):
    """Build a unique value renderer on 'zone' with a red-to-yellow ramp.

    The renderer is built per run, rather than read from a .lyrx file, so it
    always has exactly one class per zone. Colours are spaced evenly between
    START_COLOR and END_COLOR, one discrete step per zone.
    """
    count = len(distances)
    infos = []
    inner = 0.0
    for i, outer in enumerate(distances):
        t = i / (count - 1) if count > 1 else 0.0
        color = [round(s + (e - s) * t) for s, e in zip(START_COLOR, END_COLOR)] + [255]
        infos.append({
            "value": str(i + 1),
            "label": f"Zone {i + 1} ({inner:g}-{outer:g} {unit_label})",
            "symbol": {
                "type": "esriSFS",
                "style": "esriSFSSolid",
                "color": color,
                "outline": {"type": "esriSLS", "style": "esriSLSSolid",
                            "color": [110, 110, 110, 255], "width": 0.4},
            },
        })
        inner = outer

    return {"type": "uniqueValue", "field1": "zone", "uniqueValueInfos": infos}


def firezone_buffer(in_features, out_workspace, widths, unit):
    """Create the fire zone rings.

    Returns the output feature class path and the outer distance of each zone.
    """
    desc = arcpy.Describe(in_features)
    if desc.shapeType != "Polygon":
        raise ValueError(f"Input must be polygon features (got {desc.shapeType}).")
    if any(w <= 0 for w in widths):
        raise ValueError("Every zone width must be greater than 0.")

    out_fc = build_output_path(in_features, out_workspace)

    # Each zone starts where the previous one ends, so the buffer distances
    # are the running total of the widths.
    distances = []
    total = 0.0
    for w in widths:
        total += w
        distances.append(total)

    unit_label = "(input units)" if unit == "Default" else unit
    arcpy.AddMessage(f"Creating {len(widths)} fire zone(s) out to {distances[-1]:g} {unit_label}:")
    inner = 0.0
    for i, outer in enumerate(distances, start=1):
        arcpy.AddMessage(f"  Zone {i}: {inner:g}-{outer:g} {unit_label}")
        inner = outer

    # Dissolve ALL is what makes each zone a ring (e.g. 30-100 ft); with NONE,
    # MultipleRingBuffer returns overlapping full buffers instead. OUTSIDE_ONLY
    # keeps the input polygon itself out of zone 1.
    arcpy.analysis.MultipleRingBuffer(
        in_features,
        out_fc,
        distances,
        unit,
        "distance",
        "ALL",
        "OUTSIDE_ONLY",
    )

    add_zone_fields(out_fc, "distance", distances, widths)

    arcpy.AddMessage(f"Output written to {out_fc}")
    return out_fc, distances


def main():
    arcpy.env.overwriteOutput = True

    in_features = arcpy.GetParameterAsText(0)
    out_workspace = arcpy.GetParameterAsText(1)
    widths_text = arcpy.GetParameterAsText(2)

    try:
        widths, unit = parse_widths(widths_text)
        out_fc, distances = firezone_buffer(in_features, out_workspace, widths, unit)
        arcpy.SetParameterAsText(3, out_fc)

        # Pro applies this to the output layer when it adds it to the map.
        unit_label = "(input units)" if unit == "Default" else unit
        renderer = zone_renderer(distances, unit_label)
        arcpy.SetParameterSymbology(3, "JSONRENDERER=" + json.dumps(renderer))
    except arcpy.ExecuteError:
        # Geoprocessing failures (locks, invalid geometry, etc.) carry their
        # own messages, so pass them straight through to the tool dialog.
        arcpy.AddError(arcpy.GetMessages(2))
        sys.exit(1)
    except Exception as e:
        arcpy.AddError(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
