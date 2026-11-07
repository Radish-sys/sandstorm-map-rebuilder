"""Extract a cooked Insurgency: Sandstorm map mod (.pak) into a rebuildable layout.

Output (in the chosen folder):
  layout.json          every level: actors, transforms, props, volumes, foliage; streaming; scenarios; ModData
  landscape/           16-bit heightmap + paint-layer PNGs per landscape, with import settings in layout.json
  textures/            mod textures decoded to PNG (tactical map, loading screens, ...)
  REPORT.md            what was found, what the rebuild script does, and what is left to do by hand
"""
import io
import json
import math
import os
import re
import struct
from collections import defaultdict

import numpy as np
from PIL import Image

from . import __version__, __build__
from .pak import Pak
from .uasset import Package, UAssetError, texture_mips

# ----------------------------------------------------------------------------- constants
SKIP_ACTOR_CLASSES = {'Model', 'World', 'Level', 'NavigationSystemModuleConfig', 'RecastNavMesh',
                      'TacticalRecastNavMesh', 'LandscapeGizmoActiveActor', 'INSWorldSettings', 'WorldSettings',
                      'InstancedFoliageActor', 'Landscape', 'LandscapeStreamingProxy', 'AbstractNavData',
                      'WorldDataLayers', 'LevelBounds', 'NavigationData', 'GameplayDebuggerPlayerManager',
                      'ModelComponent', 'Polys', 'BrushComponent',
                      'LODActor'}  # HLOD clusters: build products (Build > Build HLODs), not content
NOISE = {'StreamingTextureData', 'VisibilityId', 'CacheMeshExtendedBounds', 'BlueprintCreatedComponents',
         'UCSSerializationIndex', 'bNetAddressable', 'CreationMethod', 'AttachParent', 'RelativeLocation',
         'RelativeRotation', 'RelativeScale3D', 'BrushBodySetup', 'Brush', 'BrushComponent', 'RootComponent',
         'LightGuid', 'LightingGuid', 'MapBuildDataId', 'bHasPerInstanceHitProxies', 'InstancingRandomSeed',
         'StaticMeshComponent', 'CapsuleComponent', 'SpriteComponent', 'ArrowComponent', 'GoodSprite',
         'BadSprite', 'StaticMeshImportVersion', 'PerInstanceSMCustomData', 'InstanceReorderTable',
         'bAdjustedLocation', 'AdjustedLocation', 'BuiltData', 'PrimaryComponentTick', 'IrrelevantLights',
         'StaticLightingBuildGuid', 'LODData', 'OldPosition', 'bOldPositionValid'}


# ----------------------------------------------------------------------------- math (UE row-vector convention)
def rot_matrix(r):
    p, y, ro = (math.radians(r.get(k, 0.0)) for k in ('pitch', 'yaw', 'roll'))
    SP, CP, SY, CY, SR, CR = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(ro), math.cos(ro)
    return np.array([[CP * CY, CP * SY, SP],
                     [SR * SP * CY - CR * SY, SR * SP * SY + CR * CY, -SR * CP],
                     [-(CR * SP * CY + SR * SY), CY * SR - CR * SP * SY, CR * CP]])


def to_m4(loc, rot, scale):
    m = np.eye(4)
    m[:3, :3] = np.diag(scale) @ rot_matrix(rot)
    m[3, :3] = loc
    return m


def from_m4(m):
    axes = m[:3, :3].copy()
    scale = np.linalg.norm(axes, axis=1)
    if np.linalg.det(axes) < 0:
        scale[2] = -scale[2]
    X, Y, Z = (axes[i] / (scale[i] if scale[i] else 1) for i in range(3))
    pitch = math.degrees(math.atan2(X[2], math.hypot(X[0], X[1])))
    yaw = math.degrees(math.atan2(X[1], X[0]))
    sy = np.array([-math.sin(math.radians(yaw)), math.cos(math.radians(yaw)), 0.0])
    roll = math.degrees(math.atan2(float(Z @ sy), float(Y @ sy)))
    return ([round(float(v), 4) for v in m[3, :3]],
            {'pitch': round(pitch, 4), 'yaw': round(yaw, 4), 'roll': round(roll, 4)},
            [round(float(v), 5) for v in scale])


def rel_m4(props):
    return to_m4(props.get('RelativeLocation', [0, 0, 0]), props.get('RelativeRotation', {}),
                 props.get('RelativeScale3D', [1, 1, 1]))


# ----------------------------------------------------------------------------- pak virtual filesystem
class ModFS:
    """Maps content package paths (/Game/..., /ModName/...) to pak entries."""

    def __init__(self, pak_paths):
        self.files = {}  # package file path (e.g. /Rust/Maps/Rust.umap) -> (pak, entry)
        self.mods = set()
        self.overridden = 0
        # A mod can ship stale chunks next to current ones (an old chunk3 from a previous upload). When the same
        # file is in several paks the NEWEST pak wins, so old headers never get paired with new bulk data.
        def age(pp):
            try:
                return os.path.getmtime(pp)
            except OSError:
                return 0
        order = sorted(range(len(pak_paths)), key=lambda i: (age(pak_paths[i]), i))
        holders = {}
        self.paks = []
        for pp in [pak_paths[i] for i in order]:
            try:
                pak = Pak(pp)
            except Exception:
                self.close()
                raise
            self.paks.append(pak)
            mount = pak.mount.replace('\\', '/')
            for ent in pak.entries:
                full = os.path.normpath(os.path.join(mount, ent)).replace('\\', '/')
                full = full.lstrip('./').lstrip('/')
                while full.startswith('../'):
                    full = full[3:]
                parts = full.split('/')
                if parts and parts[0] == 'Insurgency':
                    parts = parts[1:]
                # current SDK: Mods/<Name>/Content ; 2020-era SDK: Plugins/<Name>/Content
                if len(parts) >= 3 and parts[0] in ('Mods', 'Plugins') and parts[2] == 'Content':
                    self.mods.add(parts[1])
                    vpath = '/' + parts[1] + '/' + '/'.join(parts[3:])
                elif parts and parts[0] == 'Content':
                    vpath = '/Game/' + '/'.join(parts[1:])
                else:
                    vpath = '//' + '/'.join(parts)  # non-content file (asset registry, etc.)
                if vpath in self.files:
                    self.overridden += 1
                self.files[vpath] = (pak, ent)
                holders.setdefault(vpath, {})[pp] = (pak, ent)
        # keep each package's files together: .uexp/.ubulk/.uptnl from the pak that supplied its header
        for vpath, (pak, ent) in list(self.files.items()):
            base, ext = os.path.splitext(vpath)
            if ext not in ('.uasset', '.umap'):
                continue
            for e2 in ('.uexp', '.ubulk', '.uptnl'):
                h = holders.get(base + e2) or {}
                if pak.path in h:
                    self.files[base + e2] = h[pak.path]
        if self.overridden:
            print('  note: %d files appear in more than one pak; using the newest pak\'s copy' % self.overridden)
        self._cache = {}

    def close(self):
        for pak in self.paks:
            pak.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self, vpath):
        pak, ent = self.files[vpath]
        return pak.read(ent)

    def exists(self, vpath):
        return vpath in self.files

    def package(self, pkg_path):
        """pkg_path like /Rust/Maps/Rust (no extension). Returns Package or None."""
        if pkg_path in self._cache:
            return self._cache[pkg_path]
        pk = None
        for ext in ('.umap', '.uasset'):
            if self.exists(pkg_path + ext):
                data = self.read(pkg_path + ext)
                if self.exists(pkg_path + '.uexp'):
                    data += self.read(pkg_path + '.uexp')
                try:
                    pk = Package(data, name=pkg_path.split('/')[-1])
                    pk.path = pkg_path
                except UAssetError as ex:
                    pk = None
                    print('  ! %s: %s' % (pkg_path, ex))
                break
        self._cache[pkg_path] = pk
        return pk

    def bulk(self, pkg_path, ext, offset, size):
        data = self.read(pkg_path + ext)
        if offset < 0:
            return b''
        return data[offset:offset + size]

    def packages(self, ext):
        return sorted(p[:-len(ext)] for p in self.files if p.endswith(ext))


# ----------------------------------------------------------------------------- helpers
def obj_path(full):
    """'/Game/X/SM_A.SM_A' -> package path '/Game/X/SM_A'."""
    return full.split('.')[0] if full and full.startswith('/') else None


LOCAL_ACTOR_PATH = re.compile(r'^[^/.:]+\.PersistentLevel\.([^.:]+)$')
ACTOR_PATH = re.compile(r'^(/[^.:]+)\.[^.:]+[:.]PersistentLevel\.([^.:]+)$')


class LevelExtractor:
    def __init__(self, fs):
        self.fs = fs
        self._tmpl = {}

    def bp_templates(self, class_path):
        """component name -> template props, for BP classes shipped in the pak."""
        pkg = obj_path(class_path)
        if not pkg or pkg.startswith('/Script/'):
            return {}
        if pkg in self._tmpl:
            return self._tmpl[pkg]
        out = {}
        bp = self.fs.package(pkg)
        if bp is not None:
            for i, e in enumerate(bp.exports):
                try:
                    pr = bp.export_props(i)
                except Exception:
                    continue
                out[e['name'].replace('_GEN_VARIABLE', '')] = pr
                out['__full__' + e['name']] = pr
        self._tmpl[pkg] = out
        return out

    def extract(self, pkg_path):
        p = self.fs.package(pkg_path)
        if p is None:
            return None
        lname = pkg_path.split('/')[-1]
        by_name = {p.full_name(i + 1): i for i in range(len(p.exports))}
        children = defaultdict(list)
        for i, e in enumerate(p.exports):
            children[e['outer']].append(i)
        lvl = next((i for i, e in enumerate(p.exports) if e['name'] == 'PersistentLevel'), None)
        if lvl is None:
            return None
        lvl += 1
        cache = {}

        def props(i):
            if i not in cache:
                pr = p.export_props(i)
                e = p.exports[i]
                if e['outer'] > 0:
                    owner = p.exports[e['outer'] - 1]
                    t = self.bp_templates(p.class_of(owner)).get(e['name'])
                    if t:
                        pr = {**{k: v for k, v in t.items() if k != 'AttachParent'}, **pr}
                cache[i] = pr
            return cache[i]

        def world_m4(ci, depth=0):
            pr = props(ci)
            m = rel_m4(pr)
            par = pr.get('AttachParent')
            if par and par in by_name and depth < 32:
                m = m @ world_m4(by_name[par], depth + 1)
            return m

        actor_idx = children[lvl]
        actor_names = {p.exports[i]['name'] for i in actor_idx}

        def clean(v):
            if isinstance(v, dict):
                if '_unparsed' in v or '_raw' in v or '_rawarray' in v:
                    return None
                return {k: c for k, x in v.items() if k not in NOISE and (c := clean(x)) is not None}
            if isinstance(v, list):
                return [clean(x) for x in v]
            if isinstance(v, str) and 'PersistentLevel.' in v:
                # '/Mod/Maps/Lvl.Lvl:PersistentLevel.Actor' (4.25+) or '...Lvl.PersistentLevel.Actor' (older)
                m = LOCAL_ACTOR_PATH.match(v)  # this level's own actors: 'Lvl.PersistentLevel.Actor'
                if m:
                    return {'actor_ref': m.group(1)} if m.group(1) in actor_names else None
                m = ACTOR_PATH.match(v)
                if m:
                    lvl_pkg, name = m.groups()
                    if lvl_pkg == pkg_path:
                        return {'actor_ref': name} if name in actor_names else None
                    return {'actor_ref': name, 'level': lvl_pkg}  # an actor in another sublevel
                return None  # a component or other sub-object of an actor: not restorable
            return v

        out = {'actors': [], 'foliage': [], 'splines': [], 'world_settings': {}, 'landscapes': [], 'streaming': [],
               'warnings': [], 'bsp_bytes': 0}

        # streaming sublevels (persistent levels)
        for i, e in enumerate(p.exports):
            c = p.class_of(e).split('.')[-1]
            if c.startswith('LevelStreaming'):
                wa = props(i).get('WorldAsset')
                if wa:
                    out['streaming'].append({'level': obj_path(wa), 'type': c})
            if c == 'Model' and e['outer'] == lvl:
                out['bsp_bytes'] = max(out['bsp_bytes'], e['size'])

        for i in actor_idx:
            e = p.exports[i]
            cls_full = p.class_of(e) or ''
            cls = cls_full.split('.')[-1]
            try:
                ap = props(i)
            except Exception as ex:
                out['warnings'].append('could not read %s (%s): %s' % (e['name'], cls, ex))
                continue
            if cls in ('INSWorldSettings', 'WorldSettings'):
                ws = clean({k: v for k, v in ap.items()
                            if k in ('DefaultScenarios', 'LightingScenarios', 'DefaultLightingScenario',
                                     'TacticalMapMaterial', 'DefaultSoundscape', 'CompassOffset')})
                out['world_settings'].update({k: v for k, v in (ws or {}).items() if v not in (None, {}, [])})
                continue
            if cls in ('Landscape', 'LandscapeStreamingProxy'):
                root = by_name.get(ap.get('RootComponent'))
                loc, rot, scale = from_m4(world_m4(root)) if root is not None else ([0, 0, 0], {}, [1, 1, 1])
                out['landscapes'].append({'actor': e['name'], 'class': cls, 'location': loc, 'rotation': rot,
                                          'scale': scale, 'material': ap.get('LandscapeMaterial'),
                                          # a landscape with no phys material falls back to the engine default,
                                          # so footstep sounds and surface impacts come out wrong everywhere
                                          'phys_material': ap.get('DefaultPhysMaterial'),
                                          'collision_mip_level': ap.get('CollisionMipLevel'),
                                          'static_lighting_lod': ap.get('StaticLightingLOD'),
                                          'lightmap_res': ap.get('StaticLightingResolution'),
                                          '_pkg': pkg_path, '_idx': i})
                # landscape splines: roads, kerbs and fences laid along the terrain are spline meshes owned by
                # the landscape actor, not actors of their own
                for j in children[i + 1]:
                    if (p.class_of(p.exports[j]) or '').endswith('.SplineMeshComponent'):
                        out['splines'].append(self._spline_mesh(props(j), world_m4(j)))
                continue
            if cls == 'InstancedFoliageActor':
                for j in children[i + 1]:
                    ce = p.exports[j]
                    if not (p.class_of(ce) or '').endswith('InstancedStaticMeshComponent'):
                        continue
                    cp = props(j)
                    out['foliage'].append(self._foliage(p, ce, cp))
                continue
            if cls in SKIP_ACTOR_CLASSES:
                continue
            if e['cls'] > 0 or obj_path(cls_full) == pkg_path:
                # the level blueprint's LevelScriptActor; the level recreates it itself
                fn = [x['name'] for x in p.exports if (p.class_of(x) or '').endswith('.Function')]
                if fn:
                    import re
                    refs = [m.group(1) for m in (re.match(r'^(.+)_ExecuteUbergraph_.+_RefProperty$', n)
                                                 for n in p.names) if m]
                    if 'PostAssociatedAkEvent' in p.names and refs:
                        # all it does is start ambient sounds on BeginPlay: AkAmbientSound.AutoPost does the same
                        out['autopost'] = refs
                        out['notes'] = out.get('notes', []) + [
                            'level blueprint only started %s on BeginPlay; replaced by AutoPost' % ', '.join(refs)]
                    else:
                        out['level_script'] = fn[:8]
                        out['warnings'].append('%s has a level blueprint with logic (%s); rebuild it by hand'
                                               % (lname, ', '.join(fn[:8])))
                continue

            root = by_name.get(ap.get('RootComponent'))
            if root is not None:
                loc, rot, scale = from_m4(world_m4(root))
            else:
                loc, rot, scale = [0, 0, 0], {'pitch': 0, 'yaw': 0, 'roll': 0}, [1, 1, 1]
            if '_GEN_VARIABLE_' in e['name'] and '_CAT' in e['name']:
                # child actor spawned by a BP ChildActorComponent: keep as a standalone actor
                owner_cls = None
                par = ap.get('ParentComponent')
                if par in by_name:
                    owner_cls = p.class_of(p.exports[p.exports[by_name[par]]['outer'] - 1])
                base = e['name'].rsplit('_', 1)[0]
                t = self.bp_templates(owner_cls).get('__full__' + base) or {}
                ap = {**{k: v for k, v in t.items() if not isinstance(v, str) or '.' not in v}, **ap}
                ap.pop('ParentComponent', None)
            a = {'name': e['name'], 'class': cls_full, 'location': loc, 'rotation': rot, 'scale': scale,
                 'props': clean(ap), 'components': {}}
            for j in children[i + 1]:
                ce = p.exports[j]
                ccls = (p.class_of(ce) or '').split('.')[-1]
                if ccls in ('BodySetup', 'Model', 'NavCollision'):
                    continue
                try:
                    cp = props(j)
                except Exception:
                    continue
                entry = {'class': p.class_of(ce), 'props': clean(cp)}
                if j == root:
                    entry['is_root'] = True
                if ccls == 'BrushComponent':
                    self._brush(p, a, cp, by_name, props, world_m4, j)
                a['components'][ce['name']] = entry
            if root is not None and cls == 'StaticMeshActor':
                rp = props(root)
                a['mesh'] = rp.get('StaticMesh')
                a['materials'] = rp.get('OverrideMaterials', [])
            out['actors'].append(a)
        for a in out['actors']:
            if a['name'] in out.get('autopost', []):
                a['props']['AutoPost'] = True
        return out

    def _brush(self, p, a, cp, by_name, props, world_m4, j):
        bs = by_name.get(cp.get('BrushBodySetup'))
        if bs is None:
            return
        elems = (props(bs).get('AggGeom') or {}).get('ConvexElems', [])
        verts = [v for el in elems for v in el.get('VertexData', [])]
        if not verts:
            return
        arr = np.array(verts)
        bmin, bmax = arr.min(0), arr.max(0)
        a['brush_box'] = {'min': bmin.round(3).tolist(), 'max': bmax.round(3).tolist()}
        if len(elems) != 1 or len(verts) != 8:
            a['brush_box']['not_a_box'] = True  # rebuilt as its bounding box
        wm = world_m4(j)
        ctr = np.append((bmin + bmax) / 2, 1) @ wm
        _, vr, vs = from_m4(wm)
        a['volume'] = {'location': [round(float(x), 4) for x in ctr[:3]], 'rotation': vr,
                       'scale': [round(float(vs[k] * (bmax[k] - bmin[k]) / 200.0), 5) for k in range(3)]}

    @staticmethod
    def _spline_mesh(cp, wm):
        sp = cp.get('SplineParams') or {}
        loc, rot, scale = from_m4(wm)
        return {'mesh': cp.get('StaticMesh'), 'materials': cp.get('OverrideMaterials', []),
                'location': loc, 'rotation': rot, 'scale': scale,
                'start_pos': sp.get('StartPos', [0, 0, 0]), 'start_tangent': sp.get('StartTangent', [100, 0, 0]),
                'end_pos': sp.get('EndPos', [100, 0, 0]), 'end_tangent': sp.get('EndTangent', [100, 0, 0]),
                'start_scale': sp.get('StartScale', [1, 1]), 'end_scale': sp.get('EndScale', [1, 1]),
                'start_roll': sp.get('StartRoll', 0.0), 'end_roll': sp.get('EndRoll', 0.0),
                'start_offset': sp.get('StartOffset', [0, 0]), 'end_offset': sp.get('EndOffset', [0, 0]),
                'forward_axis': str(cp.get('ForwardAxis', 'ESplineMeshAxis::X')).split('::')[-1],
                'up_dir': cp.get('SplineUpDir', [0, 0, 1]),
                'smooth_roll_scale': bool(cp.get('bSmoothInterpRollScale', False)),
                'cast_shadow': cp.get('CastShadow', True),
                'collision_profile': (cp.get('BodyInstance') or {}).get('CollisionProfileName'),
                'cull_end': cp.get('LDMaxDrawDistance', 0)}

    def _foliage(self, p, ce, cp):
        d = p.data[ce['off']:ce['off'] + ce['size']]
        inst = []
        cm = rel_m4(cp)
        for o in range(len(d) - 8):
            es, n = struct.unpack_from('<ii', d, o)
            if es == 64 and 0 < n < 500000 and o + 8 + n * 64 <= len(d):
                m = struct.unpack_from('<16f', d, o + 8)
                if abs(m[3]) < 1e-3 and abs(m[7]) < 1e-3 and abs(m[11]) < 1e-3 and abs(m[15] - 1) < 1e-3:
                    mats = np.frombuffer(d, '<f4', n * 16, o + 8).reshape(n, 4, 4).astype(np.float64)
                    inst = [from_m4(mm @ cm) for mm in mats]
                    break
        return {'mesh': cp.get('StaticMesh'), 'materials': cp.get('OverrideMaterials', []),
                'cast_shadow': cp.get('CastShadow', True),
                'cull_start': cp.get('InstanceStartCullDistance', 0),
                'cull_end': cp.get('InstanceEndCullDistance', 0),
                'collision_profile': (cp.get('BodyInstance') or {}).get('CollisionProfileName'),
                'instances': [{'location': a, 'rotation': b, 'scale': c} for a, b, c in inst]}


# ----------------------------------------------------------------------------- textures
def _mip_candidates(fs, pkg, mip):
    """Every plausible place a streamed mip's bytes can be, most likely first."""
    off, size, flags = mip['offset'], mip['size'], mip.get('flags', 0)
    exts = ('.uptnl', '.ubulk') if flags & 0x800 else ('.ubulk', '.uptnl')
    # UE stores the offset relative to the package's BulkDataStartOffset unless BULKDATA_NoOffsetFixUp (0x10000)
    bs = getattr(pkg, 'bulk_start', 0) or 0
    shifts = [0] if flags & 0x10000 or not bs else [bs, 0]
    if not flags & 0x100:  # payload at the end of the .uexp: offset is into the whole package
        for sh in shifts:
            if 0 <= off + sh and off + sh + size <= len(pkg.data):
                yield 'uexp%+d' % sh, pkg.data[off + sh:off + sh + size]
    for ext in exts:
        if not fs.exists(pkg.path + ext):
            continue
        data = fs.read(pkg.path + ext)
        for sh in shifts:
            if 0 <= off + sh and off + sh + size <= len(data):
                yield '%s%+d' % (ext, sh), data[off + sh:off + sh + size]


def _rgb(fmt, w, h, b):
    try:
        return np.asarray(decode_texture(fmt, w, h, b).convert('RGB'), np.float32)
    except Exception:
        return None


def _matches(fmt, mip, b, ref):
    """Does this candidate, shrunk to the reference mip's size, look like the reference mip?"""
    img = _rgb(fmt, mip['w'], mip['h'], b)
    if img is None:
        return False
    rw, rh = ref.shape[1], ref.shape[0]
    small = np.asarray(Image.fromarray(img.astype(np.uint8)).resize((rw, rh), Image.BOX), np.float32)
    return float(np.abs(small - ref).mean()) < 24.0


def top_mip(fs, pkg, idx):
    """The largest mip whose bytes really belong to this texture.

    Streamed mips are checked against the largest mip stored inline in the package (always correct):
    a wrong file or offset decodes to TV static, so a candidate that doesn't shrink down to the inline mip
    is rejected. If no streamed mip checks out, the inline mip is used (lower resolution, never noise)."""
    fmt, mips = texture_mips(pkg, idx)
    inline = [m for m in mips if m['data'] is not None and len(m['data']) == m['size'] and m['size']]
    ref_mip = max(inline, key=lambda m: m['w'] * m['h']) if inline else None
    ref = _rgb(fmt, ref_mip['w'], ref_mip['h'], ref_mip['data']) if ref_mip else None
    if ref is not None and min(ref.shape[:2]) < 8:
        ref = None  # too small to tell noise from a picture
    for m in mips:
        if m is ref_mip:
            break
        if m['data'] is not None:
            return fmt, m['w'], m['h'], m['data']
        first = None
        for k, (where, b) in enumerate(_mip_candidates(fs, pkg, m)):
            if ref is None:
                first = (where, b)
                break
            if _matches(fmt, m, b, ref):
                if k:  # not where the header said; worth knowing if it ever happens
                    MIP_FIXES[where] = MIP_FIXES.get(where, 0) + 1
                return fmt, m['w'], m['h'], b
        if first:
            return fmt, m['w'], m['h'], first[1]
    if ref_mip is not None:
        if len(mips) and mips[0] is not ref_mip:
            LOW_RES.append('%s (%dx%d instead of %dx%d)' % (
                pkg.path, ref_mip['w'], ref_mip['h'], mips[0]['w'], mips[0]['h']))
        return fmt, ref_mip['w'], ref_mip['h'], ref_mip['data']
    return fmt, 0, 0, None


MIP_FIXES = {}
LOW_RES = []


def decode_texture(fmt, w, h, b):
    if fmt in ('PF_B8G8R8A8',):
        a = np.frombuffer(b, np.uint8)[:w * h * 4].reshape(h, w, 4)[:, :, [2, 1, 0, 3]]
        return Image.fromarray(a, 'RGBA')
    if fmt in ('PF_R8G8B8A8',):
        return Image.fromarray(np.frombuffer(b, np.uint8)[:w * h * 4].reshape(h, w, 4), 'RGBA')
    if fmt == 'PF_G8':
        return Image.fromarray(np.frombuffer(b, np.uint8)[:w * h].reshape(h, w), 'L')
    four = {'PF_DXT1': b'DXT1', 'PF_DXT5': b'DXT5', 'PF_BC4': b'ATI1', 'PF_BC5': b'ATI2'}.get(fmt)
    if four or fmt == 'PF_BC7':
        if fmt == 'PF_BC7':
            pf = struct.pack('<II4s20x', 32, 4, b'DX10')
            dx10 = struct.pack('<IIIII', 98, 3, 0, 1, 0)
        else:
            pf = struct.pack('<II4s20x', 32, 4, four)
            dx10 = b''
        hdr = struct.pack('<4sIIIIIII44x', b'DDS ', 124, 0x1007, h, w, len(b), 0, 1) + pf + struct.pack('<I16x', 0x1000)
        return Image.open(io.BytesIO(hdr + dx10 + b))
    raise ValueError('pixel format %s not supported' % fmt)


# ----------------------------------------------------------------------------- landscape
def _full_size(img, sb, warn):
    """A landscape heightmap/weightmap texture at the size its ScaleBias was made for (1/sb.x by 1/sb.y).
    Cooks can leave only a lower mip in the pak; scale that back up so component offsets line up."""
    h, w = img.shape
    fw = int(round(1.0 / sb[0])) if sb and sb[0] else w
    fh = int(round(1.0 / sb[1])) if sb and sb[1] else h
    if (fw, fh) == (w, h) or fw < w or fh < h or fw % w or fh % h:
        return img
    warn('only a %dx%d mip of a %dx%d landscape texture is in the pak; that area is lower resolution' % (
        w, h, fw, fh))
    return np.repeat(np.repeat(img, fh // h, axis=0), fw // w, axis=1)


def _fit(src, n, warn):
    """A component's n x n block; pads with edge values if the texture ran short instead of failing."""
    if src.shape == (n, n):
        return src
    warn('a component block was %dx%d instead of %dx%d; edges padded' % (src.shape[0], src.shape[1], n, n))
    if src.size == 0:
        return np.full((n, n), 32768 if src.dtype == np.uint16 else 0, src.dtype)
    out = np.pad(src[:n, :n], ((0, max(0, n - src.shape[0])), (0, max(0, n - src.shape[1]))), mode='edge')
    return out


def export_landscape(fs, ls, outdir, tag):
    p = fs.package(ls['_pkg'])
    actor_i = ls['_idx']
    by_name = {p.full_name(i + 1): i for i in range(len(p.exports))}
    comps = []
    for i, e in enumerate(p.exports):
        if e['outer'] == actor_i + 1 and (p.class_of(e) or '').endswith('.LandscapeComponent'):
            comps.append(p.export_props(i))
    if not comps:
        return None
    q = comps[0].get('ComponentSizeQuads', 63)
    ss = comps[0].get('SubsectionSizeQuads', q)
    ns = comps[0].get('NumSubsections', 1)
    bx = [c.get('SectionBaseX', 0) for c in comps]
    by = [c.get('SectionBaseY', 0) for c in comps]
    minx, miny = min(bx), min(by)
    W, H = max(bx) - minx + q + 1, max(by) - miny + q + 1
    hm = np.full((H, W), 32768, np.uint16)
    covered = np.zeros((H, W), bool)
    layers = {}
    texcache = {}

    def tex(name):
        if name not in texcache:
            idx = by_name.get(name)
            texcache[name] = top_mip(fs, p, idx) if idx is not None else (None, 0, 0, None)
        return texcache[name]

    missing_tex = 0
    warned = set()

    def warn(msg):
        if msg not in warned:
            warned.add(msg)
            print('  ! landscape:', msg)
    for c in comps:
        vx0, vy0 = c.get('SectionBaseX', 0) - minx, c.get('SectionBaseY', 0) - miny
        fmt, tw, th, b = tex(c.get('HeightmapTexture'))
        if b is None:
            missing_tex += 1
            continue
        t = np.frombuffer(b, np.uint8)[:tw * th * 4].reshape(th, tw, 4)
        sb = c.get('HeightmapScaleBias') or [1, 1, 0, 0]
        t16 = _full_size((t[:, :, 2].astype(np.uint16) << 8) | t[:, :, 1], sb, warn)
        th, tw = t16.shape
        tx0, ty0 = int(round(sb[2] * tw)), int(round(sb[3] * th))
        for sy in range(ns):
            for sx in range(ns):
                src = _fit(t16[ty0 + sy * (ss + 1): ty0 + sy * (ss + 1) + ss + 1,
                               tx0 + sx * (ss + 1): tx0 + sx * (ss + 1) + ss + 1], ss + 1, warn)
                y0, x0 = vy0 + sy * ss, vx0 + sx * ss
                hm[y0:y0 + ss + 1, x0:x0 + ss + 1] = src
                covered[y0:y0 + ss + 1, x0:x0 + ss + 1] = True
        # paint layers
        wts = c.get('WeightmapTextures') or []
        wsb = c.get('WeightmapScaleBias') or [1, 1, 0, 0]
        for al in c.get('WeightmapLayerAllocations') or []:
            li = al.get('LayerInfo')
            if not li:
                continue
            ti, ch = al.get('WeightmapTextureIndex', 0), al.get('WeightmapTextureChannel', 0)
            if ti >= len(wts):
                continue
            fmt, ww, wh, wb = tex(wts[ti])
            if wb is None:
                continue
            img = _full_size(np.frombuffer(wb, np.uint8)[:ww * wh * 4].reshape(wh, ww, 4)[:, :, [2, 1, 0, 3][ch]],
                             wsb, warn)
            wh, ww = img.shape
            wx0, wy0 = int(round(wsb[2] * ww)), int(round(wsb[3] * wh))
            L = layers.setdefault(li, np.zeros((H, W), np.uint8))
            for sy in range(ns):
                for sx in range(ns):
                    src = img[wy0 + sy * (ss + 1): wy0 + sy * (ss + 1) + ss + 1,
                              wx0 + sx * (ss + 1): wx0 + sx * (ss + 1) + ss + 1]
                    y0, x0 = vy0 + sy * ss, vx0 + sx * ss
                    L[y0:y0 + src.shape[0], x0:x0 + src.shape[1]] = src
    os.makedirs(outdir, exist_ok=True)
    hm_file = '%s_Heightmap_%dx%d.png' % (tag, W, H)
    Image.fromarray(hm).save(os.path.join(outdir, hm_file))
    layer_files = {}
    vis_file = None
    for li, L in layers.items():
        lp = fs.package(obj_path(li))
        lname = li.split('.')[-1].replace('_LayerInfo', '')
        if lp is not None:
            try:
                lname = lp.export_props(0).get('LayerName', lname)
            except Exception:
                pass
        if L.max() == 0:
            continue
        fn = '%s_Layer_%s.png' % (tag, lname)
        Image.fromarray(L, 'L').save(os.path.join(outdir, fn))
        # The engine's DataLayer is not a paint layer: it is the visibility mask, and where it is painted the
        # terrain is cut away entirely. Left unapplied it fills in every stairwell, trench and cellar the map
        # has, so it is kept apart from the layers that go in the material.
        if 'EditorLandscapeResources' in (obj_path(li) or '') or lname == 'DataLayer':
            vis_file = fn
        else:
            layer_files[lname] = fn
    sc = ls['scale']
    loc = ls['location']
    # The "New Landscape > Import" panel takes the CENTER of the landscape, the actor location is the corner
    centre = [round(loc[0] + (W - 1) * sc[0] / 2 + minx * sc[0], 3),
              round(loc[1] + (H - 1) * sc[1] / 2 + miny * sc[1], 3), loc[2]]
    actor_loc = [round(loc[0] + minx * sc[0], 3), round(loc[1] + miny * sc[1], 3), loc[2]]
    res = {
        'actor': ls['actor'], 'level': ls['_pkg'], 'material': ls['material'],
        'phys_material': ls.get('phys_material'),
        'collision_mip_level': ls.get('collision_mip_level'),
        'static_lighting_lod': ls.get('static_lighting_lod'),
        'lightmap_res': ls.get('lightmap_res'),
        'visibility_layer': vis_file,
        'heightmap': hm_file, 'layers': layer_files,
        'resolution': [W, H], 'section_size_quads': ss, 'sections_per_component': ns,
        'components': [(W - 1) // q, (H - 1) // q],
        'import_panel_location': centre, 'actor_location': actor_loc,
        'rotation': ls['rotation'], 'scale': sc,
        'height_range_cm': [round((int(hm.min()) - 32768) / 128 * sc[2], 1),
                            round((int(hm.max()) - 32768) / 128 * sc[2], 1)],
        'holes': int((~covered).sum()), 'missing_heightmaps': missing_tex,
    }
    return res


# ----------------------------------------------------------------------------- inventory
def classify_assets(fs, mods):
    inv = {'game_overrides': [], 'mod_assets': defaultdict(list)}
    for pkg in fs.packages('.uasset'):
        if pkg.startswith('/Game/'):
            inv['game_overrides'].append(pkg)
            continue
        if pkg.startswith('//'):
            continue
        p = fs.package(pkg)
        cls = '?'
        if p is not None and p.exports:
            # main export: the one named like the package (or its _C class)
            short = pkg.split('/')[-1]
            main = next((e for e in p.exports if e['name'] in (short, short + '_C')), p.exports[0])
            cls = (p.class_of(main) or '?').split('.')[-1]
        inv['mod_assets'][cls].append(pkg)
    return inv


def collect_refs(levels):
    refs = defaultdict(set)

    def walk(v, lvl):
        if isinstance(v, dict):
            for x in v.values():
                walk(x, lvl)
        elif isinstance(v, list):
            for x in v:
                walk(x, lvl)
        elif isinstance(v, str) and v.startswith('/') and not v.startswith('/Script/') and '.' in v:
            refs[obj_path(v)].add(lvl)

    for name, d in levels.items():
        for a in d['actors']:
            refs[obj_path(a['class'])].add(name)
            walk(a.get('props'), name)
            walk(a.get('mesh'), name)
            walk(a.get('materials'), name)
            for c in a['components'].values():
                walk(c.get('props'), name)
        for f in d['foliage'] + d.get('splines', []):
            walk(f['mesh'], name)
            walk(f.get('materials'), name)
    refs.pop(None, None)
    return {k: sorted(v) for k, v in refs.items() if not k.startswith('/Script')}


# ----------------------------------------------------------------------------- custom meshes + materials
def main_export(p, pkg):
    short = pkg.split('/')[-1]
    for i, e in enumerate(p.exports):
        if e['outer'] == 0 and e['name'] in (short, short + '_C'):
            return i
    return next((i for i, e in enumerate(p.exports) if e['outer'] == 0), 0)


def package_deps(p):
    return {im['name'] for im in p.imports if im['class_name'] == 'Package' and im['name'].startswith('/')}


def mod_closure(fs, roots, game_seeds=()):
    """Every mod (non-/Game) package reachable from roots through package imports. game_seeds are /Game packages
    to include as they are (the author's own masters shipped under /Game); their /Game dependencies are not followed."""
    seen, todo = set(), [r for r in roots if r and not r.startswith('/Game/') and not r.startswith('//')]
    todo += [g for g in game_seeds if g]
    while todo:
        k = todo.pop()
        if k in seen or not fs.exists(k + '.uasset'):
            continue
        seen.add(k)
        p = fs.package(k)
        if p is None:
            continue
        for d in package_deps(p):
            if d not in seen and not d.startswith('/Game/') and not d.startswith('/Script/') and not d.startswith('/Engine/'):
                todo.append(d)
    return seen


def tex_role(name):
    import re
    n = name.lower().split('.')[-1]
    if 'normal' in n or re.search(r'(nrm|_nor|_n)(_|\d|$)', n):
        return 'normal'
    if 'rough' in n or re.search(r'(_rma|_orm|_r)(_|\d|$)', n):
        return 'roughness'
    if re.search(r'(_ao|occlusion|_m$|mask|_msk|detail|height|disp|spec|metal|_h$|opacity|emiss|cavity|gloss|_mra|bump|'
                 r'cubemap|bokeh|noise|flow)', n):
        return 'other'
    if re.search(r'(albedo|_alb|basecolor|base_color|_bc|diffuse|_d$|_col|color|_c$)', n):
        return 'base_color'
    return 'maybe_base'


def pick_textures(paths):
    roles = {}
    for t in paths or []:
        if not isinstance(t, str):
            continue
        r = tex_role(t)
        roles.setdefault(r, obj_path(t))
    base = roles.get('base_color') or roles.get('maybe_base') or roles.get('other')
    return {'base_color': base, 'normal': roles.get('normal')}


def _gloss_tex_kind(name, is_param=False):
    """'masks' (RMA, roughness in R), 'rough', 'metal', or None. Colour and normal maps are never gloss sources."""
    import re
    n = (name or '').lower().split('/')[-1].split('.')[-1]
    if is_param:
        if 'mask' in n or 'rma' in n:
            return 'masks'
        if 'rough' in n:
            return 'rough'
        if 'metal' in n:
            return 'metal'
        return None
    if tex_role(n) == 'normal' or re.search(r'(albedo|basecolor|base_color|diffuse|_bc|_col|_d)(_|\d|$)', n):
        return None
    if re.search(r'(_m|_mask|_masks|_rma|_orm|_mra)$', n):
        return 'masks'
    if 'rough' in n:
        return 'rough'
    if 'metal' in n:
        return 'metal'
    return None


def mask_channel_stats(img):
    """Per-channel (mean, p10, p90) in 0..1 of a decoded texture, from a small thumbnail."""
    im = img.convert('RGBA')
    im.thumbnail((128, 128))
    out = {}
    for ch, band in zip('RGBA', im.split()):
        h = band.histogram()
        n = max(sum(h), 1)
        acc, p10, p90 = 0, None, None
        for i, c in enumerate(h):
            acc += c
            if p10 is None and acc >= n * 0.10:
                p10 = i
            if p90 is None and acc >= n * 0.90:
                p90 = i
        out[ch] = (round(sum(i * c for i, c in enumerate(h)) / n / 255, 3), (p10 or 0) / 255, (p90 or 0) / 255)
    return out


def pick_rough_channel(stats):
    """Which channel of a packed mask texture holds roughness. The name convention (RMA: R) is the default, but
    some asset packs (MAWI's _msk) pack R = height, B = occlusion, A = roughness. A varying alpha together with
    a mid-grey, height-shaped R channel is that layout; otherwise R stands."""
    def flat(ch):
        m, lo, hi = stats[ch]
        return hi - lo < 0.03
    if flat('A'):
        return 'R', 'single-layer mask, R'
    m, lo, hi = stats['R']
    if flat('R') or (0.35 <= m <= 0.65 and lo >= 0.2 and hi <= 0.8):
        return 'A', 'R is a height map (mean %.2f), roughness read from alpha' % m
    return 'R', 'R'


def base_gloss(r, textures):
    """Stand-in roughness/metallic for a custom base material.
    Final roughness = saturate(rough + rough_amt * texture[rough_ch]); same shape for metallic."""
    g = {'rough': 0.5}
    if r and r['kind'] == 'const':
        g['rough'] = r['value']
        return g
    if r and r['kind'] == 'dynamic':
        g['dynamic'] = True
        for t in textures or []:
            k = _gloss_tex_kind(t)
            if k == 'masks' and 'rough_tex' not in g:   # Masks (RMA): R is roughness
                g.update(rough=0.0, rough_tex=obj_path(t), rough_ch='R', rough_amt=1.0)
            elif k == 'rough' and 'rough_tex' not in g:
                g.update(rough=0.0, rough_tex=obj_path(t), rough_ch='R', rough_amt=1.0)
            elif k == 'metal' and 'metal_tex' not in g:
                g.update(metal_tex=obj_path(t), metal_ch='R', metal_amt=1.0)
    return g


def instance_gloss(parent_g, m):
    """A material instance's own scalar/texture parameters applied on top of its parent's gloss.
    Parameter values are inherited down the chain the way UE does it, then folded into rough/rough_amt."""
    g = dict(parent_g or {'rough': 0.5})
    sc = {(p.get('ParameterInfo') or {}).get('Name', ''): p.get('ParameterValue')
          for p in m.get('ScalarParameterValues') or []}
    for tp in m.get('TextureParameterValues') or []:
        nm = (tp.get('ParameterInfo') or {}).get('Name') or ''
        tv = tp.get('ParameterValue')
        if not isinstance(tv, str):
            continue
        k = _gloss_tex_kind(tv) if _gloss_tex_kind(nm, True) is None else _gloss_tex_kind(nm, True)
        if tex_role(tv) == 'normal' or k is None:
            continue
        if k in ('masks', 'rough'):
            g.update(rough_tex=obj_path(tv), rough_ch='R')
        elif k == 'metal':
            g.update(metal_tex=obj_path(tv), metal_ch='R', metal_amt=1.0)
    for name, key in (('Min Roughness', '_min'), ('Max Roughness', '_max'), ('RoughnessValue', '_rv')):
        if isinstance(sc.get(name), (int, float)):
            g[key] = sc[name]
    if g.get('rough_tex'):
        lo, hi = g.get('_min', 0.0), g.get('_max', 1.0)
        g['rough'] = lo + g.get('_rv', 0.0)       # RoughnessValue offsets the mask
        g['rough_amt'] = hi - lo
    elif g.get('dynamic') and '_rv' in g:
        g['rough'] = g['_rv']                       # no texture: the parameter is the roughness
    for k in ('rough', 'rough_amt', 'metal_amt'):
        if k in g:
            g[k] = round(float(g[k]), 3)
    return g


def route_stand_in_params(materials):
    """Instances anywhere below a stand-in: send their textures and colour to its BaseColor/Normal/Tint."""
    def root_of(pkg, depth=0):
        m = materials.get(pkg)
        if not m or depth > 20:
            return None
        if m.get('substitute'):
            return pkg
        return root_of(obj_path(m.get('Parent')), depth + 1)
    for pkg, m in materials.items():
        if m.get('substitute') or 'error' in m or not root_of(obj_path(m.get('Parent'))):
            continue
        roles = {}
        for tp in m.get('TextureParameterValues') or []:
            nm = (tp.get('ParameterInfo') or {}).get('Name')
            tv = tp.get('ParameterValue')
            if nm and isinstance(tv, str):
                # the parameter's name is the author's word for what the slot is; the texture's name is the fallback
                r = tex_role(nm) if tex_role(nm) != 'maybe_base' else tex_role(tv)
                roles[nm] = {'base_color': 'BaseColor', 'maybe_base': 'BaseColor', 'normal': 'Normal'}.get(r)
                roles[nm + '\0sure'] = r in ('base_color', 'normal')  # bookkeeping, dropped below
        # a guess ("maybe base colour") never displaces a parameter that says BaseColor or Normal outright
        for slot in ('BaseColor', 'Normal'):
            if any(v == slot and roles.get(k + '\0sure') for k, v in roles.items() if '\0' not in k):
                for k in [k for k, v in roles.items() if '\0' not in k and v == slot and not roles.get(k + '\0sure')]:
                    roles[k] = None
        roles = {k: v for k, v in roles.items() if '\0' not in k}
        # several explicit parameters for one slot (bark + the moss layered on top): the main surface wins
        for slot in ('BaseColor', 'Normal'):
            ks = [k for k, v in roles.items() if v == slot]
            if len(ks) > 1:
                keep = next((k for k in ks if 'top' not in k.lower() and 'layer' not in k.lower()), ks[0])
                for k in ks:
                    if k != keep:
                        roles[k] = None
        m['param_roles'] = roles
        vroles = {}
        for vp in m.get('VectorParameterValues') or []:
            nm = (vp.get('ParameterInfo') or {}).get('Name') or ''
            low = nm.lower()
            # only a parameter that is a colour becomes the tint: a normal multiplier (0.25, 0.25, 1) or a wind
            # vector read as a colour tints the whole surface blue
            is_colour = any(k in low for k in ('color', 'colour', 'tint', 'albedo', 'diffuse'))
            not_colour = any(k in low for k in ('uv', 'coord', 'offset', 'tiling', 'scale', 'emiss', 'normal', 'mask',
                                                'multiplier', 'wind', 'displace', 'intensity', 'power', 'rough',
                                                'spec', 'metal', 'ao', 'param'))
            vroles[nm] = 'Tint' if is_colour and not not_colour else None
        tints = [k for k, v in vroles.items() if v == 'Tint']
        if len(tints) > 1:
            # the pack's base colour, not its snow / winter / wet / dead variant
            variant = ('snow', 'winter', 'ice', 'wet', 'dry', 'dead', 'top', 'layer', 'alt', 'second', 'subsurface')
            keep = next((k for k in tints if not any(w in k.lower() for w in variant)), tints[0])
            for k in tints:
                if k != keep:
                    vroles[k] = None
        m['vector_roles'] = vroles
    # gloss, resolved top-down so each instance starts from its parent's
    def resolve(pkg, depth=0):
        m = materials.get(pkg)
        if not m or depth > 20:
            return None
        if m.get('substitute'):
            return m.get('gloss')
        if 'gloss' in m:
            return m['gloss']
        pg = resolve(obj_path(m.get('Parent')), depth + 1)
        if pg is None:
            return None
        m['gloss'] = instance_gloss(pg, m)
        return m['gloss']
    for pkg in list(materials):
        if 'error' not in materials[pkg]:
            resolve(pkg)


GRASS_TYPES = {}  # landscape grass types (auto-spawned ground grass), filled by export_custom_meshes


def export_mesh(fs, p, i, pkg, mdir):
    """One cooked StaticMesh -> FBX under meshes/. Returns its layout entry."""
    from .staticmesh import read_static_mesh, read_collision, write_fbx
    m = read_static_mesh(p, i, ubulk=lambda ext, off, size, _k=pkg: (
        fs.bulk(_k, ext, off, size) if fs.exists(_k + ext) else None))
    col = read_collision(p, m['body_export'])
    rel = pkg.strip('/') + '.fbx'
    fn = os.path.join(mdir, *rel.split('/'))
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    hulls = write_fbx(fn, m, col)
    # normals_ok: the FBX carries the original normals, decoded correctly (r39 on); the rebuild imports them
    # instead of computing new ones. Older extracts lack the flag and keep the computed normals
    return {'file': 'meshes/' + rel, 'materials': m['materials'], 'normals_ok': True,
            'lightmap_index': m['lightmap_index'], 'lightmap_res': m['lightmap_res'],
            'trace': col['trace'], 'profile': col['profile'], 'hulls': hulls,
            'verts': len(m['verts']), 'tris': len(m['tris']), 'colors': bool(m['colors']),
            'bounds': [[round(min(v[k] for v in m['verts']), 3) for k in range(3)],
                       [round(max(v[k] for v in m['verts']), 3) for k in range(3)]]}


def _texture_closure(fs, paths, depth=0, seen=None):
    """The textures a material really uses: its own texture imports, plus those of the material functions and
    materials it imports (marketplace packs keep per-surface textures inside MTL_*_MF functions). Only packages
    that are textures come back; references outside the paks are kept as they are (they may be game textures)."""
    seen = set() if seen is None else seen
    out = []
    for t in paths:
        pkg = obj_path(t) if (isinstance(t, str) and '.' in t) else t
        if not pkg or pkg in seen:
            continue
        seen.add(pkg)
        if pkg.startswith('/Engine/') or not fs.exists(pkg + '.uasset'):
            # outside the paks: keep it only if it is plausibly a texture (engine material functions, parameter
            # collections and the like must never reach a texture slot)
            low = pkg.lower()
            if '/functions/' in low or '/materials/' in low or low.split('/')[-1].startswith(('mf_', 'mpc_', 'm_', 'mi_', 'mtl_')):
                continue
            out.append(t)
            continue
        p = fs.package(pkg)
        if p is None or not p.exports:
            continue
        cls = (p.class_of(p.exports[main_export(p, pkg)]) or '').split('.')[-1]
        if cls.startswith('Texture'):
            out.append(pkg)
        elif cls in ('MaterialFunction', 'MaterialFunctionInstance', 'Material', 'MaterialInstanceConstant') and depth < 4:
            deps = [d for d in package_deps(p) if not d.startswith('/Script/')]
            out += [x for x in _texture_closure(fs, deps, depth + 1, seen) if x not in out]
    return out


def export_custom_meshes(fs, out, roots, materials, game_masters=()):
    closure = mod_closure(fs, roots, game_masters)
    from . import gloss
    meshes, subs, errors = {}, {}, {}
    rfinds = {}
    mdir = os.path.join(out, 'meshes')
    for pkg in sorted(closure):
        p = fs.package(pkg)
        if p is None or not p.exports:
            continue
        i = main_export(p, pkg)
        cls = (p.class_of(p.exports[i]) or '').split('.')[-1]
        if cls == 'StaticMesh':
            try:
                meshes[pkg] = export_mesh(fs, p, i, pkg, mdir)
            except Exception as ex:
                errors[pkg] = str(ex)
        elif cls == 'LandscapeGrassType':
            try:
                GRASS_TYPES[pkg] = p.export_props(i)
            except Exception as ex:
                errors[pkg] = 'grass type: %s' % ex
        elif cls == 'Material':
            try:
                pr = p.export_props(i)
            except Exception:
                pr = {}
            texs = [t for t in ((pr.get('CachedExpressionData') or {}).get('ReferencedTextures') or [])
                    if isinstance(t, str)]
            if not texs:
                texs = [im['name'] for im in p.imports if im['class_name'] == 'Package'
                        and fs.exists(im['name'] + '.uasset')]
            texs = _texture_closure(fs, texs)  # through material functions down to the textures themselves
            picked = pick_textures(texs)
            try:
                e = p.exports[i]
                rfinds[pkg] = gloss.roughness_finds(gloss.shader_blobs(p.data[e['off']:e['off'] + e['size']]))
            except Exception:
                pass
            subs[pkg] = {'substitute': True, 'textures': [obj_path(t) if '.' in t else t for t in texs],
                         'base_color': picked['base_color'], 'normal': picked['normal'],
                         'two_sided': bool(pr.get('TwoSided')), 'blend_mode': pr.get('BlendMode'),
                         'phys_material': pr.get('PhysMaterial'),
                         'opacity_mask_clip': pr.get('OpacityMaskClipValue')}
    # how glossy each custom material is, read from its compiled shaders
    key = gloss.override_key(rfinds.values())
    for pkg, f in rfinds.items():
        r = gloss.classify(f, key)
        subs[pkg]['gloss'] = base_gloss(r, subs[pkg]['textures'])
    # material instances whose parent is one of those substitutes: say which texture is which
    for pkg, m in materials.items():
        if 'error' in m:
            continue
        par = obj_path(m.get('Parent'))
        if par in subs:
            roles = {}
            for tp in m.get('TextureParameterValues') or []:
                nm = (tp.get('ParameterInfo') or {}).get('Name')
                tv = tp.get('ParameterValue')
                if nm and isinstance(tv, str):
                    r = tex_role(nm) if tex_role(nm) in ('normal', 'roughness', 'base_color') else tex_role(tv)
                    roles[nm] = {'base_color': 'BaseColor', 'maybe_base': 'BaseColor', 'normal': 'Normal'}.get(r)
            m['param_roles'] = roles
    return meshes, subs, errors


# ----------------------------------------------------------------------------- blueprints
BP_SKIP_CDO = {'UberGraphFrame', 'bHasNativizedParent', 'SimpleConstructionScript', 'InheritableComponentHandler',
               'UberGraphFunction', 'RootComponent'}


def _bytecode_vectors(d):
    import struct as st
    out = []
    for o in range(len(d) - 13):
        if d[o] == 0x23:  # EX_VectorConst
            v = st.unpack_from('<fff', d, o + 1)
            if all(abs(x) < 1e6 for x in v) and any(abs(x) > 1 for x in v) and \
                    all(abs(x - round(x, 2)) < 1e-3 for x in v):
                out.append([round(x, 3) for x in v])
    return out


def analyze_blueprint(fs, pkg):
    """What a cooked Blueprint class is made of: parent, changed defaults, components, and whether it has logic."""
    p = fs.package(pkg)
    if p is None:
        return None
    short = pkg.split('/')[-1]
    gen = next((i for i, e in enumerate(p.exports) if e['name'] == short + '_C'), None)
    if gen is None:
        return None
    info = {'parent': p.full_name(p.exports[gen]['sup']), 'cdo': {}, 'components': {}, 'functions': [],
            'calls': [], 'vectors': []}
    for i, e in enumerate(p.exports):
        cls = (p.class_of(e) or '').split('.')[-1]
        if e['name'] == 'Default__' + short + '_C':
            try:
                pr = p.export_props(i)
            except Exception:
                pr = {}
            info['cdo'] = {k: v for k, v in pr.items() if k not in BP_SKIP_CDO and
                           not (isinstance(v, str) and v.startswith('Default__'))}
        elif e['name'].endswith('_GEN_VARIABLE'):
            try:
                info['components'][e['name'][:-len('_GEN_VARIABLE')]] = {'class': p.class_of(e), 'props': p.export_props(i)}
            except Exception:
                pass
        elif cls == 'Function' and not e['name'].endswith('__DelegateSignature'):
            info['functions'].append(e['name'])
            if e['name'].startswith('ExecuteUbergraph'):
                info['vectors'] += _bytecode_vectors(p.data[e['off']:e['off'] + e['size']])
    info['calls'] = sorted({im['name'] for im in p.imports if im['class_name'] == 'Function'
                            and im['name'] != 'Default__Function'} |
                           {n for n in p.names if n in ('LaunchCharacter', 'PostAkEvent', 'SpawnEmitterAtLocation',
                                                        'SetActorLocation', 'K2_SetActorLocation', 'AddImpulse')})
    logic = [f for f in info['functions'] if f.startswith('ExecuteUbergraph') or not f.startswith('BndEvt')]
    if not info['functions']:
        info['kind'] = 'property_only'
    elif 'LaunchCharacter' in info['calls'] and len(info['vectors']) == 1 and \
            any(c['class'].endswith('CapsuleComponent') for c in info['components'].values()):
        info['kind'] = 'jump_pad'
        info['launch_velocity'] = info['vectors'][0]
    elif info['parent'] and info['parent'].endswith('.Emitter') and set(info['functions']) <= {
            'ReceiveBeginPlay', 'ExecuteUbergraph_' + short}:
        info['kind'] = 'property_only'  # an Emitter that only (re)activates itself on BeginPlay
    else:
        info['kind'] = 'logic'
    info['has_logic'] = bool(logic)
    return info


def analyze_blueprints(fs, levels, scenarios):
    roots = set()
    for d in levels.values():
        for a in d['actors']:
            roots.add(obj_path(a['class']))
    for sv in scenarios.values():
        if isinstance(sv.get('GameMode'), str):
            roots.add(obj_path(sv['GameMode']))
    out = {}
    todo = [r for r in roots if r and not r.startswith('/Script') and not r.startswith('/Game/')
            and not r.startswith('/Engine/')]
    while todo:
        pkg = todo.pop()
        if pkg in out or not fs.exists(pkg + '.uasset'):
            continue
        info = analyze_blueprint(fs, pkg)
        if info is None:
            continue
        out[pkg] = info
        # classes this one points at (e.g. a game mode's DefaultPawnClass) need rebuilding first
        for v in info['cdo'].values():
            q = obj_path(v) if isinstance(v, str) else None
            if q and fs.exists(q + '.uasset') and not q.startswith('/Game/'):
                todo.append(q)
    return out


GAMEPLAY_CLASSES = ('INSPlayerStart', 'SpawnZone', 'SpawnZoneCounterAttack', 'CaptureZone', 'ObjectiveCapturable',
                    'ObjectiveDestructible', 'ObjectiveWeaponCache', 'Obj_WeaponCache_Ins_C', 'Obj_WeaponCache_Sec_C',
                    'INSPatrolArea', 'RestrictedArea')


def is_gameplay_level(d):
    """Scenario sublevels (spawns, objectives) are rebuilt and re-linked to the scenarios; never swap in the original."""
    return any(a['class'].split('.')[-1] in GAMEPLAY_CLASSES for a in d.get('actors') or [])


GRAFT_REF = re.compile(rb'/[A-Za-z0-9_]+/[A-Za-z0-9_/\-]+')


def fix_dynamic_class_imports(head):
    """The game's own Blueprints were nativized when older mods were cooked, so those mods import them as
    /Script/CoreUObject.DynamicClass. The current game ships them as plain BlueprintGeneratedClass, and the loader
    refuses the import ("class mismatch BlueprintGeneratedClass != DynamicClass"): every cast to BP_Character_Player
    fails and every spawn of BP_Projectile_M67 spawns nothing. Rewrite those imports in place (same size)."""
    try:
        p = Package(head)
    except Exception:
        return head, 0
    if 'DynamicClass' not in p.names or 'BlueprintGeneratedClass' not in p.names or '/Script/Engine' not in p.names:
        return head, 0
    bpgc, eng = p.names.index('BlueprintGeneratedClass'), p.names.index('/Script/Engine')
    b, n = bytearray(head), 0
    for i, im in enumerate(p.imports):
        if im['class_name'] == 'DynamicClass':
            o = p.import_off + i * 28
            struct.pack_into('<iiii', b, o, eng, 0, bpgc, 0)
            n += 1
    return bytes(b), n


def export_graft_files(fs, levels, persistent, blueprints, mod, out):
    """The original cooked files behind the mod's scripted Blueprints, for the graft pak: each placed Blueprint
    with graph logic plus everything in the paks it references, followed through the references. This includes
    files the mod shipped under /Game/ (TORO's chicken skeleton, physics asset and animations live at
    /Game/FarmAnimalsPack/, not /TORO/). Written as graft/<root>/<path> with the raw .uasset/.uexp/.ubulk/.uptnl,
    levels excluded. The SDK step decides which of them the rebuilt mod still needs.
    A streamed sublevel whose level Blueprint has logic (TORO_EasterEgg: shoot five chickens and they all attack)
    is kept whole too: the level Blueprint lives inside the level file, so the graft pak ships the original level
    in place of the rebuilt one. Persistent levels are never replaced."""
    import shutil
    if not mod:
        return 0, []
    seeds = set()
    glevels = sorted(m for m, d in levels.items() if d.get('level_script') and m not in persistent
                     and m.startswith('/' + mod + '/') and not is_gameplay_level(d))
    for m in glevels:
        seeds.add(m)
        seeds.add(m + '_BuiltData')
        d = levels[m]
        d['warnings'] = [w if 'level blueprint with logic' not in w else
                         '%s has a level blueprint with logic (%s); the graft pak ships the original level in place '
                         'of the rebuilt one, so edits to this level in the editor will not be in the game' % (
                             m.split('/')[-1], ', '.join(d['level_script'])) for w in d['warnings']]
    for d in levels.values():
        for a in d['actors']:
            pkg = obj_path(a['class'])
            info = blueprints.get(pkg)
            if info and info.get('has_logic') and pkg.startswith('/' + mod + '/'):
                seeds.add(pkg)
    todo, seen = list(seeds), set()
    while todo:
        pkg = todo.pop()
        head = pkg + ('.umap' if fs.exists(pkg + '.umap') else '.uasset')
        if pkg in seen or not fs.exists(head):
            continue
        seen.add(pkg)
        for m in GRAFT_REF.findall(fs.read(head)):
            q = m.decode('ascii', 'replace').split('.')[0]
            if q not in seen and fs.exists(q + '.uasset'):
                todo.append(q)
    dest = os.path.join(out, 'graft')
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    fixed = 0
    for pkg in sorted(seen):
        for ext in ('.umap', '.uasset', '.uexp', '.ubulk', '.uptnl'):
            if fs.exists(pkg + ext):
                f = os.path.join(dest, *pkg.strip('/').split('/')) + ext
                os.makedirs(os.path.dirname(f), exist_ok=True)
                data = fs.read(pkg + ext)
                if ext in ('.uasset', '.umap'):
                    data, k = fix_dynamic_class_imports(data)
                    fixed += k
                with open(f, 'wb') as fh:
                    fh.write(data)
    if seen:
        print('  %d original packages kept in graft/ for the scripted Blueprints%s' % (
            len(seen), ' and level(s) ' + ', '.join(m.split('/')[-1] for m in glevels) if glevels else ''))
    if fixed:
        print('  %d import(s) of nativized game Blueprints pointed at the current classes' % fixed)
    return len(seen), glevels


def export_sounds(paks, out):
    """Wwise banks with PCM audio -> plain .wav files (named after the bank). paks: the ModFS's open Pak objects."""
    import struct as st
    res = []
    for pak in paks:
        for ent in pak.entries:
            if not ent.lower().endswith('.bnk'):
                continue
            d = pak.read(ent)
            secs, o = {}, 0
            while o < len(d) - 8:
                tag = d[o:o + 4]
                n = st.unpack_from('<I', d, o + 4)[0]
                secs[tag] = (o + 8, n)
                o += 8 + n
            if b'DIDX' not in secs or b'DATA' not in secs:
                continue
            a, n = secs[b'DIDX']
            da = secs[b'DATA'][0]
            for k in range(n // 12):
                wid, off, sz = st.unpack_from('<III', d, a + k * 12)
                w = d[da + off:da + off + sz]
                fo, do = w.find(b'fmt '), w.find(b'data')
                if fo < 0 or do < 0:
                    continue
                fmt, ch, rate, _, _, bits = st.unpack_from('<HHIIHH', w, fo + 8)
                if fmt not in (1, 0xFFFE) or bits != 16:
                    res.append({'bank': ent, 'error': 'compressed audio (codec 0x%04x); extract with vgmstream' % fmt})
                    continue
                dl = st.unpack_from('<I', w, do + 4)[0]
                pcm = w[do + 8:do + 8 + dl]
                name = os.path.splitext(os.path.basename(ent))[0] + ('' if n == 12 else '_%d' % k)
                os.makedirs(os.path.join(out, 'sounds'), exist_ok=True)
                fn = os.path.join(out, 'sounds', name + '.wav')
                with open(fn, 'wb') as fh:
                    fh.write(b'RIFF' + st.pack('<I', 36 + len(pcm)) + b'WAVEfmt ' +
                             st.pack('<IHHIIHH', 16, 1, ch, rate, rate * ch * 2, ch * 2, 16) +
                             b'data' + st.pack('<I', len(pcm)) + pcm)
                res.append({'bank': ent, 'file': 'sounds/' + name + '.wav', 'channels': ch, 'rate': rate,
                            'seconds': round(len(pcm) / (rate * ch * 2), 2)})
    return res


# ----------------------------------------------------------------------------- main
def order_mods(fs, pak_paths):
    """The mod roots in the paks, the one that owns the maps first; any others are dependencies.
    The mod the paks are named after comes first, then the one with the most maps (a pak can carry a few maps
    from other mods, e.g. TORO also ships /Assault/Maps/Assault_Survival)."""
    map_roots = [m.split('/')[1] for m in fs.packages('.umap') if not m.startswith('/Game/') and not m.startswith('//')]
    pak_name = re.split(r'pakchunk', os.path.basename(pak_paths[0]), flags=re.I)[0].lower() if pak_paths else ''
    return sorted(fs.mods, key=lambda m: (m.lower() != pak_name, -map_roots.count(m), m))


def run(pak_paths, out):
    os.makedirs(out, exist_ok=True)
    GRASS_TYPES.clear()  # module-level collectors; the app can extract several maps in one session
    MIP_FIXES.clear(); del LOW_RES[:]
    with ModFS(pak_paths) as fs:  # closed even when the extraction fails, so the paks aren't left locked
        return _run(fs, pak_paths, out)


def _run(fs, pak_paths, out):
    pak_warnings = []
    mods = order_mods(fs, pak_paths)
    print('mods in pak:', ', '.join(mods) or '(none: base-game paths only)')
    ex = LevelExtractor(fs)
    maps = fs.packages('.umap')
    levels = {}
    for m in maps:
        d = ex.extract(m)
        if d is None:
            print('  ! could not read level', m)
            continue
        levels[m] = d
        print('  %-48s actors=%5d foliage=%5d landscapes=%d' % (
            m, len(d['actors']), sum(len(f['instances']) for f in d['foliage']), len(d['landscapes'])))

    # scenarios / ModData / other data assets
    scenarios, moddata = {}, {}
    for pkg in fs.packages('.uasset'):
        if pkg.startswith('/Game/') or pkg.startswith('//'):
            continue
        p = fs.package(pkg)
        if p is None or not p.exports:
            continue
        cls = (p.class_of(p.exports[0]) or '').split('.')[-1]
        if cls.startswith('Scenario'):
            try:
                scenarios[pkg] = {'class': cls, **p.export_props(0)}
            except Exception as ex2:
                scenarios[pkg] = {'class': cls, 'error': str(ex2)}
        elif cls == 'ModData':
            try:
                moddata[pkg] = p.export_props(0)
            except Exception:
                pass

    # persistent levels = levels not streamed in by any other level, plus any level a scenario plays on: an author
    # can leave an empty wrapper level that streams the real map as a sublevel (Abandoned Valley's AV), which
    # would otherwise demote the real map to a sublevel and leave the scenarios pointing at nothing
    streamed = {s['level'] for d in levels.values() for s in d['streaming']}
    played = {obj_path(s.get('Level')) for s in scenarios.values() if isinstance(s.get('Level'), str)}
    persistent = [m for m in levels if m not in streamed or m in played]

    # landscapes
    landscapes = []
    for m, d in levels.items():
        for k, ls in enumerate(d['landscapes']):
            tag = m.split('/')[-1] + ('' if k == 0 else '_%d' % k)
            res = export_landscape(fs, ls, os.path.join(out, 'landscape'), tag)
            if res:
                landscapes.append(res)
        d['landscapes'] = [x['actor'] for x in d['landscapes']]

    # assets the old mod shipped under /Game that are NEW (not base-game copies) but used by the map:
    # a mod can't write to /Game, so these get relocated into the mod folder
    ws_refs = set()
    for d in levels.values():
        for v in (d['world_settings'] or {}).values():
            for x in (v if isinstance(v, list) else v.values() if isinstance(v, dict) else [v]):
                for y in (x.values() if isinstance(x, dict) else [x]):
                    if isinstance(y, str) and y.startswith('/Game/'):
                        ws_refs.add(obj_path(y))
    relocate = {}
    root = '/' + mods[0] if mods else None

    def want_relocated(pkg):
        return root and pkg.startswith('/Game/') and pkg in ws_refs and fs.exists(pkg + '.uasset')

    # /Game assets the map uses that the old pak also shipped. Usually base-game copies, but some are the
    # author's own (e.g. MI_Construction_Tarp_Missing). The rebuild only relocates these if the SDK lacks them.
    level_refs = collect_refs(levels)
    # the landscape's material is referenced by the Landscape actor only, which the actor walk does not cover
    for lsd in landscapes:
        lm = obj_path(lsd.get('material')) if isinstance(lsd.get('material'), str) else None
        if lm:
            level_refs.setdefault(lm, []).append(lsd['level'])
    lazy = {k for k in level_refs if k.startswith('/Game/') and fs.exists(k + '.uasset') and k not in ws_refs}
    # landscape grass types (the auto-spawned ferns, needles, pebbles) a pack keeps under /Game: reachable from the
    # landscape material through its instances and functions. Their meshes are carried in like any /Game mesh
    game_grass = {}
    todo_g = [obj_path(lsd.get('material')) for lsd in landscapes if isinstance(lsd.get('material'), str)]
    seen_g = set()
    while todo_g:
        pkg = todo_g.pop()
        if not pkg or pkg in seen_g or not pkg.startswith('/Game/') or not fs.exists(pkg + '.uasset'):
            continue
        seen_g.add(pkg)
        p = fs.package(pkg)
        if p is None or not p.exports:
            continue
        cls = (p.class_of(p.exports[main_export(p, pkg)]) or '').split('.')[-1]
        if cls == 'LandscapeGrassType':
            try:
                game_grass[pkg] = p.export_props(main_export(p, pkg))
            except Exception:
                continue
            for v in game_grass[pkg].get('GrassVarieties') or []:
                gm = obj_path(v.get('GrassMesh')) if isinstance(v.get('GrassMesh'), str) else None
                if gm and gm.startswith('/Game/') and fs.exists(gm + '.uasset'):
                    lazy.add(gm)
        elif cls in ('Material', 'MaterialInstanceConstant', 'MaterialFunction', 'MaterialFunctionInstance') \
                and len(seen_g) < 400:
            todo_g += [d for d in package_deps(p) if d.startswith('/Game/')]
    # the material instances a /Game mesh's slots point at are only reachable through the mesh; if the game lacks
    # the mesh it usually lacks those too (SM_Glowstick -> MI_Glowstick), so they get the same fallback
    for pkg in sorted(lazy):
        p = fs.package(pkg)
        if p is None or not p.exports:
            continue
        if (p.class_of(p.exports[main_export(p, pkg)]) or '').split('.')[-1] != 'StaticMesh':
            continue
        for d in package_deps(p):
            if d.startswith('/Game/') and d not in lazy and fs.exists(d + '.uasset'):
                q = fs.package(d)
                if q is not None and q.exports and (q.class_of(q.exports[main_export(q, d)]) or '').endswith(
                        'MaterialInstanceConstant'):
                    lazy.add(d)
    fallback = {}
    for pkg, props in game_grass.items():
        GRASS_TYPES[pkg] = props
        if root:
            fallback[pkg] = root + '/Relocated' + pkg[len('/Game'):]
    if game_grass:
        print('  %d landscape grass types found under /Game' % len(game_grass))

    # material instances shipped by the mod (tactical map materials etc.)
    materials = {}
    for pkg in fs.packages('.uasset'):
        if pkg.startswith('//') or (pkg.startswith('/Game/') and not want_relocated(pkg) and pkg not in lazy):
            continue
        p = fs.package(pkg)
        if p is None or not p.exports or not (p.class_of(p.exports[0]) or '').endswith('MaterialInstanceConstant'):
            continue
        try:
            pr = p.export_props(0)
            materials[pkg] = {k: pr.get(k) for k in ('Parent', 'ScalarParameterValues', 'VectorParameterValues',
                                                      'TextureParameterValues', 'PhysMaterial',
                                                      'BasePropertyOverrides') if pr.get(k) is not None}
            if pkg.startswith('/Game/'):
                target = relocate if pkg not in lazy else fallback
                if pkg in lazy:
                    materials[pkg]['lazy'] = True
                target[pkg] = root + '/Relocated' + pkg[len('/Game'):]
                for tp in materials[pkg].get('TextureParameterValues') or []:
                    tv = obj_path(tp.get('ParameterValue'))
                    if tv and tv.startswith('/Game/') and fs.exists(tv + '.uasset'):
                        target[tv] = root + '/Relocated' + tv[len('/Game'):]
        except Exception as ex2:
            materials[pkg] = {'error': str(ex2)}

    inv = classify_assets(fs, mods)
    refs = collect_refs(levels)
    # the tactical map material is referenced from World Settings only; it needs a stand-in like any other
    for name, d in levels.items():
        tm = (d.get('world_settings') or {}).get('TacticalMapMaterial')
        if isinstance(tm, str) and tm.startswith('/'):
            refs.setdefault(obj_path(tm), []).append(name)
    in_pak = {k[:-len('.uasset')] for k in fs.files if k.endswith('.uasset')}
    for sk, sv in scenarios.items():
        for key in ('GameMode', 'DefaultTheater'):
            v = sv.get(key)
            if isinstance(v, str) and v.startswith('/') and not v.startswith('/Script/'):
                refs.setdefault(obj_path(v), []).append(sk)
    custom_refs = {k: v for k, v in refs.items() if k in in_pak and not k.startswith('/Game/')}
    absent = sorted(k for k in refs if not k.startswith(('/Game/', '/Engine/', '/Script/')) and k not in in_pak
                    and fs.mods and k.split('/')[1] in fs.mods)
    if absent:
        pak_warnings.append('%d asset(s) the levels use are not in any of the paks given (%s%s). If the mod has more '
                            'paks on mod.io, add them and extract again; otherwise the author never shipped them. '
                            'Chunk numbers with gaps (chunk0, chunk1, chunk10) are normal and do not mean a pak is '
                            'missing.' % (len(absent), ', '.join(a.split('/')[-1] for a in absent[:8]),
                                          '...' if len(absent) > 8 else ''))
        print('  ! ' + pak_warnings[-1])
    override_refs = {k: v for k, v in refs.items() if k in in_pak and k.startswith('/Game/')}

    # custom static meshes -> FBX, custom materials -> simple stand-ins built from their textures
    print('exporting custom meshes...')
    meshes, subs, mesh_errors = export_custom_meshes(fs, out, list(custom_refs) + list(materials), materials)
    # meshes the old pak shipped under /Game (e.g. a marketplace pack installed into Content/): the rebuild
    # uses the SDK's copy if there is one, else imports this FBX inside the mod
    mdir = os.path.join(out, 'meshes')
    game_mesh_errors = {}
    for pkg in sorted(lazy):
        p = fs.package(pkg)
        if p is None or not p.exports:
            continue
        i = main_export(p, pkg)
        if (p.class_of(p.exports[i]) or '').split('.')[-1] != 'StaticMesh':
            continue
        try:
            meshes[pkg] = dict(export_mesh(fs, p, i, pkg, mdir), lazy=True)
            fallback[pkg] = root + '/Relocated' + pkg[len('/Game'):]
        except Exception as ex:
            # usually a base-game copy the SDK already has; only matters if the rebuild logs it MISSING
            game_mesh_errors[pkg] = str(ex)
    # /Game materials the meshes' slots point at that the pak ships: Merge Actors saves the merged material under
    # /Game next to the source meshes (MallConflict's M_SM_StoreFrontSet_..._2C059A1C...), so the SDK never has it,
    # and the walks above only reach /Game materials through levels and /Game meshes, not through the mod's own
    # meshes. Carried like any lazy instance: rebuilt inside the mod only if the game lacks it
    for me in list(meshes.values()):
        for slot in me.get('materials') or []:
            mp = obj_path(slot.get('material')) if isinstance(slot.get('material'), str) else None
            if not root or not mp or not mp.startswith('/Game/') or mp in materials or mp in lazy \
                    or not fs.exists(mp + '.uasset'):
                continue
            q = fs.package(mp)
            if q is None or not q.exports:
                continue
            qcls = (q.class_of(q.exports[main_export(q, mp)]) or '').split('.')[-1]
            if qcls == 'Material':
                lazy.add(mp)  # the master-material pass below gives it a stand-in
            elif qcls == 'MaterialInstanceConstant':
                try:
                    pr = q.export_props(main_export(q, mp))
                except Exception as ex2:
                    materials[mp] = {'error': str(ex2)}
                    continue
                materials[mp] = {k: pr.get(k) for k in ('Parent', 'ScalarParameterValues', 'VectorParameterValues',
                                                         'TextureParameterValues', 'PhysMaterial',
                                                         'BasePropertyOverrides') if pr.get(k) is not None}
                materials[mp]['lazy'] = True
                fallback[mp] = root + '/Relocated' + mp[len('/Game'):]
                for tp in materials[mp].get('TextureParameterValues') or []:
                    tv = obj_path(tp.get('ParameterValue'))
                    if tv and tv.startswith('/Game/') and fs.exists(tv + '.uasset'):
                        fallback[tv] = root + '/Relocated' + tv[len('/Game'):]
    # the author's own master materials shipped under /Game (MM_PBR_CUSTOM...): the SDK doesn't have them, so the
    # instances that use them would have no parent. Stand-ins for those too, rebuilt inside the mod only if the
    # game really lacks them (same fallback rule as the /Game meshes and instances above)
    game_masters = set()
    # walk every parent chain: marketplace packs chain instance -> instance -> master (Rock01 -> Rocks_MASTER ->
    # General_MASTER). Intermediate instances under /Game are carried into the mod like any lazy instance; the
    # Material at the top gets the stand-in
    todo_parents = [obj_path(m.get('Parent')) for m in materials.values() if isinstance(m, dict)]
    seen_parents = set()
    while todo_parents:
        par = todo_parents.pop()
        if not par or par in seen_parents or not par.startswith('/Game/') or not fs.exists(par + '.uasset'):
            continue
        seen_parents.add(par)
        p = fs.package(par)
        if p is None or not p.exports:
            continue
        cls = (p.class_of(p.exports[main_export(p, par)]) or '').split('.')[-1]
        if cls == 'Material':
            game_masters.add(par)
        elif cls == 'MaterialInstanceConstant' and par not in materials and root:
            try:
                pr = p.export_props(main_export(p, par))
            except Exception as ex2:
                materials[par] = {'error': str(ex2)}
                continue
            materials[par] = {k: pr.get(k) for k in ('Parent', 'ScalarParameterValues', 'VectorParameterValues',
                                                      'TextureParameterValues', 'PhysMaterial',
                                                      'BasePropertyOverrides') if pr.get(k) is not None}
            materials[par]['lazy'] = True
            fallback[par] = root + '/Relocated' + par[len('/Game'):]
            for tp in materials[par].get('TextureParameterValues') or []:
                tv = obj_path(tp.get('ParameterValue'))
                if tv and tv.startswith('/Game/') and fs.exists(tv + '.uasset'):
                    fallback[tv] = root + '/Relocated' + tv[len('/Game'):]
            todo_parents.append(obj_path(materials[par].get('Parent')))
    for pkg in lazy:
        p = fs.package(pkg)
        if p is not None and p.exports and (p.class_of(p.exports[main_export(p, pkg)]) or '').endswith('.Material'):
            game_masters.add(pkg)
    if game_masters and root:
        _, gsubs, _ = export_custom_meshes(fs, out, [], materials, game_masters)
        for pkg, v in gsubs.items():
            if pkg.startswith('/Game/') and pkg not in subs:
                v['lazy'] = True
                subs[pkg] = v
                fallback[pkg] = root + '/Relocated' + pkg[len('/Game'):]
                for t in v.get('textures') or []:
                    if isinstance(t, str) and t.startswith('/Game/') and fs.exists(t + '.uasset'):
                        fallback[t] = root + '/Relocated' + t[len('/Game'):]
    for k, v in subs.items():
        materials.setdefault(k, v)
    print('  %d meshes written to meshes/, %d custom materials get stand-ins, %d meshes failed' % (
        len(meshes), len(subs), len(mesh_errors)))
    # textures (UI etc.) outside map packages
    tex_out = os.path.join(out, 'textures')
    textures = []
    tex_stats = {}
    for pkg in fs.packages('.uasset'):
        if pkg.startswith('//') or (pkg.startswith('/Game/') and pkg not in relocate and pkg not in fallback):
            continue
        p = fs.package(pkg)
        if p is None or not p.exports or not (p.class_of(p.exports[0]) or '').endswith('.Texture2D'):
            continue
        try:
            fmt, w, h, b = top_mip(fs, p, 0)
            if b is None:
                continue
            img = decode_texture(fmt, w, h, b)
            os.makedirs(tex_out, exist_ok=True)
            fn = pkg.strip('/').replace('/', '__') + '.png'
            img.save(os.path.join(tex_out, fn))
            if tex_role(pkg) in ('roughness', 'other'):  # a possible gloss source: remember how its channels look
                try:
                    tex_stats[pkg] = mask_channel_stats(img)
                except Exception:
                    pass
            tp = {}
            try:
                tp = {k: v for k, v in p.export_props(0).items() if k in ('LODGroup', 'SRGB', 'CompressionSettings',
                                                                          'MipGenSettings', 'NeverStream')}
            except Exception:
                pass
            textures.append({'asset': pkg, 'file': fn, 'size': [w, h], 'format': fmt, 'props': tp,
                             'lazy': pkg in fallback})
        except Exception as ex2:
            textures.append({'asset': pkg, 'error': str(ex2)})


    route_stand_in_params(materials)
    picked = {}
    for m in materials.values():
        g = m.get('gloss') if isinstance(m, dict) else None
        if not g or not g.get('rough_tex'):
            continue
        tpkg = g['rough_tex'].split('.')[0]
        if tpkg in tex_stats:
            if tpkg not in picked:
                picked[tpkg] = pick_rough_channel(tex_stats[tpkg])
                if picked[tpkg][0] != 'R':
                    print('  roughness texture %s: %s' % (tpkg.split('/')[-1], picked[tpkg][1]))
            g['rough_ch'] = picked[tpkg][0]
    blueprints = analyze_blueprints(fs, levels, scenarios)
    graft, graft_levels = export_graft_files(fs, levels, persistent, blueprints, mods[0] if mods else None, out)
    sounds = export_sounds(fs.paks, out)
    print('  %d Blueprints analysed, %d sounds written to sounds/' % (len(blueprints), len(
        [x for x in sounds if 'file' in x])))

    layout = {
        'tool': 'sandstorm-map-rebuilder', 'format': 2, 'tool_version': __version__, 'tool_build': __build__,
        'source_paks': [os.path.basename(x) for x in pak_paths], 'pak_warnings': pak_warnings,
        'mods': mods, 'persistent_levels': persistent,
        'levels': levels, 'landscapes': landscapes,
        'scenarios': scenarios, 'moddata': moddata, 'textures': textures, 'materials': materials,
        'relocate': relocate, 'relocate_if_missing': fallback,
        'dep_mods': mods[1:], 'meshes': meshes, 'mesh_errors': mesh_errors, 'grass_types': GRASS_TYPES,
        'mip_fixes': MIP_FIXES, 'low_res_textures': LOW_RES, 'game_mesh_errors': game_mesh_errors,
        'blueprints': blueprints, 'sounds': sounds, 'graft_files': graft, 'graft_levels': graft_levels,
        'custom_assets_referenced': custom_refs, 'game_overrides_referenced': override_refs,
        'inventory': {'game_overrides': inv['game_overrides'],
                      'mod_assets': {k: v for k, v in inv['mod_assets'].items()}},
    }
    if MIP_FIXES:
        print('  textures: streamed mips found at a corrected location: %s' % ', '.join(
            '%s x%d' % kv for kv in sorted(MIP_FIXES.items())))
    if LOW_RES:
        print('  textures: %d had no usable full-size data; used the inline mip (see REPORT.md)' % len(LOW_RES))
    with open(os.path.join(out, 'layout.json'), 'w', encoding='utf-8') as fh:
        json.dump(layout, fh, indent=1, default=str)
    write_report(layout, os.path.join(out, 'REPORT.md'))
    return layout


def write_report(L, path):
    lines = ['# Extraction report', '',
             'Extractor: v%s %s' % (L.get('tool_version', '?'), L.get('tool_build', '')),
             'Source: %s' % ', '.join(L['source_paks']),
             'Mod content root(s): %s' % (', '.join('/' + m for m in L['mods']) or 'none'), '']
    if L.get('target_mod'):
        lines += ['**Rebuild into a mod named `%s`.**%s' % (L['target_mod'], (
            ' Maps renamed: %s. The original keeps its own names, so the two can be installed side by side.' % (
                ', '.join('`%s` -> `%s`' % kv for kv in L['map_renames'].items()))) if L.get('renames') else
            ' This is the original\'s name: players who still have the original subscribed will load that one instead.'), '']
    if L.get('preflight'):  # it repeats the pak warnings, with the other checks
        from .preflight import report_section
        lines += report_section(L['preflight'])
    else:
        for w in L.get('pak_warnings') or []:
            lines += ['> **Warning:** ' + w, '']
    lines += ['## Levels', '', '| Level | Actors | Foliage | Streaming sublevels |', '|---|---|---|---|']
    for name, d in L['levels'].items():
        lines.append('| %s%s | %d | %d | %d |' % (name, ' (persistent)' if name in L['persistent_levels'] else '',
                                                 len(d['actors']), sum(len(f['instances']) for f in d['foliage']),
                                                 len(d['streaming'])))
    lines.append('')
    todo = []
    if L['landscapes']:
        lines += ['## Landscapes (import by hand)', '']
        for ls in L['landscapes']:
            lines += ['**%s** in `%s`' % (ls['actor'], ls['level']), '',
                      '- Heightmap file: `landscape/%s`' % ls['heightmap'],
                      '- Material: `%s`' % ls['material'],
                      '- **Import panel Location: `%s`** (the panel uses the centre)' % ', '.join(map(str, ls['import_panel_location'])),
                      '- Actor location afterwards should read: `%s`' % ', '.join(map(str, ls['actor_location'])),
                      '- Scale: `%s`, Rotation: `%s`' % (', '.join(map(str, ls['scale'])), ls['rotation']),
                      '- Section Size: %dx%d quads, Sections Per Component: %dx%d, Components: %dx%d, Resolution %dx%d' % (
                          ls['section_size_quads'], ls['section_size_quads'], ls['sections_per_component'],
                          ls['sections_per_component'], ls['components'][0], ls['components'][1],
                          ls['resolution'][0], ls['resolution'][1]),
                      '- Paint layers (create a Weight-Blended layer info for each, then pick the file): ' +
                      (', '.join('%s -> `landscape/%s`' % kv for kv in ls['layers'].items()) or 'none'),
                      '- Height range: %s cm' % ls['height_range_cm'], '']
            if ls['missing_heightmaps']:
                lines += ['> Warning: %d components had no readable heightmap; those areas are flat.'
                          % ls['missing_heightmaps'], '']
            if ls['holes']:
                lines += ['> Note: %d heightmap points are outside every component (gaps between components); they '
                          'import at zero height.' % ls['holes'], '']
            todo.append('Import landscape `%s` into `%s` (settings above).' % (ls['actor'], ls['level'].split('/')[-1]))
    if L['custom_assets_referenced'] or L.get('meshes') or L.get('mesh_errors'):
        meshes = L.get('meshes') or {}
        mats = L.get('materials') or {}
        texs = {t['asset'] for t in L.get('textures', []) if 'file' in t}
        stand_in = [k for k in mats if mats[k].get('substitute')]
        bps = L.get('blueprints') or {}
        auto_bp = [k for k, v in bps.items() if v.get('kind') == 'property_only']
        pads = [k for k, v in bps.items() if v.get('kind') == 'jump_pad']
        grafted = [k for k, v in bps.items() if v.get('has_logic')] if L.get('graft_files') else []
        manual_refs = {k: v for k, v in L['custom_assets_referenced'].items()
                       if k not in meshes and k not in mats and k not in texs and k not in auto_bp and k not in pads
                       and k not in grafted}
        lines += ['## Custom assets', '',
                  '- **%d static meshes** were exported to `meshes/` as FBX (geometry, UVs, normals, vertex colours, '
                  'material slots, simple collision). The rebuild script imports them at their original paths.'
                  % len(meshes),
                  '- **%d material instances** are recreated by the rebuild script.' % len(
                      [k for k in mats if not mats[k].get('substitute') and 'error' not in mats[k]]),
                  '- **%d custom materials** (full material graphs) cannot be rebuilt; each gets a stand-in material '
                  'instance at the same path that uses its base colour and normal textures. They will look close, '
                  'not identical (no custom shader effects).' % len(stand_in)]
        if auto_bp:
            lines.append('- **%d Blueprints** that only change settings (game modes, characters...) are rebuilt by the '
                         'script: %s.' % (len(auto_bp), ', '.join(k.split('/')[-1] for k in sorted(auto_bp))))
        if grafted:
            lines.append('- **%d scripted Blueprints** (%s) have graph logic, which cannot be rebuilt. Their original '
                         'compiled files are kept in `graft/`: run **Place original Blueprints**, and **Build graft '
                         'pak** after every cook.' % (len(grafted), ', '.join(k.split('/')[-1] for k in sorted(grafted))))
        if pads:
            lines.append('- **%d jump pads** are placed with their original launch velocities. They share one '
                         'Blueprint whose logic you paste in once (the rebuild log says how).' % len(pads))
        snd = [x for x in L.get('sounds') or [] if 'file' in x]
        if snd:
            lines.append('- **%d sounds** were extracted to `sounds/` as .wav (%s).' % (
                len(snd), ', '.join(x['file'].split('/')[-1] for x in snd)))
        lines.append('')
        if L.get('mesh_errors'):
            lines += ['Meshes that could not be read (export these with FModel):', '']
            lines += ['- `%s`: %s' % kv for kv in sorted(L['mesh_errors'].items())]
            lines.append('')
        if L.get('low_res_textures'):
            lines += ['Textures whose full-size data is not in the paks (used at the size the package itself '
                      'carries, so they look blurry, not broken):', '']
            lines += ['- %s' % x for x in L['low_res_textures']]
            lines.append('')
        cls_of = {p: c for c, ps in (L['inventory'].get('mod_assets') or {}).items() for p in ps}
        hlod = [k for k in manual_refs if cls_of.get(k) == 'HLODProxy']
        for k in hlod:
            manual_refs.pop(k)
        if hlod:
            lines += ['HLOD proxies (%s) are build products, not content: the rebuild leaves the HLOD clusters out; run '
                      '**Build > Build HLODs** in the editor if you want them (optional, performance only).' % ', '.join(
                          k.split('/')[-1] for k in hlod), '']
        if manual_refs:
            lines += ['### Still needed by hand', '',
                      'Blueprints, particles, sounds and anything else listed here must be recreated at the **same path** '
                      '(see `layout.json` for Blueprint components and their settings).', '']
            for k, v in sorted(manual_refs.items()):
                if k in cls_of:
                    why = cls_of[k] + ', in the pak but not a type the tool rebuilds'
                elif k.startswith('/Game/'):
                    why = 'shipped under /Game'
                else:
                    why = 'not in any pak given'
                lines.append('- `%s` (%s; used in %s)' % (k, why, ', '.join(x.split('/')[-1] for x in v)))
            lines.append('')
            todo.append('Recreate %d custom assets by hand (Blueprints, particles, sounds).' % len(manual_refs))
        if L.get('dep_mods'):
            lines += ['The old pak also carried assets from other mods (%s). The rebuild puts them under '
                      '`/<your mod>/Deps/<mod>/`.' % ', '.join(L['dep_mods']), '']
    if L['inventory']['game_overrides']:
        lines += ['## Base-game overrides in the old pak', '',
                  'The old pak shipped %d files under `/Game/`. Do **not** copy these into the new mod; '
                  'the rebuild uses the real base-game assets. If one of them no longer exists in the game, '
                  'the rebuild log will say `MISSING asset`.' % len(L['inventory']['game_overrides']), '']
    bsp = [n for n, d in L['levels'].items() if d['bsp_bytes'] > 4096]
    if bsp:
        lines += ['## BSP geometry', '', 'These levels contain compiled BSP brushes, which cannot be rebuilt '
                  'automatically. Use FModel to view them and re-block them with brushes or meshes:', '']
        lines += ['- `%s` (%d KB)' % (n, L['levels'][n]['bsp_bytes'] // 1024) for n in bsp]
        lines.append('')
        todo.append('Rebuild BSP geometry in: %s.' % ', '.join(n.split('/')[-1] for n in bsp))
    if L['scenarios']:
        lines += ['## Scenarios found', '']
        for k, s in L['scenarios'].items():
            subs = ', '.join(x.get('Name', '?') for x in (s.get('Sublevels') or []) if isinstance(x, dict))
            lines.append('- `%s`: %s, sublevels: %s' % (k.split('/')[-1], (s.get('GameMode') or '?').split('.')[-1], subs or 'none'))
        lines.append('')
    warns = [w for d in L['levels'].values() for w in d['warnings']]
    if warns:
        lines += ['## Warnings', ''] + ['- ' + w for w in warns] + ['']
    lines += ['## To do after running the rebuild script', ''] + ['- [ ] ' + t for t in todo] + [
        '- [ ] Check `rebuild_log.txt` for MISSING classes/assets.',
        '- [ ] Scenarios, World Settings (Default Scenarios, Lighting Scenarios, Tactical Map Material), ModData.',
        '- [ ] Build All, cook, test in the real game, upload crediting the original author.', '']
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines))
