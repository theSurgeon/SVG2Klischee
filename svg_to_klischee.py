"""
SVG → Heidelberg-Tiegel Klischee Generator
===========================================
Blender Python Script (Blender 3.x / 4.x)

Anleitung:
  1. Blender öffnen
  2. Scripting-Workspace wählen
  3. Dieses Skript laden und "Run Script" klicken
  4. Im sich öffnenden Panel (N-Panel → "Klischee") die SVG-Datei
     auswählen und Parameter anpassen.

Was das Skript erzeugt:
  - Grundplatte (Zinkdicke + Trägermaterial)
  - Extrudiertes Druckrelief aus der SVG
  - Optional: Beschnitt-Rand und Passermarken
  - Export als STL oder OBJ für CNC-Fräse / 3D-Druck

Technische Referenz Heidelberg Tiegel (Platen Press):
  - Druckform-Bereich:  max. 260 × 185 mm  (Kegel-Tiegel GT52)
  - Klischee-Dicke:     0,90 mm Typenhöhe = 23,566 mm  (DIN 16500)
  - Relieftiefe:        0,4 – 0,8 mm (Hochdruck)
  - Trägerplatte:       optional, Dicke konfigurierbar
"""

import bpy
import bmesh
import os
import math
from bpy.types import Panel, Operator, PropertyGroup
from bpy.props import (
    StringProperty, FloatProperty, BoolProperty,
    EnumProperty, IntProperty
)


# ---------------------------------------------------------------------------
# Konstanten – Typografische Höhe DIN 16500
# ---------------------------------------------------------------------------
TYPHOEHE_MM = 1.21            # Typenhöhe (Träger + Relief)
BLENDER_UNIT = 1.0            # 1 BU = 1 mm (wird via scene.unit_settings gesetzt)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def mm(val: float) -> float:
    """Konvertiert mm → Blender Units (1 BU = 1 mm)."""
    return val


def set_scene_units():
    """Setzt Szene auf Millimeter."""
    scene = bpy.context.scene
    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.scale_length = 0.001
    scene.unit_settings.length_unit = 'MILLIMETERS'


def clear_collection(name: str):
    """Löscht eine Collection und alle Objekte darin."""
    if name in bpy.data.collections:
        col = bpy.data.collections[name]
        for obj in list(col.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(col)


def get_or_create_collection(name: str) -> bpy.types.Collection:
    if name not in bpy.data.collections:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return bpy.data.collections[name]


def link_to_collection(obj, col_name: str):
    col = get_or_create_collection(col_name)
    if obj.name not in col.objects:
        col.objects.link(obj)
    # Aus Root-Collection entfernen, falls vorhanden
    if obj.name in bpy.context.scene.collection.objects:
        bpy.context.scene.collection.objects.unlink(obj)


# ---------------------------------------------------------------------------
# SVG Import & Kurvenverarbeitung
# ---------------------------------------------------------------------------

def import_svg(filepath: str) -> list:
    """
    Importiert SVG und gibt Liste der erzeugten Kurvenobjekte zurück.
    Blender's built-in SVG-Importer erzeugt Kurvenobjekte.
    """
    before = set(bpy.data.objects.keys())

    bpy.ops.import_curve.svg(filepath=filepath)

    after = set(bpy.data.objects.keys())
    new_objs = [bpy.data.objects[n] for n in (after - before)]

    # Nur Kurven-Objekte behalten
    curves = [o for o in new_objs if o.type == 'CURVE']
    return curves


def normalize_curves(curves: list, props):
    """
    Skaliert und positioniert Kurven so, dass sie auf die
    Druckplatte passen.
    """
    if not curves:
        return

    # Alle Kurven in eine temporäre Collection
    for c in curves:
        c.select_set(True)
    bpy.context.view_layer.objects.active = curves[0]

    # Gemeinsamen Bounding-Box-Mittelpunkt berechnen
    min_x = min(c.location.x + c.bound_box[0][0] * c.scale.x for c in curves)
    max_x = max(c.location.x + c.bound_box[6][0] * c.scale.x for c in curves)
    min_y = min(c.location.y + c.bound_box[0][1] * c.scale.y for c in curves)
    max_y = max(c.location.y + c.bound_box[6][1] * c.scale.y for c in curves)

    svg_w = max_x - min_x
    svg_h = max_y - min_y

    if svg_w < 0.0001 or svg_h < 0.0001:
        return  # Sicherheitscheck

    # Skalierungsfaktor – SVG-Koordinaten sind oft in Pixeln (96 dpi)
    # Umrechnung: 1 px = 25.4/96 mm ≈ 0.2646 mm
    # Blenders SVG-Importer skaliert auf BU, wir korrigieren auf mm
    px_to_mm = 25.4 / 96.0
    scale_factor = px_to_mm

    # Optionaler Fit-to-plate
    target_w = mm(props.plate_width - 2 * props.margin)
    target_h = mm(props.plate_height - 2 * props.margin)

    if props.fit_to_plate:
        fit_scale = min(target_w / (svg_w * scale_factor),
                        target_h / (svg_h * scale_factor))
        scale_factor *= fit_scale

    for c in curves:
        c.scale.x *= scale_factor
        c.scale.y *= scale_factor
        # Z-Skalierung auf 1 lassen (Tiefe wird später gesetzt)
        c.scale.z = 1.0

    # Neu zentrieren relativ zur Platte
    # Nach Skalierung neue Bounds
    new_min_x = min(c.location.x + c.bound_box[0][0] * c.scale.x for c in curves)
    new_max_x = max(c.location.x + c.bound_box[6][0] * c.scale.x for c in curves)
    new_min_y = min(c.location.y + c.bound_box[0][1] * c.scale.y for c in curves)
    new_max_y = max(c.location.y + c.bound_box[6][1] * c.scale.y for c in curves)

    cx = (new_min_x + new_max_x) / 2
    cy = (new_min_y + new_max_y) / 2

    for c in curves:
        c.location.x -= cx
        c.location.y -= cy

    # Finale Motivgrösse zurückgeben (nach Skalierung, in mm)
    motif_w = (new_max_x - new_min_x)
    motif_h = (new_max_y - new_min_y)
    return motif_w, motif_h


def mirror_curves(curves: list):
    """
    Spiegelt alle Kurvenobjekte auf der X-Achse um ihren eigenen
    geometrischen Mittelpunkt.

    Blenders SVG-Importer legt Spline-Punkte in lokalen Koordinaten ab,
    deren Ursprung (0,0) oft NICHT der geometrische Mittelpunkt ist.
    Ein einfaches co.x *= -1 würde den Schwerpunkt verschieben.

    Korrekte Methode:
      1. Geometrischen Mittelpunkt cx aller Punkte (Weltkoordinaten) berechnen
      2. Jeden Punkt um cx spiegeln: new_x = 2*cx - old_x
      3. Location des Objekts ebenfalls spiegeln
    """
    for c in curves:
        if c.type != 'CURVE':
            continue

        # Alle Punkte in Weltkoordinaten sammeln, um den echten Mittelpunkt zu finden
        world_xs = []
        for spline in c.data.splines:
            if spline.type == 'BEZIER':
                for bp in spline.bezier_points:
                    # Weltkoordinate = location.x + lokale_x * scale.x
                    world_xs.append(c.location.x + bp.co.x * c.scale.x)
            else:
                for pt in spline.points:
                    world_xs.append(c.location.x + pt.co.x * c.scale.x)

        if not world_xs:
            continue

        # Mittelpunkt in Weltkoordinaten
        world_cx = (min(world_xs) + max(world_xs)) / 2.0

        # Spiegelachse in lokalen Koordinaten des Objekts
        # world_cx = c.location.x + local_cx * c.scale.x
        # => local_cx = (world_cx - c.location.x) / c.scale.x
        if abs(c.scale.x) < 1e-9:
            continue
        local_cx = (world_cx - c.location.x) / c.scale.x

        # Jeden Spline-Punkt um local_cx spiegeln: new_x = 2*local_cx - old_x
        for spline in c.data.splines:
            if spline.type == 'BEZIER':
                for bp in spline.bezier_points:
                    bp.co.x            = 2 * local_cx - bp.co.x
                    bp.handle_left.x   = 2 * local_cx - bp.handle_left.x
                    bp.handle_right.x  = 2 * local_cx - bp.handle_right.x
            else:
                for pt in spline.points:
                    pt.co.x = 2 * local_cx - pt.co.x




def apply_taper_to_mesh(mesh_obj: bpy.types.Object, props):
    """
    Konisches (Trapez-)Profil analog einer Fotopolymer-Belichtung.

    Konzept: Face-Normalen als Belichtungsrichtung
    -----------------------------------------------
    Jede SEITENFLAECHE des Reliefs hat eine Flaechennormale die exakt
    senkrecht zur Kontur zeigt:
      - Aussenkontur-Seite  -> Normale zeigt nach AUSSEN
      - Innenkontur-Seite   -> Normale zeigt nach INNEN

    Die unteren Vertices (Reliefwurzel, Z~0) werden um den taper_amount
    entlang der gemittelten XY-Normalenrichtung ihrer angrenzenden
    Seitenflaechen verschoben.

    Vorteile gegenueber Island/Ray-Casting:
      - Kein Innen/Aussen-Erkennung noetig
      - Funktioniert fuer beliebige Topologien (@, &, verschachtelte Formen)
      - Physikalisch korrekt: jede Kante wachst senkrecht zu sich selbst
      - Stabile Ergebnisse unabhaengig von Mesh-Qualitaet
    """
    taper    = mm(props.taper_amount)
    relief_h = mm(props.relief_depth)

    if relief_h < 0.0001 or taper < 0.0001:
        return

    me = mesh_obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    # ------------------------------------------------------------------
    # 1. Fuer jeden Vertex: gemittelte XY-Normale der angrenzenden
    #    Seitenflaechen akkumulieren.
    #    Seitenflaechen = Faces deren Normale nicht (nahezu) vertikal ist.
    #    (Deck- und Bodenflaechen haben Z-Normale ~ +-1, werden ignoriert)
    # ------------------------------------------------------------------
    vert_normal_x = [0.0] * len(bm.verts)
    vert_normal_y = [0.0] * len(bm.verts)
    vert_count    = [0]   * len(bm.verts)

    for face in bm.faces:
        fn = face.normal
        # Seitenflaeche: |fn.z| deutlich kleiner als 1
        if abs(fn.z) > 0.7:
            continue   # Deck- oder Bodenflaeche -> ignorieren

        for v in face.verts:
            vert_normal_x[v.index] += fn.x
            vert_normal_y[v.index] += fn.y
            vert_count[v.index]    += 1

    # ------------------------------------------------------------------
    # 2. Untere Vertices verschieben (Z ~ 0 = Reliefwurzel)
    #    depth_factor: 1.0 ganz unten, 0.0 an der Druckflaeche
    # ------------------------------------------------------------------
    for v in bm.verts:
        depth_factor = 0.5 - (v.co.z / relief_h)  # Z zentriert: -relief_h/2..+relief_h/2
        depth_factor = max(0.0, min(1.0, depth_factor))

        if depth_factor < 0.0001:
            continue   # Druckflaeche bleibt exakt

        if vert_count[v.index] == 0:
            continue   # kein Seitenflaechen-Beitrag

        nx = vert_normal_x[v.index]
        ny = vert_normal_y[v.index]
        length = math.sqrt(nx * nx + ny * ny)

        if length < 0.0001:
            continue

        # Normieren und skalieren
        offset  = taper * depth_factor
        v.co.x += (nx / length) * offset
        v.co.y += (ny / length) * offset

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    me.update()

def extrude_curves(curves: list, props) -> list:
    """
    Konvertiert Kurven zu Meshes mit Extrusion (Reliefhöhe).
    Positioniert das Relief auf der Grundplatte.
    Wendet optional Spiegelung (vor Extrusion) und konische Basis (nach Mesh-
    Konvertierung) an.
    """
    mesh_objs = []

    # Spiegelung VOR der Extrusion (auf Kurven-Ebene, seitenverkehrt für Druck)
    if props.mirror_motif:
        mirror_curves(curves)

    for curve_obj in curves:
        # Extrusionstiefe auf der Kurve setzen
        curve_obj.data.extrude = mm(props.relief_depth / 2)
        curve_obj.data.dimensions = '2D'
        curve_obj.data.fill_mode = 'BOTH'

        # Z-Position = Oberkante der Grundplatte
        curve_obj.location.z = mm(props.plate_thickness + props.relief_depth / 2)

        # Zu Mesh konvertieren
        bpy.ops.object.select_all(action='DESELECT')
        curve_obj.select_set(True)
        bpy.context.view_layer.objects.active = curve_obj
        bpy.ops.object.convert(target='MESH')

        mesh_obj = bpy.context.view_layer.objects.active
        mesh_obj.name = f"Relief_{curve_obj.name}"

        # Konische Basis nach der Mesh-Konvertierung
        if props.use_taper_base:
            apply_taper_to_mesh(mesh_obj, props)

        mesh_objs.append(mesh_obj)

    return mesh_objs


# ---------------------------------------------------------------------------
# Grundplatte
# ---------------------------------------------------------------------------

def create_base_plate(props) -> bpy.types.Object:
    """
    Erstellt die Trägerplatte des Klischees.
    Typenhöhe = Träger + Relief = 23.566 mm (DIN 16500)
    """
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0))
    plate = bpy.context.active_object
    plate.name = "Grundplatte_Klischee"

    plate.scale.x = mm(props.plate_width)
    plate.scale.y = mm(props.plate_height)
    plate.scale.z = mm(props.plate_thickness)

    # Verschieben so dass Unterseite bei Z=0
    plate.location.z = mm(props.plate_thickness) / 2

    bpy.ops.object.transform_apply(scale=True, location=False)

    # Material
    mat = bpy.data.materials.new("Mat_Grundplatte")
    mat.diffuse_color = (0.7, 0.7, 0.72, 1.0)  # Zink-Grau
    plate.data.materials.append(mat)

    return plate


# ---------------------------------------------------------------------------
# Passermarken
# ---------------------------------------------------------------------------

def create_register_marks(props) -> list:
    """
    Erstellt Kreuz-Passermarken in den Ecken der Platte.
    Wichtig für die Ausrichtung im Tiegel.
    """
    marks = []
    if not props.add_register_marks:
        return marks

    offset = mm(5.0)   # Abstand vom Rand
    r      = mm(1.5)   # Radius
    depth  = mm(props.relief_depth)
    z      = mm(props.plate_thickness)

    half_w = mm(props.plate_width) / 2 - offset
    half_h = mm(props.plate_height) / 2 - offset

    positions = [
        ( half_w,  half_h),
        (-half_w,  half_h),
        ( half_w, -half_h),
        (-half_w, -half_h),
    ]

    for i, (px, py) in enumerate(positions):
        # Kreis
        bpy.ops.mesh.primitive_cylinder_add(
            radius=r, depth=depth,
            location=(px, py, z + depth / 2)
        )
        circle = bpy.context.active_object
        circle.name = f"Passer_{i+1}"

        # Kreuz (zwei Boxen)
        for axis in ['X', 'Y']:
            bpy.ops.mesh.primitive_cube_add(size=1, location=(px, py, z + depth / 2))
            bar = bpy.context.active_object
            bar.name = f"PasserKreuz_{i+1}_{axis}"
            if axis == 'X':
                bar.scale = (mm(4.0), mm(0.4), depth)
            else:
                bar.scale = (mm(0.4), mm(4.0), depth)
            bpy.ops.object.transform_apply(scale=True)
            marks.append(bar)

        marks.append(circle)

    return marks


# ---------------------------------------------------------------------------
# Schnittrand (Beschnitt)
# ---------------------------------------------------------------------------

def create_bleed_border(props) -> bpy.types.Object | None:
    """
    Erstellt einen erhabenen Rand als Druckgrenze.
    """
    if not props.add_bleed_border:
        return None

    outer_w = mm(props.plate_width)
    outer_h = mm(props.plate_height)
    border   = mm(props.bleed_size)
    depth    = mm(props.relief_depth * 0.5)  # Halbe Reliefhöhe
    z        = mm(props.plate_thickness)

    bm = bmesh.new()

    def add_rect(w, h, z_off):
        verts = [
            bm.verts.new(( w/2,  h/2, z_off)),
            bm.verts.new((-w/2,  h/2, z_off)),
            bm.verts.new((-w/2, -h/2, z_off)),
            bm.verts.new(( w/2, -h/2, z_off)),
        ]
        return verts

    # Äußeres und inneres Rechteck → Rahmen
    outer_b = add_rect(outer_w, outer_h, 0)
    outer_t = add_rect(outer_w, outer_h, depth)
    inner_b = add_rect(outer_w - 2*border, outer_h - 2*border, 0)
    inner_t = add_rect(outer_w - 2*border, outer_h - 2*border, depth)

    bm.verts.ensure_lookup_table()

    # Seitenflächen
    sides = [(outer_b, outer_t), (inner_b, inner_t)]
    for bot, top in sides:
        n = len(bot)
        for j in range(n):
            bm.faces.new([bot[j], bot[(j+1)%n], top[(j+1)%n], top[j]])

    # Stirnflächen (Außen oben / Innen oben / Boden)
    bm.faces.new(outer_t)
    bm.faces.new(inner_t[::-1])

    # Verbindung Außen-Innen oben
    for j in range(4):
        bm.faces.new([outer_t[j], outer_t[(j+1)%4],
                      inner_t[(j+1)%4], inner_t[j]])

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    mesh = bpy.data.meshes.new("Beschnittrand_Mesh")
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new("Beschnittrand", mesh)
    obj.location = (0, 0, z)
    bpy.context.scene.collection.objects.link(obj)
    return obj


# ---------------------------------------------------------------------------
# Boolean: Relief in Platte einbetten (optional)
# ---------------------------------------------------------------------------

def merge_with_boolean(base: bpy.types.Object, relief_objs: list, props):
    """
    Vereint Grundplatte und Relief-Meshes mit Boolean UNION,
    um ein wasserdichtes Einzelobjekt für die CNC-Ausgabe zu erhalten.
    """
    if not props.use_boolean_merge or not relief_objs:
        return

    bpy.ops.object.select_all(action='DESELECT')
    base.select_set(True)
    bpy.context.view_layer.objects.active = base

    for rel in relief_objs:
        mod = base.modifiers.new(name="Bool_Relief", type='BOOLEAN')
        mod.operation = 'UNION'
        mod.object = rel
        mod.solver = 'FAST'

        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(rel, do_unlink=True)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_model(props, objects: list):
    """Exportiert alle Objekte als STL oder OBJ."""
    if not props.auto_export:
        return

    path = bpy.path.abspath(props.export_path)
    os.makedirs(path, exist_ok=True)

    basename = os.path.splitext(os.path.basename(props.svg_path))[0]
    filepath = os.path.join(path, f"{basename}_klischee")

    bpy.ops.object.select_all(action='DESELECT')
    for o in objects:
        if o and o.name in bpy.data.objects:
            o.select_set(True)

    if props.export_format == 'STL':
        bpy.ops.wm.stl_export(
            filepath=filepath + ".stl",
            export_selected_objects=True,
            ascii_format=False,
            apply_modifiers=True,
            global_scale=1.0,
        )
    elif props.export_format == 'OBJ':
        bpy.ops.wm.obj_export(
            filepath=filepath + ".obj",
            export_selected_objects=True,
            apply_modifiers=True,
            global_scale=1.0,
        )

    print(f"[Klischee] Exportiert nach: {filepath}")


# ---------------------------------------------------------------------------
# Hauptfunktion
# ---------------------------------------------------------------------------

def generate_klischee(props):
    """Vollständiger Workflow: SVG → Klischee-3D-Modell."""

    set_scene_units()
    clear_collection("Klischee")

    if not props.svg_path or not os.path.isfile(bpy.path.abspath(props.svg_path)):
        raise FileNotFoundError(f"SVG nicht gefunden: {props.svg_path}")

    print("[Klischee] Importiere SVG …")
    curves = import_svg(bpy.path.abspath(props.svg_path))

    if not curves:
        raise RuntimeError("Keine Kurvenobjekte im SVG gefunden.")

    print(f"[Klischee] {len(curves)} Kurvenobjekt(e) importiert.")

    print("[Klischee] Normalisiere Kurven …")
    motif_w, motif_h = normalize_curves(curves, props)

    if props.fit_plate_to_motif:
        print(f"[Klischee] Passe Platte an Motiv an: {motif_w:.2f} x {motif_h:.2f} mm …")
        props.plate_width  = motif_w + 2 * props.margin
        props.plate_height = motif_h + 2 * props.margin

    print("[Klischee] Erstelle Grundplatte …")
    base = create_base_plate(props)
    link_to_collection(base, "Klischee")

    print("[Klischee] Extrudiere Relief …")
    relief_objs = extrude_curves(curves, props)
    for r in relief_objs:
        mat = bpy.data.materials.new("Mat_Relief")
        mat.diffuse_color = (0.85, 0.85, 0.1, 1.0)  # Gelb/Gold für Relief
        r.data.materials.append(mat)
        link_to_collection(r, "Klischee")

    print("[Klischee] Passermarken …")
    marks = create_register_marks(props)
    for m in marks:
        link_to_collection(m, "Klischee")

    print("[Klischee] Beschnittrand …")
    border = create_bleed_border(props)
    if border:
        link_to_collection(border, "Klischee")

    if props.use_boolean_merge:
        print("[Klischee] Boolean Merge …")
        merge_with_boolean(base, relief_objs, props)
        all_objects = [base] + marks + ([border] if border else [])
    else:
        all_objects = [base] + relief_objs + marks + ([border] if border else [])

    print("[Klischee] Export …")
    export_model(props, all_objects)

    # Kamera auf Klischee ausrichten
    bpy.ops.object.select_all(action='DESELECT')
    base.select_set(True)
    bpy.ops.view3d.camera_to_view_selected()

    print("[Klischee] ✓ Fertig!")
    return all_objects


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class KlischeeProperties(PropertyGroup):

    svg_path: StringProperty(
        name="SVG-Datei",
        description="Pfad zur SVG-Vorlage",
        subtype='FILE_PATH',
        default=""
    )

    # --- Plattengeometrie ---
    plate_width: FloatProperty(
        name="Breite (mm)",
        description="Breite der Klischeeplatte",
        default=100.0, min=10.0, max=260.0,
        unit='LENGTH', subtype='DISTANCE'
    )
    plate_height: FloatProperty(
        name="Höhe (mm)",
        description="Höhe der Klischeeplatte",
        default=70.0, min=10.0, max=185.0,
        unit='LENGTH', subtype='DISTANCE'
    )
    plate_thickness: FloatProperty(
        name="Trägerdicke (mm)",
        description="Dicke der Trägerplatte (ohne Relief). "
                    "Träger + Relief = Typenhöhe 23.566 mm",
        default=0.25, min=0.1, max=23.5,
        unit='LENGTH', subtype='DISTANCE'
    )
    relief_depth: FloatProperty(
        name="Reliefhöhe (mm)",
        description="Höhe des erhabenen Druckrelief",
        default=0.96, min=0.1, max=3.0,
        unit='LENGTH', subtype='DISTANCE'
    )
    margin: FloatProperty(
        name="Randabstand (mm)",
        description="Mindestabstand vom SVG-Motiv zum Plattenrand",
        default=5.0, min=0.0, max=30.0,
        unit='LENGTH', subtype='DISTANCE'
    )

    # --- SVG-Skalierung ---
    fit_to_plate: BoolProperty(
        name="Motiv einpassen",
        description="SVG automatisch auf Druckfläche skalieren",
        default=False
    )

    # --- Extras ---
    mirror_motif: BoolProperty(
        name="Motiv spiegeln (Druckkorrektur)",
        description="Spiegelt das Motiv auf der X-Achse. "
                    "Pflicht für Hochdruck: das Klischee muss seitenverkehrt sein, "
                    "damit der Druck lesbar wird",
        default=True
    )
    use_taper_base: BoolProperty(
        name="Konische Basis",
        description="Das Relief wird zur Trägerplatte hin konisch breiter (Trapezprofil). "
                    "Die Druckfläche oben entspricht exakt dem SVG-Motiv; "
                    "unten ist das Profil breiter für bessere Standfestigkeit beim Druck",
        default=True
    )
    taper_amount: FloatProperty(
        name="Konizität (mm)",
        description="Wie viel breiter die Basis gegenüber der Druckfläche ist "
                    "(seitlicher Versatz pro Seite an der Wurzel des Reliefs)",
        default=0.5, min=0.01, max=2.0,
        unit='LENGTH', subtype='DISTANCE'
    )
    add_register_marks: BoolProperty(
        name="Passermarken",
        description="Kreuz-Passermarken in den Ecken hinzufügen",
        default=False
    )
    add_bleed_border: BoolProperty(
        name="Beschnittrand",
        description="Erhabenen Druckbegrenzungsrahmen hinzufügen",
        default=False
    )
    bleed_size: FloatProperty(
        name="Randbreite (mm)",
        default=2.0, min=0.5, max=10.0,
        unit='LENGTH', subtype='DISTANCE'
    )

    fit_plate_to_motif: BoolProperty(
        name="Platte an Motiv anpassen",
        description="Plattenbreite und -höhe werden automatisch auf die "
                    "Motivgrösse + Randabstand gesetzt",
        default=False
    )

    # --- Verarbeitung ---
    use_boolean_merge: BoolProperty(
        name="Boolean-Merge",
        description="Relief und Platte zu einem Mesh verschmelzen (langsamer, "
                    "aber für CNC empfohlen)",
        default=False
    )

    # --- Export ---
    auto_export: BoolProperty(
        name="Automatisch exportieren",
        description="Nach der Generierung exportieren",
        default=False
    )
    export_path: StringProperty(
        name="Export-Ordner",
        subtype='DIR_PATH',
        default="//"
    )
    export_format: EnumProperty(
        name="Format",
        items=[
            ('STL', "STL", "Für 3D-Druck / CNC"),
            ('OBJ', "OBJ", "Mit UV und Material"),
        ],
        default='STL'
    )


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class KLISCHEE_OT_generate(Operator):
    bl_idname = "klischee.generate"
    bl_label = "Klischee generieren"
    bl_description = "SVG importieren und Klischee-3D-Modell erstellen"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.klischee_props
        try:
            generate_klischee(props)
            self.report({'INFO'}, "Klischee erfolgreich erstellt!")
        except FileNotFoundError as e:
            self.report({'ERROR'}, f"SVG nicht gefunden: {e}")
            return {'CANCELLED'}
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Unerwarteter Fehler: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
        return {'FINISHED'}


class KLISCHEE_OT_set_typhoehe(Operator):
    bl_idname = "klischee.set_typhoehe"
    bl_label = "Typenhöhe setzen"
    bl_description = "Trägerdicke automatisch auf DIN-Typenhöhe berechnen"

    def execute(self, context):
        props = context.scene.klischee_props
        props.plate_thickness = TYPHOEHE_MM - props.relief_depth
        self.report({'INFO'},
            f"Trägerdicke = {props.plate_thickness:.3f} mm "
            f"(Typenhöhe {TYPHOEHE_MM} mm − Relief {props.relief_depth} mm)")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class KLISCHEE_PT_main(Panel):
    bl_label = "Heidelberg Tiegel – Klischee"
    bl_idname = "KLISCHEE_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Klischee"

    def draw(self, context):
        layout = self.layout
        props = context.scene.klischee_props

        # SVG
        box = layout.box()
        box.label(text="SVG-Vorlage", icon='FILE_IMAGE')
        box.prop(props, "svg_path")

        # Platte
        box = layout.box()
        box.label(text="Plattengeometrie", icon='CUBE')
        col = box.column(align=True)
        col.prop(props, "plate_width")
        col.prop(props, "plate_height")
        col.separator()
        col.prop(props, "plate_thickness")
        col.prop(props, "relief_depth")
        col.separator()
        row = col.row()
        row.label(text=f"Typenhöhe: {props.plate_thickness + props.relief_depth:.3f} mm")
        row = col.row()
        row.alert = abs((props.plate_thickness + props.relief_depth) - TYPHOEHE_MM) > 0.01
        row.operator("klischee.set_typhoehe", icon='DRIVER_DISTANCE')
        col.prop(props, "margin")

        # SVG-Skalierung
        box = layout.box()
        box.label(text="SVG-Skalierung", icon='FULLSCREEN_ENTER')
        box.prop(props, "fit_to_plate")

        # Extras
        box = layout.box()
        box.label(text="Extras", icon='TOOL_SETTINGS')
        box.prop(props, "mirror_motif")
        box.separator()
        box.prop(props, "use_taper_base")
        if props.use_taper_base:
            box.prop(props, "taper_amount")
        box.separator()
        box.prop(props, "fit_plate_to_motif")
        box.prop(props, "add_register_marks")
        box.prop(props, "add_bleed_border")
        if props.add_bleed_border:
            box.prop(props, "bleed_size")
        box.prop(props, "use_boolean_merge")

        # Export
        box = layout.box()
        box.label(text="Export", icon='EXPORT')
        box.prop(props, "auto_export")
        if props.auto_export:
            box.prop(props, "export_path")
            box.prop(props, "export_format")

        # Hinweis Typenhöhe
        info = layout.box()
        info.label(text=f"ℹ  Typenhöhe: {props.plate_thickness + props.relief_depth:.3f} mm  (Soll: {TYPHOEHE_MM} mm)", icon='INFO')
        info.label(text=f"   Max. Format GT52: 260 × 185 mm")

        layout.separator()
        layout.operator("klischee.generate",
                        text="▶  Klischee generieren",
                        icon='SHADERFX')


# ---------------------------------------------------------------------------
# Registrierung
# ---------------------------------------------------------------------------

classes = [
    KlischeeProperties,
    KLISCHEE_OT_generate,
    KLISCHEE_OT_set_typhoehe,
    KLISCHEE_PT_main,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.klischee_props = bpy.props.PointerProperty(
        type=KlischeeProperties
    )
    print("[Klischee] Add-on registriert. Panel: N-Panel → 'Klischee'")


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.klischee_props


if __name__ == "__main__":
    register()
