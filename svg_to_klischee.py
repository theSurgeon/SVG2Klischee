bl_info = {
    "name": "SVG → Klischee Generator",
    "author": "SVG2Klischee",
    "version": (1, 1, 0),
    "blender": (3, 0, 0),
    "location": "View3D › N-Panel › Klischee",
    "description": "Erzeugt ein druckfähiges 3D-Klischee für den Heidelberg-Tiegel aus einer SVG-Datei",
    "category": "Import-Export",
}

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
import re
import math
import tempfile
import xml.dom.minidom
import mathutils
from mathutils import Vector
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


def _read_length(raw):
    """'40mm' → (40.0, 'mm'); '120' → (120.0, ''); ungültig → (None, '')."""
    m = re.match(r'\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z%]*)\s*$', raw or '')
    return (float(m.group(1)), m.group(2).lower()) if m else (None, '')


def svg_scale_mm_per_bu(filepath: str) -> float:
    """
    Faktor, mit dem die Koordinaten der importierten Kurven (Blender-Einheiten,
    BU) in Millimeter umgerechnet werden.

    Blenders SVG-Importer rechnet je nach Attributen des <svg>-Elements anders
    (90 dpi in Metern, Einheit der Höhe, viewBox auf Breite/Höhe abgebildet …).
    Statt das nachzubauen, wird gemessen: Ein Referenz-Quadrat von 1000 SVG-
    Einheiten mit denselben <svg>-Attributen wird importiert und vermessen.

    Massgebend für die echte Grösse ist wie in der Web-Version (svgMmPerUnit):
    Breite mit physischer Einheit + viewBox → Breite_mm / viewBox-Breite,
    sonst 96 dpi (25.4/96 mm pro Einheit).
    """
    root = xml.dom.minidom.parse(filepath).documentElement

    # Was der Importer aus einer SVG-Einheit macht (BU pro Einheit), gemessen
    ref = xml.dom.minidom.Document()
    svg = ref.createElement('svg')
    for k, v in root.attributes.items():
        svg.setAttribute(k, v)
    path = ref.createElement('path')
    path.setAttribute('d', 'M0 0 L1000 0 L1000 1000 L0 1000 Z')
    svg.appendChild(path)
    ref.appendChild(svg)
    with tempfile.TemporaryDirectory() as tmp:
        ref_file = os.path.join(tmp, 'klischee_referenz.svg')
        with open(ref_file, 'w', encoding='utf-8') as f:
            f.write(ref.toxml())
        ref_curves = import_svg(ref_file)
    xs = [(c.matrix_world @ bp.co).x for c in ref_curves for s in c.data.splines for bp in s.bezier_points]
    ref_cols = {col for c in ref_curves for col in c.users_collection}
    for c in ref_curves:
        data = c.data
        bpy.data.objects.remove(c, do_unlink=True)
        if data.users == 0:
            bpy.data.curves.remove(data)
    for col in ref_cols:
        if col != bpy.context.scene.collection and not col.objects and not col.children:
            bpy.data.collections.remove(col)
    if len(xs) < 2 or max(xs) - min(xs) <= 0:
        raise RuntimeError("SVG-Massstab konnte nicht bestimmt werden (Referenz-Import fehlgeschlagen).")
    bu_per_unit = (max(xs) - min(xs)) / 1000.0

    # Was es physisch sein soll (mm pro SVG-Einheit)
    to_mm = {'mm': 1.0, 'cm': 10.0, 'in': 25.4, 'pt': 25.4 / 72, 'pc': 25.4 / 6}
    vb = [float(v) for v in re.split(r'[\s,]+', root.getAttribute('viewBox').strip()) if v] \
        if root.getAttribute('viewBox') else []
    mm_per_unit = 25.4 / 96.0
    w_val, w_unit = _read_length(root.getAttribute('width'))
    if w_val and w_unit in to_mm and len(vb) == 4 and vb[2] > 0:
        mm_per_unit = w_val * to_mm[w_unit] / vb[2]

    return mm_per_unit / bu_per_unit


def curve_loops(curve_obj, resolution: int = 24) -> list:
    """
    Geschlossene Umrisse eines importierten Kurvenobjekts als Punktlisten
    (x, y) in BU (Weltkoordinaten). Kurven werden mit `resolution` Teilungen
    je Segment ausgewertet (wie getPoints(24) in der Web-Version), gerade
    Segmente liefert Blender ohne Zwischenpunkte.
    """
    cu = curve_obj.data
    cu.dimensions = '2D'
    cu.fill_mode = 'NONE'
    cu.extrude = 0.0
    cu.bevel_depth = 0.0
    cu.resolution_u = resolution
    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev = curve_obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    mw = curve_obj.matrix_world
    co = [(mw @ v.co).to_2d() for v in me.vertices]
    nbr = [[] for _ in co]
    for e in me.edges:
        a, b = e.vertices
        nbr[a].append(b)
        nbr[b].append(a)
    ev.to_mesh_clear()

    loops, seen = [], [False] * len(co)
    for start in range(len(co)):
        if seen[start] or len(nbr[start]) != 2:
            continue
        loop, prev, cur = [], -1, start
        while not seen[cur]:
            seen[cur] = True
            loop.append((co[cur].x, co[cur].y))
            nxt = [v for v in nbr[cur] if v != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def _signed_area(pts) -> float:
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                     for i in range(len(pts)))


def _point_in_polygon(p, poly) -> bool:
    x, y, inside = p[0], p[1], False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def group_shapes(loops: list) -> list:
    """
    Ordnet die Umrisse eines Kurvenobjekts zu Formen (Aussenkontur + Löcher)
    nach der Gerade-ungerade-Regel wie Blenders Füllung: Ein Umriss in einer
    ungeraden Zahl anderer Umrisse ist ein Loch der kleinsten umgebenden Kontur.
    """
    depth = [sum(1 for j, o in enumerate(loops) if j != i and _point_in_polygon(l[0], o))
             for i, l in enumerate(loops)]
    shapes = {i: [] for i, dp in enumerate(depth) if dp % 2 == 0}
    for i, dp in enumerate(depth):
        if dp % 2 == 1:
            parents = [j for j in shapes if depth[j] == dp - 1 and _point_in_polygon(loops[i][0], loops[j])]
            if parents:
                shapes[min(parents, key=lambda j: abs(_signed_area(loops[j])))].append(loops[i])
    return [(loops[i], holes) for i, holes in shapes.items()]


def _drop_collinear(pts) -> list:
    """Entfernt exakt kollineare Zwischenpunkte (gerade Segmente) – Geometrie bleibt gleich."""
    out = list(pts)
    changed = True
    while changed and len(out) > 3:
        changed = False
        for i in range(len(out)):
            a, p, b = out[i - 1], out[i], out[(i + 1) % len(out)]
            ux, uy, vx, vy = p[0] - a[0], p[1] - a[1], b[0] - p[0], b[1] - p[1]
            if abs(ux * vy - uy * vx) <= 1e-12 * (math.hypot(ux, uy) * math.hypot(vx, vy) + 1e-30) \
                    and ux * vx + uy * vy > 0:
                del out[i]
                changed = True
                break
    return out


def build_relief_mesh(name: str, outer: list, holes: list, z_bottom: float, z_top: float,
                      taper: float, mirror: bool, warnings: list):
    """
    Baut ein konisches Relief (Trapezprofil) als geschlossenes Mesh, wie
    buildTaperedPrism in der Web-Version:
      Druckfläche (z_top):    Kontur exakt wie im SVG (bis auf ≤ 7 µm Bereinigung)
      Basis       (z_bottom): Aussenkontur um `taper` gewachsen, Löcher geschrumpft
    Koordinaten in mm. Gibt das Objekt zurück oder None, wenn nach dem
    Bereinigen keine Fläche bleibt (Staub unter 5 µm).
    """
    outer = clean_contour(_drop_collinear(outer))
    holes = [h for h in (clean_contour(_drop_collinear(h)) for h in holes) if len(h) >= 3]
    if len(outer) < 3:
        return None

    # Einheitlicher Umlaufsinn: aussen CCW, Löcher CW – das Material liegt dann
    # immer links der Laufrichtung, die Wandnormale zeigt nach rechts.
    if _signed_area(outer) < 0:
        outer.reverse()
    holes = [h if _signed_area(h) < 0 else h[::-1] for h in holes]

    tops = [outer] + holes
    bases = [offset_contour(outer, +taper, warnings)] + [offset_contour(h, -taper, warnings) for h in holes]
    flat_top = [p for loop in tops for p in loop]
    flat_base = [p for loop in bases for p in loop]

    area2 = lambda p, q, r: (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def triangulate(loops):
        tris = mathutils.geometry.tessellate_polygon([[Vector((x, y, 0.0)) for x, y in l] for l in loops])
        flat = [p for l in loops for p in l]
        # einheitlich CCW (Normale +Z); Dreiecke ohne Fläche behalten ihre Reihenfolge
        return [(a, b, c) if area2(flat[a], flat[b], flat[c]) >= 0 else (a, c, b) for a, b, c in tris]

    tris = triangulate(tops)
    # Basis: Dreiecke der Druckfläche wiederverwenden (gleiche Punkte wie die Wände).
    # Klappt dabei eines um (enge Spalten an der Basis zugelaufen), Basis separat
    # triangulieren. Ein Druckflächen-Dreieck ohne Fläche wird gegen den
    # Umlaufsinn aller Dreiecke geprüft.
    sg_all = math.copysign(1.0, sum(area2(flat_top[a], flat_top[b], flat_top[c]) for a, b, c in tris) or 1.0)

    def base_flipped(a, b, c):
        at = area2(flat_top[a], flat_top[b], flat_top[c])
        sg = 1.0 if at > 1e-12 else (-1.0 if at < -1e-12 else sg_all)
        return area2(flat_base[a], flat_base[b], flat_base[c]) * sg < -1e-12
    tris_base = triangulate(bases) if any(base_flipped(*t) for t in tris) else tris

    bm = bmesh.new()
    sx = -1.0 if mirror else 1.0
    top_v = [bm.verts.new((sx * x, y, z_top)) for x, y in flat_top]
    base_v = [bm.verts.new((sx * x, y, z_bottom)) for x, y in flat_base]

    faces = []
    for a, b, c in tris:                                   # Druckfläche, Normale +Z
        faces.append((top_v[a], top_v[b], top_v[c]))
    for a, b, c in tris_base:                              # Basis, Normale −Z
        faces.append((base_v[a], base_v[c], base_v[b]))
    off = 0
    for loop in tops:                                      # Wände, Diagonale b0–t1
        n = len(loop)
        for i in range(n):
            j = (i + 1) % n
            t0, t1, b0, b1 = top_v[off + i], top_v[off + j], base_v[off + i], base_v[off + j]
            faces.append((t0, b0, t1))
            faces.append((t1, b0, b1))
        off += n

    for f in faces:
        f = f[::-1] if mirror else f                       # Spiegeln kehrt die Wicklung um
        if bm.faces.get(f) is None:                        # dasselbe Dreieck nur einmal anlegen
            bm.faces.new(f)

    me = bpy.data.meshes.new(f"{name}_Mesh")
    bm.to_mesh(me)
    bm.free()
    me.update()
    obj = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(obj)
    return obj


# ---------------------------------------------------------------------------
# Taper: Basis-Kontur (1:1-Port von cleanContour/offsetContour aus
# svg2klischee_web.html – gleiche Schritte, gleiche Schwellen, damit Web und
# Blender dasselbe Relief bauen). Punkte sind (x, y)-Tupel in mm.
# ---------------------------------------------------------------------------

def clean_contour(pts):
    """
    Bereinigt eine Kontur, bevor Druckfläche UND Basis daraus gebaut werden
    (beide nutzen dieselben Punkte): Folgepunkte näher als MERGE (5 µm) zum
    zuletzt behaltenen Punkt fallen weg. Ein Rundungsfehler am Schlusspunkt
    bildet sonst eine winzige Schlaufe, die sich schon in der Druckfläche kreuzt.
    Die Kontur weicht höchstens MERGE·√2 (≈ 7 µm) von der Eingabe ab, am
    Konturende im ungünstigsten Fall doppelt so viel – nie sichtbar.
    """
    MERGE = 5e-3                                              # mm
    close = lambda a, b: abs(a[0] - b[0]) < MERGE and abs(a[1] - b[1]) < MERGE
    P = []
    for p in pts:
        if not P or not close(p, P[-1]):
            P.append((p[0], p[1]))
    while len(P) > 1 and close(P[-1], P[0]):
        P.pop()
    return P


def offset_contour(pts, offset, warnings, miter_limit=4.0):
    """
    Basis-Kontur (z=0) für den Taper.

    Jede Kante der Druckfläche wird um genau |offset| senkrecht verschoben,
    damit jede Seitenwand denselben Anzug hat und eben ist. Die Ecken liegen
    auf dem Schnittpunkt der beiden verschobenen Nachbarkanten (Gehrung).
    Rückgabe: genau len(pts) Punkte, base[i] gehört zu pts[i] (1:1).
      offset > 0 → weg vom eigenen Innenraum (Aussenkontur wächst)
      offset < 0 → in den eigenen Innenraum (Loch schrumpft)

    Enge Stellen (Spalt schmaler als 2·|offset|) werden als Wellenfront
    gerechnet (gerades Skelett): zugelaufene Kanten verschmelzen, kollidierende
    Teile halten kurz vor der Berührung an, eine Kontur, die ganz zuliefe oder
    sich umstülpte, hält kurz davor an. Hinweise landen in `warnings`.
    Ausführlich kommentiert in svg2klischee_web.html (offsetContour).
    """
    INF = float('inf')
    n = len(pts)
    d = abs(offset)
    copy = lambda: [(p[0], p[1]) for p in pts]
    if n < 3 or not (d > 0):
        return copy()

    EPS  = 1e-9          # mm: Punkte gelten als gleich
    BACK = 0.02 * d      # so viel früher anhalten als die Berührung (Zeit = mm Versatz)
    GMIN = 0.05 * d      # Mindestanzug einer Fächerwand beim Anhalten

    # ── 1. Doppelte Folgepunkte zusammenfassen
    U = []
    uOf = [0] * n
    same = lambda a, b: abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS
    for i in range(n):
        if U and same(pts[i], U[-1]):
            uOf[i] = len(U) - 1
            continue
        U.append((pts[i][0], pts[i][1]))
        uOf[i] = len(U) - 1
    while len(U) > 1 and same(U[-1], U[0]):
        last = len(U) - 1
        for i in range(n):
            if uOf[i] == last:
                uOf[i] = 0
        U.pop()
    m = len(U)
    if m < 3:
        return copy()

    # ── 2. Umlaufsinn, Kantenrichtungen, freie Seite
    area = 0.0
    for k in range(m):
        a, b = U[k], U[(k + 1) % m]
        area += a[0] * b[1] - b[0] * a[1]
    if abs(area) < EPS:
        return copy()
    side = (1 if area > 0 else -1) * (1 if offset > 0 else -1)

    ex = [0.0] * m; ey = [0.0] * m; nx = [0.0] * m; ny = [0.0] * m
    for k in range(m):
        a, b = U[k], U[(k + 1) % m]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        ex[k] = (b[0] - a[0]) / L; ey[k] = (b[1] - a[1]) / L
        nx[k] = side * ey[k];      ny[k] = -side * ex[k]
    turn = [0.0] * m
    for k in range(m):
        i = (k - 1 + m) % m
        turn[k] = side * math.atan2(ex[i] * ey[k] - ey[i] * ex[k], ex[i] * ex[k] + ey[i] * ey[k])

    # ── 3. Wellenfront bis Zeit T (= Versatz in mm)
    def simulate(T, freezeAt):
        px = [p[0] for p in U]; py = [p[1] for p in U]; tp = [0.0] * m
        vx = [0.0] * m; vy = [0.0] * m
        prev = [(k - 1 + m) % m for k in range(m)]; nxt = [(k + 1) % m for k in range(m)]
        eIn = list(prev); eOut = list(range(m)); parent = list(range(m))
        absd = [[] for _ in range(m)]
        tn = list(turn); frz = list(freezeAt); ver = [0] * m
        alive = [True] * m
        st = {'count': m, 't': 0.0}
        X = lambda k, s: px[k] + vx[k] * (s - tp[k])
        Y = lambda k, s: py[k] + vy[k] * (s - tp[k])

        # Binärer Heap wie im Web: [Zeit, Typ, Knoten, Version, Nachbar, Version];
        # bei gleicher Zeit zuerst Verschmelzen (Typ 0), dann Anhalten (Typ 1/2)
        heap = []
        before = lambda a, b: a[0] < b[0] or (a[0] == b[0] and a[1] < b[1])

        def push(e):
            heap.append(e)
            i = len(heap) - 1
            while i > 0:
                p = (i - 1) >> 1
                if not before(heap[i], heap[p]):
                    break
                heap[p], heap[i] = heap[i], heap[p]; i = p

        def pop():
            top = heap[0]; last = heap.pop()
            if heap:
                heap[0] = last
                i = 0
                while True:
                    l = 2 * i + 1; r = l + 1; s = i
                    if l < len(heap) and before(heap[l], heap[s]): s = l
                    if r < len(heap) and before(heap[r], heap[s]): s = r
                    if s == i: break
                    heap[s], heap[i] = heap[i], heap[s]; i = s
            return top

        def set_velocity(k):
            t = st['t']
            px[k] = X(k, t); py[k] = Y(k, t); tp[k] = t; ver[k] += 1
            if t >= frz[k]:
                vx[k] = 0.0; vy[k] = 0.0; return
            a, b = eIn[k], eOut[k]
            c = 1 + nx[a] * nx[b] + ny[a] * ny[b]
            if c > 1e-12:
                ux = (nx[a] + nx[b]) / c; uy = (ny[a] + ny[b]) / c        # exakte Gehrung
            elif tn[k] > 0:
                ux = ex[a]; uy = ey[a]                                    # Haarnadel-Spitze
            else:
                vx[k] = 0.0; vy[k] = 0.0; return                          # zugelaufener Schlitz
            ln = math.hypot(ux, uy)
            if tn[k] > 0 and (c <= 1e-12 or ln > miter_limit):
                ux *= miter_limit / ln; uy *= miter_limit / ln
            vx[k] = ux; vy[k] = uy

        def schedule_edge(a, b):
            t = st['t']; e = eOut[a]
            L = (X(b, t) - X(a, t)) * ex[e] + (Y(b, t) - Y(a, t)) * ey[e]
            rate = (vx[a] - vx[b]) * ex[e] + (vy[a] - vy[b]) * ey[e]
            if L <= 1e-9:
                push([t, 0, a, ver[a], b, ver[b]])
            elif rate > 1e-12:
                push([t + L / rate, 0, a, ver[a], b, ver[b]])

        def schedule_node(k):
            t = st['t']
            if t < frz[k] < INF:
                push([frz[k], 1, k, ver[k], -1, 0])
            tv = INF
            for c in absd[k]:
                r = vx[k] * nx[c] + vy[k] * ny[c]
                if r >= -1e-12:
                    continue
                g = (px[k] - U[c][0]) * nx[c] + (py[k] - U[c][1]) * ny[c]
                tv = min(tv, t + max(0.0, (g - GMIN) / -r))
            if tv < INF:
                push([tv, 2, k, ver[k], -1, 0])

        def merge(k, j, qx, qy):
            absd[k].append(eOut[k])
            absd[k].extend(absd[j])
            eOut[k] = eOut[j]; nxt[k] = nxt[j]; prev[nxt[j]] = k
            tn[k] += tn[j]; frz[k] = min(frz[k], frz[j])
            alive[j] = False; parent[j] = k; ver[j] += 1; st['count'] -= 1
            px[k] = qx; py[k] = qy; tp[k] = st['t']; vx[k] = 0.0; vy[k] = 0.0
            return k

        def settle(k):
            set_velocity(k)
            schedule_edge(prev[k], k); schedule_edge(k, nxt[k]); schedule_node(k)

        for k in range(m):
            set_velocity(k)
        for k in range(m):
            if alive[k]:
                schedule_edge(k, nxt[k]); schedule_node(k)

        while heap and st['count'] > 2:
            te, typ, k, vk, j, vj = pop()
            if te > T:
                break
            if not alive[k] or ver[k] != vk:
                continue
            if typ == 0 and (not alive[j] or ver[j] != vj or nxt[k] != j):
                continue
            st['t'] = max(st['t'], te)
            t = st['t']
            if typ == 0:
                qx = (X(k, t) + X(j, t)) / 2; qy = (Y(k, t) + Y(j, t)) / 2
                if vx[k] == 0 and vy[k] == 0:
                    qx = X(k, t); qy = Y(k, t)
                elif vx[j] == 0 and vy[j] == 0:
                    qx = X(j, t); qy = Y(j, t)
                settle(merge(k, j, qx, qy))
            else:
                if typ == 2:
                    frz[k] = t
                settle(k)

        t = st['t']; count = st['count']
        tEnd = T if count > 2 else t
        if count <= 2:
            ids = [k for k in range(m) if alive[k]]
            qx = sum(X(k, t) for k in ids) / len(ids)
            qy = sum(Y(k, t) for k in ids) / len(ids)
            for k in ids:
                px[k] = qx; py[k] = qy; vx[k] = 0.0; vy[k] = 0.0; tp[k] = t; parent[k] = ids[0]
            parent[ids[0]] = ids[0]

        def find(k):
            while parent[k] != k:
                k = parent[k]
            return k
        P = []
        stop = []
        for k in range(m):
            r = find(k)
            P.append((X(r, tEnd), Y(r, tEnd)))
            stop.append(min(tEnd, frz[r]))
        return {'P': P, 'stop': stop, 'vanished': count <= 2, 'tVanish': t}

    # ── 4. Kreuzungstest (Sweep, nur echte Kreuzungen / Überlappungen)
    def seg_cross(a, b, c, e):
        rx = b[0] - a[0]; ry = b[1] - a[1]; sx = e[0] - c[0]; sy = e[1] - c[1]
        qx = c[0] - a[0]; qy = c[1] - a[1]
        rr = math.hypot(rx, ry); ss = math.hypot(sx, sy)
        den = rx * sy - ry * sx
        if abs(den) <= 1e-12 * rr * ss:
            if abs(qx * ry - qy * rx) > EPS * rr:
                return False                                          # parallel, nicht kollinear
            t0 = (qx * rx + qy * ry) / (rr * rr)
            t1 = t0 + (sx * rx + sy * ry) / (rr * rr)
            return (min(1, max(t0, t1)) - max(0, min(t0, t1))) * rr > EPS
        t = (qx * sy - qy * sx) / den; u = (qx * ry - qy * rx) / den
        return 1e-9 < t < 1 - 1e-9 and 1e-9 < u < 1 - 1e-9

    def crossings(P, first_only):
        k = len(P); segs = []; out = []
        for i in range(k):
            a, b = P[i], P[(i + 1) % k]
            if same(a, b):
                continue
            segs.append([i, a, b, min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])])
        # Sweep entlang der Achse mit der kleineren Gesamtausdehnung (nur Boxen tauschen)
        sx = sum(g[4] - g[3] for g in segs); sy = sum(g[6] - g[5] for g in segs)
        if sy < sx:
            for g in segs:
                g[3], g[5] = g[5], g[3]; g[4], g[6] = g[6], g[4]
        segs.sort(key=lambda g: g[3])
        for s in range(len(segs)):
            A = segs[s]
            u = s + 1
            while u < len(segs) and segs[u][3] <= A[4] + EPS:
                B = segs[u]; u += 1
                if B[5] > A[6] + EPS or B[6] < A[5] - EPS:
                    continue
                if seg_cross(A[1], A[2], B[1], B[2]):
                    out.append((A[0], B[0]))
                    if first_only:
                        return out
        return out

    # ── 5. Kollisionen nicht benachbarter Teile: Beteiligte vor der Berührung anhalten
    freezeAt = [INF] * m
    pair_key = lambda c: min(c) * m + max(c)
    cross0 = {pair_key(c) for c in crossings(U, False)}

    def new_crossings(P, first_only):
        if not cross0:
            return crossings(P, first_only)
        r = [c for c in crossings(P, False) if pair_key(c) not in cross0]
        return r[:1] if first_only else r

    def flipped(P):
        s = 0.0
        for k in range(m):
            a, b = P[k], P[(k + 1) % m]
            s += a[0] * b[1] - b[0] * a[1]
        return s * area <= 0

    st5 = {'D': d}
    B = simulate(st5['D'], freezeAt)
    if not B['vanished'] and flipped(B['P']):
        lo, hi = 0.0, st5['D']
        for _ in range(30):
            mid = (lo + hi) / 2; Bm = simulate(mid, freezeAt)
            if Bm['vanished'] or flipped(Bm['P']):
                hi = mid
            else:
                lo = mid
        B = {'vanished': True, 'tVanish': hi}
    if B['vanished']:
        st5['D'] = max(0.0, B['tVanish'] - BACK); B = simulate(st5['D'], freezeAt)

    def bisect_round():
        D = st5['D']; lo, hi = 0.0, D
        for _ in range(14):
            mid = (lo + hi) / 2
            if new_crossings(simulate(mid, freezeAt)['P'], True):
                hi = mid
            else:
                lo = mid
        t_stop = max(0.0, lo - BACK)
        for (i, k) in new_crossings(simulate(hi, freezeAt)['P'], False):
            for v in (i, i + 1, k, k + 1):
                freezeAt[v % m] = min(freezeAt[v % m], t_stop)
        return simulate(D, freezeAt)

    it = 0
    while it < 20 and new_crossings(B['P'], True):
        B = bisect_round(); it += 1
    # Viele Berührungen (Schreibschrift): Rückfall auf groben Vorlauf in 32 Zeitschritten
    if new_crossings(B['P'], True):
        D = st5['D']
        for s in range(1, 33):
            ts = D * s / 32; t_stop = max(0.0, D * (s - 1) / 32 - BACK)
            w = 0
            for _rep in range(8):
                changed = False
                for (i, k) in new_crossings(simulate(ts, freezeAt)['P'], False):
                    for c in (i, k):
                        for v in range(c - w, c + 2 + w):
                            q = v % m
                            if freezeAt[q] > t_stop:
                                freezeAt[q] = t_stop; changed = True
                if not changed:
                    break
                w = 2 * w + 1
        B = simulate(D, freezeAt)
    while it < 32 and new_crossings(B['P'], True):
        B = bisect_round(); it += 1

    # ── 6. Endkontrolle am fertigen Netz, lokale Korrektur
    def problems(Bs):
        bad = set()
        for k in range(m):
            j = (k + 1) % m; T0, T1, B0, B1 = U[k], U[j], Bs[k], Bs[j]
            ok = ((B0[0] - T0[0]) * nx[k] + (B0[1] - T0[1]) * ny[k] >= -1e-9 and
                  (B1[0] - T1[0]) * nx[k] + (B1[1] - T1[1]) * ny[k] >= -1e-9)
            fx = B1[0] - B0[0]; fy = B1[1] - B0[1]; fl = math.hypot(fx, fy)
            if ok and fl >= EPS:                                       # sonst Fächerdreieck
                ok = (fx * ex[k] + fy * ey[k] >= 0 and
                      (B0[0] - T1[0]) * side * fy - (B0[1] - T1[1]) * side * fx >= -1e-9 * fl)
            if not ok:
                bad.add(k); bad.add(j)
        for s in range(1, 9):
            lam = s / 8; S = []
            for k in range(m):
                j = (k + 1) % m
                S.append((U[k][0] + lam * (Bs[k][0] - U[k][0]), U[k][1] + lam * (Bs[k][1] - U[k][1])))
                S.append((U[j][0] + lam * (Bs[k][0] - U[j][0]), U[j][1] + lam * (Bs[k][1] - U[j][1])))
            for (a, b) in crossings(S, False):
                for w in (a >> 1, b >> 1):
                    bad.add(w); bad.add((w + 1) % m)
            sa = 0.0
            for q in range(len(S)):
                p, r = S[q], S[(q + 1) % len(S)]
                sa += p[0] * r[1] - r[0] * p[1]
            if sa * area < 0 and abs(sa) > EPS:
                bad.update(range(m))
        if flipped(Bs):
            bad.update(range(m))                                       # Basis gespiegelt
        return bad

    bad0 = problems(U)
    bad = problems(B['P'])
    Bp = B['P']
    for _ in range(40):
        if not bad:
            break
        for v in bad:
            freezeAt[v] = 0.6 * min(freezeAt[v], B['stop'][v])
        B = simulate(st5['D'], freezeAt); Bp = B['P']
        bad = problems(Bp)
    ramps = 0
    for k in range(m):
        j = (k + 1) % m
        if same(Bp[k], Bp[j]) and (Bp[k][0] - U[k][0]) * nx[k] + (Bp[k][1] - U[k][1]) * ny[k] > 2 * d:
            ramps += 1
    if ramps:
        warnings.append(f"Ein Spalt im Motiv ist schmaler als {2 * d:.2f} mm: Sein Boden steigt zum "
                        "engen Ende hin fast auf Druckhöhe an und kann Farbe annehmen. "
                        "Taper verkleinern oder Motiv prüfen.")
    if bad and all(v in bad0 for v in bad):
        warnings.append("Eine Kontur überschneidet sich schon in der Druckfläche (Schlaufe oder Steg "
                        "ohne Breite); dort sind die Wände senkrecht. Motiv prüfen.")
    elif bad:
        # Notbremse: Versatz dieser Kontur gleichmässig verkleinern, bis das Netz gültig ist
        full = Bp
        f = 1.0
        while True:
            f *= 0.8
            Bp = [(U[k][0] + f * (p[0] - U[k][0]), U[k][1] + f * (p[1] - U[k][1])) for k, p in enumerate(full)]
            if not (f > 1e-3 and problems(Bp)):
                break
        if f <= 1e-3:
            Bp = list(U)
        warnings.append("Eine Kontur liess sich nicht sauber mit Taper bauen; sie hat überall senkrechte "
                        "Wände. Motiv prüfen." if f <= 1e-3 else
                        f"Eine Kontur liess sich nur mit {f * d:.3f} mm statt {d} mm Taper sauber bauen; "
                        "ihre Wände sind überall steiler.")

    return [Bp[uOf[i]] for i in range(n)]


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
        mod.solver = 'EXACT'   # robust bei Überlappungen und gemeinsamen Flächen

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
    """
    Vollständiger Workflow: SVG → Klischee-3D-Modell.
    Gibt (Objekte, Hinweise) zurück; Hinweise stammen aus dem Taper (enge Spalten,
    verkleinerter Taper, sich selbst kreuzende Druckfläche).
    """

    set_scene_units()
    clear_collection("Klischee")

    if not props.svg_path or not os.path.isfile(bpy.path.abspath(props.svg_path)):
        raise FileNotFoundError(f"SVG nicht gefunden: {props.svg_path}")
    svg_file = bpy.path.abspath(props.svg_path)

    print("[Klischee] Importiere SVG …")
    mm_per_bu = svg_scale_mm_per_bu(svg_file)
    curves = import_svg(svg_file)

    if not curves:
        raise RuntimeError("Keine Kurvenobjekte im SVG gefunden.")

    print(f"[Klischee] {len(curves)} Kurvenobjekt(e) importiert.")

    # Umrisse in mm übernehmen, danach Kurven und Import-Collection entfernen
    shapes = []
    for c in curves:
        loops = [[(x * mm_per_bu, y * mm_per_bu) for x, y in l] for l in curve_loops(c)]
        shapes.extend(group_shapes(loops))
    import_cols = {col for c in curves for col in c.users_collection}
    for c in curves:
        data = c.data
        bpy.data.objects.remove(c, do_unlink=True)
        if data.users == 0:
            bpy.data.curves.remove(data)
    for col in import_cols:
        if col.name != "Klischee" and not col.objects and not col.children and col != bpy.context.scene.collection:
            bpy.data.collections.remove(col)
    if not shapes:
        raise RuntimeError("Das SVG enthält keine geschlossenen Umrisse.")

    # Motiv zentrieren, optional auf die Druckfläche einpassen
    xs = [p[0] for o, _ in shapes for p in o]
    ys = [p[1] for o, _ in shapes for p in o]
    motif_w, motif_h = max(xs) - min(xs), max(ys) - min(ys)
    if max(motif_w, motif_h) < 0.01:
        raise RuntimeError("Das Motiv enthält keine druckbaren Flächen (kleiner als 10 µm).")
    if max(motif_w, motif_h) > 2000.0:
        raise RuntimeError(f"Unplausible Motivgrösse {motif_w:.4f} × {motif_h:.4f} mm – "
                           "SVG-Einheiten prüfen (width/height/viewBox).")
    k = 1.0
    if props.fit_to_plate:
        k = min((props.plate_width - 2 * props.margin) / motif_w,
                (props.plate_height - 2 * props.margin) / motif_h)
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    tf = lambda l: [((x - cx) * k, (y - cy) * k) for x, y in l]
    shapes = [(tf(o), [tf(h) for h in hs]) for o, hs in shapes]
    print(f"[Klischee] Motiv: {motif_w * k:.2f} × {motif_h * k:.2f} mm")

    print("[Klischee] Baue Relief …")
    warnings = []
    taper = props.taper_amount if props.use_taper_base else 0.0
    relief_objs = []
    for i, (outer, holes) in enumerate(shapes):
        obj = build_relief_mesh(f"Relief_{i + 1:03d}", outer, holes,
                                props.plate_thickness, props.plate_thickness + props.relief_depth,
                                taper, props.mirror_motif, warnings)
        if obj:
            relief_objs.append(obj)
    if not relief_objs:
        raise RuntimeError("Das Motiv enthält keine druckbaren Flächen (alle Formen schmaler als 5 µm).")

    if props.fit_plate_to_motif:
        # Platte aus der tatsächlichen Relief-Basis (inkl. Taper) + Randabstand
        vs = [v.co for r in relief_objs for v in r.data.vertices]
        rw = max(v.x for v in vs) - min(v.x for v in vs)
        rh = max(v.y for v in vs) - min(v.y for v in vs)
        print(f"[Klischee] Passe Platte an Motiv an: {rw:.2f} x {rh:.2f} mm …")
        props.plate_width  = rw + 2 * props.margin
        props.plate_height = rh + 2 * props.margin

    print("[Klischee] Erstelle Grundplatte …")
    base = create_base_plate(props)
    link_to_collection(base, "Klischee")

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
    for w in dict.fromkeys(warnings):
        print(f"[Klischee] Hinweis: {w}")
    return all_objects, list(dict.fromkeys(warnings))


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
            _, warnings = generate_klischee(props)
            if warnings:
                self.report({'WARNING'}, "Klischee erstellt – mit Hinweis: " + " | ".join(warnings))
            else:
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
