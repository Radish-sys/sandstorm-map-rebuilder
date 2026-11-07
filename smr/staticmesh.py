"""Cooked UStaticMesh (UE4.2x) reader: LOD0 geometry + collision, and an ASCII FBX writer
that the UE FBX importer reads back at the original coordinates."""
import struct
from .uasset import R


class MeshError(Exception):
    pass


def _bulk(r):
    """TArray BulkSerialize: element size, count, raw bytes."""
    es, n = r.i32(), r.i32()
    b = r.b[r.p:r.p + es * n]
    r.p += es * n
    return es, n, b


def _pos_buffer_at(b, p, strip=True):
    """True if an inline position vertex buffer starts at p: 2 strip bytes (none on UE 4.22), stride 12, N, then a bulk
    array of 12-byte elements with the same N."""
    try:
        stride, nv, es, n = struct.unpack_from('<iiii', b, p + (2 if strip else 0))
    except struct.error:
        return False
    return stride == 12 and es == 12 and n == nv and nv > 0


def _half(h):
    return struct.unpack('<e', struct.pack('<H', h))[0]


def _section_size(b, p, nsec):
    """FStaticMeshSection grew over engine versions (28 bytes on 4.22, 36 on 4.24+); pick the one that parses."""
    for size in (36, 28, 32, 40):
        try:
            ok = True
            prev_end = 0
            for k in range(nsec):
                mi, first, ntri, vmin, vmax = struct.unpack_from('<iIIII', b, p + k * size)
                if not (0 <= mi < 256) or vmin > vmax or first != prev_end:
                    ok = False
                    break
                prev_end = first + ntri * 3
            if not ok:
                continue
            q = p + nsec * size
            maxdev, cooked_out, inlined = struct.unpack_from('<fii', b, q)
            if abs(maxdev) >= 1e7:
                continue
            if cooked_out in (0, 1) and inlined in (0, 1):
                return size  # 4.24+: LOD streaming fields follow
            if _pos_buffer_at(b, q + 4, strip=False):
                return size  # 4.22: the position buffer follows the deviation directly
        except struct.error:
            continue
    raise MeshError('unrecognised mesh section layout')


def read_static_mesh(pkg, idx, ubulk=None):
    """Return dict(verts, normals, uvs[ch], colors|None, tris, sections, materials, lightmap_index,
    body_setup_export) for LOD0 of StaticMesh export idx."""
    e = pkg.exports[idx]
    end = e['off'] + e['size']
    r = R(pkg.data, e['off'])
    props = pkg.read_props(r, end)
    if r.i32():          # object guid
        r.p += 16
    r.p += 2             # strip flags
    cooked = r.i32()
    if not cooked:
        raise MeshError('mesh is not cooked')
    body = r.i32()
    r.i32()              # nav collision
    r.p += 16            # lighting guid
    nsock = r.i32()
    r.p += 4 * nsock
    nlod = r.i32()
    if nlod < 1:
        raise MeshError('no LODs')
    # ---- first LOD that has geometry (LOD0 can be cooked out by a platform MinLOD) ----
    lod_used = None
    sec_size = None
    for li in range(nlod):
        r.p += 2             # strip flags
        nsec = r.i32()
        if sec_size is None:
            sec_size = _section_size(r.b, r.p, nsec)
        sections = []
        for _ in range(nsec):
            mi, first, ntri, vmin, vmax = struct.unpack_from('<iIIII', r.b, r.p)
            r.p += sec_size  # + bools: collision, shadow (+ force opaque, ray tracing on 4.24+)
            sections.append(dict(material=mi, first=first, ntri=ntri))
        r.f32()              # max deviation
        if _pos_buffer_at(r.b, r.p, strip=False):
            # UE 4.22 (pak v8): no LOD streaming fields, and the position buffer has no strip flags
            cooked_out, inlined, pos_strip = 0, 1, False
        else:
            cooked_out, inlined, pos_strip = r.i32(), r.i32(), True
        if not inlined and not cooked_out:
            # cooks from before LOD streaming (UE 4.24 and earlier: Sandstorm mods from 2020) have no cookedOut /
            # inlined fields; the dwords read as them are other flags and the buffers follow inline a little later.
            # Look for the position buffer's signature instead of trusting those two values.
            for k in (4, 0, 8, 12):
                if _pos_buffer_at(r.b, r.p + k):
                    r.p += k
                    inlined = True
                    break
        if cooked_out:
            continue
        if not inlined:
            # streamed LOD: a bulk-data header here, the same buffers in the .ubulk (or optional .uptnl) file
            flags = r.u32()
            if flags & 0x2000:   # BULKDATA_Size64Bit
                r.i64()
                size = r.i64()
            else:
                r.i32()
                size = r.i32()
            off = r.i64()
            if not flags & 0x10000:   # stored relative to the package's BulkDataStartOffset unless NoOffsetFixUp
                off += getattr(pkg, 'bulk_start', 0) or 0
            if flags & 0x40:     # BULKDATA_ForceInlinePayload
                payload = r.b[r.p:r.p + size]
            else:
                ext = '.uptnl' if flags & 0x800 else '.ubulk'   # never the other file: same offset, wrong data
                payload = ubulk(ext, off, size) if ubulk else None
                if payload is not None and len(payload) != size:
                    raise MeshError('LOD%d: the %s file is shorter than its header says (%d bytes at %d)'
                                    % (li, ext, size, off))
                if not payload:
                    raise MeshError('LOD%d is stored in a separate .ubulk file that is not in any of the paks '
                                    '(the mod may be missing a pak)' % li)
            r = R(payload, 0)
        lod_used = li
        break
    if lod_used is None:
        raise MeshError('every LOD is cooked out')
    if pos_strip:
        r.p += 2         # buffer strip flags (4.23+)
    # positions
    stride, nv = r.i32(), r.i32()
    es, n, b = _bulk(r)
    if stride != 12 or es != 12 or n != nv:
        raise MeshError('unexpected position buffer layout')
    pos = [struct.unpack_from('<fff', b, i * 12) for i in range(nv)]
    # tangents + uvs
    r.p += 2
    ntex, nv2, full_uv, hi_tan = r.u('<IIii')
    if nv2 != nv:
        raise MeshError('vertex count mismatch')
    es, n, b = _bulk(r)
    nrm = []
    if hi_tan:
        for i in range(nv):
            z = struct.unpack_from('<hhhh', b, i * 16 + 8)
            nrm.append(tuple(c / 32767.0 for c in z[:3]))
    else:
        # FPackedNormal has been signed bytes (x / 127) since UE 4.20; reading them unsigned turned every normal
        # into nonsense (straight up decoded as roughly (-1, -1, 0)). Second of the two packed vectors: TangentZ.
        for i in range(nv):
            z = struct.unpack_from('<bbbb', b, i * 8 + 4)
            nrm.append(tuple(max(-1.0, c / 127.0) for c in z[:3]))
    es, n, b = _bulk(r)
    uvs = [[] for _ in range(ntex)]
    for i in range(nv):
        for c in range(ntex):
            k = (i * ntex + c)
            if full_uv:
                uvs[c].append(struct.unpack_from('<ff', b, k * 8))
            else:
                h = struct.unpack_from('<HH', b, k * 4)
                uvs[c].append((_half(h[0]), _half(h[1])))
    # colors
    r.p += 2
    r.i32()              # colour stride
    cnv = r.i32()
    colors = None
    if cnv:
        es, n, b = _bulk(r)
        colors = [(b[i * 4 + 2], b[i * 4 + 1], b[i * 4], b[i * 4 + 3]) for i in range(cnv)]  # BGRA -> RGBA
    # indices
    is32 = r.i32()
    es, n, b = _bulk(r)
    if is32:
        idxs = list(struct.unpack_from('<%dI' % (n // 4), b, 0))
    else:
        idxs = list(struct.unpack_from('<%dH' % (n // 2), b, 0))
    tris = []
    tri_mat = []
    for s in sections:
        for t in range(s['ntri']):
            a = s['first'] + t * 3
            tris.append(tuple(idxs[a:a + 3]))
            tri_mat.append(s['material'])
    if any(i >= nv for t in tris for i in t):
        raise MeshError('index out of range')
    mats = []
    for m in props.get('StaticMaterials', []) or []:
        mats.append(dict(slot=m.get('MaterialSlotName') or 'Material', material=m.get('MaterialInterface')))
    return dict(name=e['name'], lod=lod_used, verts=pos, normals=nrm, uvs=uvs, colors=colors, tris=tris, tri_mat=tri_mat,
                materials=mats, lightmap_index=props.get('LightMapCoordinateIndex', 1),
                lightmap_res=props.get('LightMapResolution'), body_export=body, props=props)


def read_collision(pkg, body_ref):
    """Simple collision from the mesh's BodySetup: convex hulls and boxes, plus the trace flag."""
    out = dict(convex=[], boxes=[], spheres=[], trace=None, profile=None)
    if not body_ref or body_ref <= 0:
        return out
    p = pkg.export_props(body_ref - 1)
    out['trace'] = p.get('CollisionTraceFlag')
    bi = p.get('DefaultInstance') or {}
    if isinstance(bi, dict):
        out['profile'] = bi.get('CollisionProfileName')
    agg = p.get('AggGeom') or {}
    for c in agg.get('ConvexElems', []) or []:
        vd = c.get('VertexData') or []
        idx = c.get('IndexData') or []
        if len(vd) >= 4:
            out['convex'].append(dict(verts=[tuple(v) for v in vd], idx=list(idx) if isinstance(idx, list) else []))
    for bx in agg.get('BoxElems', []) or []:
        out['boxes'].append(bx)
    for sp in agg.get('SphereElems', []) or []:
        out['spheres'].append(sp)
    return out


def _hull_tris(verts):
    # scipy (Qhull) on purpose: collision hulls from real models have many coplanar points, which a simple
    # incremental hull gets wrong (r39 tried one: 783 of Panji's 2242 hulls came out wrong)
    try:
        from scipy.spatial import ConvexHull
        import numpy as np
        h = ConvexHull(np.array(verts))
        return [tuple(int(i) for i in s) for s in h.simplices]
    except Exception:
        return []


def _box_mesh(bx):
    import math
    c = bx.get('Center') or [0, 0, 0]
    ex = [bx.get('X', 0) / 2, bx.get('Y', 0) / 2, bx.get('Z', 0) / 2]
    rot = bx.get('Rotation') or {'pitch': 0, 'yaw': 0, 'roll': 0}
    p, y, rl = (math.radians(rot.get(k, 0)) for k in ('pitch', 'yaw', 'roll'))
    # UE FRotator -> matrix (same convention as FRotationMatrix)
    sp, cp, sy, cy, sr, cr = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(rl), math.cos(rl)
    X = (cp * cy, cp * sy, sp)
    Y = (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp)
    Z = (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp)
    vs = []
    for sx in (-1, 1):
        for syy in (-1, 1):
            for sz in (-1, 1):
                a, b_, d = sx * ex[0], syy * ex[1], sz * ex[2]
                vs.append(tuple(c[i] + a * X[i] + b_ * Y[i] + d * Z[i] for i in range(3)))
    return vs


# --------------------------------------------------------------------------- FBX
def _arr(vals, fmt='%.9g'):
    # 9 significant digits round-trip a float32 exactly. 6 snapped vertices more than 1 km from the mesh's pivot to
    # 1 cm steps (the big merged meshes), enough for cracks and z-fighting between parts that touched in the original
    return ','.join(fmt % v for v in vals)


def write_fbx(path, mesh, collision=None):
    """ASCII FBX 7.4 in the axis system UE's own exporter uses (Z up, -Y front, Y mirrored)."""
    name = mesh['name']
    lines = []
    W = lines.append
    W('; FBX 7.4.0 project file')
    W('FBXHeaderExtension:  {\n\tFBXHeaderVersion: 1003\n\tFBXVersion: 7400\n\tCreator: "SandstormMapRebuilder"\n}')
    W('GlobalSettings:  {\n\tVersion: 1000\n\tProperties70:  {')
    for k, v in (('UpAxis', 2), ('UpAxisSign', 1), ('FrontAxis', 1), ('FrontAxisSign', -1), ('CoordAxis', 0),
                 ('CoordAxisSign', 1), ('OriginalUpAxis', 2), ('OriginalUpAxisSign', 1)):
        W('\t\tP: "%s", "int", "Integer", "",%d' % (k, v))
    W('\t\tP: "UnitScaleFactor", "double", "Number", "",1\n\t\tP: "OriginalUnitScaleFactor", "double", "Number", "",1')
    W('\t}\n}')

    nmat = max(1, len(mesh['materials']))
    uid = [1000]

    def nid():
        uid[0] += 1
        return uid[0]

    def geom_block(gid, gname, verts, tris, normals=None, uvs=None, colors=None, tri_mat=None):
        b = []
        b.append('\tGeometry: %d, "Geometry::%s", "Mesh" {' % (gid, gname))
        flat = []
        for (x, y, z) in verts:
            flat += [x, -y, z]
        b.append('\t\tVertices: *%d {\n\t\t\ta: %s\n\t\t}' % (len(flat), _arr(flat)))
        # keep the original winding: the importer flips Y back without touching the order (checked in the SDK)
        pvi = []
        for (a, c, d) in tris:
            pvi += [a, c, -d - 1]
        b.append('\t\tPolygonVertexIndex: *%d {\n\t\t\ta: %s\n\t\t}' % (len(pvi), _arr(pvi, '%d')))
        b.append('\t\tGeometryVersion: 124')
        layers = []
        if normals:
            nf = []
            for (x, y, z) in normals:
                nf += [x, -y, z]
            b.append('\t\tLayerElementNormal: 0 {\n\t\t\tVersion: 101\n\t\t\tName: ""\n'
                     '\t\t\tMappingInformationType: "ByVertice"\n\t\t\tReferenceInformationType: "Direct"\n'
                     '\t\t\tNormals: *%d {\n\t\t\t\ta: %s\n\t\t\t}\n\t\t}' % (len(nf), _arr(nf)))
            layers.append(('LayerElementNormal', 0))
        for ci, ch in enumerate(uvs or []):
            uf = []
            for (u, v) in ch:
                uf += [u, 1.0 - v]
            b.append('\t\tLayerElementUV: %d {\n\t\t\tVersion: 101\n\t\t\tName: "UVChannel_%d"\n'
                     '\t\t\tMappingInformationType: "ByVertice"\n\t\t\tReferenceInformationType: "Direct"\n'
                     '\t\t\tUV: *%d {\n\t\t\t\ta: %s\n\t\t\t}\n\t\t}' % (ci, ci + 1, len(uf), _arr(uf)))
            layers.append(('LayerElementUV', ci))
        if colors:
            cf = []
            for c in colors:
                cf += [c[0] / 255.0, c[1] / 255.0, c[2] / 255.0, c[3] / 255.0]
            b.append('\t\tLayerElementColor: 0 {\n\t\t\tVersion: 101\n\t\t\tName: "Col"\n'
                     '\t\t\tMappingInformationType: "ByVertice"\n\t\t\tReferenceInformationType: "Direct"\n'
                     '\t\t\tColors: *%d {\n\t\t\t\ta: %s\n\t\t\t}\n\t\t}' % (len(cf), _arr(cf)))
            layers.append(('LayerElementColor', 0))
        if tri_mat is not None:
            b.append('\t\tLayerElementMaterial: 0 {\n\t\t\tVersion: 101\n\t\t\tName: ""\n'
                     '\t\t\tMappingInformationType: "ByPolygon"\n\t\t\tReferenceInformationType: "IndexToDirect"\n'
                     '\t\t\tMaterials: *%d {\n\t\t\t\ta: %s\n\t\t\t}\n\t\t}' % (len(tri_mat), _arr(tri_mat, '%d')))
            layers.append(('LayerElementMaterial', 0))
        # Smoothing groups. A cooked vertex buffer already splits vertices wherever the original mesh had a
        # hard edge, so every edge that is still shared in the index buffer is a soft one: one group for the
        # whole mesh says exactly that, and the hard edges survive as the splits themselves. Without this layer
        # the import logs "No smoothing group information was found in this FBX scene" and the normals UE
        # computes from it are undefined, which is what left merged meshes rendering near-black.
        if tris:
            b.append('\t\tLayerElementSmoothing: 0 {\n\t\t\tVersion: 102\n\t\t\tName: ""\n'
                     '\t\t\tMappingInformationType: "ByPolygon"\n\t\t\tReferenceInformationType: "Direct"\n'
                     '\t\t\tSmoothing: *%d {\n\t\t\t\ta: %s\n\t\t\t}\n\t\t}'
                     % (len(tris), _arr([1] * len(tris), '%d')))
            layers.append(('LayerElementSmoothing', 0))
        # layer 0 holds one of each type, extra UV channels go to their own layers
        by_layer = {}
        for typ, ti in layers:
            by_layer.setdefault(ti if typ == 'LayerElementUV' else 0, []).append((typ, ti))
        for li in sorted(by_layer):
            b.append('\t\tLayer: %d {\n\t\t\tVersion: 100' % li)
            for typ, ti in by_layer[li]:
                b.append('\t\t\tLayerElement:  {\n\t\t\t\tType: "%s"\n\t\t\t\tTypedIndex: %d\n\t\t\t}' % (typ, ti))
            b.append('\t\t}')
        b.append('\t}')
        return b

    objs = []
    conns = []
    gid, mid = nid(), nid()
    tri_mat = [min(m, nmat - 1) for m in mesh['tri_mat']]
    objs += geom_block(gid, name, mesh['verts'], mesh['tris'], mesh['normals'], mesh['uvs'], mesh['colors'], tri_mat)
    objs.append('\tModel: %d, "Model::%s", "Mesh" {\n\t\tVersion: 232\n\t\tProperties70:  {\n\t\t}\n'
                '\t\tShading: T\n\t\tCulling: "CullingOff"\n\t}' % (mid, name))
    conns.append('\tC: "OO",%d,0' % mid)
    conns.append('\tC: "OO",%d,%d' % (gid, mid))
    seen = set()
    for i in range(nmat):
        slot = mesh['materials'][i]['slot'] if i < len(mesh['materials']) else 'Material'
        base = slot
        k = 1
        while slot in seen:
            slot = '%s_%d' % (base, k); k += 1
        seen.add(slot)
        matid = nid()
        objs.append('\tMaterial: %d, "Material::%s", "" {\n\t\tVersion: 102\n\t\tShadingModel: "phong"\n'
                    '\t\tMultiLayer: 0\n\t\tProperties70:  {\n\t\t}\n\t}' % (matid, slot))
        conns.append('\tC: "OO",%d,%d' % (matid, mid))
    # simple collision as UCX_ / UBX_ children
    hulls = []
    if collision:
        for c in collision.get('convex', []):
            t = []
            idx = c['idx']
            if idx and len(idx) % 3 == 0:
                t = [tuple(idx[j:j + 3]) for j in range(0, len(idx), 3)]
            else:
                t = _hull_tris(c['verts'])
            if t:
                hulls.append(('UCX', c['verts'], t))
        for bx in collision.get('boxes', []):
            vs = _box_mesh(bx)
            t = _hull_tris(vs)
            if t:
                hulls.append(('UBX', vs, t))
    for n, (pre, vs, t) in enumerate(hulls):
        cg, cm = nid(), nid()
        cname = '%s_%s_%02d' % (pre, name, n)
        objs += geom_block(cg, cname, vs, t)
        objs.append('\tModel: %d, "Model::%s", "Mesh" {\n\t\tVersion: 232\n\t\tProperties70:  {\n\t\t}\n'
                    '\t\tShading: T\n\t\tCulling: "CullingOff"\n\t}' % (cm, cname))
        conns.append('\tC: "OO",%d,0' % cm)
        conns.append('\tC: "OO",%d,%d' % (cg, cm))
    nmodels = sum(1 for o in objs if o.startswith('\tModel:'))
    ngeo = sum(1 for o in objs if o.startswith('\tGeometry:'))
    nmats = sum(1 for o in objs if o.startswith('\tMaterial:'))
    W('Definitions:  {\n\tVersion: 100\n\tCount: %d' % (1 + nmodels + ngeo + nmats))
    W('\tObjectType: "GlobalSettings" {\n\t\tCount: 1\n\t}')
    for typ, n, cls in (('Model', nmodels, 'FbxNode'), ('Geometry', ngeo, 'FbxMesh'),
                        ('Material', nmats, 'FbxSurfacePhong')):
        W('\tObjectType: "%s" {\n\t\tCount: %d\n\t\tPropertyTemplate: "%s" {\n\t\t}\n\t}' % (typ, n, cls))
    W('}')
    W('Objects:  {')
    lines.extend(objs)
    W('}')
    W('Connections:  {')
    lines.extend(conns)
    W('}')
    with open(path, 'w', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')
    return len(hulls)
